"""Offline verification: request-shape construction + the verify command.

The live probes (a bogus-key call returning 401/403) confirmed these
shapes reach both providers; these tests pin the construction so a future
edit can't silently malform the request.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from sabnyx import llm
from sabnyx.schemas import HealthProbe, IngestChangeset


def test_anthropic_params_shape():
    p = llm.anthropic_params(
        model="claude-sonnet-4-6",
        role="standard",
        task_system="do the thing",
        user="hello",
        output_format=IngestChangeset,
        max_tokens=16000,
    )
    assert p["model"] == "claude-sonnet-4-6"
    assert p["max_tokens"] == 16000
    assert p["output_format"] is IngestChangeset
    # two system blocks; the stable contract first, carrying the cache breakpoint
    assert len(p["system"]) == 2
    assert p["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert p["system"][1]["text"] == "do the thing"
    assert p["messages"] == [{"role": "user", "content": "hello"}]
    # standard/deep get adaptive thinking
    assert p["thinking"] == {"type": "adaptive"}


def test_anthropic_params_fast_has_no_thinking():
    p = llm.anthropic_params(
        model="claude-haiku-4-5",
        role="fast",
        task_system="t",
        user="u",
        output_format=HealthProbe,
        max_tokens=200,
    )
    assert "thinking" not in p


def test_openai_body_embeds_schema():
    b = llm.openai_body(
        model="anthropic/claude-haiku-4.5",
        task_system="t",
        user="u",
        output_format=IngestChangeset,
        max_tokens=16000,
    )
    assert b["model"] == "anthropic/claude-haiku-4.5"
    assert b["response_format"]["type"] == "json_schema"
    assert b["response_format"]["json_schema"]["name"] == "IngestChangeset"
    # the schema rides in the prompt too, so endpoints that ignore the
    # response_format param still get it
    assert "IngestChangeset" in b["response_format"]["json_schema"]["schema"]["title"]
    assert "schema" in b["messages"][1]["content"].lower()


def test_openai_base_url_requires_config(monkeypatch):
    monkeypatch.delenv("SABNYX_LLM_BASE_URL", raising=False)
    assert llm.openai_base_url("openrouter") == llm.OPENROUTER_BASE_URL
    with pytest.raises(ValueError, match="SABNYX_LLM_BASE_URL is required"):
        llm.openai_base_url("openai-compatible")


def test_openai_api_key_required(monkeypatch):
    monkeypatch.delenv("SABNYX_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ValueError, match="no API key"):
        llm.openai_api_key("openrouter")


# -- the verify command -----------------------------------------------------------------


def test_verify_reports_success(monkeypatch):
    from sabnyx.commands import verify as verify_mod

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")  # pass the credential pre-check
    monkeypatch.setattr(
        verify_mod.llm,
        "parse_structured",
        lambda **kw: HealthProbe(ok=True, note="contract received"),
    )
    result = CliRunner().invoke(verify_mod.verify_cmd, ["--role", "fast"])
    assert result.exit_code == 0, result.output
    assert "live path verified" in result.output
    assert "contract received" in result.output


def test_verify_reports_failure(monkeypatch):
    from sabnyx.commands import verify as verify_mod

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    def boom(**kw):
        raise RuntimeError("nope")

    monkeypatch.setattr(verify_mod.llm, "parse_structured", boom)
    result = CliRunner().invoke(verify_mod.verify_cmd, ["--role", "fast"])
    assert result.exit_code == 1
    assert "FAILED" in result.output


def test_verify_no_credential_is_friendly(monkeypatch):
    from sabnyx.commands import verify as verify_mod

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("SABNYX_LLM_PROVIDER", raising=False)
    result = CliRunner().invoke(verify_mod.verify_cmd, [])
    assert result.exit_code == 1
    assert "set ANTHROPIC_API_KEY" in result.output


def test_verify_all_roles(monkeypatch):
    from sabnyx.commands import verify as verify_mod

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    seen: list[str] = []

    def fake(**kw):
        seen.append(kw["role"])
        return HealthProbe(ok=True, note="ok")

    monkeypatch.setattr(verify_mod.llm, "parse_structured", fake)
    result = CliRunner().invoke(verify_mod.verify_cmd, ["--all-roles"])
    assert result.exit_code == 0, result.output
    assert seen == ["fast", "standard", "deep"]
