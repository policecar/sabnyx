"""`sabnyx lint` — Gates 1, 2, 4 (intent, precision, containment).

Exit codes:
    0  all gates pass
    1  gate failures found
    2  usage error
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import click
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .. import fm
from ..paths import DATA_ROOT, OBSERVATIONS, SOURCES, VAULT
from ..util import walk_vault_md


def _lintable(path: Path) -> bool:
    """A schema-bearing vault .md file.

    Excludes operational `_review` files and raw `sources/` (no schema applies).
    Vault-membership scoping is applied separately by `--staged` (git reports
    repo-wide changes); explicitly-passed files are linted wherever they live.
    """
    return (
        path.suffix == ".md"
        and "_review" not in path.parts
        and SOURCES not in path.resolve().parents
    )


def _gate_4_containment(path: Path, meta: dict, kind: str) -> list[str]:
    """Gate 4: tier boundaries.

    intake/ must carry status: draft
    atlas/ must carry status: final
    observation must live under intake/.observations/
    """
    errors: list[str] = []
    status = meta.get("status")
    if kind == "intake" and status != "draft":
        errors.append(f"intake/ file has status={status!r}, expected 'draft'")
    if kind == "atlas" and status != "final":
        errors.append(f"atlas/ file has status={status!r}, expected 'final'")
    if meta.get("type") == "observation" and OBSERVATIONS not in path.parents:
        errors.append("observation file is not under vault/intake/.observations/")
    return errors


# -- staged mode: validate the index, not the working tree --------------------
#
# @decision DEC-LINT-001: --staged validates the blob each index entry holds,
#   read with `git cat-file --batch`, against the entry's own path.
# @rationale: the commit contains the index blobs. Reading the working-tree
#   file instead lets an unstaged correction hide an invalid staged page (and
#   an unstaged edit block a valid one). Names come NUL-delimited from raw
#   diff output (-z: no quoting, so a non-ASCII or quoted name is not lost);
#   classification and exclusions are lexical on that path, so a working-tree
#   symlink cannot change which tier an entry is checked as. Nothing writes
#   the index or the working tree.

# Index modes holding file content. 120000 (symlink) and 160000 (gitlink)
# hold a link target or a commit id, not a page: those entries fail.
_REGULAR_MODES = frozenset({"100644", "100755"})


class _StagedError(Exception):
    """Git could not report the index or a staged blob. The gate fails
    closed: it cannot vouch for a commit it did not see."""


@dataclass(frozen=True)
class _Blob:
    """One staged page: the bytes the commit will contain, and its path."""

    path: Path  # DATA_ROOT / index path, lexical, never resolved
    rel: PurePosixPath  # the same path relative to VAULT
    data: bytes


def _git(args: list[str], stdin: bytes | None = None) -> bytes:
    """Run git in DATA_ROOT's repository (the vault's, which may not be the
    code repo's) and return stdout. Raises _StagedError on any failure."""
    try:
        result = subprocess.run(
            ["git", *args], cwd=DATA_ROOT, input=stdin, capture_output=True, check=False
        )
    except OSError as e:
        raise _StagedError(f"git {args[0]}: {e}") from e
    if result.returncode != 0:
        detail = os.fsdecode(result.stderr).strip() or f"exit status {result.returncode}"
        raise _StagedError(f"git {args[0]} in {DATA_ROOT}: {detail}")
    return result.stdout


def _staged_targets() -> tuple[list[_Blob], list[tuple[Path, list[str]]]]:
    """The staged vault pages to validate, and entries that fail as they
    stand (unmerged, or not a regular file). Deletions are not read."""
    # Outside a repository `git diff --cached` falls back to --no-index and
    # answers with its usage text; ask the plain question first.
    _git(["rev-parse", "--git-dir"])
    # --no-renames: a rename is an addition plus a deletion, whatever
    # diff.renames says. --relative: paths relative to DATA_ROOT, even when
    # it is a subdirectory of its repository.
    out = _git(
        [
            "diff",
            "--cached",
            "--raw",
            "-z",
            "--no-renames",
            "--no-abbrev",
            "--no-color",
            "--no-ext-diff",
            "--relative",
        ]
    )
    records = out.split(b"\0")
    if records.pop() != b"" or len(records) % 2:
        raise _StagedError("git diff: malformed -z output")

    vault = PurePosixPath(VAULT.relative_to(DATA_ROOT).as_posix())
    sources = PurePosixPath(SOURCES.relative_to(DATA_ROOT).as_posix())
    wanted: list[tuple[PurePosixPath, str]] = []
    rejected: list[tuple[Path, list[str]]] = []
    for header, raw_path in zip(records[::2], records[1::2], strict=True):
        # ":<old mode> <new mode> <old sha> <new sha> <status>"
        fields = header.decode("ascii", errors="replace").split(" ")
        if not header.startswith(b":") or len(fields) != 5:
            raise _StagedError(f"git diff: unexpected record {header!r}")
        _old_mode, new_mode, _old_sha, new_sha, status = fields
        rel = PurePosixPath(os.fsdecode(raw_path))
        if status == "D" or not _staged_lintable(rel, vault, sources):
            continue
        if status == "U":
            rejected.append((DATA_ROOT / rel, ["unmerged in the index: resolve and stage it"]))
        elif status not in ("A", "M", "T"):
            raise _StagedError(f"git diff: unexpected status {status!r} for {rel}")
        elif new_mode not in _REGULAR_MODES:
            rejected.append((DATA_ROOT / rel, [f"staged as mode {new_mode}, not a regular file"]))
        else:
            wanted.append((rel, new_sha))

    blobs = _read_blobs([sha for _rel, sha in wanted])
    targets = [_Blob(DATA_ROOT / rel, rel.relative_to(vault), blobs[sha]) for rel, sha in wanted]
    return targets, rejected


