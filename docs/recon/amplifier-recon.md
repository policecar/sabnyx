# Amplifier Ecosystem Recon (for sabnyx)

Scope: ~/src/joi/{amplifier, amplifier-bundle-recipes, amplifier-bundle-skills,
amplifier-bundle-routing-matrix, amplifier-module-tool-search,
amplifier-module-tool-skills, amplifier-module-loop-streaming}.
Date: 2026-04-22.

## WHAT_IS_AMPLIFIER

Amplifier is a **modular AI-agent runtime + CLI** from Microsoft MADE:Explorations
(github.com/microsoft/amplifier). The `amplifier` repo here is an umbrella
package whose only deps are `amplifier-core` (the kernel) and `amplifier-app-cli`
(reference CLI) — see amplifier/pyproject.toml lines 11-14, 28-30.
amplifier/docs/MODULES.md is the authoritative component catalog.

It is simultaneously:
- a **runtime kernel** (`amplifier-core`: `ModuleCoordinator`, `HookRegistry`,
  `events`, `message_models` — see imports in
  amplifier-module-loop-streaming/amplifier_module_loop_streaming/__init__.py:17-33);
- an **orchestrator framework** (orchestrator modules: loop-basic, loop-streaming,
  loop-events implement the agent execution loop);
- a **plugin/distribution system** (modules are uv-installable Python packages
  registered via entry-point `amplifier.modules` — see
  amplifier-module-tool-search/pyproject.toml:14-15).

So: framework + runtime + orchestrator + plugin host. The CLI is one face;
"web, mobile, voice, A2A" are explicitly future faces (amplifier/README.md:17).
Status: research demo, "early development, may change significantly"
(amplifier/README.md:6). Umbrella version 0.1.0; core >=1.0.10.

## RECIPE_INVENTORY

The abstractions, in code:

- **Module** = Python package + entry-point `amplifier.modules: name = pkg:mount`.
  `mount(coordinator, config)` registers tools/hooks/orchestrators on the kernel.
  Types: orchestrator | provider | tool | context | hooks (MODULES.md:80-141).
- **Bundle** = a directory with `bundle.md` (YAML frontmatter `bundle:` + `includes:`
  + markdown body) plus subdirs: `behaviors/`, `context/`, `agents/`, `modules/`,
  `skills/`, `routing/`, `examples/`, `templates/`. Composable; includes can be
  `git+https://...` URLs. See amplifier-bundle-recipes/bundle.md:1-15,
  amplifier-bundle-skills/bundle.md, amplifier-bundle-routing-matrix/bundle.md.
- **Behavior** = YAML in `behaviors/` declaring which tool modules to mount and
  with what config (e.g. amplifier-bundle-skills/behaviors/skills.yaml).
- **Recipe** = declarative YAML executed by the `tool-recipes` module
  (amplifier-bundle-recipes/modules/tool-recipes/). Schema 1.3.0
  (docs/RECIPE_SCHEMA.md): top-level name/description/version/context/steps;
  steps may be agent-delegation steps (`agent`, `mode`, `provider`, `model` or
  `model_role`, `prompt`, `output`, `timeout`, `requires_approval`) or bash
  steps (`type: bash`); supports `{{var}}` interpolation, foreach (parallel),
  conditional execution, recursion + rate-limit guards.
- **Routing matrix** = curated YAML mapping semantic `model_role` (coding,
  reasoning, fast, vision, …) → ranked provider/model candidates; the
  `hooks-routing` module resolves at session start
  (amplifier-bundle-routing-matrix/README.md:34-77, routing/{balanced,quality,
  economy,anthropic,openai,gemini,copilot}.yaml).
- **Skill** = Agent Skills spec package (SKILL.md + refs) loaded by `tool-skills`
  with progressive disclosure (metadata→content→refs); supports `auto_load`,
  fork execution, shell preprocessing
  (amplifier-module-tool-skills/README.md:9-24).

