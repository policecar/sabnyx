"""Tests for the retrieval seam (Phase 4).

Covers the four pieces of the contract: (1) GrepRetriever with no query is
byte-for-byte today's full dump; (2) a query reorders by term overlap while
preserving recall within budget; (3) under a tight budget the ranking decides
who survives truncation; (4) the SABNYX_RETRIEVER switch + factory. Plus a
seam test that `ask` still feeds real context and cites with the grep impl.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sabnyx import context, fm, retrieval

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _atlas_meta(description: str, tags: list[str] | None = None) -> dict:
    return {
        "type": "concept",
        "description": description,
        "tags": tags or [],
        "status": "final",
        "promoted_from": None,
        "promoted_date": None,
        "last_verified": None,
        "proof": None,
    }


def _atlas_file(
    dir_: Path, slug: str, *, description: str, body: str, tags: list[str] | None = None
) -> Path:
    p = dir_ / f"{slug}.md"
    fm.write(p, _atlas_meta(description, tags), body)
    return p


@pytest.fixture
def atlas(tmp_path):
    """A small atlas of three files with deliberately disjoint vocabularies,
    so a query can only match one of them. Returns (dir, slug_index)."""
    d = tmp_path / "atlas" / "concepts"
    d.mkdir(parents=True)
    _atlas_file(
        d,
        "markdown-surfaces",
        description="markdown is the canonical agent surface",
        body="Agents prefer markdown surfaces; html is merely a projection layer.",
    )
    _atlas_file(
        d,
        "vector-search",
        description="embeddings power semantic recall over a corpus",
        body="Vector indexes retrieve passages by cosine similarity of embeddings.",
    )
    _atlas_file(
        d,
        "git-sync",
        description="git synchronises the single-user vault across machines",
        body="One operator, many machines, kept consistent purely by git push and pull.",
    )
    return d, context.slug_index(d)


# ---------------------------------------------------------------------------
# GrepRetriever — the grep impl behind the seam
# ---------------------------------------------------------------------------


def test_no_query_is_byte_identical_to_excerpts(atlas):
    """The seam's load-bearing guarantee: query-blind grep == today's dump."""
    _, index = atlas
    r = retrieval.GrepRetriever()
    assert r.retrieve(None, index) == context.excerpts(index)
    assert r.retrieve("", index) == context.excerpts(index)
    # a query made only of noise (no >=2-char tokens) also degrades to dump
    assert r.retrieve("a !", index) == context.excerpts(index)


def test_query_ranks_overlapping_file_first_without_dropping_others(atlas):
    """Within budget: same set as the dump, just reordered by relevance."""
    _, index = atlas
    out = retrieval.GrepRetriever().retrieve("semantic embeddings cosine recall", index)
    # recall preserved — every file still rendered
    for slug in index:
        assert f"### {slug}" in out, slug
    # but the only overlapping file leads
    pos = {s: out.index(f"### {s}") for s in index}
    assert pos["vector-search"] < pos["markdown-surfaces"]
    assert pos["vector-search"] < pos["git-sync"]


def test_tight_budget_keeps_relevant_drops_rest(atlas):
    """Once the budget bites, the ranking decides who survives — graceful,
    relevance-prioritised truncation (the trigger to add a vector retriever)."""
    _, index = atlas
    # budget < any single body: only the top-ranked file renders
    out = retrieval.GrepRetriever().retrieve("vector embeddings cosine", index, budget=10)
    assert "### vector-search" in out
    assert "### markdown-surfaces" not in out
    assert "### git-sync" not in out


def test_exclude_is_honored(atlas):
    _, index = atlas
    target = index["vector-search"]
    out = retrieval.GrepRetriever().retrieve("vector embeddings", index, exclude=target)
    assert "### vector-search" not in out
    assert "### markdown-surfaces" in out
    assert "### git-sync" in out


def test_grep_retriever_satisfies_protocol():
    assert isinstance(retrieval.GrepRetriever(), retrieval.Retriever)


# ---------------------------------------------------------------------------
# The SABNYX_RETRIEVER switch + factory
# ---------------------------------------------------------------------------


def test_get_retriever_defaults_to_grep(monkeypatch):
    monkeypatch.delenv("SABNYX_RETRIEVER", raising=False)
    r = retrieval.get_retriever()
    assert isinstance(r, retrieval.GrepRetriever)
    assert r.name == "grep"


def test_get_retriever_explicit_grep(monkeypatch):
    monkeypatch.setenv("SABNYX_RETRIEVER", "grep")
    assert isinstance(retrieval.get_retriever(), retrieval.GrepRetriever)


def test_get_retriever_unknown_fails_fast(monkeypatch):
    monkeypatch.setenv("SABNYX_RETRIEVER", "warp-drive")
    with pytest.raises(ValueError, match="warp-drive"):
        retrieval.get_retriever()


# ---------------------------------------------------------------------------
# Seam test: ask still feeds real context and cites, through the grep impl
# ---------------------------------------------------------------------------


def test_ask_feeds_grep_context_and_still_cites(tmp_vault, monkeypatch):
    from click.testing import CliRunner

    from sabnyx.commands import ask as ask_mod
    from sabnyx.schemas import AskAnswer

    _atlas_file(
        tmp_vault["ATLAS_CONCEPTS"],
        "markdown-is-the-new-html",
        description="markdown is the canonical agent surface",
        body="Agents prefer markdown surfaces; html is a projection.",
    )

    calls: list[dict] = []

    def fake_parse_structured(**kwargs):
        calls.append(kwargs)
        return AskAnswer(
            answer="Markdown; see [[markdown-is-the-new-html]].",
            cited=["markdown-is-the-new-html"],
            capture=None,
        )

    monkeypatch.setattr(ask_mod.llm, "parse_structured", fake_parse_structured)
    result = CliRunner().invoke(
        ask_mod.ask_cmd, ["what surface should agents get?", "--no-capture"]
    )

    assert result.exit_code == 0, result.output
    # the grep retriever actually assembled and fed the atlas body to the model
    user = calls[0]["user"]
    assert "### markdown-is-the-new-html" in user
    assert "Agents prefer markdown surfaces" in user
    # and the citation survived end-to-end
    assert "markdown-is-the-new-html" in result.output
