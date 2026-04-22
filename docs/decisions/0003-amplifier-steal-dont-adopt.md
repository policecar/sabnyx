# ADR 0003 — Amplifier: steal patterns, do not adopt runtime

Date: 2026-04-22
Status: accepted

## Context

The jibot architecture document describes recipes, sprites, and a
multi-agent framework. It cites Microsoft's Amplifier
(github.com/microsoft/amplifier) as the operating substrate.

Recon (docs/recon/amplifier-recon.md) found:

  1. Amplifier is a Python framework + runtime kernel + orchestrator
     + plugin host + CLI, explicitly labeled "may change
     significantly" (v0.1.0 umbrella, v1.0.10 core).
  2. None of the jibot pipeline names (knowledge-intake, reweave,
     meeting-extract, capture-to-jibrain, people-*, book-ingest,
     talk-*) exist as recipes in amplifier-bundle-recipes. They're
     jibot-internal sprite/agent names.
  3. Joi's actual integration (jibot-3/scripts/amplifier_bridge.py)
     uses Amplifier's *skills layer* for Gmail/Calendar only — not
     the recipe engine.
  4. Bundle DSL (`bundle.md`) + recipe schema + ModuleCoordinator API
     are real lock-in if adopted.

## Decision

Do not adopt Amplifier as a runtime. Steal the useful shapes:

  - Recipe schema (declarative YAML, stage-name + inputs + outputs)
  - model_role indirection (prompts reference "coding", "reasoning",
    "fast"; the runtime resolves to an actual model)
  - agentskills.io-style SKILL.md layout for stage-3 skill loading

Keep the door open to shell out to Amplifier CLI for specific
operations (the `amplifier_bridge.py` pattern) if a recipe there
genuinely gives us leverage.

## Consequences

+ Zero runtime commitment at stage 0. Sabnyx is 7 Python files.
+ If Amplifier stabilizes and becomes the standard, adoption later
  is mechanical — our schemas are a superset of theirs.
- We write our own recipe executor at stage 2 (small).
- We don't get Amplifier's built-in tools for free. That's fine;
  our tools are a thin layer over the CLI and the ledger.
