"""Parse and validate frontmatter for vault files.

Uses python-frontmatter for the YAML split, pydantic for the schema.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import frontmatter
import yaml
from pydantic import ValidationError

from . import paths
from .schemas import AtlasFrontmatter, IntakeFrontmatter, ObservationFrontmatter

FileKind = Literal["intake", "atlas", "observation"]


class FrontmatterError(Exception):
    """Raised when a file's frontmatter violates its schema."""

    def __init__(self, path: Path, kind: FileKind, errors: list[str]):
        self.path = path
        self.kind = kind
        self.errors = errors
        super().__init__(f"{path}: {kind} frontmatter invalid: {errors}")


def classify(path: Path) -> FileKind:
    """Classify a vault file by its tier relative to paths.VAULT.

    Classification keys off the VAULT-relative path, never the absolute one
    (a data root like /home/user/intake/... must not misclassify the whole
    vault). Files outside the vault (explicitly-passed lint targets) fall
    back to a component scan. Raises ValueError for paths no schema tier
    covers — e.g. domains/, which carries its own per-topic schema (SPEC §1);
    vault-walking callers skip those, they don't crash.
    """
    try:
        parts = path.resolve().relative_to(paths.VAULT.resolve()).parts
    except ValueError:
        parts = path.parts
    if ".observations" in parts:
        return "observation"
    if "intake" in parts:
        return "intake"
    if "atlas" in parts:
        return "atlas"
    raise ValueError(f"{path}: cannot classify (not under intake/atlas/observations)")


def load(path: Path) -> tuple[dict, str, FileKind]:
    """Load and validate a file. Returns (frontmatter_dict, body, kind).

    Raises FrontmatterError on schema violations or malformed YAML, and
    ValueError (from classify) for files no schema tier covers.
    """
    kind = classify(path)
    try:
        post = frontmatter.load(str(path))
    except yaml.YAMLError as e:
        # Malformed YAML (unclosed bracket, a tab in the block, ...) must be
        # the same recoverable error as a schema violation, not a raw parser
        # exception that aborts a whole tend run.
        raise FrontmatterError(path, kind, [f"malformed YAML: {e}"]) from e
    meta = dict(post.metadata)
    body = post.content

    model = {
        "intake": IntakeFrontmatter,
        "atlas": AtlasFrontmatter,
        "observation": ObservationFrontmatter,
    }[kind]

    try:
        model.model_validate(meta)
    except ValidationError as e:
        errors = [f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()]
        raise FrontmatterError(path, kind, errors) from e

    return meta, body, kind


def write(path: Path, meta: dict, body: str) -> None:
    """Write a file with frontmatter. Does NOT validate — use after load/build."""
    path.parent.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post(body, **meta)
    path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
