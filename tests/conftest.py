"""Shared fixtures: a tmp-path vault that no test can pollute the repo with.

ROOT != DATA_ROOT: mirrors production layout where the code repo and the data
repo live in separate directories.  tests/conftest.py previously set both to
the same tmp_path, which masked the class of bug where vault/ledger content
was relativized against ROOT (the code root) instead of DATA_ROOT (the data
root).  See: the vault/sources/start.md crash on the server.

DATA_ROOT hard floor (2026-10 security review): `paths.py` resolves DATA_ROOT
at import time from SABNYX_DATA_DIR, or failing that, the repo `.env` — on the
operator's machine that `.env` points at the LIVE vault. `tmp_vault` below
only protects tests that request it, so a test that forgets the fixture (or a
new command module patched is missing from `_PATH_BINDING_MODULES`) would
otherwise write straight into the real vault. The two module-level statements
immediately below close that gap for the WHOLE session, unconditionally,
before `sabnyx` (and therefore `sabnyx.paths`) is ever imported.
"""

from __future__ import annotations

import os
import tempfile

# --- DATA_ROOT hard floor -------------------------------------------------
# A real env var beats both a repo .env and the ROOT default — see
# paths.py's `effective_data_dir` precedence: env > .env > ROOT. Setting it
# here, before the first `import sabnyx`, pins DATA_ROOT under a fresh tmp
# dir for every test in this session, even one that never touches
# `tmp_vault` and even on a machine whose `.env` points SABNYX_DATA_DIR at
# the live vault.
_DATA_ROOT_FLOOR: str = tempfile.mkdtemp(prefix="sabnyx-pytest-data-")
os.environ["SABNYX_DATA_DIR"] = _DATA_ROOT_FLOOR

import ast  # noqa: E402
import difflib  # noqa: E402
from datetime import UTC, datetime  # noqa: E402
from pathlib import Path  # noqa: E402
from types import ModuleType  # noqa: E402

import pytest  # noqa: E402

from sabnyx import ledger, services, sources  # noqa: E402
from sabnyx import paths as paths_mod  # noqa: E402
from sabnyx.commands import ask as ask_mod  # noqa: E402
from sabnyx.commands import entities as entities_mod  # noqa: E402
from sabnyx.commands import health as health_mod  # noqa: E402
from sabnyx.commands import ingest as ingest_mod  # noqa: E402
from sabnyx.commands import lint as lint_mod  # noqa: E402
from sabnyx.commands import new as new_mod  # noqa: E402
from sabnyx.commands import observations as observations_mod  # noqa: E402
from sabnyx.commands import promote as promote_mod  # noqa: E402
from sabnyx.commands import reconcile as reconcile_mod  # noqa: E402
from sabnyx.commands import reweave as reweave_mod  # noqa: E402
from sabnyx.commands import scan as scan_mod  # noqa: E402
from sabnyx.commands import serve as serve_mod  # noqa: E402
from sabnyx.commands import tend as tend_mod  # noqa: E402
from sabnyx.commands import triage as triage_mod  # noqa: E402
from sabnyx.paths import repo_root  # noqa: E402


def _data_root_mapping(data_root: Path) -> dict[str, Path | dict[str, Path]]:
    """Every `paths` constant derived from DATA_ROOT, by name.

    Pure path arithmetic — no filesystem access — so this doubles as the
    single source of truth for (a) what `tmp_vault` redirects and (b) which
    `paths` names are "DATA_ROOT-derived" for the module-coverage audit in
    tests/test_fixture_coverage.py. `ROOT` is deliberately absent: it must
    never be redirected (config.py, llm.py, lint.py's `--staged` git lookup
    all legitimately read the real code-repo root).
    """
    vault = data_root / "vault"
    atlas = vault / "atlas"
    atlas_concepts = atlas / "concepts"
    atlas_references = atlas / "references"
    atlas_ideas = atlas / "ideas"
    atlas_people = atlas / "people"
    atlas_orgs = atlas / "orgs"
    atlas_projects = atlas / "projects"
    ops = data_root / "ops"
    return {
        "DATA_ROOT": data_root,
        "VAULT": vault,
        "INTAKE": vault / "intake",
        "OBSERVATIONS": vault / "intake" / ".observations",
        "ATLAS": atlas,
        "ATLAS_CONCEPTS": atlas_concepts,
        "ATLAS_REFERENCES": atlas_references,
        "ATLAS_IDEAS": atlas_ideas,
        "ATLAS_PEOPLE": atlas_people,
        "ATLAS_ORGS": atlas_orgs,
        "ATLAS_PROJECTS": atlas_projects,
        "SOURCES": vault / "sources",
        "REVIEW": vault / "_review",
        "OPS": ops,
        "EVENTS_JSONL": ops / "events.jsonl",
        "KNOWLEDGE_LOG": ops / "knowledge-log.md",
        "ATLAS_TYPE_DIRS": {
            "concept": atlas_concepts,
            "reference": atlas_references,
            "idea": atlas_ideas,
            "person": atlas_people,
            "org": atlas_orgs,
            "project": atlas_projects,
        },
    }


