# Plan: precise feedback telemetry before analytics

Status: proposed
Depends on: PR #15 event schema; age trigger may land independently
Scope: decision outcome facts first, minimal reporting after real data exists

## Outcome

Collect enough precise facts to answer how the maintenance loop behaves without
pretending operational outcomes are model accuracy.

The current ledger records LLM calls and some triage decisions. It does not
distinguish every reason a proposal was or was not applied. A report built now
would conflate operator rejection, duplicates, missing input, non-interactive
execution, validation failure, and budget exhaustion.

## Phase 1: complete decision instrumentation

Keep `triage.decided` as one event per item, matching the PR #15 shape. Extend
it rather than introducing a second batch-shaped event with the same kind.

Payload:

```json
{
  "proposal_id": "sha256:...",
  "batch_id": "sha256:...",
  "slug": "page-slug",
  "action": "promote | keep | discard",
  "atlas_type": "concept | reference | idea | null",
  "reason": "model reason, max 120 chars",
  "trigger": "threshold | age | manual",
  "outcome": "applied | accepted_noop | declined | skipped_duplicate | noninteractive | failed",
  "error_type": "ValidationError | null"
}
```

Rules:

- `proposal_id` hashes batch ID + canonical item payload.
- One event is emitted for every recognized proposal, including promotion.
- Outcome is assigned only after the deterministic apply/confirmation path.
- `failed` records error class, never raw secrets or full exception text.
- Unknown slugs get a separate `triage.rejected` event because there is no
  stable subject file.
- Existing keep/discard events remain replayable; missing fields mean legacy.

Add corresponding decision events only where a real operator decision exists:

- `reweave.decided`: applied, declined, noninteractive, no-change, failed.
- `update.completed` owns update outcomes if the experiment lands; do not add a
  parallel event.
- `llm.failed` already covers provider/schema failures.

Budget exhaustion may add `llm.budget_exceeded`, but only if the event can be
written without recursively obscuring the original exception.

## What the data can and cannot say

Safe initial measures:

- proposals and outcomes by action/model/prompt hash;
- duplicate-skip, validation-failure, and non-interactive rates;
- human decline rate where an actual prompt occurred;
- tend cadence and queue-trigger distribution;
- LLM calls, tokens, retries, and failures already recorded.

Do not label any of these "accuracy." Accuracy requires an independent ground
truth or a later operator judgment. In particular:

- applied does not prove correct;
- declined does not prove incorrect;
- later promotion of the same slug does not prove a prior discard was wrong;
- duplicate skips say nothing about model quality.

## Phase 2: accumulate before building a control plane

Let telemetry accrue until at least:

- 30 triage item outcomes;
- 10 interactive decisions;
- multiple prompt hashes or model versions if comparison is desired.

Until then, use `opsctl query --kind triage.decided --json`. Do not add a price
table, dashboard, Telegram push, or large review command for an empty dataset.

## Phase 3: minimal `opsctl review`

After the sample gate, add a read-only report with three sections:

1. Activity: calls/tokens/failures by day and role.
2. Decision outcomes: counts and rates by action/outcome, explicitly not
   accuracy.
3. Loop health: tend cadence, threshold-vs-age triggers, pending intent counts.

Support `--since` and `--json` using the existing query timestamp parser and
serde structs. Unknown event kinds and legacy payloads are skipped or counted
as legacy, never fatal.

Estimated monetary cost is deferred. Model IDs are configurable and provider
pricing changes; a maintained price table is only worth owning after token
reporting is used in decisions.

## Notifications and digests

Deferred to a separate operator decision because they create external side
effects and retention policy:

- no automatic Telegram messages;
- no default `--notify` in heartbeat recipes;
- no daily `_review/` files or pruning behavior;
- no `notify.sent` event yet.

If unattended decisions are demonstrably missed, start with a read-only digest
printed by tend and let the scheduler explicitly opt into one outbound channel.

## Privacy and retention

- Reasons are capped at 120 characters and must not include source excerpts.
- Error payloads store class/category, not exception bodies that may include
  provider or path detail.
- Proposal hashes commit to normalized metadata, not raw source text.
- Ledger retention remains append-only; reports must work without exporting it.

## Tests

- Every triage action/outcome path emits exactly one event.
- Decline vs non-interactive vs duplicate vs failure remain distinguishable.
- Legacy PR #15 events parse in future review code.
- Stable proposal IDs for canonical identical inputs; changed model proposal
  changes ID.
- Error and reason truncation cannot leak full source/provider responses.
- Minimal review fixture verifies counts, legacy handling, `--since`, and JSON.
- Metrics labels never say accuracy or correctness.

## Delivery

Land Phase 1 alone. Review the real ledger sample before authorizing Phase 3.
That pause is part of the design: measurement questions should follow observed
behavior, not be copied wholesale from jibot's fleet-scale review system.
