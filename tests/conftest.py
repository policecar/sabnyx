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
    from sabnyx.commands import ingest as ingest_mod

    root = tmp_path
    vault = root / "vault"
    mapping = {
        "ROOT": root,
        "VAULT": vault,
        "INTAKE": vault / "intake",
        "OBSERVATIONS": vault / "intake" / ".observations",
        "ATLAS": vault / "atlas",
        "ATLAS_CONCEPTS": vault / "atlas" / "concepts",
        "ATLAS_REFERENCES": vault / "atlas" / "references",
        "SOURCES": vault / "sources",
        "OPS": root / "ops",
        "EVENTS_JSONL": root / "ops" / "events.jsonl",
        "KNOWLEDGE_LOG": root / "ops" / "knowledge-log.md",
    }
    for mod in (paths_mod, sources, ingest_mod, ledger):
        for name, value in mapping.items():
            if hasattr(mod, name):
                monkeypatch.setattr(mod, name, value)

    def _ensure():
        for key in (
            "INTAKE",
            "OBSERVATIONS",
            "ATLAS_CONCEPTS",
            "ATLAS_REFERENCES",
            "SOURCES",
            "OPS",
        ):
            mapping[key].mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(sources, "ensure_tree", _ensure)
    monkeypatch.setattr(ingest_mod, "ensure_tree", _ensure)
    _ensure()
    return mapping
