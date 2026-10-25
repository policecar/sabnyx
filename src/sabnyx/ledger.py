"""Append-only ledger at ops/events.jsonl.

Rules:
  - Never edit a past line. To correct, append a compensating event.
  - Never write without a content_hash.
  - Read via replay(), never by parsing lines ad-hoc.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

from pydantic import ValidationError

from .paths import EVENTS_JSONL, OPS
from .schemas import LedgerEvent, LedgerPlane


class LedgerError(RuntimeError):
    """A ledger line failed to parse. Carries the 1-based line number in its
    message (parity with opsctl's read_events) so the operator can find and
    append a compensating fix instead of guessing which line bricked replay."""


class LedgerWriteError(LedgerError):
    """An append failed after its first write(2): the line may be on disk.

    Raised by append_event for any exception from the first write(2) through
    close -- a short write, or an OSError (or any other Exception) from
    write, _fsync or close. A KeyboardInterrupt in the same region is
    LedgerWriteInterrupted instead. A caller that mutated a file before
    appending must NOT restore it on either (the ledger may already record
    the mutation); run opsctl verify. Failures before that point
    (OPS.mkdir, event construction, open) propagate unchanged, so a caller
    can treat them as "nothing on disk". Subclasses LedgerError so existing
    ``except LedgerError`` sites keep catching the short write;
    LedgerError's own docstring (parse failures) is deliberately not
    widened. See docs/decisions/0008-ledger-append-failure-semantics.md.
    """


class LedgerWriteInterrupted(KeyboardInterrupt):
    """A KeyboardInterrupt that arrived after append_event's first write(2):
    the line may be on disk, so a caller must not restore (as for
    LedgerWriteError).

    Still a KeyboardInterrupt, not an Exception: the interrupt keeps its
    meaning (exit 130) and no ``except Exception`` handler can swallow it.
    A KeyboardInterrupt before the first write(2) propagates unchanged.
    Other BaseExceptions (SystemExit, GeneratorExit) are not classified;
    callers treat them conservatively (no restore).
    """


def _reject_floats(value: object, _path: str = "payload") -> None:
    """Recursively reject float values in a hashable payload.

    The content_hash must be byte-identical between this implementation and the
    Rust one (opsctl, ADR-0002). Python's ``json.dumps`` and Rust's
    ``serde_json`` stringify floats differently at scientific-notation
    magnitudes (e.g. ``1e+20`` vs ``100000000000000000000.0``) and Python also
    emits the non-JSON tokens ``NaN``/``Infinity``. Rather than carry a shared
    float formatter, the contract is simply: **payloads contain no floats**.
    Callers that need a fractional quantity must store it as a string or a
    scaled integer. ``bool`` is a subclass of ``int`` and is allowed.
    """
    if isinstance(value, float):
        raise TypeError(
            f"{_path}: float values are not allowed in a hashed ledger payload "
            f"(got {value!r}); use a string or a scaled integer instead — "
            "floats break Python/Rust content_hash parity (ADR-0002)."
        )
    if isinstance(value, dict):
        for k, v in value.items():
            _reject_floats(v, f"{_path}.{k}")
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _reject_floats(v, f"{_path}[{i}]")


def _canonical(payload: dict) -> bytes:
    """Canonical JSON bytes for hashing: sorted keys, no whitespace.

    ``allow_nan=False`` ensures we never emit ``NaN``/``Infinity`` (which are
    invalid JSON and unparseable by Rust); ``_reject_floats`` enforces the
    no-floats contract that keeps the hash byte-compatible with opsctl.
    """
    _reject_floats(payload)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )


def content_hash(payload: dict) -> str:
    """SHA-256 of the canonical payload, prefixed for Lemma-compatibility.

    Only the *payload* is hashed — never ``ts`` (an opaque, non-canonical
    timestamp) or any other envelope field. This is the contract opsctl mirrors.
    """
    h = hashlib.sha256(_canonical(payload)).hexdigest()
    return f"sha256:{h}"


def now() -> datetime:
    """UTC timestamp, tz-aware. Use this instead of datetime.now()."""
    return datetime.now(UTC)


def today() -> date:
    """UTC calendar date. Use this instead of date.today().

    The single clock seam (with now()): observation dates and filenames
    derive from it, so goldens are timezone-independent. A filing late in
    the evening east of UTC is dated the previous day."""
    return now().date()


def _fsync(fd: int) -> None:
    """fsync `fd`, upgrading to a full cache flush where the platform needs it.

    Plain fsync(2) on Darwin/APFS only pushes data to the drive's write
    cache, not to the platter/flash itself — the kernel's actual durability
    barrier there is F_FULLFSYNC. Linux and other POSIX filesystems already
    get that guarantee from fsync(2) alone. opsctl's Rust appender calls
    sync_all(), which issues F_FULLFSYNC on Apple targets automatically;
    this mirrors that on the Python side so the two appenders share one
    durability guarantee instead of silently diverging on macOS.
    """
    if hasattr(fcntl, "F_FULLFSYNC"):
        try:
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            return
        except OSError:
            # Some filesystems (e.g. exFAT, certain network mounts) reject
            # F_FULLFSYNC outright. Fall back to the weaker guarantee rather
            # than failing an append that otherwise succeeded.
            pass
    os.fsync(fd)


def _fsync_dir(path: Path) -> None:
    """Best-effort fsync of directory `path`, mirroring the pattern in
    fm.write (the atomic replace).

    A file's own fsync makes its *contents* durable, but the directory
    entry that makes a brand-new file discoverable after a crash is
    separate metadata living in the parent directory's inode, not the
    file's. This only needs to run the first time a file is created inside
    `path` — later appends to an already-existing file don't touch the
    directory. Failure here is swallowed: by the time this is called the
    append itself already succeeded, so raising would be a worse failure
    mode than a directory entry that might not survive a *subsequent*
    crash — a case opsctl verify / replay() can detect.
    """
    with contextlib.suppress(OSError):
        dfd = os.open(path, os.O_RDONLY)
        try:
            _fsync(dfd)
        finally:
            os.close(dfd)


def append_event(
    *,
    kind: str,
    actor: str,
    subject: str,
    payload: dict,
    plane: LedgerPlane = "event",
    ts: datetime | None = None,
) -> LedgerEvent:
    """Append one event to ops/events.jsonl and return the typed record.

    The append is the ledger's commit point: once this returns, the event
    is durable (see the fsync comment inline for exactly what "durable"
    covers on each platform). Any exception from the first write(2) through
    close raises LedgerWriteError, and a Ctrl-C there raises
    LedgerWriteInterrupted: the event line may *already* be on disk,
    so it is not proof the append didn't happen, and a short write in
    particular can't be undone (the partial line is on disk for good; the
    fix is to run opsctl verify, not to retry the write). Anything raised
    earlier (OPS.mkdir, event construction, open) is not a LedgerWriteError
    and means no byte reached the ledger file.
    Callers must therefore not blindly retry a failed append: retrying a
    `wire.received` or `tend.completed` just produces a harmless duplicate
    audit entry, not corruption. (services.ingest_text already handles this
    for captures by deduping on title via FileExistsError before it ever
    calls append_event.)

    Cost: measured on this machine, fsync on APFS runs ~0.05ms per append;
    F_FULLFSYNC and Linux ext4 fsync are millisecond-scale. At the ledger's
    real write rate (~70 events/day plus one tend.completed per 15-minute
    heartbeat) that's negligible, so there's no batching API here.
    """
    OPS.mkdir(parents=True, exist_ok=True)
    created = not EVENTS_JSONL.exists()
    event = LedgerEvent(
        ts=ts or now(),
        plane=plane,
        kind=kind,
        actor=actor,
        subject=subject,
        payload=payload,
        content_hash=content_hash(payload),
    )
    data = (event.model_dump_json() + "\n").encode("utf-8")
    # One unbuffered write(2) of payload + newline under O_APPEND, so a
    # concurrent appender (opsctl's supervise daemon vs an interactive CLI)
    # can't interleave bytes within a line — mirrors opsctl's append. A
    # buffered text writer may split large lines across write() syscalls.
    # (PIPE_BUF governs pipes, not regular files, so it buys nothing here.)
    # An unbuffered write(2) can still return fewer bytes than requested
    # without raising, so the byte count is checked explicitly below.
    # _fsync() then makes the line durable to disk before we return:
    # durable against a process crash always (write(2) already reached the
    # OS page cache); durable against power loss on Linux, and on macOS via
    # F_FULLFSYNC where the filesystem honours it (APFS does). The
    # directory entry itself is only fsync'd the first time the ledger file
    # is created (_fsync_dir below) — later appends don't touch the
    # directory. opsctl's appender mirrors both of these.
    #
    # Failure classes (IDD §10, D15): open() sits outside the try, so a
    # pre-write failure (read-only mount, permissions) propagates as itself
    # and the caller knows nothing reached the file. From the first write(2)
    # through close, every exception becomes LedgerWriteError and a Ctrl-C
    # becomes LedgerWriteInterrupted: bytes may be on disk, and the caller
    # must not act as if the append never happened (ADR 0008).
    f = EVENTS_JSONL.open("ab", buffering=0)
    try:
        with f:
            n = f.write(data)
            if n != len(data):
                raise LedgerWriteError(
                    f"short write to ledger: {n} of {len(data)} bytes — the partial "
                    "line is on disk; run opsctl verify"
                )
            _fsync(f.fileno())
    except LedgerWriteError:
        raise
    except Exception as e:
        raise LedgerWriteError(
            f"ledger append failed after write began ({e}) — the line may be on "
            "disk; run opsctl verify"
        ) from e
    except KeyboardInterrupt as e:
        raise LedgerWriteInterrupted(
            "interrupted after the ledger write began — the line may be on disk; run opsctl verify"
        ) from e
    if created:
        _fsync_dir(EVENTS_JSONL.parent)
    return event


def replay() -> Iterator[LedgerEvent]:
    """Yield every event in order. The only sanctioned read path.

    Raises LedgerError naming the 1-based line number and a snippet of the
    offending line (parity with opsctl) — never skips silently.
    """
    if not EVENTS_JSONL.exists():
        return
    with EVENTS_JSONL.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield LedgerEvent.model_validate_json(line)
            except ValidationError as e:
                snippet = line[:60] + ("…" if len(line) > 60 else "")
                raise LedgerError(f"ledger line {lineno}: {e} — line starts: {snippet}") from e


def count() -> int:
    """How many events on the ledger."""
    return sum(1 for _ in replay())