def _staged_lintable(rel: PurePosixPath, vault: PurePosixPath, sources: PurePosixPath) -> bool:
    """_lintable for an index path: the same exclusions, decided on the
    path itself, never on what the working tree has there."""
    return (
        rel.suffix == ".md"
        and vault in rel.parents
        and "_review" not in rel.parts
        and sources not in rel.parents
    )


def _read_blobs(shas: list[str]) -> dict[str, bytes]:
    """Blob contents by id, from one `git cat-file --batch`. A missing
    object or a short read raises _StagedError rather than dropping a file."""
    if not shas:
        return {}
    out = _git(["cat-file", "--batch"], stdin="".join(f"{s}\n" for s in shas).encode())
    blobs: dict[str, bytes] = {}
    pos = 0
    for sha in shas:
        # "<sha> blob <size>\n<content>\n", or "<sha> missing\n"
        eol = out.find(b"\n", pos)
        header = out[pos:eol].decode("ascii", errors="replace").split(" ")
        if eol < 0 or len(header) != 3 or header[:2] != [sha, "blob"] or not header[2].isdigit():
            raise _StagedError(f"git cat-file: cannot read staged blob {sha}: {' '.join(header)}")
        start = eol + 1
        end = start + int(header[2])
        if out[end : end + 1] != b"\n":
            raise _StagedError(f"git cat-file: truncated output for blob {sha}")
        blobs[sha] = out[start:end]
        pos = end + 1
    return blobs


@click.command("lint")
@click.argument("files", nargs=-1, type=click.Path(exists=True, path_type=Path))
@click.option("--staged", is_flag=True, help="Only lint files staged for git commit.")
def lint_cmd(files: tuple[Path, ...], staged: bool) -> None:
    """Gates 1, 2, 4. Hard-fail on any violation."""
    console = Console()

    targets: Sequence[Path | _Blob]
    failures: list[tuple[Path, list[str]]] = []
    if staged:
        try:
            targets, failures = _staged_targets()
        except _StagedError as e:
            console.print(Text(f"could not inspect staged files: {e}", style="red"))
            sys.exit(1)
    elif files:
        targets = [f for f in files if _lintable(f)]
    else:
        targets = walk_vault_md(VAULT)

    if not targets and not failures:
        console.print("[dim]no vault .md files to lint[/dim]")
        sys.exit(0)

    skipped = 0
    for target in targets:
        path = target.path if isinstance(target, _Blob) else target
        errs: list[str] = []
        try:
            kind = (
                fm.classify_relative(target.rel) if isinstance(target, _Blob) else fm.classify(path)
            )
        except ValueError:
            # No schema tier applies (domains/ carries its own per-topic
            # schema, SPEC §1) — skip with a note, like sources/ (ADR-0005).
            # Only classification may skip: load() raises other ValueErrors
            # (UnicodeDecodeError is one), and those are failures.
            skipped += 1
            continue
        try:
            if isinstance(target, _Blob):
                meta, _body = fm.load_text(target.data.decode("utf-8"), kind, path=path)
            else:
                meta, _body, kind = fm.load(path)
        except fm.FrontmatterError as e:
            failures.append((path, e.errors))
            continue
        except Exception as e:
            failures.append((path, [f"parse error: {e}"]))
            continue

        # Gate 1: intent (description present for typed content)
        if kind in ("intake", "atlas") and not meta.get("description"):
            errs.append("Gate 1 intent: missing description")

        # Gate 4: containment
        errs.extend(_gate_4_containment(path, meta, kind))

        if errs:
            failures.append((path, errs))

    if skipped:
        console.print(f"[dim]{skipped} file(s) skipped (no schema tier, e.g. domains/)[/dim]")

    if failures:
        table = Table(title="sabnyx lint — failures", show_lines=True)
        table.add_column("File", style="bold")
        table.add_column("Gate / error")
        for path, errs in failures:
            # Text, not markup: a file name or error may contain "[".
            table.add_row(
                Text(str(path.relative_to(DATA_ROOT))), Text("\n".join(f"- {e}" for e in errs))
            )
        console.print(table)
        console.print(f"[red]{len(failures)} file(s) failed lint[/red]")
        sys.exit(1)

    console.print(f"[green]lint OK[/green] ({len(targets)} file(s))")
    sys.exit(0)
