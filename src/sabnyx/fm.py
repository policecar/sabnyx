"""Parse and validate frontmatter for vault files.

Uses python-frontmatter for the YAML split, pydantic for the schema.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import stat
from collections.abc import Mapping
from pathlib import Path, PurePath
from typing import Any, Literal

import frontmatter
import yaml
from frontmatter.default_handlers import YAMLHandler
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


class _SafeYAML(YAMLHandler):
    """YAML frontmatter through yaml.safe_load, passed explicitly at every
    parse site in this module (and nowhere else parses frontmatter: a
    source guard in tests/test_source_guards.py enforces both).

    python-frontmatter's own YAMLHandler already *defaults* to SafeLoader
    (``kwargs.setdefault("Loader", SafeLoader)``, read from its source), but
    a default is a library choice a kwarg can override; this handler ignores
    loader kwargs entirely. A tag such as ``!!python/object/apply`` raises
    a YAMLError instead of constructing anything (IDD §10a).

    Passing a handler explicitly also skips the library's format detection
    (so a ``+++`` TOML or ``{`` JSON header is never parsed), and with it the
    ``detect()`` check that the text *starts* with a fence. ``split`` keeps
    that rule: without it, a file with no frontmatter but two ``---`` rules
    in its body would have the text between them parsed as frontmatter.
    """

    def load(self, fm: str, **kwargs: object) -> Any:
        return yaml.safe_load(fm)

    def split(self, text: str) -> tuple[str, str]:
        boundary = self.FM_BOUNDARY
        if boundary is None:
            # YAMLHandler sets it; None means the library changed under us.
            raise RuntimeError("frontmatter YAMLHandler.FM_BOUNDARY is unset")
        if not boundary.match(text):
            # parse() treats ValueError as "no frontmatter": the same result
            # auto-detection gives for text that doesn't start with a fence.
            raise ValueError("no frontmatter fence at the start of the text")
        return super().split(text)


_SAFE_YAML = _SafeYAML()


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
    return _classify_parts(parts, path)


def classify_relative(rel: PurePath) -> FileKind:
    """Classify a VAULT-relative path lexically, the same rules as classify().

    Touches no filesystem and resolves no symlink: for a path that names
    something other than the working-tree file, e.g. a git index entry
    (lint --staged), where the working tree may hold a different object.
    Raises ValueError like classify().
    """
    return _classify_parts(rel.parts, rel)


def _classify_parts(parts: tuple[str, ...], label: PurePath) -> FileKind:
    if ".observations" in parts:
        return "observation"
    if "intake" in parts:
        return "intake"
    if "atlas" in parts:
        return "atlas"
    raise ValueError(f"{label}: cannot classify (not under intake/atlas/observations)")


def load(path: Path) -> tuple[dict, str, FileKind]:
    """Load and validate a file. Returns (frontmatter_dict, body, kind).

    Raises FrontmatterError on schema violations or malformed YAML, and
    ValueError (from classify) for files no schema tier covers.
    """
    kind = classify(path)
    try:
        post = frontmatter.load(str(path), handler=_SAFE_YAML)
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


def load_text(text: str, kind: FileKind, *, path: Path) -> tuple[dict, str]:
    """Parse and strictly validate `text` as a `kind` file. Returns (meta, body).

    load() for text already in memory: a transition parses the exact
    decoded bytes it patches, and then its own patched output, without a
    second read of the file. `path` is only named in errors. Raises
    FrontmatterError on malformed YAML or a schema violation, as load() does.
    """
    try:
        meta, body = frontmatter.parse(text, handler=_SAFE_YAML)
    except yaml.YAMLError as e:
        raise FrontmatterError(path, kind, [f"malformed YAML: {e}"]) from e
    meta = dict(meta)
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
    return meta, body


def split_text(text: str) -> tuple[str, str]:
    """Split raw file text at the end of its frontmatter: (head, body) with
    ``head + body == text`` exactly.

    `head` is any leading whitespace, the opening fence, the YAML and the
    closing fence as load_text's handler matches them (FM_BOUNDARY, applied
    after the same leading strip python-frontmatter does); `body` is every
    byte after that, unnormalized (CRLF, indentation, trailing blanks kept).
    For byte-preserving edits that must stay out of the frontmatter: validate
    with load_text, edit `body`, rejoin. Raises ValueError when the text does
    not open with a fenced block.
    """
    boundary = _SAFE_YAML.FM_BOUNDARY
    if boundary is None:
        raise RuntimeError("frontmatter YAMLHandler.FM_BOUNDARY is unset")
    start = len(text) - len(text.lstrip())
    fences = boundary.finditer(text[start:])
    opening, closing = next(fences, None), next(fences, None)
    if opening is None or opening.start() != 0 or closing is None:
        raise ValueError("no fenced frontmatter block at the start of the text")
    cut = start + closing.end()
    return text[:cut], text[cut:]


def write(path: Path, meta: dict, body: str) -> None:
    """Write a file with frontmatter. Does NOT validate — use after load/build.

    Renders meta + body, then writes through write_text_atomic (below),
    which owns the atomicity, permission and durability guarantees.
    """
    # Assign .metadata rather than passing **meta: a frontmatter key named
    # "content" or "handler" would collide with Post's own parameters.
    post = frontmatter.Post(body)
    post.metadata = dict(meta)
    write_text_atomic(path, frontmatter.dumps(post) + "\n")


def write_text_atomic(path: Path, text: str) -> None:
    """Write `text` to `path` atomically. Does NOT validate.

    Atomic: writes the full content to a sibling temp file, then
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

    After `os.replace`, the parent directory is fsync'd too: fsyncing only
    the temp file's content makes the bytes durable, but the rename (the
    directory-entry change os.replace performs) needs its own fsync on the
    containing directory to survive power loss, not just a process crash --
    the crash-safety `os.replace` already buys on its own. Best-effort: a
    failure here is swallowed, since the crash-safety property this function
    exists for is already satisfied by the point `os.replace` returns.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    old_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None

    tmp_name = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        # O_EXCL + O_CREAT, mode 0o666: the kernel applies the umask itself,
        # same as a plain open()/write_text() would — tempfile.NamedTemporary
        # File hardcodes 0600 regardless of umask, which would silently
        # tighten every brand-new vault file's permissions.
        fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        # newline="": write exactly `text`, no "\n" → os.linesep translation
        # on any platform (CRLF files keep their bytes).
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as tmp:
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
        if old_mode is not None:
            os.chmod(tmp_name, old_mode)
        os.replace(tmp_name, path)
        with contextlib.suppress(OSError):
            dfd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


