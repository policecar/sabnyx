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

## The hash contract (Python ⇆ Rust)

`opsctl` (Rust) reads and writes the same `ops/events.jsonl` as the Python
side, so `content_hash` must be byte-identical across both. The contract that
makes this hold:

1. **Only `payload` is hashed.** Never `ts`, `kind`, `actor`, or any other
   envelope field. So the two implementations need only agree on payload
   canonicalization.
2. **`ts` is an opaque RFC3339 string.** Both writers emit microsecond
   precision with a `Z` suffix (pydantic on the Python side;
   `to_rfc3339_opts(Micros, true)` on the Rust side). Because `ts` is excluded
   from the hash, the zero-microsecond edge case (where the two could differ by
   a trailing `.000000`) is harmless — `ts` is only ever compared
   lexicographically. Do not fold `ts` into the hash without first making both
   writers byte-identical.
3. **Payloads contain no floats.** Python's `json.dumps` and Rust's
   `serde_json` stringify floats differently at scientific-notation magnitudes
   (`1e+20` vs `100000000000000000000.0`), and Python can emit the non-JSON
   tokens `NaN`/`Infinity`. Rather than ship a shared float formatter, hashed
   payloads must use strings or scaled integers for fractional quantities.
   Python enforces this in `ledger._reject_floats`; opsctl's `task.*` payloads
   are ints/strings.

Canonicalization is otherwise `json.dumps(sort_keys=True,
separators=(",",":"), ensure_ascii=True)` — sorted keys, no whitespace,
non-ASCII escaped as `\uXXXX` (surrogate pairs for supplementary chars). The
parity is tested both ways: `opsctl` shells out to the Python implementation
(`test_compat_*`) and the Python suite pins the same hardcoded vectors
(`tests/test_ledger.py`).

## Upgrade path (if needed)

  jsonl -> sqlite (same schema) -> opsctl (adopt directly)

The `LedgerEvent` pydantic model is the stable boundary. Storage
changes under it; consumers don't.
