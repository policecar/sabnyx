"""Context assembly for LLM calls: filter-before-read over the vault.

Grep-era retrieval (descriptions as the index, excerpts as the bodies).
This module is the grep impl's *core*; ``retrieval.py`` wraps it behind a
pluggable ``Retriever`` seam so a semantic index drops in later without
touching call sites. Functions take explicit directories so tests can
redirect the vault wholesale.
"""

from __future__ import annotations

from pathlib import Path

from . import fm
from .util import walk_vault_md

EXCERPT_CHARS = 2500
TOTAL_CHARS = 300_000


def slug_index(dir_: Path) -> dict[str, Path]:
    """slug -> path for every knowledge file under dir_."""
    return {p.stem: p for p in walk_vault_md(dir_)}


def description_index(dir_: Path, label: str) -> str:
    """Slug + description listing of a vault directory — the cheap index."""
    lines: list[str] = []
    for p in sorted(dir_.rglob("*.md")):
        if ".observations" in p.parts:
            continue
        try:
            meta, _body, _kind = fm.load(p)
        except Exception:
            continue
        tags = ", ".join(meta.get("tags", []))
        lines.append(f"- {p.stem}: {meta.get('description', '')}" + (f" [{tags}]" if tags else ""))
    return f"## {label}\n" + ("\n".join(lines) if lines else "(empty)")


def excerpts(
    index: dict[str, Path],
    exclude: Path | None = None,
    excerpt_chars: int = EXCERPT_CHARS,
    total_chars: int = TOTAL_CHARS,
    order: list[str] | None = None,
) -> str:
    """Description + body excerpt per file — the richer context block.

    ``order`` lets a retriever supply a relevance ranking (slugs first-to-last);
    slugs absent from ``index`` are skipped. When ``None`` the files render in
    slug order — the default the seam preserves byte-for-byte.
    """
    parts: list[str] = []
    budget = total_chars
    slugs = order if order is not None else sorted(index)
    for slug in slugs:
        p = index.get(slug)
        if p is None or p == exclude or budget <= 0:
            continue
        try:
            meta, body, _ = fm.load(p)
        except Exception:
            continue
        excerpt = body.strip()[:excerpt_chars]
        budget -= len(excerpt)
        parts += [
            f"### {slug}",
            f"description: {meta.get('description', '')}",
            f"tags: {', '.join(meta.get('tags', []))}",
            excerpt,
            "",
        ]
    return "\n".join(parts) if parts else "(empty)"
