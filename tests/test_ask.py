"""Tests for the ask command: routing, grounded answers, capture-back."""

from __future__ import annotations

from click.testing import CliRunner

from sabnyx import ledger
from sabnyx.schemas import AskAnswer, ProposedIntake

CAPTURING = AskAnswer(
    answer="Markdown should be the canonical surface; see [[markdown-is-the-new-html]].",
    cited=["markdown-is-the-new-html"],
    capture=ProposedIntake(
        type="thought",
        title="Agents prefer markdown surfaces",
        description="Synthesis: agent-facing surfaces should serve markdown; HTML is projection.",
        tags=["publishing"],
        body="Builds on [[markdown-is-the-new-html]].",
    ),
)

PLAIN = AskAnswer(answer="The vault does not cover this.", cited=[], capture=None)


def _run_ask(monkeypatch, args, answer=CAPTURING, input_text=None):
    from sabnyx.commands import ask as ask_mod

    calls: list[dict] = []

    def fake_parse_structured(**kwargs):
        calls.append(kwargs)
        return answer

    monkeypatch.setattr(ask_mod.llm, "parse_structured", fake_parse_structured)
    runner = CliRunner()
    result = runner.invoke(ask_mod.ask_cmd, args, input=input_text)
    return result, calls


def test_ask_answers_and_captures_with_yes(tmp_vault, monkeypatch):
    result, calls = _run_ask(monkeypatch, ["what surface should agents get?", "--yes"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]["output_format"] is AskAnswer
    assert "canonical surface" in result.output

    # capture filed into intake/ through the validated writer
    intake = tmp_vault["INTAKE"] / "agents-prefer-markdown-surfaces.md"
    assert intake.exists()
    text = intake.read_text()
    assert "type: thought" in text
    assert "source-id:sabnyx-ask:" in text

    kinds = [e.kind for e in ledger.replay()]
    assert "ask.answered" in kinds
    assert "intake.created" in kinds

    log = tmp_vault["KNOWLEDGE_LOG"].read_text()
    assert "] ask-capture | Agents prefer markdown surfaces" in log


def test_ask_no_capture_flag_suppresses_filing(tmp_vault, monkeypatch):
    result, _ = _run_ask(monkeypatch, ["q?", "--no-capture"])
    assert result.exit_code == 0, result.output
    assert not list(tmp_vault["INTAKE"].glob("*.md"))
    kinds = [e.kind for e in ledger.replay()]
    assert "ask.answered" in kinds
    assert "intake.created" not in kinds


def test_ask_declined_capture(tmp_vault, monkeypatch):
    result, _ = _run_ask(monkeypatch, ["q?"], input_text="n\n")
    assert result.exit_code == 0, result.output
    assert not list(tmp_vault["INTAKE"].glob("*.md"))


def test_ask_without_capture_proposal_never_prompts(tmp_vault, monkeypatch):
    # no input provided: a prompt would raise an Abort and exit non-zero
    result, _ = _run_ask(monkeypatch, ["q?"], answer=PLAIN)
    assert result.exit_code == 0, result.output
    assert "does not cover" in result.output
    assert not list(tmp_vault["INTAKE"].glob("*.md"))


def test_clean_citations_drops_blanks_and_dedupes():
    from sabnyx.services import _clean_citations

    assert _clean_citations(["a", "", " a ", "B", "b"], "irrelevant") == ["a", "B"]


def test_clean_citations_falls_back_to_answer_wikilinks():
    from sabnyx.services import _clean_citations

    # model left cited blank but cited inline — recover from the prose
    out = _clean_citations(["", "  "], "see [[foo-bar]] and [[baz]] and [[foo-bar]]")
    assert out == ["foo-bar", "baz"]


def test_ask_routes_by_message(tmp_vault, monkeypatch):
    _, calls = _run_ask(monkeypatch, ["[deep] q?", "--no-capture"])
    assert calls[0]["role"] == "deep"
    _, calls = _run_ask(monkeypatch, ["what time?", "--no-capture"])
    assert calls[0]["role"] == "fast"
    _, calls = _run_ask(monkeypatch, ["what time?", "--role", "standard", "--no-capture"])
    assert calls[0]["role"] == "standard"
