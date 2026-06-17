# sabnyx

A personal knowledge **brain**, modeled on Joi Ito's jibot architecture and
distilled to a single user. Markdown is the substrate; an LLM does the
bookkeeping. sabnyx ingests sources, answers questions, connects ideas, and
maintains itself — and every change is recorded on an append-only ledger.

Three tiers (`intake → atlas → domains`), YAML frontmatter as a strict
contract, an append-only event ledger, and a small Rust control plane
(`opsctl`) that schedules the maintenance loop.

- **Substrate:** plain git + markdown. Obsidian-compatible, Obsidian-optional.
  No database. Git is also your multi-machine sync.
- **Single-user, no legacy bits.** The multi-user / at-scale half of jibot
  (people-CRM-scale entity resolution, enterprise RAG, container fleets,
  payments) is deliberately out of scope. The read-only public *face* is a
  separate repo, **halbor** (see the roadmap).
- **Model-agnostic.** Runs on Kimi/Moonshot, Anthropic, OpenRouter, or any
  OpenAI-compatible endpoint — one config switch.

## Read in this order

| File | What |
|------|------|
| `SPEC.md` | the contract: tiers, types, frontmatter, boundaries, gates |
| `ROUTING.md` | the decision tree: where does a given piece of content go |
| `AGENTS.md` | operator instructions for humans and LLMs on this vault |
| `docs/build-out-plan.md` | the active milestone: the self-maintaining brain |
| `docs/roadmap.md` | the original stage 0–3 arc |

## Setup

```sh
uv sync                          # install Python deps
```

Configure a model provider. The simplest path is Kimi/Moonshot — drop a
gitignored `.env` file at the repo root:

```sh
# .env  (auto-loaded; real env vars always win)
SABNYX_LLM_PROVIDER=moonshot
MOONSHOT_API_KEY=sk-...
```

Then prove the live path:

```sh
uv run sabnyx verify           # one cheap structured call per role
```

