# Roadmap

Stage 0 through stage 3, with ZK provenance and x402 slotted in.
Each stage is independently valuable; sabnyx works if you stop at any
stage.

## Stage 0 — skeleton (this commit)

  - Three-tier directory structure (intake, atlas, domains, _review)
  - Frontmatter schemas (pydantic) — intake, atlas, observation
  - Routing decision tree documented in ROUTING.md
  - NormalizedEnvelope pattern (pydantic stub, cli adapter)
  - Event ledger at ops/events.jsonl (append-only, content-hashed)
  - Knowledge provenance log at ops/knowledge-log.md
  - Observations directory with schema
  - CLI: new, lint (gates 1+2+4), triage, promote, health
  - Hard pre-commit lint (gates 1, 2, 4 block commits)
  - ZK/x402 syntax reserved (HTML-comment round-trip, `proof:` field)
  - Smoke tests

## Stage 1 — daily driver (week 1-2)

  - Real content flows through the pipeline: bookmark every day,
    triage weekly, promote monthly
  - AGENTS.md gets tightened against real friction
  - Tag taxonomy emerges empirically; promote repeated tags to
    SPEC addendum
  - Health dashboard: intake-age histogram, atlas growth, broken-link
    trend (script in ops/scripts/dashboard.py)
  - Import scripts for common sources (browser bookmarks, paper PDFs,
    a text dump of existing notes)
  - First `domains/` candidate identified (the topic that keeps
    outgrowing `atlas/concepts/`)

## Stage 2 — knowledge compounds (week 3-6)

  - Reweave pass implemented as a command:
    `sabnyx reweave <atlas-file>` — searches vault for related files,
    proposes marker-block additions, flags contradictions as
    observations
  - Contradiction scan (grep-based at first; vector search in stage 3
    if needed)
  - Full seven gates in health check (add materia, currency, cost,
    verification)
  - First `domains/<topic>/` graduated with its own SCHEMA.md
  - Recipes (bash/python) that chain commands:
    `recipes/daily-triage.sh`, `recipes/weekly-health.sh`
  - Obsidian opened on top of the vault (optional; a convenience, not
    a dependency)

## Stage 3 — public/agent surface (when you want it)

  Major moves, each independently opt-in:

  - Agent daemon: a single-process scheduler that runs triage,
    reweave, and health on cron. Data shapes mirror nanoclaw
    (SQLite scheduled_tasks, file-IPC) so we can lift its core
    scheduler if we want later. See
    docs/decisions/0004-mirror-nanoclaw-contracts.md.

  - Telegram adapter: the second NormalizedEnvelope source. New
    intake by message: "sabnyx, remember this: ..."

  - Web surface (markdown-first site): serve `vault/atlas/` as
    llms.txt-indexed markdown to bots, HTML to browsers.
    Cloudflare Pages Functions, jibot style.

  - ZK provenance (Lemma): every published atlas file gets a
    docHash + selective-disclosure proof. The `proof:` field in
    frontmatter finally becomes non-null. Events.jsonl gets Merkle
    roots committed daily. See
    docs/decisions/0005-zk-provenance-lemma.md (to be written).

  - x402 wallet: sabnyx gets a USDC-on-Base address. Agents calling
    the public API pay for premium endpoints (deep search, reweave
    across large corpora). Receipts embedded as
    `<!-- x402-tx:0x... -->` comments. See
    docs/decisions/0006-x402-wallet.md (to be written).

  - People graph: if scale warrants, adapt the 7-pass cross-linker
    from jibot, scoped to whatever people have accumulated
    organically.

  - Vector search: only if grep stops finding things. At that
    point, QMD-style local index over `vault/` is the move.

## What we explicitly will not do

  - Adopt Amplifier as a runtime. Steal patterns (recipe schema,
    model-role indirection, skills layout). See
    docs/decisions/0003-amplifier-steal-dont-adopt.md.

  - Build a multi-user vault. Sabnyx is single-operator. A team
    surface is a projection of the vault (read-only), not a fork.

  - Write a custom markdown renderer. The vault is readable in any
    editor, any IDE, GitHub, Obsidian, Logseq. That's the point.
