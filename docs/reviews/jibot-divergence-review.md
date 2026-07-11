# jibot divergence review

Date: 2026-07-11
Status: current after PR #15 hardening

Where sabnyx still differs from its jibot/Switchboard inspiration after the
milestone-1 build-out and the PR #15 codebase hardening pass. This is a
directional review, not a mandate to copy jibot: solo scale, the plain-file
contract, and provenance-based trust remain the deciding constraints.

Sources:

- `docs/spec/jibot.md`, the repository-local mirror of the source architecture;
- Joi Ito's April 2026 "What I Learned from Karpathy's LLM Wiki" gist;
- the repository contract: SPEC.md, ROUTING.md, and AGENTS.md;
- the post-PR #15 implementation, including the independent follow-up fixes.

The source mirror covers jibot through 2026-06-07. Later jibot changes are not
evidence about the current contract unless the mirror is refreshed.

## Verdict

sabnyx is a faithful, intentionally smaller implementation of jibot's
knowledge layer. PR #15 closed several gaps an earlier draft of this review
identified: health now detects dead ends, promotion uses the shared knowledge
log, pending-observation counts and scan dedupe respect status, atlas removals
project consistently in Python and Rust, and slug collisions resolve through
one policy.

Three live concerns remain:

1. **Existing-page updates are manual.** ROUTING's highest-value branch says to
   enrich an existing atlas page instead of filing a duplicate. Ingest can
   detect overlap but cannot queue or preview a source-backed amendment.
2. **Observations have status but no lifecycle command.** Producers create
   useful signals and readers increasingly filter pending status, yet the
   operator has no supported way to resolve or archive them. Gate 7 therefore
   trends toward accumulated work without a dispatch surface.
3. **The loop records events but does not yet learn from them.** A small intake
   queue can starve below the count threshold, and decision outcomes are not
   precise enough to support trustworthy quality analysis.

One earlier "gap" is instead an intentional divergence:

- **Intake is typed by source form** (`bookmark`, `paper`, `note`, `thought`),
  while atlas is typed by knowledge meaning (`concept`, `reference`, `idea`,
  entities). This keeps unattended promotion conservative. An LLM-proposed
  destination kind may inform triage, but must not silently turn a model
  judgment into a deterministic promotion gate.

## Checklist against jibot's 17 items

| # | Item | sabnyx | Current assessment |
|---|------|--------|--------------------|
| 1 | Three-tier directories | yes | intake/atlas/domains plus immutable sources |
| 2 | Frontmatter + description | yes | pydantic, extra-forbid, 30–200 character descriptions |
| 3 | Routing decision tree | partial | contract complete; existing-page update arm remains manual |
| 4 | Triage process | yes | deterministic gates plus batched LLM tier |
| 5 | Health monitoring | yes | orphans, dead ends, links, currency, provenance, queue cost |
| 6 | Reweave pass | yes | intent-driven, marker-block-only; born-in-atlas pages need a sweep |
| 7 | Observations mechanism | partial | producers/readers exist; lifecycle command does not |
| 8 | Seven pipeline gates | yes | 1/2/4 hard; 3/5/6/7 reported |
| 9 | Currency tracking | yes | reference freshness and `last_verified` |
| 10 | Workspace boundaries | yes | role convention, lint containment, narrow write paths |
| 11 | Multi-machine subset sync | deliberate no | private git data repo, all-or-nothing per clone |
| 12 | GTD/task integration | deliberate no | action management is outside the knowledge-vault charter |
| 13 | Domain graduation | partial | criteria are explicit in ROUTING; migration remains deliberate/manual |
| 14 | Keyword/vector/deep search | partial | grep default, vector opt-in, no persistent/deep index |
| 15 | Contact intelligence | deliberate no | lightweight entity pages and aliases only |
| 16 | Operational control plane | partial | query/project/supervise/verify; no review analytics yet |
| 17 | Continuous improvement review | no | event substrate exists; valid outcome metrics do not yet |

The missing pieces are not equal. Existing-page updates change knowledge
behavior; observation lifecycle restores an already-declared contract;
feedback metrics should wait until outcomes are recorded precisely.

## Confirmed alignment

The following are implemented and need no divergence work:

- Three tiers with hard containment and a separate immutable raw-source layer.
- Description-first retrieval and bounded context assembly.
- Reweave link priorities, marker-only writes, and contradiction observations.
- Two-speed promotion with a provenance trust gate.
- Ask capture through CLI, Telegram, and R1 over one services layer.
- Type-specific extraction depth and role-based model routing.
- Normalized envelopes and fail-closed remote actors.
- One append-only, cross-language-compatible ledger with verification.
- Read-only halbor projection: the public face cannot mutate the vault.
- Dead-end detection, unified promotion provenance, compensating atlas removal,
  and consistent slug/alias resolution from PR #15 and its follow-up.