See [LLM providers](#llm-providers) for Anthropic / OpenRouter / others.

## Commands

```sh
# capture & query
uv run sabnyx new --type thought          # create an intake file by hand
uv run sabnyx ingest <file|--url|--stdin> # LLM: source → structured intake (+ obs)
uv run sabnyx ask "..."                   # LLM: answer from the vault, cite [[links]]

# self-maintenance
uv run sabnyx tend                        # the heartbeat (see below)
uv run sabnyx triage                      # write a review report of the intake queue
uv run sabnyx promote <intake.md> --as concept   # move one file intake → atlas
uv run sabnyx reweave                     # LLM: backward-link recent promotions
uv run sabnyx scan                        # LLM: contradiction scan across atlas
uv run sabnyx lint                        # gates 1,2,4 (frontmatter + containment)
uv run sabnyx health                      # orphans, broken links, currency, cost

# channels & ops
uv run sabnyx serve                       # Telegram interface (trusted senders)
uv run sabnyx verify                      # live LLM connectivity probe
```

Every command that mutates the vault appends one event to `ops/events.jsonl`.

## The heartbeat (`sabnyx tend`)

One schedulable command that runs the whole self-maintenance cycle:

1. **Auto-promote** — deterministic, no LLM. Six gates must pass: promotable
   type (`bookmark`/`paper` → `reference`), not temporal, required fields,
   `description` ≥ 30 chars, no duplicate slug, and a **trusted `source_agent`**
   (`SABNYX_TRUSTED_SOURCES`, default `sabnyx-ingest`).
2. **LLM triage** — for everything that didn't auto-promote, *only* once
   `≥ --threshold` (default 5) items are pending: one batched call proposes
   promote / keep / discard. Human-gated unless `--yes`. Discard never deletes.
3. **Reweave** — consume pending `reweave.proposed` intents (backward links +
   contradiction observations).
4. **Health** — a concise audit summary.

```sh
uv run sabnyx tend --dry-run     # preview everything; writes NOTHING
uv run sabnyx tend --yes         # apply, unattended
```

Schedule it with `opsctl supervise` (below) or `recipes/heartbeat.sh`.

## opsctl — the control plane (Rust)

`opsctl` owns the event ledger and supervises scheduled work. It is
byte/parse-compatible with the Python ledger (same `ops/events.jsonl`,
same `content_hash`), so the two interoperate freely (ADR-0002).

```sh
cd opsctl && cargo build --release      # binary: opsctl/target/release/opsctl

opsctl query --kind atlas.promoted --limit 10   # filter the ledger (--json too)
opsctl project                                   # rebuild state from the log:
                                                 #  counts, pending reweave, atlas files
opsctl supervise --label heartbeat --every 900 -- uv run sabnyx tend --yes
opsctl supervise --label heartbeat --once    -- uv run sabnyx tend --yes   # for cron
```

`supervise` emits `task.started` / `task.completed` / `task.failed` events with
retry and backoff. Use `--every N` for a launchd/daemon loop, `--once` for cron.

## LLM providers

All model traffic goes through one structured call site with three layers of
indirection: **role → model → provider** (ADR-0006). Call sites name a *role*
(`fast` / `standard` / `deep`), never a model.

| Provider (`SABNYX_LLM_PROVIDER`) | Auth | Notes |
|---|---|---|
| `moonshot` | `MOONSHOT_API_KEY` | Kimi. Defaults: fast/standard `kimi-k2.5`, deep `kimi-k2.6` |
| `anthropic` (default) | `ANTHROPIC_API_KEY` | native SDK, prompt caching, adaptive thinking |
| `openrouter` | `OPENROUTER_API_KEY` | any model OpenRouter serves |
| `openai-compatible` | `SABNYX_LLM_API_KEY` + `SABNYX_LLM_BASE_URL` | ollama, vLLM, a proxy, … |

Override any role's model with `SABNYX_MODEL_FAST` / `_STANDARD` / `_DEEP`.
Every call is ledgered (`llm.requested` intent + `llm.completed` action) with
provider, model, prompt hash, and token usage — the prompt text never is.

## Layout

```
vault/                 the knowledge itself (git is the source of truth)
  intake/              disposable queue; new material lands here
    .observations/     agent-logged signals (gap, contradiction, …)
  atlas/               durable, typed, promoted knowledge
    concepts/  references/  ideas/
  sources/             retained raw artifacts (immutable provenance)
  domains/             graduated topics with their own schema (later)
  _review/             operational; never knowledge

ops/
  events.jsonl         append-only ledger (the substrate's shadow)
  knowledge-log.md     human-readable provenance (Gate 3)

src/sabnyx/            the Python package
  llm.py               the single model boundary (providers, roles, ledgering)
  services.py          shared orchestration (ingest, ask, promote, triage)
  commands/            new, ingest, ask, tend, triage, promote, reweave,
                       scan, lint, health, serve, verify
  config.py            local .env autoload, trusted-source allowlist

opsctl/                the Rust control plane (ledger, query, project, supervise)
recipes/               cron/launchd-ready scripts (heartbeat.sh)
docs/                  decisions/ (ADRs), recon/, roadmap.md, build-out-plan.md
```

## Status

Milestone 1 — *the self-maintaining brain* — in progress. Done: the LLM loop
(ingest / ask / reweave / scan), the Kimi/Moonshot provider, the heartbeat
(`tend`), and `opsctl`. Next: a lightweight entity layer, a pluggable retrieval
seam, and **halbor** (the read-only projection/face). See
`docs/build-out-plan.md`.

## Credits

Architecture adapted from Joi Ito's jibot
(<https://jibot.md/learnings/architecture>) — three-tier discipline, the
routing tree, the `description` field, the reweave pass, observations, the
seven gates, the four-plane ledger, and the normalized envelope. A full local
mirror of the source lives at
`vault/atlas/references/switchboard-jibrain-jibot-architecture.md`. Recon notes
on Joi's open-source repos are in `docs/recon/`.
