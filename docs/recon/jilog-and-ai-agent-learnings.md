# jilog + ai-agent-learnings — reuse assessment for sabnyx

Source: github.com/Joi/jilog (Rust, MIT, v0.9.0, 122 commits, last 2026-10-13,
~25k LOC across 5 crates, 435 `#[test]`s) and github.com/Joi/ai-agent-learnings
(12 markdown docs + D2 diagrams, MIT, archived 2026-07 in favor of jibot.md).
Both read from shallow clones on 2026-10-22; facts below are from the code
unless marked (inferred).

## Verdict

Neither replaces sabnyx.

- **ai-agent-learnings** is the earlier public edition of the document sabnyx
  is adapted from (`docs/spec/jibot.md`). Everything structural in it is
  already here: three tiers, `description` as index, routing tree, seven
  gates, reweave with bridge/enrichment/correction priority
  (`commands/reweave.py`), six auto-promote gates incl. trusted agent,
  observations. It records *no* relationship to jilog.
- **jilog** is the `opsctl review nightly` + ledger crates extracted from
  Joi's fleet. It reads AI-agent *session transcripts* (Claude Code, Codex,
  Copilot, Amplifier, NanoClaw, agentsview) for corrections / errors / stuck
  loops / spend and writes a nightly digest, filing issues into `kata`. It
  makes no LLM calls and has no vault, no markdown knowledge, no links, no
  promotion, no Telegram/HTTP ingress. The public CLI creates no ledger events
  (segment writers appear only in `mod tests`; producers live in private
  opsctl). It answers "how are my agents behaving", not "what do I know" — a
  tool you could run *beside* sabnyx, never instead of it.

## jilog — what it is

- Crates: `ledger-core` (Event / Segment / SegmentStore / zone), `ledger-sqlite`
  (rebuildable projection), `ledger-spool` (cross-machine transport via
  Syncthing), `jilog-review` (Reader / Tracker traits, detectors, digest),
  `jilog` (clap CLI: `review nightly`, `query`, `spool emit|ingest|status`,
  `config check`; `supervise`/`rebuild`/`status` are listed as planned).
- Event: UUIDv7 id, zone, source, source_seq, correlation/causation ids,
  actor_ref, object_ref, `event_class` enum (Ingest, Route, Decision,
  StateChange, Claim, Delivery, Projection, Health, Approval, NoteMeta),
  `payload_tier` (MetadataOnly / Structured / Confidential), payload.
  Segments are pretty-JSON files `segments/<source>-<seq:06>.json` with a
  CRC32 checksum.
- Review outputs: `~/.jilog/digests/learning-digest-<date>.md` with YAML
  frontmatter counts (`signals_captured`, `p0_count`, …) + `--json`; a
  processed-sessions file with `.retry` / `.render` / `.signals` sidecars.
- Detectors are regex heuristics (`detectors.rs` `CORRECTION_PATTERNS`); the
  author's own spec records 254 false-positive corrections/day before 0.9 and
  `errors: 0 / patterns: 0` for the Claude Code and Codex readers.

## STEAL_PATTERN (→ `docs/plans/jilog-borrowings.md`)

- **Signal ledger: stable content hash + independent lifecycle flags**
  (`crates/jilog-review/src/signal_ledger.rs:39-51`). Hand-rolled FNV-1a64
  over length-prefixed fields ("std's DefaultHasher makes no such promise")
  so `("ab","c") != ("a","bc")`; one TSV line per signal
  `<first-seen>\t<c|f|cf>\t<session-mtime>\t<kind>:<session>:<hash>`; 90-day
  retention. Solves "the same signal counted five nights running" — the
  exact problem sabnyx's `scan` (file-pair glob) and `entities` (filename
  glob) each solve ad hoc today.
- **Reopen only if closed-as-done** (`crates/jilog-review/src/trackers/kata.rs:82-101`).
  A recurring signal reopens a closed issue only when `closed_reason == done`;
  `wontfix` / `duplicate` / `superseded` / `audit-no-change` and a *missing*
  reason (schema drift) are left untouched. Create uses `--idempotency-key`
  = slugged title so it is a no-op even when `list_open()` misses.
- **Byte-stable digest + golden tests** (`crates/jilog-review/tests/golden_digest.rs`,
  `tests/golden/learning-digest.md`, `crates/jilog/tests/golden/review-nightly.json`).
  Regen only with `UPDATE_GOLDEN=1`; "a regen run never reports as a passing
  compare". Frontmatter counts make the digest `yq`-able as a day series.
- **Health-pattern detectors with hard thresholds** (`health.rs` consts):
  compaction storm ≥3 per 10 min, stuck loop ≥4 identical tool calls, resume
  storm ≥3 per 30 min, runaway ≥150 tool calls per session. Pure functions
  over an event stream; no model in the loop.
- **Incremental verify with watermark + gap backfill**
  (`crates/ledger-core/src/store.rs:401-470`). Skips `seq <= watermark`
  unless previously failed or previously *missing*; records current gaps so a
  later-synced segment below the watermark still gets verified.
- **Minimal pluggable Reader trait with graceful degradation**
  (`crates/jilog-review/src/reader.rs:157`). Required
  `name / discover(since) / load(handle)`; optional `seat / agent / machine /
  dedupe_key / load_events / load_stats` default to `None`; readers without
  an event stream simply never yield `Pattern` signals.

## From ai-agent-learnings — two ideas jibot.md later dropped

- **`requests/` folder** (`collaborative-knowledge-domains.md:122`): "the
  maintainer couldn't ask for clarification … The `requests/` folder closes
  that gap." A channel for the maintenance agent to park a question for the
  operator instead of guessing or aborting.
- **`CONTEXT.md` with live "Current State" counters and a session checklist**
  (`domain-context-template.md`) — a read-first file per domain. Relevant
  the day the first `domains/<topic>/` graduates; not before.

## SKIP

- jilog's ledger crates in place of `opsctl`: five crates, UUIDv7, SQLite
  projection, spool transport — the fleet-scale half sabnyx scoped out
  (ADR-0002, build-out non-goals). Flat JSONL + `content_hash` is sufficient.
- jilog's transcript readers: no signal for our stack (see detector stats
  above) and they would audit the operator's *coding* sessions, not the vault.
- No-clobber hard-link publish (`segment.rs:328`): only matters when a
  non-git sync can race the writer. `ledger.py`'s single O_APPEND write(2) +
  fsync is the right shape for one JSONL file.
- `rust_decimal` for money: `spend.py` counts calls, not currency — nothing
  to fix.
- ai-agent-learnings' multi-contributor material (per-contributor intake
  folders, trust tiers, Onyx governance, Syncthing shares): multi-user is an
  explicit non-goal.
