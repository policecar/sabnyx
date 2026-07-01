# SPEC — the contract

This document defines the rules every file in the vault must obey.
Agents and humans alike. If this document and the code disagree,
the code is wrong.

Adapted from jibot.md/learnings/architecture. Deviations are noted
inline with `DEVIATION:`.

## 1. Three tiers

    intake/    temporal, disposable, flat, a QUEUE
    atlas/    durable, typed, authoritative, SEARCHED
    domains/  deep, schema'd per topic, GRADUATED from atlas

The key insight: intake/ is disposable. Value lives in atlas/ and
domains/. Agents may write freely to intake/. Writes to atlas/
must go through triage.

## 2. Content types

### Intake types (temporal)

    bookmark         a URL captured for later
    paper            academic or technical reading
    note             longer-form text, imported or manual
    thought          original reflection, short or long
    meeting-extract  (reserved; not used at stage 0)

### Atlas types (durable)

#### Knowledge types (claim-bearing)

    concept    a pattern, idea, or mental model worth finding again
    reference  an external resource: paper, book, tool, website
    idea       something the operator wants to do or try

#### Entity types (lightweight reference scaffolding)

    person     a person the vault refers to
    org        an organization, company, or institution
    project    a named effort the vault tracks

Entity pages live under `atlas/people`, `atlas/orgs`, `atlas/projects`.
They carry the atlas schema plus an `aliases` list (alternate names the
page answers to). This is deliberately lightweight: entity pages and
aliases, NOT Senzing-scale identity resolution or a people CRM (those
stay explicit non-goals). The `entities` command surfaces gaps
(targets referenced N× with no page) and alias/slug collisions.

### Observation type (meta)

    observation  an agent-logged signal about the system itself

DEVIATION from jibot: we use `person`/`org`/`project` rather than jibot's
`person`/`organization`. Un-deferred in Phase 3 as the lightweight entity
layer above (stage 0 dropped them until the vault was big enough to need
one); large-scale entity resolution remains out of scope.

## 3. Frontmatter schemas

Every file MUST have YAML frontmatter. No exceptions. The `lint`
script enforces this and the git pre-commit hook blocks violations.

### Intake schema

    ---
    type: bookmark | paper | note | thought | meeting-extract
    description: "<30-200 chars: what is this and why would I search for it?>"
    source: manual | web | email | import | clipboard
    source_url: https://...               # required for bookmark, paper
    source_date: 2026-04-22               # ISO date of the source itself
    tags: []
    status: draft
    created: 2026-04-22T19:55:00Z         # ISO 8601 with TZ
    source_agent: manual | sabnyx-cli | sabnyx-ingest | sabnyx-ingest-web
                  | sabnyx-telegram | sabnyx-r1 | <agent-name>
                  # provenance-trust label: only operator-supplied local input
                  # (sabnyx-ingest) is trusted for unattended auto-promotion.
                  # Remote/third-party agents (-web, -telegram, -r1) are not.
    ---

### Atlas schema

    ---
    type: concept | reference | idea | person | org | project
    description: "<30-200 chars>"
    tags: []
    aliases: []                           # alternate names (entity types)
    status: final
    promoted_from: intake/<slug>.md       # null if born in atlas
    promoted_date: 2026-04-22
    last_verified: 2026-04-22             # required for time-sensitive types
    proof: null                           # Lemma ZK proof, populated later
    ---

### Observation schema

    ---
    type: observation
    category: gap | contradiction | connection | friction | structural
    observed: 2026-04-22
    source_agent: manual | sabnyx-cli | <agent-name>   # e.g. sabnyx-ingest,
                  # sabnyx-tend, sabnyx-scan, sabnyx-reweave, sabnyx-entities
    status: pending | promoted | archived
    involves: ["<file1.md>", "<file2.md>"]
    ---

## 4. The `description` field

The single most load-bearing field in the whole system. Minimum 30
characters. It differs from the title. It answers the question:
"what is this and why would I search for it?"

Good:

    "Oracle technology repositioned as clean data source for AI,
     not traditional on-chain usage"
    "Lawrence Lessig argues transparency creates cynicism without
     structural reform"

Bad:

    "A note about oracles"         too vague, just restates title
    ""                             missing — file is unfindable