Recipes shipped (examples/, ~30 files) — generic engineering only:
simple-analysis, code-review-comprehensive, comprehensive-review,
security-audit-comprehensive, test-generation-comprehensive,
dependency-upgrade(+staged), conditional-workflow, parallel-multi-perspective,
multi-file-analysis, multi-level-python-code-analysis,
repo-activity-analysis, multi-repo-activity-report, feature-announcement,
session-extract, session-verify, attractor (seed→scaffold→factory→report),
+ context-intelligence/ subtree (foundation/compression/synthesis/
verification/tier1-3 ≈ 27 small recipes).

## ADOPT_OR_STEAL

**Mapping to jibot pipelines (Q4):** *zero direct hits.* `knowledge-intake`,
`meeting-extract`, `reweave`, `capture-to-jibrain`, `people-{harvest,enrich,
review}`, `book-ingest`, `talk-{builder,refresh,to-gamma,to-slides}` are
**jibot-internal sprite/agent names**, not recipes shipped in
amplifier-bundle-recipes (verified by grep — only ai-agent-learnings/
architecture.md mentions them). They are also not Amplifier modules. The
existing bridge (jibot-3/scripts/amplifier_bridge.py:1-13, 28) just imports
`amplifier.skills.gmail/google_calendar` from a user-local Amplifier checkout
at ~/amplifier — i.e. Joi reuses Amplifier's *skills layer* for Google APIs,
not its recipe engine.

Structural analogues (steal these as templates):
- knowledge-intake → multi-file-analysis.yaml (parallel foreach over inputs)
- meeting-extract → simple-analysis-recipe.yaml (extract→identify→report 3-step)
- reweave → context-intelligence/verification/progressive-iteration.yaml +
  adversarial-verification.yaml (iterative consistency passes)
- triage / capture-to-jibrain → conditional-workflow.yaml (router on classification)
- people-harvest/enrich/review → dependency-upgrade-staged-recipe.yaml
  (5 stages with `requires_approval` gates) — the closest pattern match
- book-ingest → attractor/attractor.yaml (seed→scaffold→factory→report)
- talk-builder / talk-to-gamma|slides → feature-announcement.yaml (3-step
  human-first artifact generation)

**Lock-in (Q5).** Adopting Amplifier = adopting (a) `amplifier-core`'s
ModuleCoordinator/HookRegistry/event-bus API, (b) the bundle.md DSL with
`@ns:context/...` reference syntax, (c) the recipe YAML schema parsed by
`tool-recipes`, (d) the entry-point convention. Modules and bundles are
git-URL-installable, so distribution is light, but **authoring against the
DSLs is the real lock-in**. Module API surface is small (one `mount()` fn);
runtime API is larger and pre-stable. CLI shell-out integration (just call
`amplifier tool invoke recipes …`) is genuinely thin and reversible.

## CONCRETE_RECOMMENDATION

For sabnyx (solo, early, vault-centric), **steal patterns; do not adopt the
runtime yet**. Specifically:

1. Copy the **recipe YAML schema** (RECIPE_SCHEMA.md) as inspiration for
   sabnyx's pipeline DSL — it's the most valuable artifact. Keep your own
   minimal executor (≤300 LOC) so you can evolve the schema.
2. Copy the **model_role indirection** (routing-matrix/README.md:34-77) — best
   abstraction in the whole stack; trivial to reimplement.
3. Copy the **bundle/behavior composition** idea but skip the markdown-with-
   frontmatter format; plain YAML is enough for one user.
4. Copy the **Agent Skills layout** (SKILL.md + progressive disclosure) —
   it's a public spec (agentskills.io), not Amplifier-specific.
5. If/when you need Google APIs (Gmail/Calendar) and don't want to write them,
   shell out to Joi's `amplifier_bridge.py` pattern — that's already proven and
   reversible.
6. Re-evaluate full adoption after amplifier-core hits a stable 1.x with
   non-research framing and after sabnyx's pipeline shapes are concrete
   (≥3 working pipelines). Until then the umbrella's "may change significantly"
   warning is disqualifying for a personal vault you intend to keep for years.

Bottom line: Amplifier is a real platform and the abstractions are good, but
adoption today is a research-grade commitment for a marginal CLI win.
Steal the schemas, keep the runtime your own.
