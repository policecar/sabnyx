"""Small utilities shared across commands."""

from __future__ import annotations

import hashlib
import re
import unicodedata
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


def slugify(text: str, max_len: int = 80) -> str:
    """Kebab-case ASCII slug, 1-80 chars, stripped of junk.

    Rules from SPEC section 6: kebab-case, ASCII, 30-80 chars ideally.
    We enforce ASCII and kebab; length clamp is a warning not an error.
    """
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    t = t.lower()
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    if not t:
        # The ASCII fold ate everything (e.g. a fully non-Latin title). Keep the
        # SPEC §6 ASCII rule but derive a stable suffix from the original text so
        # distinct titles don't all collapse to one colliding "untitled" slug.
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
        t = f"untitled-{digest}"
    if len(t) > max_len:
        t = t[:max_len].rstrip("-")
    return t


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


def set_marker_block(body: str, name: str, lines: list[str], heading: str | None = None) -> str:
    """Replace the named marker block's content, preserving everything
    outside it byte-for-byte. If the block is absent, append a new
    section at the end (optionally under `heading`) — append, never
    rewrite, per the round-trip invariant in docs/reserved-syntax.md.

    Raises ValueError when exactly one of BEGIN/END is present (a half-open
    block, e.g. after a manual edit): appending a fresh block there would
    nest/duplicate markers, which SPEC §8 forbids. Callers report and skip."""
    begin, end = _markers(name)
    content = "\n".join(lines)
    block = f"{begin}\n{content}\n{end}" if lines else f"{begin}\n{end}"

    if begin in body and end in body:
        pre = body.split(begin, 1)[0]
        post = body.split(end, 1)[1]
        return pre + block + post
    if begin in body or end in body:
        half = "BEGIN" if begin in body else "END"
        raise ValueError(
            f"half-open marker block sabnyx:{name}: found {half} without its pair — "
            "refusing to append a duplicate block (SPEC §8)"
        )

    sep = "" if body.endswith("\n\n") else ("\n" if body.endswith("\n") else "\n\n")
    section = f"## {heading}\n\n{block}\n" if heading else f"{block}\n"
    return body + sep + section
