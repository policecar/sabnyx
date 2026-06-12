"""Small utilities shared across commands."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path


def slugify(text: str, max_len: int = 80) -> str:
    """Kebab-case ASCII slug, 1-80 chars, stripped of junk.

    Rules from SPEC section 6: kebab-case, ASCII, 30-80 chars ideally.
    We enforce ASCII and kebab; length clamp is a warning not an error.
    """
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    t = t.lower()
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    if not t:
        t = "untitled"
    if len(t) > max_len:
        t = t[:max_len].rstrip("-")
    return t


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
