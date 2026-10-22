"""Parse and validate frontmatter for vault files.

Uses python-frontmatter for the YAML split, pydantic for the schema.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import stat
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
    """Write a file with frontmatter. Does NOT validate — use after load/build.

    Atomic: renders the full content, writes it to a sibling temp file, then
    `os.replace`s it over `path`. A reader (or a crash) never observes a
    half-written file — either the old content or the new content, never a
    truncated mix. The temp file is created with `os.open(..., 0o666)`, not
    via `tempfile.NamedTemporaryFile` (which always mode-0600s regardless of
    umask) — so a brand-new file still gets the same `0o666 & ~umask` the
    kernel would have given a plain `write_text` (0o644 on a typical
    server). If `path` already exists, its permission bits are copied onto
    the temp file instead, so a rewrite doesn't silently reset them to the
    umask (e.g. a 0600 file in the vault must stay 0600).

    `path` must be a real file path, not a symlink the caller wants followed:
    os.replace() swaps the directory entry at `path` itself, so writing
    through a symlink would replace the link, not its target. No current
    caller writes through a symlink into the vault (grepped call sites).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # Assign .metadata rather than passing **meta: a frontmatter key named
    # "content" or "handler" would collide with Post's own parameters.
    post = frontmatter.Post(body)
    post.metadata = dict(meta)
    content = frontmatter.dumps(post) + "\n"

    old_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None

    tmp_name = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        # O_EXCL + O_CREAT, mode 0o666: the kernel applies the umask itself,
        # same as a plain open()/write_text() would — tempfile.NamedTemporary
        # File hardcodes 0600 regardless of umask, which would silently
        # tighten every brand-new vault file's permissions.
        fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        if old_mode is not None:
            os.chmod(tmp_name, old_mode)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise
