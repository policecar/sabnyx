"""Shared fixtures: a tmp-path vault that no test can pollute the repo with."""

from __future__ import annotations

import pytest

from sabnyx import ledger, sources


@pytest.fixture
def tmp_vault(tmp_path, monkeypatch):
    """Redirect every path constant the LLM-driven flows touch into tmp_path.

    Covers both import styles: modules that `from ..paths import X` get
    their module attribute patched directly (hasattr-guarded); modules
    that use `paths.X` at call time are covered by patching paths itself.
    """
    from sabnyx import paths as paths_mod
    from sabnyx import services as services_mod
    from sabnyx.commands import ask as ask_mod
    from sabnyx.commands import health as health_mod
    from sabnyx.commands import ingest as ingest_mod
    from sabnyx.commands import promote as promote_mod
    from sabnyx.commands import reweave as reweave_mod
    from sabnyx.commands import scan as scan_mod
    from sabnyx.commands import serve as serve_mod
    from sabnyx.commands import tend as tend_mod

    root = tmp_path
    vault = root / "vault"
    atlas_concepts = vault / "atlas" / "concepts"
    atlas_references = vault / "atlas" / "references"
    atlas_ideas = vault / "atlas" / "ideas"
    mapping = {
        "ROOT": root,
        "VAULT": vault,
        "INTAKE": vault / "intake",
        "OBSERVATIONS": vault / "intake" / ".observations",
        "ATLAS": vault / "atlas",
        "ATLAS_CONCEPTS": atlas_concepts,
        "ATLAS_REFERENCES": atlas_references,
        "ATLAS_IDEAS": atlas_ideas,
        "SOURCES": vault / "sources",
        "OPS": root / "ops",
        "EVENTS_JSONL": root / "ops" / "events.jsonl",
        "KNOWLEDGE_LOG": root / "ops" / "knowledge-log.md",
        "ATLAS_TYPE_DIRS": {
            "concept": atlas_concepts,
            "reference": atlas_references,
            "idea": atlas_ideas,
        },
    }
    for mod in (
        paths_mod,
        sources,
        ingest_mod,
        health_mod,
        serve_mod,
        ledger,
        tend_mod,
        promote_mod,
        reweave_mod,
        services_mod,
    ):
        for name, value in mapping.items():
            if hasattr(mod, name):
                monkeypatch.setattr(mod, name, value)

    def _ensure():
        for key in (
            "INTAKE",
            "OBSERVATIONS",
            "ATLAS_CONCEPTS",
            "ATLAS_REFERENCES",
            "ATLAS_IDEAS",
            "SOURCES",
            "OPS",
        ):
            mapping[key].mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(sources, "ensure_tree", _ensure)
    monkeypatch.setattr(ingest_mod, "ensure_tree", _ensure)
    monkeypatch.setattr(ask_mod, "ensure_tree", _ensure)
    monkeypatch.setattr(scan_mod, "ensure_tree", _ensure)
    monkeypatch.setattr(serve_mod, "ensure_tree", _ensure)
    monkeypatch.setattr(tend_mod, "ensure_tree", _ensure)
    _ensure()
    return mapping
