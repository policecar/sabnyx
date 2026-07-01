"""`sabnyx new` — pre-write validation, schema-backed source_url rule, Gate 3 log."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from sabnyx import ledger


@pytest.fixture
def new_mod(tmp_vault, monkeypatch):
    from sabnyx.commands import new as new_mod

    monkeypatch.setattr(new_mod, "DATA_ROOT", tmp_vault["DATA_ROOT"])
    monkeypatch.setattr(new_mod, "INTAKE", tmp_vault["INTAKE"])
    monkeypatch.setattr(new_mod, "OBSERVATIONS", tmp_vault["OBSERVATIONS"])
    monkeypatch.setattr(new_mod, "ensure_tree", lambda: None)
    return new_mod


def _invoke(new_mod, *args):
    return CliRunner().invoke(new_mod.new_cmd, list(args))


def test_new_validates_before_writing(tmp_vault, new_mod):
    # 29 chars: one short of the minimum. Must exit 3 with NOTHING on disk
    # (previously it wrote the file, then deleted it).
    result = _invoke(new_mod, "--type", "note", "--title", "Short Desc", "--description", "x" * 29)
    assert result.exit_code == 3, result.output
    assert "nothing written" in result.output
    assert not list(tmp_vault["INTAKE"].glob("*.md"))
    assert not tmp_vault["EVENTS_JSONL"].exists()


def test_new_bookmark_without_url_rejected_by_schema(tmp_vault, new_mod):
    result = _invoke(
        new_mod,
        "--type",
        "bookmark",
        "--title",
        "A Bookmark",
        "--description",
        "a description that is definitely at least 30 characters long",
    )
    assert result.exit_code == 3, result.output
    assert "source_url is required" in result.output
    assert not list(tmp_vault["INTAKE"].glob("*.md"))


def test_new_appends_knowledge_log(tmp_vault, new_mod):
    result = _invoke(
        new_mod,
        "--type",
        "thought",
        "--title",
        "Gate Three",
        "--description",
        "a description that is definitely at least 30 characters long",
    )
    assert result.exit_code == 0, result.output
    assert (tmp_vault["INTAKE"] / "gate-three.md").exists()
    # Gate 3 grep contract: `grep "^# \[" knowledge-log.md`
    log = tmp_vault["KNOWLEDGE_LOG"].read_text()
    assert any(ln.startswith("# [") and "new | Gate Three" in ln for ln in log.splitlines())
    kinds = [e.kind for e in ledger.replay()]
    assert "intake.created" in kinds
