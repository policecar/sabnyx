"""The one reader for vault/intake/.observations/ (IDD §5/§6, D1, D7, D13).

Every command that needs observations asks this module; nothing else
enumerates the folder (a source guard in tests/test_source_guards.py
enforces that). One parse path means every reader agrees on what is
pending: an archived or malformed file is never a pending one.

This module never writes and never imports `writers`. Imports point one
way: commands → writers → observations → fm / schemas / paths.

References ``paths`` by module attribute (``paths.OBSERVATIONS``, never
``from .paths import OBSERVATIONS``) so tests redirect the vault without
listing this module in conftest's path-binding tuple.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import fm, paths
from .schemas import ObservationFrontmatter, ObservationStatus


@dataclass(frozen=True)
class Observation:
    """One valid observation file. `category` is the type; this is the instance.

    No body (no reader needs it) and no obs_id: the id needs the vault's
    alias index, and keeping it off the record keeps status polls cheap.
    """

    path: Path  # absolute
    rel: str  # DATA_ROOT-relative, POSIX: the ledger `subject` form
    fm: ObservationFrontmatter


def load_all() -> tuple[list[Observation], list[tuple[Path, str]]]:
    """Every observation file: (valid records, malformed files with the error).

    Both lists are sorted by filename. A malformed file is returned, never
    swallowed and never counted: callers report it (health, triage) or
    ignore it explicitly. Only regular ``*.md`` files directly under the
    folder are read. A missing folder is empty, not an error.

    Cost: parses every observation file on every call (no cache).
    """
    valid: list[Observation] = []
    malformed: list[tuple[Path, str]] = []
    if not paths.OBSERVATIONS.is_dir():
        return valid, malformed
    files = sorted(
        (p for p in paths.OBSERVATIONS.glob("*.md") if p.is_file()), key=lambda p: p.name
    )
    for p in files:
        try:
            meta, _body, _kind = fm.load(p)
        except fm.FrontmatterError as e:
            malformed.append((p, "; ".join(e.errors)))
            continue
        except UnicodeDecodeError as e:
            # Not UTF-8: as malformed as bad YAML, and must not crash a
            # status poll. Anything else (permissions, I/O) propagates.
            malformed.append((p, f"not UTF-8: {e}"))
            continue
        valid.append(
            Observation(
                path=p,
                rel=p.relative_to(paths.DATA_ROOT).as_posix(),
                fm=ObservationFrontmatter.model_validate(meta),
            )
        )
    return valid, malformed


def observations(
    status: ObservationStatus | Literal["all"] = "pending",
) -> list[Observation]:
    """Valid observations with `status` (default: pending), sorted by filename.

    Malformed files are in no status: use load_all() to see them.
    """
    valid, _malformed = load_all()
    if status == "all":
        return valid
    return [o for o in valid if o.fm.status == status]
