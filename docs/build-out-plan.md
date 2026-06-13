# Build-out plan — milestone 1: the self-maintaining brain

Status: active · Started 2026-06-13

## Goal

Turn sabnyx from a self-driven CLI into a **self-maintaining, single-user
knowledge brain** with a separate **projection layer (halbor)**. Concretely it:

- triages, reweaves, and health-checks itself on a schedule (the *heartbeat*);
- keeps its event ledger in a small **Rust `opsctl`** control plane that also
  supervises scheduled work;
- recognizes recurring **entities** (people / orgs / projects) and flags gaps;
- exposes **halbor** — a read-only downstream face over the vault, never a
  second source of truth.

This is the jibot/Switchboard pattern distilled to one user, no legacy bits.

## Phases

### Phase 1 — Heartbeat (`sabnyx tend`)
Two-speed triage + auto-reweave + health, in one schedulable command.

- **Deterministic auto-promote** (no LLM), all gates must pass:
  1. promotable type (`bookmark`/`paper` → `reference`; `note`/`thought`
     deferred to LLM tier; `meeting-extract` never)
  2. not temporal
  3. required fields present (type, description, source, status)
  4. description ≥ 30 chars
  5. no duplicate slug already in atlas/
  6. trusted source (`source_agent` in the configured allowlist;
     default `{"sabnyx-ingest"}`, override `SABNYX_TRUSTED_SOURCES`)
- **LLM triage** tier: when ≥ N (default 5) items remain un-auto-promoted,
  one batched `parse_structured` call proposes per-item routing
  (promote-as-X / keep / discard) — human-gated unless `--yes`.
- **Reweave recent**: consume pending `reweave.proposed` intents.
- **Health + currency**: run the health audit; surface stale/uncurrent atlas.
- `--dry-run` shows the whole plan and writes nothing; every mutation ledgered.

Success: a dirty intake queue → triaged, reweaved, health-checked vault in one
command; trusted ingested items auto-promote; nothing else moves without a gate.

### Phase 2 — opsctl (Rust)
Minimal control plane owning the event ledger (ADR-0002; same jsonl schema).

- `opsctl append | query | project | supervise`
- `project` rebuilds current state from the log; `supervise` runs `sabnyx tend`
  on a schedule with retry and `task_started/completed/failed` events.
- Byte-compatible with the existing `ops/events.jsonl`; Python keeps working
  (the schema is the contract, not the implementation language).

### Phase 3 — Lightweight entity layer
`person` / `org` / `project` atlas types + gap detection ("referenced N× with
no page" → `gap` observation) + alias/dedup resolution. **No** Senzing/Nyne.

### Phase 4 — Retrieval seam
Pluggable retrieval interface; grep impl now, local-embedding vector index a
drop-in later (deferred until the vault outgrows grep — the doc's own rule).

### Phase 5 — halbor (the face)
New repo. Read-only projection of the vault: frontmatter sensitivity filter →
staged markdown + `llms.txt`, plus a thin persona/query interface. One
direction only: vault → halbor.

## Explicit non-goals (the legacy / multi-user / at-scale half)

- Senzing-scale entity resolution / Nyne enrichment / people CRM
- Enterprise Onyx deployment (halbor is the single-user analogue)
- Multi-machine Syncthing (git already syncs one user across machines)
- NanoClaw container fleet / multi-channel gateway
- Payments (x402/USDC) — not even in jibot; ledger hashing covers integrity

## Progress & resume (last updated 2026-06-13)

**Done (committed on `feat/brain-buildout`):**
- Phase 0 (pre-existing, from `feat/jibot-agent`): full LLM loop —
  ingest / ask / reweave / scan / verify / serve — + Kimi/Moonshot provider,
  citation fixes, the captured jibot source mirror.
- **Phase 1 — heartbeat** (`effa4fe`): `sabnyx tend` (two-speed triage →
  reweave → health), `services.promote_file`, deterministic gate,
  `config.trusted_sources`, `recipes/heartbeat.sh`. 113 Python tests.
- **Phase 2 — opsctl** (`342edb4`): Rust crate `opsctl/` (query / project /
  supervise), ledger byte/hash-compatible with Python. 15 Rust tests.
- README rewritten and accurate (`f1e5095`).

**Next: Phase 3 — lightweight entity layer.** Planned changes:
- SPEC §2: un-defer `person`/`org` types; add `project`. Note: lightweight
  (entity pages + `aliases`), NOT Senzing-scale resolution (stays out).
- `schemas.py`: `AtlasType += person|org|project`; `AtlasFrontmatter.aliases`.
- `paths.py`: entity type dirs (e.g. `atlas/people`, `atlas/orgs`,
  `atlas/projects`) + `ensure_tree`; `promote --as` choices.
- New `sabnyx entities` command: `list`, `gaps` (recurring broken-wikilink
  targets ≥ N → `gap` observations), `resolve` (alias/dup collisions, report).
- Tests; keep the 113 green.

Then Phase 4 (retrieval seam) and Phase 5 (**halbor** — sibling repo at
`/Users/p/src/_px/halbor`, the read-only projection/face; vault → halbor only).

**Environment / resume checklist:**
- Work in worktree `/Users/p/src/_px/sabnyx/.worktrees/brain` (branch
  `feat/brain-buildout`, off `feat/jibot-agent`). `uv run pytest -q` → 113.
- Other worktree: `.worktrees/kimi-agent` (branch `feat/jibot-agent`, the Kimi
  agent; PR to `main` deprioritized but pending push by the operator).
- `main` has an uncommitted `.gitignore` (the `moonshot` ignore fix) — folds in
  when `feat/jibot-agent`/`feat/brain-buildout` merges, or commit it to main.
- Kimi key lives in the gitignored `moonshot` file at each worktree root
  (`MOONSHOT_API_KEY=...`); `SABNYX_LLM_PROVIDER=moonshot`. Use sparingly.
- halbor repo: `/Users/p/src/_px/halbor` (sibling), currently empty.
