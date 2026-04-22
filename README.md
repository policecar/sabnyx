# sabnyx

A personal knowledge vault modeled on Joi Ito's jibot architecture.
Three tiers (intake -> atlas -> domains), YAML frontmatter as contract,
append-only event ledger, agent-operable.

Substrate: plain git + markdown. Obsidian-compatible, Obsidian-optional.
No database dependency at stage 0. Single-user for now; designed to
extend to a small circle, and eventually a public/agent-facing surface.

## Read in this order

  SPEC.md      the contract: tiers, types, frontmatter, boundaries
  ROUTING.md   the decision tree: where does a given piece of content go
  AGENTS.md    operator instructions for humans and LLMs on this vault
  docs/roadmap.md   stages 0 through 3, including ZK provenance and x402

## Layout

    vault/              the knowledge itself
      intake/           disposable queue; new material lands here
        .observations/  agent-logged signals (gap, contradiction, ...)
      atlas/            durable, typed, promoted knowledge
        concepts/       patterns, ideas, mental models
        references/     external resources: papers, bookmarks, tools
        ideas/          things I might want to do
      domains/          graduated areas with their own schema (empty for now)
      _review/          operational, never knowledge

    ops/
      events.jsonl      append-only ledger (the substrate)
      knowledge-log.md  human-readable provenance (Gate 3)
      schemas/          pydantic: frontmatter, envelope, event
      scripts/          new_intake, triage, promote, lint, health, ledger

    adapters/           interface normalizers (NormalizedEnvelope)
      cli.py            first interface: shell

    docs/
      recon/            reuse assessments of Joi's open-source repos
      decisions/        architectural decision records
      roadmap.md

## Day-to-day

    uv sync                                           # install deps once
    uv run sabnyx new --type thought                  # create an intake file
    uv run sabnyx lint                                # gates 1, 2, 4
    uv run sabnyx triage                              # review intake queue
    uv run sabnyx promote vault/intake/<file>.md      # -> atlas
    uv run sabnyx health                              # orphans, links, drift

## Status

Stage 0 scaffolding. See docs/roadmap.md for what comes next.

## Credits

Architecture directly adapted from Joi Ito's jibot
(https://jibot.md/learnings/architecture). The routing decision tree,
three-tier discipline, reweave pass, observations mechanism, seven
pipeline gates, and normalized envelope pattern are all his.
Recon notes on his open-source repos live in docs/recon/.
