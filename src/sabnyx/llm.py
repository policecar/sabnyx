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
    "moonshot": {
        # Moonshot / Kimi (OpenAI-compatible). All three roles use the sharp
        # K2 family — the older moonshot-v1 models follow structured-output
        # instructions poorly. fast/standard share the cheaper K2.5; deep gets
        # the newest K2.6. Override any with SABNYX_MODEL_FAST/_STANDARD/_DEEP.
        "fast": "kimi-k2.5",
        "standard": "kimi-k2.5",
        "deep": "kimi-k2.6",
    },
}

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
MOONSHOT_BASE_URL = "https://api.moonshot.ai/v1"


def provider() -> str:
    """Active provider. anthropic | openrouter | openai-compatible | moonshot."""
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


def anthropic_params(
    *,
    model: str,
    role: ModelRole,
    task_system: str,
    user: str,
    output_format: type[T],
    max_tokens: int,
) -> dict:
    """The exact kwargs sent to messages.parse. Pure, so the request shape
    is verifiable offline (test_verify)."""
    params: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "system": [
            # stable prefix first: the vault contract, cached across calls
            {"type": "text", "text": contract_text(), "cache_control": {"type": "ephemeral"}},
            # task-specific instructions after the cache breakpoint
            {"type": "text", "text": task_system},
        ],
        "messages": [{"role": "user", "content": user}],
        "output_format": output_format,
    }
    if role != "fast":
        # adaptive thinking is supported on the sonnet/opus tiers
        params["thinking"] = {"type": "adaptive"}
    return params


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

    client = anthropic.Anthropic()
    response = client.messages.parse(
        **anthropic_params(
            model=model,
            role=role,
            task_system=task_system,
            user=user,
            output_format=output_format,
            max_tokens=max_tokens,
        )
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


_PROVIDER_DEFAULT_BASE_URL = {
    "openrouter": OPENROUTER_BASE_URL,
    "moonshot": MOONSHOT_BASE_URL,
}


def openai_base_url(prov: str) -> str:
    base_url = os.environ.get(
        "SABNYX_LLM_BASE_URL",
        _PROVIDER_DEFAULT_BASE_URL.get(prov, ""),
    ).rstrip("/")
    if not base_url:
        raise ValueError("SABNYX_LLM_BASE_URL is required for provider=openai-compatible")
    return base_url


def openai_api_key(prov: str) -> str:
    # provider-native key var honored as a fallback after the generic one
    native = {"openrouter": "OPENROUTER_API_KEY", "moonshot": "MOONSHOT_API_KEY"}.get(prov)
    api_key = os.environ.get("SABNYX_LLM_API_KEY") or (os.environ.get(native, "") if native else "")
    if not api_key:
        hint = f"SABNYX_LLM_API_KEY (or {native})" if native else "SABNYX_LLM_API_KEY"
        raise ValueError(f"no API key for provider {prov!r}; set {hint}")
    return api_key


def openai_body(
    *,
    model: str,
    task_system: str,
    user: str,
    output_format: type[T],
    max_tokens: int,
    prov: str = "openai-compatible",
) -> dict:
    """The OpenAI-compatible chat-completions request body. Pure; the JSON
    schema is embedded in the prompt so it survives endpoints that ignore
    response_format.

    Moonshot/Kimi supports the universal ``{"type": "json_object"}`` mode but
    not OpenAI's strict ``json_schema`` type, so it gets the former. Either
    way the schema rides in the prompt and we validate with pydantic on our
    side — the response_format is a hint, never the contract.
    """
    schema = output_format.model_json_schema()
    if prov == "moonshot":
        # json_object mode requires the word "json" to appear in the messages;
        # the user content below already does.
        response_format: dict = {"type": "json_object"}
    else:
        response_format = {
            "type": "json_schema",
            "json_schema": {"name": output_format.__name__, "schema": schema, "strict": True},
        }
    return {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": contract_text() + "\n\n" + task_system},
            {
                "role": "user",
                "content": user
                + "\n\nReturn ONLY a single JSON object that is a valid *instance* of the "
                "schema below — that is, the actual data, NOT the schema definition "
                'itself (do not echo keys like "$defs", "properties", or "title"). '
                "No prose, no markdown code fences.\nJSON schema (for reference only):\n"
                + json.dumps(schema),
            },
        ],
        "response_format": response_format,
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

    base_url = openai_base_url(prov)
    api_key = openai_api_key(prov)
    body = openai_body(
        model=model,
        task_system=task_system,
        user=user,
        output_format=output_format,
        max_tokens=max_tokens,
        prov=prov,
    )
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

    choice = data["choices"][0]
    content = choice["message"]["content"] or ""
    # Empty content is the reasoning-model trap: kimi-k2.x bill thinking against
    # max_tokens, so too small a budget exhausts the limit during the reasoning
    # phase (finish_reason="length") and content comes back "". Fail loud here —
    # otherwise it surfaces downstream as a cryptic "Invalid JSON: EOF".
    if not content.strip():
        finish = choice.get("finish_reason")
        if finish == "length":
            raise RuntimeError(
                f"model returned empty output ({purpose}): hit max_tokens={max_tokens} "
                "before emitting content — reasoning models bill thinking against "
                "max_tokens, so raise the budget."
            )
        raise RuntimeError(
            f"model returned empty output ({purpose}): finish_reason={finish!r}, "
            "no content in the response."
        )
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
        "stop_reason": choice.get("finish_reason"),
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
