"""Tests for the ask command: routing, grounded answers, capture-back."""

from __future__ import annotations

import json

import pytest
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


def test_clean_citations_fallback_filters_hallucinated_slugs():
    from pathlib import Path

    from sabnyx.services import _clean_citations

    index = {"foo-bar": Path("vault/atlas/concepts/foo-bar.md")}  # lowercased keys
    out = _clean_citations(["", ""], "see [[foo-bar]] and the invented [[not-a-page]]", index)
    assert out == ["foo-bar"]  # the hallucinated slug never reaches the ledger


def test_ask_forced_capture_with_no_proposal_prints_note(tmp_vault, monkeypatch):
    result, _ = _run_ask(monkeypatch, ["q?", "--capture"], answer=PLAIN)
    assert result.exit_code == 0, result.output
    assert "proposed no capture" in result.output
    assert not list(tmp_vault["INTAKE"].glob("*.md"))


def test_ask_routes_by_message(tmp_vault, monkeypatch):
    _, calls = _run_ask(monkeypatch, ["[deep] q?", "--no-capture"])
    assert calls[0]["role"] == "deep"
    _, calls = _run_ask(monkeypatch, ["what time?", "--no-capture"])
    assert calls[0]["role"] == "fast"
    _, calls = _run_ask(monkeypatch, ["what time?", "--role", "standard", "--no-capture"])
    assert calls[0]["role"] == "standard"


# -- best-effort capture: a malformed optional capture never sinks the answer --------

_ANSWER_WITH_BAD_CAPTURE = json.dumps(
    {
        "answer": "Markdown is the surface. [[markdown-is-the-new-html]]",
        "cited": ["markdown-is-the-new-html"],
        "capture": {
            "type": "thought",
            "title": "Agents prefer markdown",
            "description": "x" * 250,  # over ProposedIntake's max_length=200
            "tags": [],
            "body": "Builds on [[markdown-is-the-new-html]].",
        },
    }
)


def _raise_structured_error(raw):
    def boom(**kwargs):
        from sabnyx import llm

        raise llm.StructuredOutputError(
            "AskAnswer validation failed", raw_content=raw, output_format=AskAnswer
        )

    return boom


def test_answer_question_salvages_when_only_capture_invalid(tmp_vault, monkeypatch):
    from sabnyx import services

    monkeypatch.setattr(
        services.llm, "parse_structured", _raise_structured_error(_ANSWER_WITH_BAD_CAPTURE)
    )
    result = services.answer_question(question="what surface should agents get?")
    assert result.capture_dropped is True
    assert result.answer.capture is None
    assert "Markdown is the surface" in result.answer.answer
    assert "markdown-is-the-new-html" in result.answer.cited


def test_answer_question_reraises_when_answer_unsalvageable(tmp_vault, monkeypatch):
    from sabnyx import services

    raw = json.dumps({"cited": [], "capture": None})  # missing the required `answer`
    monkeypatch.setattr(services.llm, "parse_structured", _raise_structured_error(raw))
    with pytest.raises(services.llm.StructuredOutputError):
        services.answer_question(question="q?")


def test_ask_cli_shows_notice_when_capture_dropped(tmp_vault, monkeypatch):
    from sabnyx.commands import ask as ask_mod

    monkeypatch.setattr(
        ask_mod.llm, "parse_structured", _raise_structured_error(_ANSWER_WITH_BAD_CAPTURE)
    )
    result = CliRunner().invoke(ask_mod.ask_cmd, ["what surface should agents get?"])
    assert result.exit_code == 0, result.output
    assert "Markdown is the surface" in result.output
    assert "capture proposal was dropped" in result.output
