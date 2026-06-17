"""Shared fixtures: a tmp-path vault that no test can pollute the repo with.

ROOT != DATA_ROOT: mirrors production layout where the code repo and the data
repo live in separate directories.  tests/conftest.py previously set both to
the same tmp_path, which masked the class of bug where vault/ledger content
was relativized against ROOT (the code root) instead of DATA_ROOT (the data
root).  See: the vault/sources/start.md crash on the server.
"""

from __future__ import annotations

import pytest

from sabnyx import ledger, sources
from sabnyx.paths import repo_root


@pytest.fixture
def tmp_vault(tmp_path, monkeypatch):
    """Redirect every path constant the LLM-driven flows touch into tmp_path.

    ROOT stays as the REAL code-repo root (repo_root()) so that config.py,
    llm.py and any other code-root consumers keep working normally.
    DATA_ROOT (and all vault/ops paths derived from it) are redirected to
    tmp_path, mirroring the production split where ROOT=/home/p/sabnyx and
    DATA_ROOT=/home/p/sabnyx-vault.

    This ensures that relative_to(ROOT) on a DATA_ROOT-resident path raises
    ValueError in tests, exactly as it would crash on the server.

    Covers both import styles: modules that `from ..paths import X` get
    their module attribute patched directly (hasattr-guarded); modules
    that use `paths.X` at call time are covered by patching paths itself.
    """
    from sabnyx import paths as paths_mod
    from sabnyx import services as services_mod
    from sabnyx.commands import ask as ask_mod
    from sabnyx.commands import entities as entities_mod
    from sabnyx.commands import health as health_mod
    from sabnyx.commands import ingest as ingest_mod
    from sabnyx.commands import promote as promote_mod
    from sabnyx.commands import reweave as reweave_mod
    from sabnyx.commands import scan as scan_mod
    from sabnyx.commands import serve as serve_mod
    from sabnyx.commands import tend as tend_mod

    # The REAL code-repo root — never redirected.
    real_root = repo_root()

    # Data repo root: a fresh temp dir, separate from the code root.
    data_root = tmp_path
    vault = data_root / "vault"
    atlas_concepts = vault / "atlas" / "concepts"
    atlas_references = vault / "atlas" / "references"
    atlas_ideas = vault / "atlas" / "ideas"
    atlas_people = vault / "atlas" / "people"
    atlas_orgs = vault / "atlas" / "orgs"
    atlas_projects = vault / "atlas" / "projects"

    # ROOT is NOT included: modules that legitimately read code-repo files
    # (config.py, llm.py, lint.py) must keep pointing at the real root.
    # The hasattr-guard means unused names are silently skipped.
    mapping = {
        "DATA_ROOT": data_root,
        "VAULT": vault,
        "INTAKE": vault / "intake",
        "OBSERVATIONS": vault / "intake" / ".observations",
        "ATLAS": vault / "atlas",
        "ATLAS_CONCEPTS": atlas_concepts,
        "ATLAS_REFERENCES": atlas_references,
        "ATLAS_IDEAS": atlas_ideas,
        "ATLAS_PEOPLE": atlas_people,
        "ATLAS_ORGS": atlas_orgs,
        "ATLAS_PROJECTS": atlas_projects,
        "SOURCES": vault / "sources",
        "OPS": data_root / "ops",
        "EVENTS_JSONL": data_root / "ops" / "events.jsonl",
        "KNOWLEDGE_LOG": data_root / "ops" / "knowledge-log.md",
        "ATLAS_TYPE_DIRS": {
            "concept": atlas_concepts,
            "reference": atlas_references,
            "idea": atlas_ideas,
            "person": atlas_people,
            "org": atlas_orgs,
            "project": atlas_projects,
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
        entities_mod,
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
            "ATLAS_PEOPLE",
            "ATLAS_ORGS",
            "ATLAS_PROJECTS",
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
    monkeypatch.setattr(entities_mod, "ensure_tree", _ensure)
    _ensure()

    # Expose both roots so individual tests can assert ROOT != DATA_ROOT.
    mapping["ROOT"] = real_root
    return mapping
