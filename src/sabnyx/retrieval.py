"""The retrieval seam: a pluggable interface over vault context assembly.

Phase 4 establishes the *interface*, not a vector index. The single thing
worth abstracting is how a query selects bodies out of the vault — today
``context.excerpts`` dumps every file (budget-bounded). This module wraps
that as a :class:`Retriever` so a future ``VectorRetriever`` is a drop-in
with zero call-site churn.

The default :class:`GrepRetriever` is *keyword-ranked*: given a query it
orders files by term overlap (no embeddings, no index, no reindex step),
then renders them through ``context.excerpts``. While the whole vault fits
the char budget this only reorders — full recall is preserved. Once the
vault outgrows the budget, the ranking decides who survives truncation, so
relevance-prioritised degradation is graceful and the moment grep starts
dropping relevant pages becomes observable — the concrete trigger to add a
vector retriever (see ``docs/plans/build-out-plan.md``). No query (e.g. the
contradiction ``scan``) → today's byte-for-byte full dump.

Switch implementations with ``SABNYX_RETRIEVER`` (default ``grep``).

@decision DEC-04: keyword-ranked grep, reorder-within-budget / never-drop-while-it-fits
@rationale: makes the query load-bearing now (vs a vestigial param) without
  committing to an embedding subsystem the 3-file vault does not need; the
  ranking doubles as graceful truncation and a data-driven trigger for the
  later vector phase.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol, runtime_checkable

from . import config, context, fm
from .context import TOTAL_CHARS

# Tokens of length >= 2 over lowercased alphanumerics. Short enough to catch
# acronyms ("ai", "ml"), long enough to drop single-char noise.
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _terms(text: str) -> set[str]:
    """Distinct lowercased tokens (length >= 2) in ``text``."""
    return {t for t in _TOKEN_RE.findall(text.lower()) if len(t) >= 2}


def _page_haystack(path: Path, cap: int) -> str:
    """slug + description + tags + body (capped at ``cap`` chars).

    Falls back to the bare slug for unreadable files rather than raising — a
    bad file should sink in ranking, not crash retrieval."""
    try:
        meta, body, _ = fm.load(path)
    except Exception:
        return path.stem
    return " ".join(
        [
            path.stem,
            str(meta.get("description", "")),
            " ".join(meta.get("tags", []) or []),
            body[:cap],
        ]
    )


def _doc_terms(path: Path) -> set[str]:
    """Searchable terms for a vault file (the keyword-ranking haystack)."""
    return _terms(_page_haystack(path, context.EXCERPT_CHARS))


def _rank(query: str, index: dict[str, Path]) -> list[str]:
    """Slugs ordered by descending query-term overlap, ties broken by slug.

    Every slug is retained (zero-overlap files come last, in slug order), so
    within budget the rendered set is identical to the unranked dump — only
    the order changes. An empty/termless query yields plain slug order, i.e.
    exactly ``context.excerpts``'s default.
    """
    qterms = _terms(query)
    if not qterms:
        return sorted(index)
    scored = [(slug, len(qterms & _doc_terms(path))) for slug, path in index.items()]
    return [slug for slug, _ in sorted(scored, key=lambda sc: (-sc[1], sc[0]))]


@runtime_checkable
class Retriever(Protocol):
    """Selects vault bodies for a query and renders them as an excerpt block.

    Returns the same shape ``context.excerpts`` produces today (so call sites
    are unchanged). ``query`` is ``None``/empty when the caller wants the whole
    set (e.g. the contradiction scan).
    """

    name: str

    def retrieve(
        self,
        query: str | None,
        index: dict[str, Path],
        *,
        exclude: Path | None = None,
        budget: int = TOTAL_CHARS,
    ) -> str: ...


class GrepRetriever:
    """Default retriever: keyword-ranked grep over ``context.excerpts``.

    With no query, reproduces today's full dump byte-for-byte. With a query,
    ranks by term overlap then renders in that order. Note the ranking
    re-reads files that ``excerpts`` also reads; harmless at vault scale (the
    budget caps the working set at ~120 files) and keeps the ranker decoupled
    from the renderer's internals.
    """

    name = "grep"

    def retrieve(
        self,
        query: str | None,
        index: dict[str, Path],
        *,
        exclude: Path | None = None,
        budget: int = TOTAL_CHARS,
    ) -> str:
        order = _rank(query, index) if query else None
        return context.excerpts(index, exclude=exclude, total_chars=budget, order=order)


# -- vector retriever (opt-in) --------------------------------------------------
# Static embeddings (model2vec) — torch-free, ~30MB, CPU. The model id is env
# overridable so a newer static model is a config change, not a code change.
DEFAULT_EMBED_MODEL = "minishlab/potion-retrieval-32M"
EMBED_CHARS = 4000

_EMBED_MODEL = None  # module-level cache; loaded once on first vector retrieval


def _embedder():
    """Load and cache the static embedding model. Lazy, so model2vec is only
    required when the vector retriever is actually selected."""
    global _EMBED_MODEL
    if _EMBED_MODEL is None:
        import os

        try:
            from model2vec import StaticModel  # ty: ignore[unresolved-import]
        except ImportError as e:
            raise RuntimeError(
                "the vector retriever needs model2vec; install the extra: "
                "`uv sync --extra vector` (or `pip install 'sabnyx[vector]'`)"
            ) from e
        model_id = os.environ.get("SABNYX_EMBED_MODEL", DEFAULT_EMBED_MODEL)
        _EMBED_MODEL = StaticModel.from_pretrained(model_id)
    return _EMBED_MODEL


def _page_text(path: Path) -> str:
    """Embeddable text for a page (the same haystack, capped for embedding)."""
    return _page_haystack(path, EMBED_CHARS)


def _vector_order(query: str, index: dict[str, Path]) -> list[str]:
    """Slugs ranked by cosine similarity of the query to each page, ties by slug."""
    import numpy as np

    model = _embedder()
    slugs = sorted(index)
    doc_vecs = np.asarray(model.encode([_page_text(index[s]) for s in slugs]), dtype="float32")
    q_vec = np.asarray(model.encode([query])[0], dtype="float32")
    doc_norm = doc_vecs / (np.linalg.norm(doc_vecs, axis=1, keepdims=True) + 1e-9)
    q_norm = q_vec / (np.linalg.norm(q_vec) + 1e-9)
    sims = doc_norm @ q_norm
    ranked = sorted(zip(slugs, sims, strict=True), key=lambda sv: (-float(sv[1]), sv[0]))
    return [s for s, _ in ranked]


class VectorRetriever:
    """Opt-in semantic retriever: static embeddings rank pages by cosine
    similarity, then render through the *same* ``context.excerpts`` block as grep
    — only the ordering changes, so the seam's contract holds. No query → grep's
    full dump. The model loads lazily on first use; tests stub ``_embedder``.
    """

    name = "vector"

    def retrieve(
        self,
        query: str | None,
        index: dict[str, Path],
        *,
        exclude: Path | None = None,
        budget: int = TOTAL_CHARS,
    ) -> str:
        order = _vector_order(query, index) if query else None
        return context.excerpts(index, exclude=exclude, total_chars=budget, order=order)


_REGISTRY = {"grep": GrepRetriever, "vector": VectorRetriever}


def get_retriever() -> Retriever:
    """The retriever named by ``SABNYX_RETRIEVER`` (default ``grep``).

    Fails fast on an unknown name rather than silently falling back — a typo
    in the switch should be loud, not a quiet behaviour change.
    """
    name = config.retriever_name()
    try:
        return _REGISTRY[name]()
    except KeyError:
        valid = ", ".join(sorted(_REGISTRY))
        raise ValueError(f"unknown SABNYX_RETRIEVER {name!r}; valid: {valid}") from None
