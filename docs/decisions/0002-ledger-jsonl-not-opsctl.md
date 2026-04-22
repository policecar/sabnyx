# ADR 0002 — Ledger: jsonl, not opsctl

Date: 2026-04-22
Status: accepted

## Context

jibot uses `opsctl`, a Rust CLI with 5 crates, 79 tests, ~2,400 LOC
in one crate alone. It implements a four-plane event architecture
(intent/event/projection/action), per-zone append-only ledgers with
typed payloads, YAML manifest validation, Supabase redacted
projector, spool transport.

For a solo vault at stage 0, this is overkill.

## Decision

Stage 0 ledger is a single `ops/events.jsonl` file. One JSON object
per line. Append-only. Schema lives in `src/sabnyx/schemas.py`
(pydantic `LedgerEvent`). Content-hashed for future Lemma
attestation.

## Consequences

+ One file, one script, one pydantic model. Entire ledger subsystem
  is ~150 LOC.
+ Replayable by `for event in ledger.replay(): ...`.
+ Upgrades cleanly to structured planes/sqlite/opsctl if we outgrow
  it — the LedgerEvent schema already carries `plane`.
+ Content-hash present from day one, so future Lemma Merkle roots
  can commit to historical events without retroactive hashing.
- No cross-machine dispatch. We don't need it.
- No built-in projections beyond what scripts compute on-demand.
  Fine.

## Upgrade path (if needed)

  jsonl -> sqlite (same schema) -> opsctl (adopt directly)

The `LedgerEvent` pydantic model is the stable boundary. Storage
changes under it; consumers don't.