# -- frontmatter patcher (observation transitions) ----------------------------
#
# @decision DEC-FM-001: transitions patch frontmatter text in place instead
#   of load → modify → frontmatter.dumps.
# @rationale: frontmatter.dumps writes keys in alphabetical order (probe,
#   design D11), so a round-trip would reorder every hand-written file a
#   resolve touches. The patcher copies every byte it does not own; the
#   only lines that change are the ones it renders. It then re-parses its
#   own output and refuses unless the parsed metadata equals the input's
#   plus exactly the updates, and the body is unchanged. Correctness is
#   checked, not assumed from the line arithmetic.


class PatchError(ValueError):
    """patch_frontmatter refused: the input is outside the shape it can
    patch without touching bytes it does not own."""


# Keys the patcher may set, and the order appended keys take.
_APPENDED_KEYS = ("resolved", "resolution", "resolution_kind")
_PATCHABLE_KEYS = frozenset({"status", *_APPENDED_KEYS})
# One fence line (after an optional trailing "\r"): the per-line reading of
# YAMLHandler.FM_BOUNDARY (r"^-{3,}\s*$", MULTILINE).
_FENCE_LINE = re.compile(r"-{3,}\s*")
# A column-0 mapping key. Indented lines and "- item" lines never match.
_KEY_LINE = re.compile(r"([A-Za-z_][A-Za-z0-9_-]*)[ \t]*:")


