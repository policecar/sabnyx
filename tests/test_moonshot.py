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
