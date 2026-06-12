# ADR 0006 — LLM boundary: one call site, role indirection, pluggable providers

Date: 2026-06-12
Status: accepted

## Context

Phase 1 of the implementation plan (docs/architecture-review-and-plan.md)
adds the first LLM calls to sabnyx. Three pressures shape the boundary:

1. Auditability — the vault's whole premise is that every mutation is
   ledgered; an un-ledgered model call would be the system's largest
   blind spot.
2. Model churn — models improve monthly; jibot's `model_role`
   indirection (recommended in ADR 0003 and the amplifier recon) decouples
   call sites from model ids.
3. Provider flexibility (operator request) — the operator wants to switch
   models freely, including via OpenRouter, not be locked to one vendor.

## Decision

All model traffic goes through `sabnyx/llm.py::parse_structured()` —
a single structured-output call site. Three layers of indirection:

  role -> model -> provider

  - Roles: `fast` / `standard` / `deep`. Call sites name a role, never a
    model. `role_for_depth()` maps jibot's extraction-depth tiers to roles;
    `role_for_message()` is a compact rule-based scorer (iblai-router
    style: user message only, <1ms, `[fast]`/`[deep]` overrides).
  - Models: per-provider role defaults, overridable per role via
    `SABNYX_MODEL_FAST/_STANDARD/_DEEP`. Switching every role to any
    model is an env-var edit.
  - Providers: `SABNYX_LLM_PROVIDER` selects the backend:
      - `anthropic` (default): native SDK; structured outputs via
        `messages.parse`, prompt caching (`cache_control`) on the stable
        SPEC+ROUTING contract block, adaptive thinking on standard/deep.
      - `openrouter`: OpenAI-compatible chat completions against
        openrouter.ai via httpx (already a transitive dep — no new SDK).
        Any model OpenRouter serves is addressable by role.
        Auth: `OPENROUTER_API_KEY` or `SABNYX_LLM_API_KEY`.
      - `openai-compatible`: same code path, arbitrary
        `SABNYX_LLM_BASE_URL` (ollama, vllm, a corporate proxy, ...).

Schema enforcement is provider-independent: the pydantic model is the
contract, validated on our side regardless of whether the provider
honored `response_format`. Every call emits an `intent` (llm.requested)
event before and an `action` (llm.completed) event after, with model,
provider, prompt hash, token usage, and stop reason — never the prompt
text itself (the ledger stays lean; the hash binds the call to content).

## Consequences

+ Swapping models or providers is configuration, not code. A new
  provider is one `_call_*` function.
+ The ledger shows exactly which model did what, at what token cost —
  the substrate for the cost gate (Gate 6) and future cost dashboards.
+ The contract block (SPEC + ROUTING) rides every call on every
  provider: Karpathy's "schema turns the chatbot into a disciplined
  maintainer" holds regardless of vendor.
- Anthropic-only niceties (prompt caching, adaptive thinking) degrade
  gracefully off-provider rather than being abstracted: caching is a
  cost optimization, not a correctness dependency.
- The OpenAI-compatible path trusts `response_format` support varies;
  the prompt embeds the JSON schema and a deterministic extractor +
  pydantic validation backstop it. A malformed response fails loudly,
  never writes.