def _render_line(key: str, value: object) -> str:
    """One `key: value` line, no line terminator. Raises unless the YAML
    is a single line that parses back to exactly `value`."""
    text = yaml.safe_dump(
        {key: value},
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
        width=float("inf"),
    )
    # One "\n"-terminated line, and no other break str.splitlines sees
    # (NEL/LS/PS are emitted raw by allow_unicode; NEL fails the round-trip).
    if not text.endswith("\n") or text.count("\n") != 1 or len(text.splitlines()) != 1:
        raise PatchError(f"{key}: value does not render as one line: {value!r}")
    if yaml.safe_load(text) != {key: value}:
        raise PatchError(f"{key}: value does not round-trip through YAML: {value!r}")
    return text[:-1]


def patch_frontmatter(raw: str, updates: Mapping[str, object]) -> str:
    """Return `raw` with `updates` applied to its frontmatter, every other
    byte copied.

    - `status`: its column-0 line is replaced in place; if the file has no
      status line, one is inserted after the column-0 `category` line.
    - `resolved`, `resolution`, `resolution_kind`: appended in that order
      before the closing fence; refused if any is already present.

    The file is split on "\\n" only, matching python-frontmatter's MULTILINE
    fence regex, never str.splitlines (which also breaks on NEL/LS/PS).
    Rendered lines take the newline of the file's first line (CRLF is
    preserved). Leading whitespace before the opening fence is copied byte
    for byte: python-frontmatter strips the text before splitting, so
    fm.load accepts such a file, and a file fm.load counts must be one the
    patcher can resolve (P1). Raises PatchError when the input is not a flat
    frontmatter block opening the file, when a key it owns cannot be located
    unambiguously, or when its own output does not re-parse to the input
    metadata plus exactly `updates` with the body unchanged.
    """
    unknown = set(updates) - _PATCHABLE_KEYS
    if unknown:
        raise PatchError(f"not patchable: {sorted(unknown)}")

    # Exactly what frontmatter.parse's .strip() removes from the front; the
    # opening fence must follow it directly.
    lead = raw[: len(raw) - len(raw.lstrip())]
    lines = raw[len(lead) :].split("\n")  # each element keeps its "\r" in a CRLF file
    cr = "\r" if lines[0].endswith("\r") else ""

    def bare(line: str) -> str:
        return line[:-1] if line.endswith("\r") else line

    if not _FENCE_LINE.fullmatch(bare(lines[0])):
        raise PatchError("frontmatter must open the file with a --- fence")
    close = next((i for i in range(1, len(lines)) if _FENCE_LINE.fullmatch(bare(lines[i]))), None)
    if close is None:
        raise PatchError("frontmatter has no closing --- fence")

    try:
        before, before_body = frontmatter.parse(raw, handler=_SAFE_YAML)
    except yaml.YAMLError as e:
        raise PatchError(f"malformed YAML: {e}") from e
    if any(isinstance(v, dict) for v in before.values()):
        raise PatchError("nested frontmatter is not patchable")
    present = sorted(k for k in _APPENDED_KEYS if k in updates and k in before)
    if present:
        raise PatchError(f"already present: {present}")

    block = lines[1:close]
    keys_at = {}
    for i, line in enumerate(block):
        m = _KEY_LINE.match(line)
        if m:
            keys_at.setdefault(m.group(1), []).append(i)
    for key in ("status", "category"):
        if len(keys_at.get(key, [])) > 1:
            raise PatchError(f"more than one column-0 {key!r} line")

    new_block = list(block)
    if "status" in updates:
        status_line = _render_line("status", updates["status"]) + cr
        if "status" in keys_at:
            new_block[keys_at["status"][0]] = status_line
        elif "status" in before:
            # parsed, but not as a plain column-0 line (quoted key, ...):
            # inserting a second one would leave the old one in the file.
            raise PatchError("status key present but not on a column-0 line")
        elif "category" in keys_at:
            new_block.insert(keys_at["category"][0] + 1, status_line)
        else:
            raise PatchError("no status line, and no category line to insert it after")
    new_block += [_render_line(k, updates[k]) + cr for k in _APPENDED_KEYS if k in updates]

    out = lead + "\n".join([lines[0], *new_block, *lines[close:]])

    after, after_body = frontmatter.parse(out, handler=_SAFE_YAML)
    if after != {**before, **updates} or after_body != before_body:
        raise PatchError("patched output does not re-parse to the input plus the updates")
    return out
