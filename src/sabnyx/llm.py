"""The LLM boundary — the single place sabnyx talks to a model.

Rules:
  - Every call emits two ledger events: an `intent` (llm.requested)
    before, an `action` (llm.completed) after. The agent is auditable
    or it doesn't run.
  - Models are addressed by role (fast/standard/deep), never by id,
    so swapping models is a config change, not a code change.
  - Providers are pluggable (ADR 0006). Default is the Anthropic API;
    OpenRouter (or any OpenAI-compatible endpoint) is a env-var switch
    away, which makes any model on the market addressable by role.
  - The vault contract (SPEC.md + ROUTING.md) is sent as a stable
    system prefix on every call. It is the "schema" layer in Karpathy's
    sense: it turns a generic model into a disciplined maintainer.

Configuration (env vars):
    SABNYX_LLM_PROVIDER   anthropic (default) | openrouter | openai-compatible
    SABNYX_LLM_BASE_URL   endpoint for openai-compatible (openrouter has a default)
    SABNYX_LLM_API_KEY    key for openai-compatible endpoints
                          (OPENROUTER_API_KEY also honored for openrouter)
    SABNYX_MODEL_FAST / _STANDARD / _DEEP   per-role model id override
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from . import ledger
from .paths import ROOT
from .schemas import ExtractionDepth, ModelRole

# -- provider + model roles -------------------------------------------------------

# Per-provider role defaults (jibot-style role indirection, ADR 0003 / 0006).
# Any role can be pointed at any model the active provider serves via
# SABNYX_MODEL_<ROLE>; that is the whole point of the indirection.
ROLE_MODELS: dict[str, dict[str, str]] = {
    "anthropic": {
        "fast": "claude-haiku-4-5",
        "standard": "claude-sonnet-4-6",
        "deep": "claude-opus-4-8",
    },
    "openrouter": {
        "fast": "anthropic/claude-haiku-4.5",
        "standard": "anthropic/claude-sonnet-4.6",
        "deep": "anthropic/claude-opus-4.8",
    },
    "openai-compatible": {
        # no sane universal defaults; set SABNYX_MODEL_* explicitly
        "fast": "",
        "standard": "",
        "deep": "",
    },
}

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def provider() -> str:
    """Active provider. anthropic | openrouter | openai-compatible."""
    p = os.environ.get("SABNYX_LLM_PROVIDER", "anthropic").lower()
    if p not in ROLE_MODELS:
        raise ValueError(f"unknown SABNYX_LLM_PROVIDER={p!r}; one of {sorted(ROLE_MODELS)}")
    return p


def model_for(role: ModelRole, prov: str | None = None) -> str:
    """Resolve a role to a concrete model id (env overridable)."""
    prov = prov or provider()
    model = os.environ.get(f"SABNYX_MODEL_{role.upper()}") or ROLE_MODELS[prov][role]
    if not model:
        raise ValueError(
            f"no model configured for role {role!r} on provider {prov!r}; "
            f"set SABNYX_MODEL_{role.upper()}"
        )
    return model


def role_for_depth(depth: ExtractionDepth) -> ModelRole:
    """Map extraction depth (jibot's word-count tiers) to a model role."""
    return {
        "shallow": "fast",
        "standard": "standard",
        "standard-plus": "standard",
        "deep": "deep",
    }[depth]  # type: ignore[return-value]


# -- rule-based message router ----------------------------------------------------
# Compact version of jibot's iblai-router: a weighted scorer over the user
# message only (never the system prompt), <1ms, no ML. `[fast]` / `[deep]`
# in the message bypass scoring entirely.

_REASONING_MARKERS = re.compile(
    r"\b(why|how does|architect|design|trade-?off|prove|deriv|compare|contrast|"
    r"synthesi[sz]|reconcile|contradict|refactor|debug|root.cause)\b",
    re.IGNORECASE,
)
_CODE_MARKERS = re.compile(r"```|\bdef |\bclass |\bimport |=>|::")


def role_for_message(text: str) -> ModelRole:
    """Score a user message and pick a model role. Rule-based, no inference."""
    lowered = text.lower()
    if "[fast]" in lowered:
        return "fast"
    if "[deep]" in lowered:
        return "deep"

    score = 0.0
    words = len(text.split())
    if words > 60:
        score += 1.0
    if words > 250:
        score += 1.0
    score += min(len(_REASONING_MARKERS.findall(text)), 3) * 0.8
    if _CODE_MARKERS.search(text):
        score += 1.0
    if text.count("?") >= 2:
        score += 0.5

    if score < 1.0:
        return "fast"
    if score < 2.5:
        return "standard"
    return "deep"


# -- the contract block (stable prefix) ---------------------------------------------


@lru_cache(maxsize=1)
def contract_text() -> str:
    """SPEC.md + ROUTING.md, the stable system prefix sent on every call."""
    parts = []
    for name in ("SPEC.md", "ROUTING.md"):
        p = ROOT / name
        if p.is_file():
            parts.append(f"<{name}>\n{p.read_text(encoding='utf-8')}\n</{name}>")
    return "\n\n".join(parts)


# -- the one public call site --------------------------------------------------------

T = TypeVar("T", bound=BaseModel)


def parse_structured(
    *,
    task_system: str,
    user: str,
    output_format: type[T],
    role: ModelRole,
    actor: str,
    purpose: str,
    max_tokens: int = 16000,
) -> T:
    """One structured LLM call: contract + task system, user content,
    pydantic-validated output. Emits intent + action ledger events.
    Provider-agnostic: dispatches on SABNYX_LLM_PROVIDER.
    """
    prov = provider()
    model = model_for(role, prov)
    prompt_hash = ledger.content_hash({"task_system": task_system, "user": user})

    ledger.append_event(
        plane="intent",
        kind="llm.requested",
        actor=actor,
        subject=purpose,
        payload={
            "provider": prov,
            "model": model,
            "role": role,
            "schema": output_format.__name__,
            "prompt_hash": prompt_hash,
        },
    )

    if prov == "anthropic":
        parsed, usage = _call_anthropic(
            model=model,
            role=role,
            task_system=task_system,
            user=user,
            output_format=output_format,
            max_tokens=max_tokens,
            purpose=purpose,
        )
    else:
        parsed, usage = _call_openai_compatible(
            prov=prov,
            model=model,
            task_system=task_system,
            user=user,
            output_format=output_format,
            max_tokens=max_tokens,
            purpose=purpose,
        )

    ledger.append_event(
        plane="action",
        kind="llm.completed",
        actor=actor,
        subject=purpose,
        payload={
            "provider": prov,
            "model": model,
            "role": role,
            "prompt_hash": prompt_hash,
            **usage,
        },
    )
    return parsed


# -- provider backends ----------------------------------------------------------------


def _call_anthropic(
    *,
    model: str,
    role: ModelRole,
    task_system: str,
    user: str,
    output_format: type[T],
    max_tokens: int,
    purpose: str,
) -> tuple[T, dict]:
    """Native Anthropic API: structured outputs via messages.parse,
    prompt caching on the stable contract block, adaptive thinking."""
    import anthropic

    kwargs: dict = {}
    if role != "fast":
        # adaptive thinking is supported on the sonnet/opus tiers
        kwargs["thinking"] = {"type": "adaptive"}

    client = anthropic.Anthropic()
    response = client.messages.parse(
        model=model,
        max_tokens=max_tokens,
        system=[
            # stable prefix first: the vault contract, cached across calls
            {
                "type": "text",
                "text": contract_text(),
                "cache_control": {"type": "ephemeral"},
            },
            # task-specific instructions after the cache breakpoint
            {"type": "text", "text": task_system},
        ],
        messages=[{"role": "user", "content": user}],
        output_format=output_format,
        **kwargs,
    )

    if response.stop_reason == "refusal":
        raise RuntimeError(f"model declined the request ({purpose})")
    parsed = response.parsed_output
    if parsed is None:
        raise RuntimeError(f"model returned no parseable {output_format.__name__} ({purpose})")

    u = response.usage
    return parsed, {
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", None),
        "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", None),
        "stop_reason": response.stop_reason,
    }


