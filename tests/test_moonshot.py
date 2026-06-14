"""Moonshot / Kimi provider wiring — offline request-shape tests.

The live path is exercised by `sabnyx verify`; these pin the request
construction so the openai-compatible Moonshot path can't silently malform.
"""

from __future__ import annotations

import pytest

from sabnyx import llm
from sabnyx.schemas import HealthProbe, IngestChangeset


def test_moonshot_is_a_known_provider(monkeypatch):
    monkeypatch.setenv("SABNYX_LLM_PROVIDER", "moonshot")
    assert llm.provider() == "moonshot"


def test_moonshot_role_defaults(monkeypatch):
    for var in ("SABNYX_MODEL_FAST", "SABNYX_MODEL_STANDARD", "SABNYX_MODEL_DEEP"):
        monkeypatch.delenv(var, raising=False)
    assert llm.model_for("fast", "moonshot") == "kimi-k2.5"
    assert llm.model_for("standard", "moonshot") == "kimi-k2.5"
    assert llm.model_for("deep", "moonshot") == "kimi-k2.6"


def test_moonshot_model_override(monkeypatch):
    monkeypatch.setenv("SABNYX_MODEL_DEEP", "kimi-k2.7-code")
    assert llm.model_for("deep", "moonshot") == "kimi-k2.7-code"


def test_moonshot_base_url_default(monkeypatch):
    monkeypatch.delenv("SABNYX_LLM_BASE_URL", raising=False)
    assert llm.openai_base_url("moonshot") == llm.MOONSHOT_BASE_URL


def test_moonshot_api_key_honors_native_var(monkeypatch):
    monkeypatch.delenv("SABNYX_LLM_API_KEY", raising=False)
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-kimi")
    assert llm.openai_api_key("moonshot") == "sk-kimi"


def test_moonshot_api_key_missing_is_friendly(monkeypatch):
    monkeypatch.delenv("SABNYX_LLM_API_KEY", raising=False)
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    with pytest.raises(ValueError, match="MOONSHOT_API_KEY"):
        llm.openai_api_key("moonshot")


def test_moonshot_body_uses_json_object_mode():
    b = llm.openai_body(
        model="kimi-k2.5",
        task_system="t",
        user="u",
        output_format=IngestChangeset,
        max_tokens=4000,
        prov="moonshot",
    )
    # Moonshot doesn't support strict json_schema; it gets json_object mode.
    assert b["response_format"] == {"type": "json_object"}
    # ...but the schema still rides in the prompt, and the word "json" is
    # present (json_object mode requires it).
    assert "schema" in b["messages"][1]["content"].lower()
    assert "json" in b["messages"][1]["content"].lower()


def test_non_moonshot_body_keeps_json_schema():
    b = llm.openai_body(
        model="anthropic/claude-haiku-4.5",
        task_system="t",
        user="u",
        output_format=HealthProbe,
        max_tokens=200,
    )
    assert b["response_format"]["type"] == "json_schema"


# -- the empty-content / reasoning-budget path ----------------------------------------


class _FakeResp:
    """Minimal stand-in for an httpx.Response at the chat-completions boundary."""

    def __init__(self, payload: dict, status_code: int = 200, text: str = ""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def raise_for_status(self) -> None:  # 2xx in these tests
        pass

    def json(self) -> dict:
        return self._payload


def _completion(content: str, finish_reason: str = "stop") -> dict:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _call(monkeypatch, payload: dict, *, max_tokens: int = 2048):
    import httpx

    monkeypatch.delenv("SABNYX_LLM_API_KEY", raising=False)
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-kimi")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResp(payload))
    return llm._call_openai_compatible(
        prov="moonshot",
        model="kimi-k2.5",
        task_system="t",
        user="u",
        output_format=HealthProbe,
        max_tokens=max_tokens,
        purpose="verify",
    )


def test_openai_compatible_parses_valid_content(monkeypatch):
    parsed, usage = _call(monkeypatch, _completion('{"ok": true, "note": "hi"}'))
    assert parsed.ok is True
    assert parsed.note == "hi"
    assert usage["stop_reason"] == "stop"
    assert usage["output_tokens"] == 5


def test_openai_compatible_empty_on_length_names_max_tokens(monkeypatch):
    # The original verify failure: reasoning ate the budget, content == "".
    with pytest.raises(RuntimeError, match=r"empty output.*max_tokens=2048"):
        _call(monkeypatch, _completion("", finish_reason="length"))


def test_openai_compatible_empty_on_stop_is_clear(monkeypatch):
    with pytest.raises(RuntimeError, match=r"empty output.*finish_reason='stop'"):
        _call(monkeypatch, _completion("", finish_reason="stop"))