## Deliberate divergences that remain sound

### Private git rather than Syncthing subsets

At solo scale, a separate private data repository is simpler and auditable.
The boundary is coarser than jibot's per-machine subsets: every clone receives
the whole vault. That is acceptable only while every clone is equally trusted.

### Lightweight entities rather than identity resolution

Entity pages, aliases, gap detection, and collision reporting cover the vault's
current need. Senzing/Nyne-scale identity resolution remains an explicit
non-goal until actual collision volume proves otherwise.

### Static projection rather than a team search service

halbor preserves the important direction: vault to face, never face to vault.
Onyx-scale machinery would add operations without improving the solo contract.

### Source-form intake typing

Form is observable at capture time; destination meaning is a judgment. Keeping
the latter behind triage prevents an LLM-produced `kind` field from widening
unattended writes to atlas. SPEC records this as a deliberate deviation.

## Live gap 1: existing-page updates are manual

ROUTING begins with "does this already exist? update it." In code, ingest sees
atlas descriptions but can only create intake files, observations, and ledger
events. Reweave edits relationship markers, not claim content.

The diagnosis is real, but a permanent autonomous update subsystem is too large
to adopt on analogy alone. Appending amendments can become a second sediment
layer rather than true compilation. The safe next move is an experiment:

- ingest may emit a validated source/page update intent;
- a separate command reads the full page and relevant source context;
- it previews a bounded marker-block amendment;
- every application is interactive in v0;
- usefulness and consolidation burden are reviewed after real examples.

See `docs/plans/atlas-update-intents-experiment.md`.

## Live gap 2: observations need a lifecycle

Observation files already declare `pending | promoted | archived`, but there is
no supported transition command. PR #15 corrected several readers to count only
pending signals; reweave context and operator dispatch still need a single
consistent rule.

The first implementation should be deliberately boring:

- list/filter observations;
- resolve or archive one while preserving its body;
- ledger the transition;
- make every reader pending-only by default;
- record an explicit target on new gap observations before attempting any
  deterministic auto-resolution.

An LLM disposition pass and category-specific "promote" automation are deferred
until manual lifecycle data demonstrates a need.

See `docs/plans/observations-and-age-triage.md`.

## Live gap 3: starvation and trustworthy feedback

Count-only triage can leave four items waiting forever. An age-based OR trigger
is a small deterministic correction and belongs with observation lifecycle.

Quality analytics require more care. Existing `triage.decided` events do not
cover every proposal/outcome, and `applied=false` would not by itself mean a
human override: it can also mean duplicate, non-interactive execution, failure,
or budget exhaustion. Calling such ratios "accuracy" would manufacture a
metric rather than measure one.

The correct order is:

1. emit one stable proposal identifier and explicit outcome reason per item;
2. accumulate real outcomes;
3. report volumes and outcome rates without calling them accuracy;
4. add `opsctl review` only after enough data exists to shape the questions.

Cost tables, Telegram pushes, and automatic digest retention are deferred. They
introduce maintenance or external side effects before a demonstrated need.

See `docs/plans/feedback-telemetry.md`.

## Secondary gaps and tripwires

These are real but not current priorities:

- **Manual atlas births miss reweave.** Add a recent-frontmatter sweep if this
  occurs in practice; do not replace the intent queue.
- **Semantic scan truncation is silent.** Warn when the atlas exceeds the
  context budget before investing in a persistent vector index.
- **Domain graduation is manual.** ROUTING already defines 10+ related files,
  recurring schema, and long-term investment as criteria. Automation is not
  required until a real candidate exists.
- **Connection density has no trend.** Dead ends are now reported; longitudinal
  density can wait for review analytics.
- **Book ingest depends on update behavior.** Do not build it before the update
  experiment demonstrates a safe, useful amendment path.

## Where sabnyx is ahead

- Provenance trust prevents remote/third-party content from unattended atlas
  promotion.
- Unattended LLM work has a fail-closed call budget.
- Python and Rust share one byte-compatible ledger and verification command.
- Reconciliation explicitly treats git/files as truth and the ledger as shadow.
- Provider/model roles are centralized behind one structured-output boundary.
- R1 supplies a voice channel without a new knowledge write path.
- Raw captures survive failed/declined derivations and can be reused forever.

## Ranked next work

1. Observation list/resolve/archive plus pending-only readers.
2. Age-based triage trigger.
3. Experimental, interactive source-backed atlas update intents.
4. Precise per-item decision outcome events.
5. Reweave sweep for recently born-in-atlas pages, if manual births exist.
6. Scan-budget truncation warning.
7. Minimal ledger review after sufficient telemetry accumulates.

The governing rule is evidence before machinery. Jibot is a quarry: sabnyx
should steal the feedback patterns that solve observed solo-vault problems,
not inherit the surrounding fleet architecture.