# Names from `_data_root_mapping` that a module might bind at MODULE level
# via `from ..paths import X` (frozen at import time). Excludes
# ATLAS_TYPE_DIRS's own dict values (not separately importable) — the key
# itself is still a valid import target (see commands/promote.py).
DATA_ROOT_DERIVED_NAMES: frozenset[str] = frozenset(
    _data_root_mapping(Path("/placeholder-data-root")).keys()
)

# Modules with a module-level `from ..paths import X` (or `from .paths import
# X`) binding for one of DATA_ROOT_DERIVED_NAMES. Patching `paths_mod`
# attributes alone does NOT reach these — that only covers call-time
# `paths.X` lookups (writers.py, context.py, fm.py, reweave.py) and
# function-local lazy imports (services.promote_file, util.walk_vault_md),
# which re-read the live `paths` module attribute on every call. A
# module-level import freezes its own copy at first import, so it must be
# patched directly, or a test using that module silently falls through to
# whatever DATA_ROOT was when the module first loaded.
#
# tests/test_fixture_coverage.py derives the "must patch" set straight from
# the source tree and asserts it is a subset of this tuple, so a new command
# module that binds one of these names can't silently fall out of coverage.
PATH_BINDING_MODULES: tuple[ModuleType, ...] = (
    paths_mod,
    sources,
    ingest_mod,
    health_mod,
    serve_mod,
    ledger,
    tend_mod,
    promote_mod,
    reweave_mod,
    services,
    entities_mod,
    lint_mod,
    new_mod,
    reconcile_mod,
    triage_mod,
)


@pytest.fixture(scope="session", autouse=True)
def _data_root_floor_guard() -> None:
    """Fail the whole session, loudly, if the DATA_ROOT floor ever breaks.

    This is the one thing standing between a test that forgets `tmp_vault`
    (or a code change to `paths.py`'s precedence order) and the operator's
    live vault. Session-scoped + autouse: runs once, before any test body,
    against the untouched import-time value (tmp_vault's per-test
    monkeypatching is function-scoped and undone between tests, so it never
    interferes with this check).
    """
    floor = Path(_DATA_ROOT_FLOOR).resolve()
    assert floor == paths_mod.DATA_ROOT, (
        f"DATA_ROOT floor broken: expected it pinned at {floor} "
        f"(SABNYX_DATA_DIR set in conftest.py before any sabnyx import), "
        f"got {paths_mod.DATA_ROOT}. Tests may be touching the real vault — "
        "stop and investigate before running anything else."
    )
    assert paths_mod.DATA_ROOT.is_relative_to(Path(tempfile.gettempdir()).resolve()), (
        f"DATA_ROOT floor ({paths_mod.DATA_ROOT}) is not under the system tmp "
        f"dir ({tempfile.gettempdir()}) — something is overriding the floor."
    )


@pytest.fixture
def tmp_vault(tmp_path, monkeypatch):
    """Redirect every path constant the LLM-driven flows touch into tmp_path.

    ROOT stays as the REAL code-repo root (repo_root()) so that config.py,
    llm.py and any other code-root consumers keep working normally.
    DATA_ROOT (and all vault/ops paths derived from it) are redirected to
    tmp_path, mirroring the production split where ROOT=/home/USER/sabnyx and
    DATA_ROOT=/home/USER/sabnyx-vault.

    This ensures that relative_to(ROOT) on a DATA_ROOT-resident path raises
    ValueError in tests, exactly as it would crash on the server.

    Covers both import styles: modules that `from ..paths import X` get
    their module attribute patched directly (hasattr-guarded, via
    PATH_BINDING_MODULES); modules that use `paths.X` at call time are
    covered by patching paths_mod itself (first in that tuple).
    """
    # The REAL code-repo root — never redirected.
    real_root = repo_root()

    # Data repo root: a fresh temp dir, separate from the code root.
    mapping = _data_root_mapping(tmp_path)

    # ROOT is NOT included: modules that legitimately read code-repo files
    # (config.py, llm.py, lint.py) must keep pointing at the real root.
    # The hasattr-guard means unused names are silently skipped.
    for mod in PATH_BINDING_MODULES:
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
            "REVIEW",
            "OPS",
        ):
            # every key listed here maps to a Path; ATLAS_TYPE_DIRS (a dict)
            # widens the inferred value type but is never iterated here.
            mapping[key].mkdir(parents=True, exist_ok=True)  # ty: ignore[unresolved-attribute]

    monkeypatch.setattr(sources, "ensure_tree", _ensure)
    monkeypatch.setattr(ingest_mod, "ensure_tree", _ensure)
    monkeypatch.setattr(ask_mod, "ensure_tree", _ensure)
    monkeypatch.setattr(scan_mod, "ensure_tree", _ensure)
    monkeypatch.setattr(serve_mod, "ensure_tree", _ensure)
    monkeypatch.setattr(tend_mod, "ensure_tree", _ensure)
    monkeypatch.setattr(entities_mod, "ensure_tree", _ensure)
    monkeypatch.setattr(new_mod, "ensure_tree", _ensure)
    monkeypatch.setattr(triage_mod, "ensure_tree", _ensure)
    monkeypatch.setattr(observations_mod, "ensure_tree", _ensure)
    _ensure()

    # Expose both roots so individual tests can assert ROOT != DATA_ROOT.
    mapping["ROOT"] = real_root
    return mapping


