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
  separate sibling repo, **halbor**.
- **Model-agnostic.** Runs on Kimi/Moonshot, Anthropic, OpenRouter, or any
  OpenAI-compatible endpoint — one config switch.

## Where things are documented

The root holds **the contract** — the files every agent loads before touching
the vault (and `opsctl` uses `SPEC.md` as its repo-root marker):

| File | What |
|------|------|
| `SPEC.md` | the contract: tiers, types, frontmatter, boundaries, gates |
| `ROUTING.md` | the decision tree: where does a given piece of content go |
| `AGENTS.md` | operator instructions for humans and LLMs on this vault |

Everything else is in `docs/` — guides (`architecture.md`, `testing.md`,
`deployment.md`), ADRs (`decisions/`), plans, dated reviews, and recon notes.
`docs/README.md` is the index.

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
uv run sabnyx entities                    # lightweight entity layer: list, gaps, resolve
uv run sabnyx lint                        # gates 1,2,4 (frontmatter + containment)
uv run sabnyx health                      # orphans, broken links, currency, cost
uv run sabnyx reconcile                   # diff the ledger against atlas/ (drift check)

# channels & ops
uv run sabnyx serve                       # Telegram interface (trusted senders)
uv run sabnyx serve-http                  # HTTP ingress for the Rabbit R1 (bearer-key allowlist)
uv run sabnyx verify                      # live LLM connectivity probe
```

The **R1 client** (`sabnyx serve-http`) is a small stdlib HTTP ingress that lets a
[Rabbit R1](https://www.rabbit.tech/) Creation drive the vault by voice. The R1
transcribes on-device and POSTs the plain-text transcript with a bearer key;
sabnyx routes it through the same `services.py` write path the CLI and Telegram
use, and returns JSON the Creation speaks back. The routes are explicit — the
path *is* the action, no LLM routing:

```
POST /ingest  {text}  → capture into intake/   → {text, filed:[...]}
POST /ask     {text}  → grounded vault answer   → {text, cited:[...]}
GET  /status          → vault pulse             → {intake, atlas, observations}
```

Like `serve`, auth is a bearer-key allowlist and it refuses to run open (empty
allowlist). Every request is logged as a `web`-sourced event (actor `sabnyx-r1`).

`serve` and `serve-http` share a rolling-window LLM spend cap across both
channels (`SABNYX_INGRESS_MAX_CALLS`/`_WINDOW_MINUTES`; see Cost & reliability
guards below) — a tripped cap returns a plain reply / HTTP 429, never a crash.

Every command that mutates the vault appends one event to `ops/events.jsonl`.

## The heartbeat (`sabnyx tend`)

One schedulable command that runs the whole self-maintenance cycle:

1. **Auto-promote** — deterministic, no LLM. Six gates must pass: promotable
   type (`bookmark`/`paper` → `reference`), not temporal, required fields,
   `description` ≥ 30 chars, no duplicate slug, and a **trusted `source_agent`**
   (`SABNYX_TRUSTED_SOURCES`, default `sabnyx-ingest`). Trust follows
   *provenance*, not channel: only operator-supplied local input is trusted —
   remotely-fetched (`ingest --url` → `sabnyx-ingest-web`) and third-party
   (`sabnyx-telegram`, `sabnyx-r1`) content is **not** auto-promotable, so
   prompt-injected content can't slip into atlas unattended.
2. **LLM triage** — for everything that didn't auto-promote, *only* once
   `≥ --threshold` (default 5) items are pending: one batched call proposes
   promote / keep / discard. Human-gated unless `--yes`. Discard never deletes.
   Below threshold, `--max-age-days` (default 7; `0` disables) is a fallback:
   once the oldest deferred item has waited that long, the whole deferred
   batch is triaged in one call, so a slow-filling queue can't starve forever.
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
same `content_hash` — only the `payload` is hashed; `ts` is opaque and
payloads carry no floats; see ADR-0002), so the two interoperate freely. Like
the Python side, it resolves the ledger from `SABNYX_DATA_DIR` (env or
`<repo>/.env`), falling back to the code repo, so both write to one file.

```sh
cd opsctl && cargo build --release      # binary: opsctl/target/release/opsctl

opsctl query --kind atlas.promoted --limit 10   # most recent 10 (--json too)
opsctl project                                   # rebuild state from the log:
                                                 #  counts, pending reweave, atlas files
opsctl verify                                    # recompute every content_hash;
                                                 #  exit 1 on any mismatch or bad line
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

**Cost & reliability guards.** Two budgets, by shape of caller: unattended
runs (`tend`) are bounded by a per-run call ceiling (`SABNYX_MAX_LLM_CALLS`,
default 50; `0` = unlimited) so a runaway queue or prompt can't spin the
provider without limit — one-shot CLI calls are uncapped. The long-lived
`serve`/`serve-http` daemons instead get a rolling-window cap shared across
both channels (`SABNYX_INGRESS_MAX_CALLS`/`_WINDOW_MINUTES`, default 60
calls / 60 min; see the serve section above), since there is no single "run"
to bound a process that polls forever. Transient provider failures (HTTP
429 / 5xx / 529) are retried with exponential backoff
(`SABNYX_LLM_MAX_RETRIES`, default 3) on both the Anthropic and
OpenAI-compatible paths.

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
                       scan, entities, lint, health, serve, serve-http, verify
  config.py            local .env autoload, trusted-source allowlist

opsctl/                the Rust control plane (ledger, query, project,
                       supervise, verify)
recipes/               cron/launchd-ready scripts (heartbeat.sh)
docs/                  guides, decisions/ (ADRs), plans/, reviews/, recon/
```

`vault/` and the two `ops/` data files are gitignored here: in production they
live in a separate private DATA repo pointed at by `SABNYX_DATA_DIR` (see
`docs/deployment.md`); without it they resolve under this repo (single-repo dev
mode).

## Status

Milestone 1 — *the self-maintaining brain* — complete: the LLM loop
(ingest / ask / reweave / scan), the heartbeat (`tend`), `opsctl`, the entity
layer (`entities`), the retrieval seam (grep default, opt-in vector), and
**halbor** (the read-only face, a separate sibling repo). A full review
against the jibot spec plus a hardening pass landed 2026-07-01 — see
`docs/reviews/codebase-spec-review.md`. What's next: `docs/plans/build-out-plan.md`.

## Credits

Architecture adapted from Joi Ito's jibot
(<https://jibot.md/learnings/architecture>) — three-tier discipline, the
routing tree, the `description` field, the reweave pass, observations, the
seven gates, the four-plane ledger, and the normalized envelope. The source
document is kept locally at `docs/spec/jibot.md` (gitignored: the text is
Joi's and unlicensed; the original answers 403 to anonymous fetchers, so agents
need the local copy). Recon notes on Joi's open-source repos are in
`docs/recon/`.
