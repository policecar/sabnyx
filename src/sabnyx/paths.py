"""Vault and ops paths resolved from either the data root or the code repo root.

Two roots exist:

  ROOT      -- the sabnyx *code* repository root (detected via SPEC.md, never
               changes at runtime).  Config files (.env, moonshot), SPEC.md,
               ROUTING.md, and ops/scripts/ live here and MUST always resolve
               from ROOT.

  DATA_ROOT -- the *data* repository root: where vault/ and ops/events.jsonl /
               ops/knowledge-log.md actually live.  Defaults to ROOT so that
               existing single-repo setups are 100% unchanged.  Override with
               the SABNYX_DATA_DIR env var to point at a separate private repo
               without touching any other code.

@decision DEC-001: Separate DATA_ROOT from ROOT via SABNYX_DATA_DIR env var
@rationale: Allows vault + ledger to live in a separate private git repo while
  keeping the code repo clean.  Defaulting to ROOT preserves existing behavior
  exactly.  A pure helper (resolve_data_root) keeps the resolution logic
  testable without import-time side effects or module-reload hacks.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


def repo_root() -> Path:
    """Return the sabnyx repo root.

    Resolves by walking up from this file. The repo root is the
    directory that contains SPEC.md.
    """
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        if (parent / "SPEC.md").is_file():
            return parent
    raise RuntimeError("could not locate sabnyx repo root (no SPEC.md found)")


def resolve_data_root(env: Mapping[str, str], root: Path) -> Path:
    """Return the data root given an environment mapping and a code root.

    If ``SABNYX_DATA_DIR`` is set and non-empty in *env*, expand and resolve
    it. Otherwise return *root* unchanged.

    This is a pure function (no side effects beyond path resolution) so that
    tests can call it directly without reloading the module.
    """
    raw = env.get("SABNYX_DATA_DIR", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return root


ROOT: Path = repo_root()
DATA_ROOT: Path = resolve_data_root(os.environ, ROOT)

VAULT: Path = DATA_ROOT / "vault"
INTAKE: Path = VAULT / "intake"
OBSERVATIONS: Path = INTAKE / ".observations"
ATLAS: Path = VAULT / "atlas"
ATLAS_CONCEPTS: Path = ATLAS / "concepts"
ATLAS_REFERENCES: Path = ATLAS / "references"
ATLAS_IDEAS: Path = ATLAS / "ideas"
# Lightweight entity tier (Phase 3): person/org/project pages live under atlas/.
ATLAS_PEOPLE: Path = ATLAS / "people"
ATLAS_ORGS: Path = ATLAS / "orgs"
ATLAS_PROJECTS: Path = ATLAS / "projects"
DOMAINS: Path = VAULT / "domains"
REVIEW: Path = VAULT / "_review"
SOURCES: Path = VAULT / "sources"  # raw immutable artifacts; no frontmatter schema

OPS: Path = DATA_ROOT / "ops"
EVENTS_JSONL: Path = OPS / "events.jsonl"
KNOWLEDGE_LOG: Path = OPS / "knowledge-log.md"


# The three lightweight entity atlas types (Phase 3). Promotable like the
# knowledge types but represent referenced things, not claims.
ENTITY_TYPES: frozenset[str] = frozenset({"person", "org", "project"})

ATLAS_TYPE_DIRS = {
    "concept": ATLAS_CONCEPTS,
    "reference": ATLAS_REFERENCES,
    "idea": ATLAS_IDEAS,
    "person": ATLAS_PEOPLE,
    "org": ATLAS_ORGS,
    "project": ATLAS_PROJECTS,
}


def ensure_tree() -> None:
    """Create all vault/ops directories if missing. Idempotent."""
    for p in [
        INTAKE,
        OBSERVATIONS,
        ATLAS_CONCEPTS,
        ATLAS_REFERENCES,
        ATLAS_IDEAS,
        ATLAS_PEOPLE,
        ATLAS_ORGS,
        ATLAS_PROJECTS,
        DOMAINS,
        REVIEW,
        SOURCES,
        OPS,
    ]:
        p.mkdir(parents=True, exist_ok=True)
    # keep empty dirs in git
    for p in [
        DOMAINS,
        REVIEW,
        SOURCES,
        OBSERVATIONS,
        ATLAS_IDEAS,
        ATLAS_REFERENCES,
        ATLAS_PEOPLE,
        ATLAS_ORGS,
        ATLAS_PROJECTS,
    ]:
        gk = p / ".gitkeep"
        if not gk.exists():
            gk.write_text("")
