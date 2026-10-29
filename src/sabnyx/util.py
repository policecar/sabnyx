"""Small utilities shared across commands."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import re
import time
import unicodedata
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path


def age_days(ts: datetime | date | str | None) -> int | None:
    """Whole days from a timestamp to now (UTC), or None if ``ts`` is None.

    Accepts an aware/naive datetime, a bare YAML date, or an ISO string.
    Naive datetimes and bare dates are interpreted as UTC.
    """
    if ts is None:
        return None
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    # NB: datetime is a subclass of date, so test datetime FIRST.
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
    elif isinstance(ts, date):
        # a bare YAML date (e.g. `last_verified: 2026-06-13`) -> midnight UTC
        ts = datetime(ts.year, ts.month, ts.day, tzinfo=UTC)
    return (datetime.now(UTC) - ts).days


# The shape slugify() emits: lowercase ASCII alnum runs joined by single
# hyphens. Used to validate fields that must already be slugs (an
# observation's `target`), never to produce one. Anchored with \A/\Z, not
# ^/$: `$` also matches before a trailing newline, so `.match("abc\n")`
# would accept it. Safe under match, search or fullmatch alike.
SLUG_RE = re.compile(r"\A[a-z0-9]+(?:-[a-z0-9]+)*\Z")


def _fold(text: str) -> str:
    """The kebab-case ASCII fold of `text`; "" when nothing slug-able remains."""
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    t = t.lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


def _clamp(t: str, max_len: int) -> str:
    return t[:max_len].rstrip("-") if len(t) > max_len else t


def slugify(text: str, max_len: int = 80) -> str:
    """Kebab-case ASCII slug, 1-80 chars, stripped of junk.

    Rules from SPEC section 6: kebab-case, ASCII, 30-80 chars ideally.
    We enforce ASCII and kebab; length clamp is a warning not an error.
    """
    t = _fold(text)
    if not t:
        # The ASCII fold ate everything (e.g. a fully non-Latin title). Keep the
        # SPEC §6 ASCII rule but derive a stable suffix from the original text so
        # distinct titles don't all collapse to one colliding "untitled" slug.
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
        t = f"untitled-{digest}"
    return _clamp(t, max_len)


def slug_fold(text: str, max_len: int = 80) -> str | None:
    """slugify without the untitled fallback: None when the fold is empty.

    For callers that must tell "this text has a slug" from "slugify made one
    up" (an observation token with no slug-able content is no identity, not
    an `untitled-<hash>` one). Equal to slugify(text, max_len) whenever it is
    not None, so a page literally named "untitled" is unaffected.
    """
    t = _fold(text)
    return _clamp(t, max_len) if t else None


def file_hash(path: Path) -> str:
    """``"sha256:" + hex digest`` of a file's raw bytes.

    Used by tend's re-triage memory (has this intake file changed since its
    last triage.decided ledger event?). Distinct from ledger.content_hash,
    which hashes a canonicalized JSON *payload*, not file bytes.
    """
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def walk_vault_md(root: Path) -> list[Path]:
    """Yield all .md files under root, skipping .git, _review/ (operational),
    and sources/ (raw immutable artifacts; no frontmatter schema applies)."""
    from .paths import SOURCES

    results: list[Path] = []
    for p in root.rglob("*.md"):
        if any(part.startswith(".git") for part in p.parts):
            continue
        if "_review" in p.parts:
            continue
        if SOURCES in p.parents:
            continue
        results.append(p)
    return sorted(results)


WIKILINK_RE = re.compile(r"\[\[([^\[\]|#]+)(?:\|[^\]]+)?(?:#[^\]]+)?\]\]")


def extract_wikilinks(body: str) -> list[str]:
    """Return raw wikilink targets (unresolved). E.g. '[[foo-bar]]' -> ['foo-bar']."""
    return [m.group(1).strip() for m in WIKILINK_RE.finditer(body)]


# -- marker blocks (SPEC §8: non-destructive agent edits) -------------------------


def _markers(name: str) -> tuple[str, str]:
    return f"<!-- BEGIN sabnyx:{name} -->", f"<!-- END sabnyx:{name} -->"


def get_marker_block(body: str, name: str) -> str | None:
    """Content between the paired sabnyx markers, or None if absent."""
    begin, end = _markers(name)
    if begin not in body or end not in body:
        return None
    inner = body.split(begin, 1)[1].split(end, 1)[0]
    return inner.strip("\n")


def set_marker_block(
    body: str,
    name: str,
    lines: list[str],
    heading: str | None = None,
    *,
    newline: str = "\n",
) -> str:
    """Replace the named marker block's content, preserving everything
    outside it byte-for-byte. If the block is absent, append a new
    section at the end (optionally under `heading`) — append, never
    rewrite, per the round-trip invariant in docs/reserved-syntax.md.

    Owned region of an existing block: the bytes strictly between the end of
    the BEGIN marker and the start of the END marker. It is re-rendered as
    ``newline`` + lines joined by ``newline`` + ``newline`` (just ``newline``
    when `lines` is empty). Every byte before BEGIN and from END on is copied.

    Missing block: `body` is kept as is, then a separator only as long as
    needed for it to end in one blank line (nothing if it already ends in
    two `newline`s, one `newline` if it ends in one, two otherwise), then
    ``## heading``, a blank line, the block and a final `newline`. Every
    rendered line ending is `newline`; pass "\\r\\n" for a CRLF file.

    Raises ValueError unless the markers form exactly one ordered pair or
    are both absent: a half-open block (manual edit damage) or an ambiguous
    layout (END before BEGIN, a second BEGIN or END) would nest, duplicate
    or swallow content, which SPEC §8 forbids. Callers report and skip."""
    begin, end = _markers(name)
    nl = newline
    block = f"{begin}{nl}{nl.join(lines)}{nl}{end}" if lines else f"{begin}{nl}{end}"

    n_begin, n_end = body.count(begin), body.count(end)
    if (n_begin, n_end) == (1, 1):
        i, j = body.index(begin), body.index(end)
        if j < i:
            raise ValueError(
                f"malformed marker block sabnyx:{name}: END precedes BEGIN — "
                "refusing to rewrite it (SPEC §8)"
            )
        return body[:i] + block + body[j + len(end) :]
    if {n_begin, n_end} == {0, 1}:
        half = "BEGIN" if n_begin else "END"
        raise ValueError(
            f"half-open marker block sabnyx:{name}: found {half} without its pair — "
            "refusing to append a duplicate block (SPEC §8)"
        )
    if (n_begin, n_end) != (0, 0):
        raise ValueError(
            f"ambiguous marker blocks sabnyx:{name}: {n_begin} BEGIN / {n_end} END — "
            "refusing to guess which one to edit (SPEC §8)"
        )

    sep = "" if body.endswith(nl * 2) else (nl if body.endswith(nl) else nl * 2)
    section = f"## {heading}{nl}{nl}{block}{nl}" if heading else f"{block}{nl}"
    return body + sep + section


# -- vault lock (shared with the bash heartbeat) -----------------------------
#
# @decision DEC-03: vault_lock lives in util.py, not services.py
# @rationale: it's a generic concurrency primitive (open/flock/poll/release)
# with no knowledge of intake/atlas/the ledger — same category as slugify or
# the marker-block helpers above, not a vault write path itself. Call sites
# (commands/serve.py, commands/serve_http.py) already import from both
# services and util; keeping it here avoids a new services->util->services
# import shape and keeps services.py focused on vault mutation logic.


class VaultLocked(RuntimeError):
    """vault_lock could not acquire the lock before its timeout elapsed."""


_LOCK_FILENAME = ".sync-tend.lock"
_POLL_INTERVAL_S = 0.2


@contextlib.contextmanager
def vault_lock(timeout: float = 120.0) -> Iterator[None]:
    """Exclusive lock on ``DATA_ROOT/.sync-tend.lock``.

    This is the SAME path and the same flock(2) semantics (LOCK_EX|LOCK_NB)
    that `ops/scripts/sabnyx-sync-tend.sh` holds via `flock -n 9` for the
    duration of a `tend` run — acquiring it here is what keeps a long-running
    writer (serve/serve-http) from interleaving vault writes with the
    heartbeat. The fd is opened O_RDWR|O_CREAT, never O_TRUNC — this must
    never touch the lock file's content, only its existence.

    Polls every `_POLL_INTERVAL_S` seconds until the lock is free or
    `timeout` elapses, then raises `VaultLocked` with a message safe to show
    an end user directly (Telegram reply / HTTP body) — it names what's
    probably holding the lock but leaks no path or exception detail.

    Do not nest calls in the same process: flock on one fd is not
    re-entrant across a second `os.open` + `flock` from the same process on
    the same file, so a nested `with vault_lock():` would just block on
    itself until it times out. Every call site in this codebase wraps one
    `services.*` call directly and is flat by construction — keep it that
    way rather than adding a reentrancy counter nothing here needs yet.
    """
    from .paths import DATA_ROOT

    lock_path = DATA_ROOT / _LOCK_FILENAME
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    acquired = False
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise VaultLocked(
                        "vault is busy (another writer — probably the heartbeat "
                        "tend run — holds the lock); try again shortly"
                    ) from None
                time.sleep(_POLL_INTERVAL_S)
        yield
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
