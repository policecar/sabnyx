"""Per-run LLM call budget and retry/backoff helpers (llm.py cost guards)."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from sabnyx import llm


class _Dummy(BaseModel):
    ok: int = 1


def _call() -> _Dummy:
    return llm.parse_structured(
        task_system="t",
        user="u",
        output_format=_Dummy,
        role="fast",
        actor="a",
        purpose="p",
    )


@pytest.fixture
def stub_provider(monkeypatch):
    """Point at moonshot and stub the network call so parse_structured runs
    end-to-end (budget charge + ledger) without touching a provider."""
    monkeypatch.setenv("SABNYX_LLM_PROVIDER", "moonshot")
    monkeypatch.setattr(llm, "_call_openai_compatible", lambda **k: (_Dummy(), {}))


def test_call_budget_caps_calls(tmp_vault, stub_provider) -> None:
    with llm.call_budget(max_calls=2):
        _call()
        _call()
        with pytest.raises(llm.LLMBudgetError):
            _call()


def test_no_cap_outside_budget_context(tmp_vault, stub_provider) -> None:
    # Without an active budget there is no ceiling (serve daemon, one-shot CLI).
    for _ in range(5):
        _call()


def test_budget_zero_means_unlimited(tmp_vault, stub_provider) -> None:
    with llm.call_budget(max_calls=0):
        for _ in range(5):
            _call()


def test_budget_does_not_leak_after_context(tmp_vault, stub_provider) -> None:
    with llm.call_budget(max_calls=1):
        _call()
    # Budget reset on exit: calls outside the context are unconstrained again.
    _call()
    _call()


def test_default_max_calls_env(monkeypatch) -> None:
    monkeypatch.delenv("SABNYX_MAX_LLM_CALLS", raising=False)
    assert llm._default_max_calls() == llm._DEFAULT_MAX_CALLS
    monkeypatch.setenv("SABNYX_MAX_LLM_CALLS", "7")
    assert llm._default_max_calls() == 7
    monkeypatch.setenv("SABNYX_MAX_LLM_CALLS", "garbage")
    assert llm._default_max_calls() == llm._DEFAULT_MAX_CALLS


def test_max_retries_env(monkeypatch) -> None:
    monkeypatch.delenv("SABNYX_LLM_MAX_RETRIES", raising=False)
    assert llm._max_retries() == llm._DEFAULT_RETRIES
    monkeypatch.setenv("SABNYX_LLM_MAX_RETRIES", "0")
    assert llm._max_retries() == 0
    monkeypatch.setenv("SABNYX_LLM_MAX_RETRIES", "-3")
    assert llm._max_retries() == 0  # clamped


def test_backoff_schedule_caps_at_30s() -> None:
    assert llm._backoff_seconds(0) == 1.0
    assert llm._backoff_seconds(1) == 2.0
    assert llm._backoff_seconds(2) == 4.0
    assert llm._backoff_seconds(20) == 30.0  # capped
