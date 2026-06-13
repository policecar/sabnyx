# ADR 0007 — Telegram as the second interface

Date: 2026-06-12
Status: accepted

## Context

Phase 6 of the implementation plan adds a second interface, the move that
finally justifies the `NormalizedEnvelope` abstraction carried since stage
0 — an abstraction is only earned once a second implementation exists.
jibot's own roadmap put a Telegram adapter next ("new intake by message:
'sabnyx, remember this: ...'"), and it is the north-star direction: one
persona, many channels, one vault.

Options weighed: Telegram, email (IMAP/SMTP), a local HTTP endpoint
(jibot's knowledge-intake sprite shape). Transport: long-poll vs webhook.

## Decision

**Telegram, long-poll, trusted-sender auto-apply.** (Operator's call.)

- **Transport: `httpx` long-poll**, no SDK. `getUpdates` + `sendMessage`
  are two calls; httpx is already a dependency (the OpenAI-compatible LLM
  path). A `sabnyx serve` process polls and dispatches inline; `--once`
  drains the backlog and exits (cron / testing). No public URL, no
  webhook infra. Webhook can come later without touching the handler.

- **Trusted-sender model.** Only `SABNYX_TELEGRAM_ALLOW` user IDs are
  honored (jibot's trusted-source allowlist). `serve` refuses to start
  with an empty allowlist rather than trust everyone. Untrusted messages
  are logged `wire.rejected` and ignored — no reply, so the bot is not a
  spam amplifier.

- **Auto-apply for trusted senders.** Captures from the wire apply
  directly into `intake/` — which is *disposable* and still passes through
  triage/promote before reaching atlas. So "auto-apply" never touches
  durable knowledge unreviewed; the containment tiers are the safety net,
  not a chat round-trip.

- **Routing is deterministic, not LLM.** Message shape picks the action
  (`/ask` or trailing `?` → ask; URL → ingest URL; `/remember` or bare
  text → capture; `/status` → health pulse). The LLM is used for the
  knowledge work, never to decide what the operator meant.

- **One write path.** The handler calls `services.py`, the same headless
  orchestration the CLI commands call. The chat interface and the terminal
  cannot drift. The handler acts on `NormalizedEnvelope`, not on Telegram
  payloads — a third channel is one more normalizer.

## Consequences

+ The envelope abstraction is now load-bearing, not speculative.
+ Capture-by-message and ask-from-phone with zero new heavy deps.
+ Every wire event is ledgered (`wire.received` / `wire.rejected` /
  `wire.errored`) with the envelope id — auditable and dedup-ready.
+ `serve --once` makes the whole loop testable offline (get_updates /
  send_message stubbed) and cron-friendly.
- Long-poll means a running process; no serverless story yet. Fine for a
  solo operator on a Mac/VM, matching jibot's own deployment.
- Telegram-specific normalizer code is ~70 lines we own. Acceptable; it is
  the only Telegram-coupled surface.
- No outbound-initiated messages (jibot's "context-aware follow-ups"
  remain unbuilt, as they are in jibot). Inbound request/reply only.
