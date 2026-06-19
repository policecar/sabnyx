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

import contextlib
import contextvars
import json
import os
import re
import time
from dataclasses import dataclass
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


# -- cost + reliability guards -------------------------------------------------------
# An always-on agent that holds an API key needs two things the boundary lacked:
# a ceiling on how many calls one run may make (so a scheduled `tend --yes` loop
# with a misbehaving prompt or a huge queue can't spin the provider unbounded),
# and bounded retry on transient provider failures (so a single 429/5xx doesn't
# abort the whole heartbeat mid-run).


class LLMBudgetError(RuntimeError):
    """Raised when a call would exceed the active per-run LLM call budget."""


_DEFAULT_MAX_CALLS = 50


def _default_max_calls() -> int:
    """Per-run call ceiling from SABNYX_MAX_LLM_CALLS (<=0 = unlimited)."""
    raw = os.environ.get("SABNYX_MAX_LLM_CALLS", "").strip()
    if not raw:
        return _DEFAULT_MAX_CALLS
    try:
        return int(raw)
    except ValueError:
        return _DEFAULT_MAX_CALLS


@dataclass
class _Budget:
    max_calls: int
    calls: int = 0


_budget: contextvars.ContextVar[_Budget | None] = contextvars.ContextVar(
    "sabnyx_llm_budget", default=None
)


@contextlib.contextmanager
def call_budget(max_calls: int | None = None):
    """Bound the number of LLM calls made within this context (fail-closed).

    `max_calls <= 0` means unlimited; `None` uses SABNYX_MAX_LLM_CALLS (default
    ``_DEFAULT_MAX_CALLS``). Wrap unattended, looping work (e.g. `tend`) so a
    runaway queue or prompt can't call the provider without bound. Outside any
    budget context there is no cap — one-shot CLI calls and the long-lived
    `serve` daemon are unaffected.
    """
    limit = _default_max_calls() if max_calls is None else max_calls
    token = _budget.set(_Budget(max_calls=limit))
    try:
        yield
    finally:
        _budget.reset(token)


def _charge_budget(purpose: str) -> None:
    """Count one call against the active budget; raise if it would exceed it."""
    b = _budget.get()
    if b is None or b.max_calls <= 0:
        return
    b.calls += 1
    if b.calls > b.max_calls:
        raise LLMBudgetError(
            f"LLM call budget exceeded ({b.calls} > {b.max_calls} calls this run) "
            f"at {purpose!r}; raise/unset SABNYX_MAX_LLM_CALLS or widen call_budget()."
        )


# Provider statuses worth retrying: rate limit, transient 5xx, Anthropic 529.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 529})
_DEFAULT_RETRIES = 3


def _max_retries() -> int:
    """Transient-failure retry count from SABNYX_LLM_MAX_RETRIES (default 3)."""
    raw = os.environ.get("SABNYX_LLM_MAX_RETRIES", "").strip()
    if not raw:
        return _DEFAULT_RETRIES
    try:
        return max(0, int(raw))
    except ValueError:
        return _DEFAULT_RETRIES


