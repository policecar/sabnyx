"""Byte-for-byte goldens for `health` and `tend --dry-run` (B3).

Why: these two reports are what the operator reads every day, and their
text is consumed by eye and by grep. A refactor that reorders a list,
re-words a warning or leaks a clock or a filesystem order into the output
should be a visible, reviewed diff, not a silent drift. Regenerate with
`UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py` — that run always
fails; rerun without it, review `git diff tests/golden/`, commit.
"""

from __future__ import annotations

import golden_vault
import pytest
from click.testing import CliRunner
from conftest import assert_golden
from rich.console import Console

from sabnyx import paths
from sabnyx.commands import health as health_mod
from sabnyx.commands import tend as tend_mod


def _golden_console() -> Console:
    return Console(width=100, no_color=True, force_terminal=False)


@pytest.fixture
def golden(tmp_vault, fixed_clock, monkeypatch):
    # Pin every input the reports read from the environment.
    monkeypatch.setenv("SABNYX_TRUSTED_SOURCES", "sabnyx-ingest")
    monkeypatch.setattr(health_mod, "_console", _golden_console)
    monkeypatch.setattr(tend_mod, "_console", _golden_console)
    golden_vault.build()


def _run(cmd, args: list[str]) -> str:
    result = CliRunner().invoke(cmd, args, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    return result.output


def test_health_golden(golden):
    # health appends a health.audited event after printing; restore the
    # fixture ledger so the second run reads the same input as the first.
    snapshot = paths.EVENTS_JSONL.read_bytes()
    first = _run(health_mod.health_cmd, [])
    paths.EVENTS_JSONL.write_bytes(snapshot)
    second = _run(health_mod.health_cmd, [])
    assert first == second
    assert_golden("health.txt", first)


def test_tend_dry_run_golden(golden):
    snapshot = paths.EVENTS_JSONL.read_bytes()
    first = _run(tend_mod.tend_cmd, ["--dry-run"])
    assert paths.EVENTS_JSONL.read_bytes() == snapshot  # dry run writes nothing
    second = _run(tend_mod.tend_cmd, ["--dry-run"])
    assert first == second
    assert_golden("tend-dry-run.txt", first)


def test_goldens_carry_the_ambiguity_lines(golden):
    """Requirement: the frozen text includes both ambiguity warnings with
    their page paths (vault-relative `atlas/…`, D6)."""
    out = _run(health_mod.health_cmd, [])
    assert (
        "ambiguous_alias: shared-lantern — pages: atlas/concepts/made-up-delta.md, "
        "atlas/concepts/made-up-epsilon.md" in out
    )
    assert (
        "ambiguous_stem: made-up-twin — pages: atlas/concepts/made-up-twin.md, "
        "atlas/projects/made-up-twin.md" in out
    )


# ---------------------------------------------------------------------------
# assert_golden: every exit
# ---------------------------------------------------------------------------


@pytest.fixture
def no_regen(monkeypatch):
    monkeypatch.delenv("UPDATE_GOLDEN", raising=False)


def test_assert_golden_match(tmp_path, no_regen):
    (tmp_path / "g.txt").write_bytes(b"same\n")
    assert_golden("g.txt", "same\n", golden_dir=tmp_path)


def test_assert_golden_mismatch_shows_diff(tmp_path, no_regen):
    (tmp_path / "g.txt").write_bytes(b"old line\n")
    with pytest.raises(pytest.fail.Exception, match=r"(?s)golden mismatch.*-old line.*\+new line"):
        assert_golden("g.txt", "new line\n", golden_dir=tmp_path)


def test_assert_golden_trailing_whitespace_is_a_mismatch(tmp_path, no_regen):
    (tmp_path / "g.txt").write_bytes(b"line\n")
    with pytest.raises(pytest.fail.Exception, match="golden mismatch"):
        assert_golden("g.txt", "line \n", golden_dir=tmp_path)


def test_assert_golden_missing_file_fails_and_creates_nothing(tmp_path, no_regen):
    with pytest.raises(pytest.fail.Exception, match="golden missing.*UPDATE_GOLDEN=1"):
        assert_golden("g.txt", "anything\n", golden_dir=tmp_path)
    assert not (tmp_path / "g.txt").exists()


def test_assert_golden_regen_writes_then_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("UPDATE_GOLDEN", "1")
    (tmp_path / "g.txt").write_bytes(b"old\n")
    with pytest.raises(pytest.fail.Exception, match="golden regenerated"):
        assert_golden("g.txt", "new\n", golden_dir=tmp_path)
    assert (tmp_path / "g.txt").read_bytes() == b"new\n"


def test_assert_golden_regen_zero_is_an_ordinary_compare(tmp_path, monkeypatch):
    monkeypatch.setenv("UPDATE_GOLDEN", "0")
    (tmp_path / "g.txt").write_bytes(b"old\n")
    with pytest.raises(pytest.fail.Exception, match="golden mismatch"):
        assert_golden("g.txt", "new\n", golden_dir=tmp_path)
    assert (tmp_path / "g.txt").read_bytes() == b"old\n"
