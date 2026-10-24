"""`sabnyx observations` list / resolve / archive (IDD §6 CLI, D12).

Driven through CliRunner against real files under tmp_vault. The clock seam
(ledger.now, and so ledger.today) is pinned so ages are deterministic.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from sabnyx import fm, ledger, paths
from sabnyx import observations as obs
from sabnyx.commands.observations import observations_cmd
from sabnyx.schemas import ObservationFrontmatter

NOW = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)
REL = "vault/intake/.observations"


@pytest.fixture
def vault(tmp_vault, monkeypatch):
    monkeypatch.setattr(ledger, "now", lambda: NOW)
    fm.write(
        tmp_vault["ATLAS_CONCEPTS"] / "alpha-concept.md",
        {"type": "concept", "description": "a synthetic page for the observations CLI tests"},
        "# alpha-concept\n",
    )
    return tmp_vault


def _obs(name: str, *, folder: Path | None = None, **meta: object) -> Path:
    base: dict = {
        "type": "observation",
        "category": "friction",
        "observed": "2026-06-12",
        "source_agent": "manual",
        "status": "pending",
    }
    p = (folder or paths.OBSERVATIONS) / name
    fm.write(p, base | meta, f"# {name}\n")
    return p


def _run(*args: str):
    return CliRunner().invoke(observations_cmd, list(args), catch_exceptions=False)


def _resolved_events() -> list:
    return [e for e in ledger.replay() if e.kind == "observation.resolved"]


# -- resolve / archive -------------------------------------------------------------------


def test_resolve_succeeds_and_prints_the_new_status(vault):
    p = _obs("a.md")
    result = _run("resolve", f"{REL}/a.md", "--reason", "fixed it")
    assert result.exit_code == 0, result.output
    assert f"resolved {REL}/a.md → promoted (done)" in result.output
    assert fm.load(p)[0]["resolution_kind"] == "done"
    (e,) = _resolved_events()
    assert (e.actor, e.subject) == ("sabnyx-cli", f"{REL}/a.md")


def test_archive_succeeds_with_its_kind(vault):
    p = _obs("a.md")
    result = _run("archive", str(p), "--kind", "duplicate", "--reason", "same as b")
    assert result.exit_code == 0, result.output
    assert f"archived {REL}/a.md → archived (duplicate)" in result.output
    assert fm.load(p)[0]["status"] == "archived"


def test_refusal_exits_1_with_the_reason(vault):
    p = _obs("a.md")
    assert _run("resolve", str(p), "--reason", "first").exit_code == 0
    before = p.read_bytes()
    result = _run("resolve", str(p), "--reason", "again")
    assert result.exit_code == 1
    assert "refused (not_pending):" in result.stderr
    assert p.read_bytes() == before
    assert len(_resolved_events()) == 1


def test_archive_kind_done_is_a_usage_error_and_touches_nothing(vault):
    p = _obs("a.md")
    before = p.read_bytes()
    result = CliRunner().invoke(
        observations_cmd, ["archive", str(p), "--kind", "done", "--reason", "x"]
    )
    assert result.exit_code == 2
    assert "Invalid value for '--kind'" in result.output
    assert p.read_bytes() == before
    assert _resolved_events() == []


def test_dotdot_file_through_the_cli_is_refused_outside(vault):
    x = _obs("x.md", folder=vault["INTAKE"])
    before = x.read_bytes()
    result = _run("resolve", f"{REL}/../x.md", "--reason", "fixed it")
    assert result.exit_code == 1
    assert "refused (outside):" in result.stderr
    assert x.read_bytes() == before


def test_relative_file_resolves_against_data_root_not_cwd(vault, tmp_path_factory, monkeypatch):
    p = _obs("2026-06-12-friction-alpha.md")
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    # A decoy at the same relative path under the cwd: it must stay untouched.
    decoy = _obs("2026-06-12-friction-alpha.md", folder=elsewhere / REL)
    decoy_bytes = decoy.read_bytes()
    monkeypatch.chdir(elsewhere)

    result = _run("resolve", f"{REL}/2026-06-12-friction-alpha.md", "--reason", "fixed it")

    assert result.exit_code == 0, result.output
    assert fm.load(p)[0]["status"] == "promoted"
    assert decoy.read_bytes() == decoy_bytes


@pytest.mark.parametrize("sub", ["resolve", "archive"])
def test_help_says_relative_file_differs_from_promote(sub):
    out = " ".join(_run(sub, "--help").output.split())
    assert "resolved against the data root (DATA_ROOT), not the current directory" in out
    assert "this differs from `promote`" in out


# -- list ---------------------------------------------------------------------------------


@pytest.fixture
def listed(vault):
    """Filename order (alpha < mid-a < mid-b < omega) differs from observed order."""
    _obs(
        "omega-early.md",
        observed="2026-06-01",
        category="gap",
        target="beta-widget",
        source_agent="sabnyx-entities",
    )
    _obs("mid-b.md", observed="2026-06-10")
    _obs(
        "mid-a.md",
        observed="2026-06-10",
        category="connection",
        source_agent="telegram-agent",
        involves=["[[alpha-concept]]", "[bold]x[/bold]"],
    )
    _obs("alpha-late.md", observed="2026-06-18")
    _obs(
        "done.md",
        status="promoted",
        resolved="2026-06-19",
        resolution="fixed",
        resolution_kind="done",
    )
    _obs(
        "gone.md",
        status="archived",
        resolved="2026-06-19",
        resolution="dup",
        resolution_kind="duplicate",
        category="gap",
    )
    return vault


def _rows(result) -> list[list[str]]:
    assert result.exit_code == 0, result.output
    return [line.split("  ") for line in result.output.splitlines() if line.startswith(REL)]


def _names(result) -> list[str]:
    return [Path(r[0]).name for r in _rows(result)]


def test_list_defaults_to_pending_sorted_by_observed_then_filename(listed):
    assert _names(_run("list")) == ["omega-early.md", "mid-a.md", "mid-b.md", "alpha-late.md"]


@pytest.mark.parametrize(
    ("args", "names"),
    [
        (["--status", "promoted"], ["done.md"]),
        (["--status", "archived"], ["gone.md"]),
        (["--status", "all", "--category", "gap"], ["omega-early.md", "gone.md"]),
        (["--category", "gap"], ["omega-early.md"]),
        (
            ["--status", "all"],
            ["omega-early.md", "mid-a.md", "mid-b.md", "done.md", "gone.md", "alpha-late.md"],
        ),
    ],
)
def test_list_filters_by_status_and_category(listed, args, names):
    assert _names(_run("list", *args)) == names


def test_list_columns_age_and_id(listed):
    rows = {Path(r[0]).name: r for r in _rows(_run("list"))}
    meta = fm.load(paths.OBSERVATIONS / "omega-early.md")[0]
    oid = obs.obs_id(ObservationFrontmatter.model_validate(meta), obs.resolver())
    assert oid is not None

    # rel, category, observed, age (against the pinned clock), id, target, involves
    assert rows["omega-early.md"] == [
        f"{REL}/omega-early.md",
        "gap",
        "2026-06-01",
        f"{(date(2026, 6, 20) - date(2026, 6, 1)).days}d",
        oid.removeprefix("sha256:")[:12],
        "beta-widget",
        "—",
    ]
    assert len(rows["omega-early.md"][4]) == 12
    int(rows["omega-early.md"][4], 16)
    # Prose (an untrusted source, a connection): no id, no target.
    mid_a = rows["mid-a.md"]
    assert mid_a[1:6] == ["connection", "2026-06-10", "10d", "—", "—"]


def test_list_prints_markup_in_involves_literally(listed):
    rows = {Path(r[0]).name: r for r in _rows(_run("list"))}
    assert rows["mid-a.md"][6] == "[[alpha-concept]], [bold]x[/bold]"


def test_list_with_nothing_to_show(vault):
    result = _run("list")
    assert result.exit_code == 0
    assert "no pending observations" in result.output
