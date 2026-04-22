"""Parse and validate frontmatter for vault files.

Uses python-frontmatter for the YAML split, pydantic for the schema.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import frontmatter
from pydantic import ValidationError

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
    """Classify a vault file by its location."""
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

    Raises FrontmatterError on schema violations.
    """
    post = frontmatter.load(str(path))
    meta = dict(post.metadata)
    body = post.content
    kind = classify(path)

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
