"""Append-only ledger at ops/events.jsonl.

Rules:
  - Never edit a past line. To correct, append a compensating event.
  - Never write without a content_hash.
  - Read via replay(), never by parsing lines ad-hoc.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime

from pydantic import ValidationError

from .paths import EVENTS_JSONL, OPS
from .schemas import LedgerEvent, LedgerPlane


class LedgerError(RuntimeError):
    """A ledger line failed to parse. Carries the 1-based line number in its
    message (parity with opsctl's read_events) so the operator can find and
    append a compensating fix instead of guessing which line bricked replay."""


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


def append_event(
    *,
    kind: str,
    actor: str,
    subject: str,
    payload: dict,
    plane: LedgerPlane = "event",
    ts: datetime | None = None,
) -> LedgerEvent:
    """Append one event to ops/events.jsonl and return the typed record."""
    OPS.mkdir(parents=True, exist_ok=True)
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
    with EVENTS_JSONL.open("ab", buffering=0) as f:
        f.write(data)
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