# ---------------------------------------------------------------------------
# Static coverage helpers for tests/test_fixture_coverage.py.
#
# Live here (not in the test file) because they read PATH_BINDING_MODULES /
# DATA_ROOT_DERIVED_NAMES, which are conftest internals; the test file just
# calls `conftest.modules_requiring_direct_patch()` and compares.
# ---------------------------------------------------------------------------

_SABNYX_SRC = Path(__file__).resolve().parent.parent / "src" / "sabnyx"
_SKIP_FILES = frozenset({"paths.py", "_dotenv.py", "__init__.py"})


def _module_level_paths_import_names(py_file: Path) -> set[str]:
    """Names bound by a MODULE-level `from .paths import X` / `from ..paths
    import X` in `py_file`. Only `tree.body` (not `ast.walk`) is scanned, so
    an import inside a function or a TYPE_CHECKING block — which does not
    freeze a value at module-import time — is correctly excluded."""
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module in ("paths", "sabnyx.paths"):
            names.update(alias.name for alias in node.names)
    return names


def _dotted_module_name(py_file: Path) -> str:
    rel = py_file.relative_to(_SABNYX_SRC.parent).with_suffix("")
    return ".".join(rel.parts)


def modules_requiring_direct_patch() -> dict[str, set[str]]:
    """Dotted module name -> DATA_ROOT-derived names it freezes at import,
    for every module under src/sabnyx that needs a direct `tmp_vault` patch.
    """
    found: dict[str, set[str]] = {}
    for py_file in sorted(_SABNYX_SRC.rglob("*.py")):
        if py_file.name in _SKIP_FILES:
            continue
        hit = _module_level_paths_import_names(py_file) & DATA_ROOT_DERIVED_NAMES
        if hit:
            found[_dotted_module_name(py_file)] = hit
    return found


# --- fixed clock + golden files (report-hygiene, B3) ------------------------

# The one instant every golden is rendered at. ledger.now() is the single
# clock seam: event timestamps, util.age_days and tend's item ages all read it.
FIXED_CLOCK = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)

GOLDEN_DIR = Path(__file__).parent / "golden"
GOLDEN_REGEN = "UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py"


@pytest.fixture
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> datetime:
    monkeypatch.setattr(ledger, "now", lambda: FIXED_CLOCK)
    return FIXED_CLOCK


def assert_golden(name: str, actual: str, *, golden_dir: Path = GOLDEN_DIR) -> None:
    """Compare `actual` with golden_dir/name, byte for byte.

    UPDATE_GOLDEN=1 rewrites the file and then FAILS, so a regen run can
    never pass for a check: rerun without it, review `git diff`, commit.
    A missing golden fails too (never created silently). Any other value of
    UPDATE_GOLDEN, including "0", is an ordinary comparison.
    """
    path = golden_dir / name
    if os.environ.get("UPDATE_GOLDEN") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(actual.encode("utf-8"))
        pytest.fail(f"golden regenerated: {name} — review git diff, rerun without UPDATE_GOLDEN")
    if not path.is_file():
        pytest.fail(f"golden missing: {name} — generate with `{GOLDEN_REGEN}`, review, commit")
    expected = path.read_bytes().decode("utf-8")
    if actual != expected:
        diff = "".join(
            difflib.unified_diff(
                expected.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile=f"golden/{name}",
                tofile="actual",
            )
        )
        pytest.fail(f"golden mismatch: {name}\n{diff}", pytrace=False)
