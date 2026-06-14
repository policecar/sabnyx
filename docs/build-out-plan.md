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
- **Phase 3 — lightweight entity layer**: SPEC §2/§3 un-defer
  `person`/`org`, add `project` + `aliases`; `schemas.AtlasType` +
  `AtlasFrontmatter.aliases`; `paths` entity dirs (`atlas/people|orgs|
  projects`) in `ATLAS_TYPE_DIRS` + `ensure_tree` + `ENTITY_TYPES`;
  `promote --as person|org|project`; new `sabnyx entities` group —
  `list` (entity pages + aliases), `gaps` (broken wikilinks ≥ threshold
  distinct referrers → `gap` observations, deduped, `--dry-run`),
  `resolve` (slug/alias collision report). 135 Python tests (22 new).
- **Phase 4 — retrieval seam**: `retrieval.Retriever` protocol + default
  `GrepRetriever` wrapping `context.excerpts`, which gains an `order=` hook
  (default byte-identical to the old slug-sorted dump). The grep impl is
  *keyword-ranked* — term-overlap ordering, reorder-within-budget /
  never-drop-while-it-fits — so the `query` is load-bearing now and budget
  truncation degrades by relevance (the observable trigger for a later
  vector phase). `SABNYX_RETRIEVER` switch via `config.retriever_name()`
  (default `grep`; unknown → fail-fast). Call sites: `ask` passes the
  question, `reweave` passes the target page, `scan` passes no query (full
  dump), ingest untouched (`description_index`). 144 Python tests
  (9 new); 135 pre-existing stayed byte-for-byte green.
- **Vector retriever (opt-in, post-milestone)**: `VectorRetriever` registered
  as `SABNYX_RETRIEVER=vector` — static embeddings (model2vec
  `potion-retrieval-32M`, torch-free, ~30MB, CPU) rank pages by cosine, then
  render through the same `context.excerpts` block as grep (only the order
  changes). Optional `[vector]` extra, lazy-loaded; **default stays grep**.
  Built ahead of the "grep demonstrably hurts" trigger by explicit request;
  the opt-in default keeps the YAGNI posture intact. 148 Python tests
  (4 new, model2vec stubbed); validated against the live model.
- **Phase 5 — halbor v0 (the face), in progress**: sibling repo
  `/Users/p/src/_px/halbor`, branch `feat/projection-core` @ `7e4ff40` (off
  its `main`). Projection core shipped: `halbor project` reads the vault as a
  *data contract* (markdown+frontmatter, no sabnyx code dep) and emits staged
  markdown + `llms.txt`. Two sensitivity gates — structural (atlas-only; the
  temporal/operational/raw tiers are never read) + per-file `visibility:`
  (opt-out, fail-closed on odd values) — plus frontmatter redaction (allowlist
  `description`/`tags`) and a one-direction guard that refuses to write into
  the vault. Contract in halbor `docs/adr/0001-projection-contract.md`. 16
  tests; verified against the live vault. **Deferred to v1:** the thin
  persona/query interface over the projected face (reuses the retrieval seam).

**Next: Phase 5 v1 — halbor persona/query interface.** A `halbor ask`-style
conversational face over the *projected* vault (`build/site` + `llms.txt`),
grounded + cited, reusing the Phase 4 retrieval pattern. Still one direction:
vault → face → reader; the face never writes back.

Phase 4 as built (2026-06-13) — the surface below, with the grep impl
keyword-ranked rather than query-blind:
- All retrieval lives in `context.py`: `slug_index(dir)`, `description_index(
  dir, label)`, `excerpts(index, exclude=None, *_chars)`. The thing to make
  pluggable is `excerpts` — today it dumps every file body within a 300k-char
  budget, query-blind. `description_index` (the cheap slug+desc index) can
  stay as-is for now.
- Call sites that must keep working unchanged (the seam's contract):
  `services.build_ingest_proposal` (desc_index ATLAS+INTAKE),
  `services.answer_question` (slug_index+excerpts+desc_index),
  `commands/scan.py` (slug_index+excerpts),
  `commands/reweave.py` (slug_index+excerpts(exclude=target)),
  `commands/tend.py` (slug_index for the reweave pass).
- Proposed shape: a `Retriever` protocol with `retrieve(query, index, *,
  exclude, budget) -> str` (returns the same excerpt-block string excerpts
  produces now). Default `GrepRetriever` wraps current `excerpts` behaviour
  (optionally keyword-filtered by the query); a later `VectorRetriever` is a
  drop-in. `ask`/`scan`/`reweave` pass their query/target as the query;
  ingest has no query so it keeps the full-dump path. Keep `context.excerpts`
  as the grep impl's core so nothing else moves. Add `SABNYX_RETRIEVER`
  env/config switch (default `grep`). Keep all 135 tests green; add retriever
  unit tests + a seam test that `ask` still cites correctly with the grep
  retriever.
- YAGNI guard: do NOT build the vector index in Phase 4 — only the seam +
  grep impl. Vector index is its own later phase once grep demonstrably
  hurts (the doc's own deferral rule).

**Environment / resume checklist:**
- Work in worktree `/Users/p/src/_px/sabnyx/.worktrees/brain` (branch
  `feat/brain-buildout`, off `feat/jibot-agent`). `uv run pytest -q` → 144.
- Other worktree: `.worktrees/kimi-agent` (branch `feat/jibot-agent`, the Kimi
  agent; PR to `main` deprioritized but pending push by the operator).
- `main` has an uncommitted `.gitignore` (the `moonshot` ignore fix) — folds in
  when `feat/jibot-agent`/`feat/brain-buildout` merges, or commit it to main.
- Kimi key lives in the gitignored `moonshot` file at each worktree root
  (`MOONSHOT_API_KEY=...`); `SABNYX_LLM_PROVIDER=moonshot`. Use sparingly.
- halbor repo: `/Users/p/src/_px/halbor` (sibling), currently empty.