A vault of 1000+ files cannot be read exhaustively. Descriptions
are the index. This is why Gate 1 exists.

## 5. Containment boundaries

    temporal (intake)    MUST NOT contain promoted authoritative content
    durable (atlas)      MUST NOT contain meeting notes, drafts, or session state
    operational (_review) MUST NOT be treated as knowledge
    raw (sources)        MUST NOT be treated as knowledge; write-once,
                         no frontmatter schema, excluded from lint (ADR 0005)
    private              out of scope at stage 0 (solo user)

Small boundary violations compound silently until the system is too
degraded to trust. The `lint` script enforces containment (Gate 4).

## 6. Naming

Files are named `<slug>.md` where `<slug>` is kebab-case, ASCII,
30-80 chars. No dates in filenames (use the `created` field). No
numeric prefixes. Slugs should read like a phrase, not a title.

Good:  `markdown-is-the-new-html.md`
Bad:   `2026-04-22-thoughts-final-v2.md`

Titles whose ASCII fold is empty (e.g. Japanese) slugify to
`untitled-<8-hex content hash>` so distinct titles never collide.

Observation files may include a date prefix because they are
time-stamped signals: `2026-04-22-gap-oracle-technology.md`.

## 7. Wikilinks

Internal references use `[[wikilink]]` syntax. The target is the
file slug without extension. Resolution is case-insensitive and
matches the nearest file under `vault/` (not `ops/` or `docs/`).

Broken wikilinks are reported by the `health` script but never
block commits (knowledge work must be allowed to outrun its
references).

## 8. Marker blocks (non-destructive updates)

Sections managed by agents must live between paired markers so
updates preserve surrounding prose:

    ## Related

    <!-- BEGIN sabnyx:related -->
    - [[concept-a]]
    - [[concept-b]]
    <!-- END sabnyx:related -->

Marker names are namespaced `sabnyx:<purpose>`. Never nest markers.
Stolen from obs-dailynotes; see docs/recon/obs-dailynotes-reuse.md.

## 9. External-ID round-trip

Proofs and receipts from external systems live as HTML comments
adjacent to the content they attest:

    <!-- lemma-proof:0xabc... -->
    <!-- x402-tx:0xdef... -->
    <!-- source-id:<external_system>:<id> -->

Tooling round-trips these without touching the prose. Also stolen
from obs-dailynotes. At stage 0 these are reserved syntax; stage 3
activates them.

## 10. Event ledger

Every mutation to the vault SHOULD append one event to
`ops/events.jsonl`. Schema:

    {
      "ts": "2026-04-22T19:55:00.000Z",
      "plane": "event" | "intent" | "action" | "projection",
      "kind": "<dotted.kind>",
      "actor": "<identifier>",
      "subject": "<file path or entity id>",
      "payload": {...},
      "content_hash": "sha256:<hex>"
    }

`content_hash` is SHA-256 of the canonical JSON of `payload`, sorted
keys, no whitespace. This pre-commits the ledger to Lemma-style
attestation without currently requiring it.

The ledger is APPEND-ONLY. Never edit a past line. To correct,
append a compensating event — e.g. `atlas.removed` (subject = the
file path) records a deliberate deletion, and `reconcile` honors it.

## 11. Gates (health checks)

Gate 1 INTENT        every file has `type` and valid `description`
Gate 2 PRECISION     frontmatter matches schema exactly (pydantic)
Gate 3 MATERIA       source provenance tracked (knowledge-log.md)
Gate 4 CONTAINMENT   tier boundaries respected
Gate 5 CURRENCY      last_verified present and fresh on dated types
Gate 6 COST          intake:atlas ratio monitored
Gate 7 VERIFICATION  orphans, broken links, pending observations tracked

Stage 0 enforces 1, 2, 4 hard (pre-commit). 3, 5, 7 are reported by
`health` with warnings. 6 is a dashboard metric; never blocks.

## 12. Agent contract

See AGENTS.md. In short: any agent operating on this vault must
(a) load SPEC.md and ROUTING.md first, (b) write only to workspaces
allowed by its role, (c) append one ledger event per mutation, and
(d) drop observations when it notices something structural.