def _call_openai_compatible(
    *,
    prov: str,
    model: str,
    task_system: str,
    user: str,
    output_format: type[T],
    max_tokens: int,
    purpose: str,
) -> tuple[T, dict]:
    """OpenRouter / any OpenAI-compatible chat-completions endpoint, via
    httpx (already a transitive dep). Structured output requested through
    response_format json_schema where supported, pydantic-validated here
    regardless — the schema is enforced on our side either way."""
    import httpx

    base_url = os.environ.get(
        "SABNYX_LLM_BASE_URL",
        OPENROUTER_BASE_URL if prov == "openrouter" else "",
    ).rstrip("/")
    if not base_url:
        raise ValueError("SABNYX_LLM_BASE_URL is required for provider=openai-compatible")

    api_key = os.environ.get("SABNYX_LLM_API_KEY") or (
        os.environ.get("OPENROUTER_API_KEY", "") if prov == "openrouter" else ""
    )
    if not api_key:
        raise ValueError(
            f"no API key for provider {prov!r}; set SABNYX_LLM_API_KEY (or OPENROUTER_API_KEY)"
        )

    schema = output_format.model_json_schema()
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": contract_text() + "\n\n" + task_system},
            {
                "role": "user",
                "content": user
                + "\n\nRespond with a single JSON object matching this schema, nothing else:\n"
                + json.dumps(schema),
            },
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": output_format.__name__, "schema": schema, "strict": True},
        },
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    if prov == "openrouter":
        headers["HTTP-Referer"] = "https://github.com/policecar/sabnyx"
        headers["X-Title"] = "sabnyx"

    resp = httpx.post(f"{base_url}/chat/completions", json=body, headers=headers, timeout=300)
    if resp.status_code == 400 and "response_format" in resp.text:
        # model doesn't support json_schema response_format; the schema is
        # already in the prompt, so retry without the parameter
        body.pop("response_format")
        resp = httpx.post(f"{base_url}/chat/completions", json=body, headers=headers, timeout=300)
    resp.raise_for_status()
    data = resp.json()

    content = data["choices"][0]["message"]["content"] or ""
    try:
        parsed = output_format.model_validate_json(_extract_json(content))
    except ValidationError as e:
        raise RuntimeError(
            f"model output failed {output_format.__name__} validation ({purpose}): {e}"
        ) from e

    u = data.get("usage", {})
    return parsed, {
        "input_tokens": u.get("prompt_tokens"),
        "output_tokens": u.get("completion_tokens"),
        "stop_reason": data["choices"][0].get("finish_reason"),
    }


def _extract_json(text: str) -> str:
    """Pull the first JSON object out of a response that may wrap it in
    prose or a code fence. Deterministic, no second LLM call."""
    text = text.strip()
    if text.startswith("{"):
        return text
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        return fence.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text
