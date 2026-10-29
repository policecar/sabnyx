"""Context assembly for LLM calls: filter-before-read over the vault.

Grep-era retrieval (descriptions as the index, excerpts as the bodies).
This module is the grep impl's *core*; ``retrieval.py`` wraps it behind a
pluggable ``Retriever`` seam so a semantic index drops in later without
touching call sites. Functions take explicit directories so tests can
redirect the vault wholesale.
"""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from . import fm, paths
from .util import slugify, walk_vault_md

EXCERPT_CHARS = 2500
TOTAL_CHARS = 300_000

_WARN_CONSOLE = Console(stderr=True)


def _index_paths(files: list[Path]) -> dict[str, Path]:
    """Build the one canonical lowercased, first-wins slug index."""
    out: dict[str, Path] = {}
    for p in files:
        key = p.stem.lower()
        if key in out:
            _WARN_CONSOLE.print(
                f"[yellow]warning:[/yellow] duplicate slug {key!r}: keeping {out[key]}, "
                f"shadowing {p}"
            )
            continue
        out[key] = p
    return out


def slug_index(dir_: Path) -> dict[str, Path]:
    """Lowercased slug -> path for every knowledge file under dir_.

    Lowercased keys match vault_slug_index's canonical form (Foo.md and
    foo.md must not be two different pages to one consumer and one page to
    another). On a stem collision the FIRST file in the deterministic walk
    order wins, loudly — silent last-wins shadowing hid files from ask and
    reweave."""
    return _index_paths(walk_vault_md(dir_))


def vault_md_files() -> list[Path]:
    """Every knowledge file under vault/, excluding observations, in walk order.

    All of them, unlike vault_slug_index: two pages may share a stem.
    References ``paths`` by module attribute so tests can redirect the vault
    wholesale (matching writers.py)."""
    return [p for p in walk_vault_md(paths.VAULT) if paths.OBSERVATIONS not in p.parents]


def vault_slug_index() -> dict[str, Path]:
    """Lowercased slug -> path for every knowledge file under vault/, excluding
    observations. The lowercase key is the canonical form health/entities use
    for case-insensitive wikilink matching. First-wins on a shared stem."""
    return _index_paths(vault_md_files())


def link_resolution_index(idx: dict[str, Path]) -> dict[str, str]:
    """Map every token a wikilink may use to its canonical slug in ``idx``.

    Tokens: each page's slugified stem (so ``[[Jane Doe]]`` credits
    ``jane-doe.md``) plus its slugified ``aliases`` frontmatter (SPEC §2:
    alternate names the page answers to). Stems win over aliases on
    collision. Pages whose frontmatter can't be loaded (schema drift,
    domains/ with its own schema) still resolve by stem — their aliases
    just aren't read. Shared by health and entities so the two subsystems
    agree on what a link resolves to.
    """
    out: dict[str, str] = {}
    for slug in idx:
        out.setdefault(slug.lower(), slug)
        out.setdefault(slugify(slug), slug)
    for slug, p in idx.items():
        try:
            meta, _body, _kind = fm.load(p)
        except Exception:
            continue
        for alias in meta.get("aliases") or []:
            akey = slugify(alias)
            if akey:
                out.setdefault(akey, slug)
    return out


def resolve_wikilink(target: str, resolution: dict[str, str]) -> str | None:
    """Canonical slug a raw wikilink target resolves to, or None (broken)."""
    t = target.strip().lower()
    return resolution.get(t) or resolution.get(slugify(target))


def link_candidates(
    files: list[Path],
) -> tuple[dict[str, frozenset[str]], dict[str, frozenset[str]]]:
    """(stem_claims, alias_claims): token -> every page in ``files`` claiming it.

    The same tokens ``link_resolution_index`` registers (stem: the lowercased
    stem and its slugify; alias: each slugified ``aliases`` entry), but kept in
    two maps and with ALL claimants instead of first-wins: the observation
    identity layer must tell a stem claim from an alias claim (stem wins) and
    see when a token is claimed twice (ambiguous, never guessed). A claimant
    is a page, named by its vault-relative POSIX path, not a slug: two pages
    in different folders can share a stem (``concepts/x.md``,
    ``projects/x.md``), and a slug-keyed index (vault_slug_index, first-wins)
    would make them one claimant. Same tolerance as ``link_resolution_index``:
    a page whose frontmatter can't be loaded still claims by stem. Non-string
    alias entries are skipped.

    Cost: one frontmatter parse per page in ``files``.
    """
    stem: dict[str, set[str]] = {}
    alias: dict[str, set[str]] = {}
    for p in files:
        rel, slug = p.relative_to(paths.VAULT).as_posix(), p.stem.lower()
        for tok in (slug, slugify(slug)):
            stem.setdefault(tok, set()).add(rel)
        try:
            meta, _body, _kind = fm.load(p)
        except Exception:
            continue
        for a in meta.get("aliases") or []:
            if not isinstance(a, str):  # slugify needs a str (defence in depth:
                # AtlasFrontmatter already rejects non-str aliases at load)
                continue
            tok = slugify(a)
            if tok:
                alias.setdefault(tok, set()).add(rel)
    return (
        {k: frozenset(v) for k, v in stem.items()},
        {k: frozenset(v) for k, v in alias.items()},
    )


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

    ``total_chars`` bounds the whole block: headers count against the budget,
    the excerpt that would overflow it is trimmed to the remainder (with a
    trim marker), and iteration stops once the budget is spent.
    """
    parts: list[str] = []
    budget = total_chars
    slugs = order if order is not None else sorted(index)
    for slug in slugs:
        if budget <= 0:
            break
        p = index.get(slug)
        if p is None or p == exclude:
            continue
        try:
            meta, body, _ = fm.load(p)
        except Exception:
            continue
        header = [
            f"### {slug}",
            f"description: {meta.get('description', '')}",
            f"tags: {', '.join(meta.get('tags', []))}",
        ]
        header_len = sum(len(h) + 1 for h in header)  # +1 per joining newline
        if header_len > budget:
            break
        excerpt = body.strip()[:excerpt_chars]
        remaining = budget - header_len
        if len(excerpt) > remaining:
            excerpt = excerpt[:remaining].rstrip() + " […]"
        budget -= header_len + len(excerpt)
        parts += [*header, excerpt, ""]
    return "\n".join(parts) if parts else "(empty)"
