"""Vault paths resolved from the repo root."""

from __future__ import annotations

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


ROOT: Path = repo_root()
VAULT: Path = ROOT / "vault"
INTAKE: Path = VAULT / "intake"
OBSERVATIONS: Path = INTAKE / ".observations"
ATLAS: Path = VAULT / "atlas"
ATLAS_CONCEPTS: Path = ATLAS / "concepts"
ATLAS_REFERENCES: Path = ATLAS / "references"
ATLAS_IDEAS: Path = ATLAS / "ideas"
DOMAINS: Path = VAULT / "domains"
REVIEW: Path = VAULT / "_review"

OPS: Path = ROOT / "ops"
EVENTS_JSONL: Path = OPS / "events.jsonl"
KNOWLEDGE_LOG: Path = OPS / "knowledge-log.md"


ATLAS_TYPE_DIRS = {
    "concept": ATLAS_CONCEPTS,
    "reference": ATLAS_REFERENCES,
    "idea": ATLAS_IDEAS,
}


def ensure_tree() -> None:
    """Create all vault/ops directories if missing. Idempotent."""
    for p in [
        INTAKE,
        OBSERVATIONS,
        ATLAS_CONCEPTS,
        ATLAS_REFERENCES,
        ATLAS_IDEAS,
        DOMAINS,
        REVIEW,
        OPS,
    ]:
        p.mkdir(parents=True, exist_ok=True)
    # keep empty dirs in git
    for p in [DOMAINS, REVIEW, OBSERVATIONS, ATLAS_IDEAS, ATLAS_REFERENCES]:
        gk = p / ".gitkeep"
        if not gk.exists():
            gk.write_text("")