def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff for a 0-based attempt index, capped at 30s."""
    return min(2.0**attempt, 30.0)


# -- the one public call site --------------------------------------------------------

T = TypeVar("T", bound=BaseModel)


class StructuredOutputError(RuntimeError):
    """A model's output could not be validated against the requested schema,
    even after repair attempts. Carries the raw text so a caller can attempt a
    best-effort salvage (e.g. drop an optional field) rather than lose the whole
    response. Subclasses RuntimeError, so existing `except RuntimeError` callers
    keep working."""

    def __init__(
        self,
        message: str,
        *,
        raw_content: str | None = None,
        output_format: type | None = None,
    ) -> None:
        super().__init__(message)
        self.raw_content = raw_content
        self.output_format = output_format


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
    # Charge the run budget before doing (or ledgering) anything: a refused call
    # is not an llm.requested.
    _charge_budget(purpose)
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
    params = anthropic_params(
        model=model,
        role=role,
        task_system=task_system,
        user=user,
        output_format=output_format,
        max_tokens=max_tokens,
    )
    retries = _max_retries()
    for attempt in range(retries + 1):
        try:
            response = client.messages.parse(**params)
            break
        except anthropic.APIStatusError as e:
            # RateLimitError (429) and InternalServerError (5xx) subclass this.
            if attempt >= retries or e.status_code not in _RETRYABLE_STATUS:
                raise
            time.sleep(_backoff_seconds(attempt))
        except anthropic.APIConnectionError:
            # Network blip / timeout (APITimeoutError subclasses this).
            if attempt >= retries:
                raise
            time.sleep(_backoff_seconds(attempt))

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


# One corrective re-ask after the first schema-validation failure. Models
# (especially smaller ones) softly violate constraints embedded in the schema —
# a single repair turn with the error fed back recovers the vast majority, and
# preserves the schema contract instead of relaxing it.
_MAX_REPAIR_ATTEMPTS = 1


def _repair_instruction(error: ValidationError) -> str:
    """The corrective follow-up turn for a re-ask after validation failed."""
    return (
        "Your previous response failed schema validation:\n"
        f"{error}\n\n"
        "Return ONLY a corrected single JSON object that satisfies the schema. "
        "Respect every field constraint — especially string length limits "
        "(e.g. a `description` must not exceed its documented max_length). "
        "No prose, no markdown fences."
    )


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
    regardless — the schema is enforced on our side either way.

    On a validation failure the model is re-asked once with the error fed back
    (``_MAX_REPAIR_ATTEMPTS``); only if the repair also fails do we raise a
    ``StructuredOutputError`` carrying the raw text for best-effort salvage."""
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

    def _post(b: dict) -> dict:
        retries = _max_retries()
        r = None
        for attempt in range(retries + 1):
            r = httpx.post(f"{base_url}/chat/completions", json=b, headers=headers, timeout=300)
            if r.status_code == 400 and "response_format" in r.text:
                # model doesn't support json_schema response_format; the schema is
                # already in the prompt, so retry without the parameter
                b.pop("response_format", None)
                r = httpx.post(
                    f"{base_url}/chat/completions", json=b, headers=headers, timeout=300
                )
            if r.status_code in _RETRYABLE_STATUS and attempt < retries:
                # transient: rate limit / 5xx — back off and retry
                time.sleep(_backoff_seconds(attempt))
                continue
            break
        assert r is not None
        r.raise_for_status()
        return r.json()

    for attempt in range(_MAX_REPAIR_ATTEMPTS + 1):
        data = _post(body)
        choice = data["choices"][0]
        content = choice["message"]["content"] or ""
        # Empty content is the reasoning-model trap: kimi-k2.x bill thinking
        # against max_tokens, so too small a budget exhausts the limit during the
        # reasoning phase (finish_reason="length") and content comes back "". A
        # re-ask can't fix a budget problem, so fail loud immediately.
        if not content.strip():
            finish = choice.get("finish_reason")
            if finish == "length":
                raise StructuredOutputError(
                    f"model returned empty output ({purpose}): hit max_tokens={max_tokens} "
                    "before emitting content — reasoning models bill thinking against "
                    "max_tokens, so raise the budget.",
                    output_format=output_format,
                )
            raise StructuredOutputError(
                f"model returned empty output ({purpose}): finish_reason={finish!r}, "
                "no content in the response.",
                output_format=output_format,
            )
        try:
            parsed = output_format.model_validate_json(_extract_json(content))
            break
        except ValidationError as e:
            if attempt >= _MAX_REPAIR_ATTEMPTS:
                raise StructuredOutputError(
                    f"model output failed {output_format.__name__} validation ({purpose}) "
                    f"after {attempt + 1} attempt(s): {e}",
                    raw_content=content,
                    output_format=output_format,
                ) from e
            # feed the validation error back and re-ask once; keep the contract
            body = {
                **body,
                "messages": [
                    *body["messages"],
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": _repair_instruction(e)},
                ],
            }

    u = data.get("usage", {})
    return parsed, {
        "input_tokens": u.get("prompt_tokens"),
        "output_tokens": u.get("completion_tokens"),
        "stop_reason": choice.get("finish_reason"),
        "repair_attempts": attempt,
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
