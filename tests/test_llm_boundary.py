"""LLM boundary failure paths: llm.failed compensation and Anthropic salvage.

The Anthropic client is faked at the module boundary — no network, no key.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from sabnyx import ledger, llm
from sabnyx.schemas import HealthProbe


class _FakeAnthropic:
    """Stands in for anthropic.Anthropic: returns one canned Message."""

    def __init__(self, raw_text: str, stop_reason: str = "end_turn"):
        response = SimpleNamespace(
            content=[SimpleNamespace(type="text", text=raw_text)],
            stop_reason=stop_reason,
            usage=SimpleNamespace(input_tokens=7, output_tokens=3),
        )
        self.messages = SimpleNamespace(create=lambda **params: response)

    @classmethod
    def install(cls, monkeypatch, raw_text: str, stop_reason: str = "end_turn"):
        monkeypatch.setattr("anthropic.Anthropic", lambda: cls(raw_text, stop_reason))


def _call_anthropic(output_format=HealthProbe):
    return llm._call_anthropic(
        model="claude-haiku-4-5",
        role="fast",
        task_system="t",
        user="u",
        output_format=output_format,
        max_tokens=200,
        purpose="test",
    )


# -- m5: a failed call leaves a compensating llm.failed action --------------------


def test_failed_call_appends_llm_failed_event(tmp_vault, monkeypatch):
    monkeypatch.setenv("SABNYX_LLM_PROVIDER", "moonshot")

    def boom(**kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(llm, "_call_openai_compatible", boom)
    with pytest.raises(RuntimeError, match="provider exploded"):
        llm.parse_structured(
            task_system="t",
            user="u",
            output_format=HealthProbe,
            role="fast",
            actor="pytest",
            purpose="test:failure",
        )
    events = list(ledger.replay())
    assert [e.kind for e in events] == ["llm.requested", "llm.failed"]
    failed = events[-1]
    assert failed.plane == "action"
    assert failed.subject == "test:failure"
    assert failed.payload["provider"] == "moonshot"
    assert failed.payload["role"] == "fast"
    assert failed.payload["error"] == "RuntimeError"
    assert failed.payload["message"].startswith("provider exploded")


def test_successful_call_appends_no_llm_failed(tmp_vault, monkeypatch):
    monkeypatch.setenv("SABNYX_LLM_PROVIDER", "moonshot")
    monkeypatch.setattr(
        llm, "_call_openai_compatible", lambda **k: (HealthProbe(ok=True, note="n"), {})
    )
    llm.parse_structured(
        task_system="t",
        user="u",
        output_format=HealthProbe,
        role="fast",
        actor="pytest",
        purpose="test:ok",
    )
    kinds = [e.kind for e in ledger.replay()]
    assert kinds == ["llm.requested", "llm.completed"]


# -- m6: Anthropic failures carry the raw text so salvage can fire ----------------


def test_anthropic_validation_failure_carries_raw_content(tmp_vault, monkeypatch):
    bad = json.dumps({"ok": True, "note": "x" * 250})  # note over max_length=200
    _FakeAnthropic.install(monkeypatch, bad)
    with pytest.raises(llm.StructuredOutputError) as ei:
        _call_anthropic()
    assert ei.value.raw_content == bad
    assert ei.value.output_format is HealthProbe


def test_anthropic_refusal_carries_raw_content(tmp_vault, monkeypatch):
    _FakeAnthropic.install(monkeypatch, "I can't help with that.", stop_reason="refusal")
    with pytest.raises(llm.StructuredOutputError, match="declined") as ei:
        _call_anthropic()
    assert ei.value.raw_content == "I can't help with that."


def test_anthropic_valid_output_parses(tmp_vault, monkeypatch):
    _FakeAnthropic.install(monkeypatch, '{"ok": true, "note": "fine"}')
    parsed, usage = _call_anthropic()
    assert parsed.ok is True
    assert usage["input_tokens"] == 7
    assert usage["stop_reason"] == "end_turn"


def test_ask_salvage_fires_on_anthropic(tmp_vault, monkeypatch):
    """End to end on the default provider: a too-long capture description no
    longer sinks the answer — salvage drops the capture and keeps the rest."""
    from sabnyx import services

    monkeypatch.delenv("SABNYX_LLM_PROVIDER", raising=False)  # anthropic default
    raw = json.dumps(
        {
            "answer": "Markdown is the surface. [[markdown-is-the-new-html]]",
            "cited": ["markdown-is-the-new-html"],
            "capture": {
                "type": "thought",
                "title": "Agents prefer markdown",
                "description": "x" * 250,  # over ProposedIntake's max_length=200
                "tags": [],
                "body": "b",
            },
        }
    )
    _FakeAnthropic.install(monkeypatch, raw)
    result = services.answer_question(question="what surface should agents get?")
    assert result.capture_dropped is True
    assert result.answer.capture is None
    assert "Markdown is the surface" in result.answer.answer
