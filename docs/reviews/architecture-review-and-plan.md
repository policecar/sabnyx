# Architecture review & implementation plan

Date: 2026-06-12
Status: historical — the plan below shipped as milestone 1
(see docs/plans/build-out-plan.md); kept for the reasoning

A review of the current sabnyx codebase against the two source documents,
and a plan for building it out into a working jibot-like system.

> Note on sources: `https://jibot.md/learnings/architecture` is gated (HTTP
> 403 to anonymous fetchers). This review works from the faithful local
> adaptation in `SPEC.md` (which annotates its `DEVIATION:`s from jibot) and
> from Karpathy's "LLM wiki" gist, which describes the same pattern in its
> most distilled form.

## TL;DR

**Continue off this base. Do not restart.** The substrate is genuinely
well-built — correct decisions, faithfully reasoned, clean code, passing
tests. But sabnyx today is the *easy 30%*: deterministic plumbing. The
*defining feature* of the jibot/Karpathy pattern — an LLM that ingests a
source and rewrites N pages, answers a query by writing a page, and reweaves
by finding semantic links — **is not built at all.** There is no LLM in the
loop anywhere (verified: zero `anthropic`/LLM references in 1066 LOC). The
work ahead is *additive*, not *corrective*, which is exactly why continuing
beats restarting.

The one real course-correction: the roadmap's priorities are inverted. It
gold-plates the substrate (ZK proofs, x402 wallet, USDC payments at stage 3)
while deferring the agentic engine — the thing that makes this a knowledge
system rather than a markdown linter — to stage 2 or later. Flip that.

## 1. What the source pattern actually requires

Strip both documents to their load-bearing claims:

**Karpathy's three layers**
- **Raw sources** (immutable): the curated corpus, single source of truth.
- **The wiki** (LLM-owned): summaries, entity/concept pages, cross-refs.
  The LLM creates and maintains this *entirely*.
- **The schema** (config, e.g. `CLAUDE.md`): defines structure and
  conventions. This is what "transforms the LLM from a generic chatbot into
  a disciplined maintainer."

**Karpathy's three operations**
- **Ingest**: process a new source *once*, updating 10–15 wiki pages — not
  just filing the document.
- **Query**: ask against the compiled wiki; *good answers become new pages*
  rather than evaporating into chat history.
- **Lint**: periodically health-check for **contradictions, stale claims,
  orphans, missing cross-references**.

**The whole economic argument**: "LLMs don't get bored, don't forget to
update cross-references… maintenance cost is near zero." The human curates
sources and asks questions; *the LLM does the bookkeeping.* Below ~100k
tokens, loading relevant pages into context beats RAG/vector search.

**What jibot adds** (per SPEC): the three-*tier* pipeline
(intake → atlas → domains), frontmatter-as-contract, the **reweave** pass
(after each promotion, walk backward: what existing pages should now link
to or be corrected by this one?), the **observations** mechanism (cheap
logged signals about the system itself), seven **gates**, the
**NormalizedEnvelope** (interface-agnostic input), and an event ledger.

The unifying insight: **the schema is the central artifact, and the LLM is
the engine.** Everything else is plumbing around those two.

## 2. Assessment of current sabnyx

### What's genuinely good (keep all of it)

- **Contract-first design.** `schemas.py` (pydantic, `extra="forbid"`) +
  `SPEC.md` *is* Karpathy's "schema layer," done right. The `description`
  field as the load-bearing search index (min 30 chars, ≠ title) is exactly
  the correct instinct for a grep-first retrieval model.
- **Three-tier discipline** mapped cleanly to the filesystem, enforced by
  `lint` Gate 4 (containment) and a pre-commit hook.
- **The ledger** (`ledger.py`): append-only, content-hashed, replayable,
  ~80 LOC. ADR-0002's "jsonl now, sqlite/opsctl later, schema is the stable
  boundary" is the right call. The `plane` field is already carried so the
  four-plane upgrade is mechanical.
- **Reweave is *anticipated* correctly.** `promote` already emits a
  `reweave.proposed` *intent* event carrying the exact prompt. The seam is
  cut; nothing executes through it yet.
- **Marker blocks + HTML-comment round-trip** for non-destructive agent
  edits — this is the right primitive for "LLM edits a section without
  clobbering my prose," and is a prerequisite for safe reweave.
- **Engineering hygiene:** 8 passing tests, ruff/ty configured, thoughtful
  ADRs, honest recon docs ("steal, don't adopt" Amplifier; mirror — don't
  fork — nanoclaw). Decisions are *reasoned*, not cargo-culted.

### The gap (this is the actual system)

Every operation that requires intelligence is a deterministic stub:

| Operation | Karpathy/jibot intent | sabnyx today |
|---|---|---|
| **ingest** | process source → rewrite 10–15 pages | **does not exist** |
| **query** | answer from wiki → answer *becomes a page* | **does not exist** |
| **triage** | LLM classifies & routes intake | hardcoded `if type == "bookmark"` string |
| **promote** | move + **execute** reweave | moves file, *emits a prompt nobody runs* |
| **reweave** | semantic backward-linking, contradiction detection | roadmapped (stage 2), unbuilt |
| **lint** | contradictions, **stale claims**, orphans | frontmatter schema check only |

The semantic gates (3 materia, 5 currency, 6 cost, 7 verification) are
report-only or absent. `lint` validates YAML shape — it cannot detect a
contradiction or a stale claim, which is precisely what Karpathy's lint *is
for*. There is **no retrieval/assembly step** that gathers relevant pages
into a context window for any LLM call, because there are no LLM calls.

So: sabnyx has faithfully built the skeleton (their word) and stopped at the
point where the value begins. This is defensible as "stage 0" — but it means
**none of the jibot value proposition is yet demonstrable.** A user today
gets a typed markdown linter with a nice ledger, not a self-maintaining wiki.

### Secondary observations

1. **Missing "raw sources" layer.** Karpathy keeps raw sources *immutable*
   as the source of truth; sabnyx's `intake/` is explicitly *disposable* and
   folds provenance into frontmatter (`source_url`, etc.) rather than
   retaining the raw artifact. For bookmarks that's fine; for papers/long
   notes that the LLM will re-summarize, losing the original is a real loss.
   Worth a decision (see plan §Phase 1).
2. **Roadmap priority inversion** (the main correction). Stage 3 carries ZK
   provenance (Lemma), an x402/USDC wallet, and a public agent-paid API —
   substantial machinery — while the core LLM loop sits in stage 2. The
   exotica should move *behind* a working ingest/query/reweave loop, not
   ahead of it.
3. **`triage` and `promote` are separate manual steps.** That's fine for a
   human, but Karpathy's "ingest" is *one* operation that lands knowledge in
   the right place and reweaves. The CLI verbs should eventually collapse
   toward `ingest` and `ask`.

## 3. Verdict: continue, with a re-ordered roadmap

Restarting would mean rebuilding correct plumbing to arrive at the same
seams that already exist (`reweave.proposed` intent, marker blocks, the
NormalizedEnvelope, the contract schemas). The deficiencies are things to
*add through existing seams*, not foundations to tear out. Continue.

The rest of this document is the build plan.

## 4. Implementation plan

Principle: **make the LLM loop real before anything else.** One end-to-end
vertical slice (ingest → page-writing → reweave → query) on the existing
substrate, then widen.

### Phase 1 — the model boundary + raw sources (small, foundational)

- Add an `anthropic` dependency and a thin `sabnyx/llm.py`: one
  `complete(system, messages, tools=…)` wrapper, model id from config, with
  **prompt caching** on the system prompt (SPEC + ROUTING are stable and
  will be sent on every call — cache them). Default to the latest Claude
  model; keep the id in one config constant.
- Introduce a `model_role` indirection (per ADR-0003: "coding"/"reasoning"/
  "fast" → concrete model). Trivial dict now; swappable later.
- **Decide the raw-sources question** (write an ADR). Recommended: add
  `vault/sources/` for retained immutable artifacts (fetched article text,
  paper PDFs/markdown), referenced from intake/atlas by id. This closes the
  one structural gap vs. Karpathy without disturbing the tiers.
- Every LLM call emits ledger events: an `intent` (the prompt/plan) and an
  `action` (what it did). This is what makes the agent *auditable* — the
  ledger's whole point — and it costs nothing to do from day one.

### Phase 2 — `sabnyx ingest` (the core operation)

This is the heart of the system; build it first and well.

1. Capture the source into `vault/sources/` (raw, immutable) and create the
   intake stub (reuse existing `new` logic).
2. **Retrieve context**: assemble the candidate-relevant slice of the
   vault. Start with grep over `description` fields + filenames + tags
   (Karpathy: under ~100k tokens this beats vectors). Feed the matched pages
   into the model.
3. **LLM pass**: given (raw source, SPEC, relevant existing pages), the
   model proposes a *changeset*: new atlas pages to create, existing pages
   to update (as marker-block edits), and observations to log. It does **not
   free-write over prose** — edits are confined to marker blocks; new pages
   go through the validated `promote` writer.
4. **Apply** the changeset through the existing validated writers
   (`fm.write` + post-write `fm.load` rollback), append ledger events, run
   `lint` on touched files, and surface a diff for human approval (keep a
   human gate until trust is earned; `requires_approval` per the Amplifier
   recipe pattern).

This collapses `new → triage → promote → reweave` into one intelligent
operation, while *reusing* every deterministic primitive already written.

### Phase 3 — `sabnyx reweave` (execute the seam that already exists)

`promote` already emits `reweave.proposed`. Build the consumer:
- Replay the ledger for pending `reweave.proposed` intents (or take a file
  arg).
- For the target page, LLM finds (a) existing pages that should link to it
  → propose additions to *their* `Related` marker blocks; (b) contradicting
  claims → write `category: contradiction` observations; (c) bridge concepts.
- Apply only marker-block edits + observations. Emit `reweave.completed`.

Now the "connect 5 existing concepts beats writing 5 new ones" principle
(ROUTING.md) is mechanized.

### Phase 4 — `sabnyx ask` (query → page)

- Retrieve relevant atlas pages (same assembly as ingest).
- LLM answers grounded in those pages, citing `[[wikilinks]]`.
- If the answer is durable and novel, offer to promote it to an atlas page
  (Karpathy: "good answers become new pages"). This closes the loop where
  the wiki *compounds* from use, not just from ingestion.

### Phase 5 — semantic `lint` / full gates

Upgrade `lint`/`health` from shape-checking to meaning-checking:
- **Contradiction scan** (Gate, semantic): pairwise/cluster LLM check across
  atlas, emitting observations. Grep-narrowed first, LLM-confirmed.
- **Stale-claim / currency** (Gate 5): flag dated `last_verified` pages and,
  for references with `source_url`, optionally re-fetch and diff.
- Wire materia (3), cost (6), verification (7) into the `health` dashboard.

### Phase 6 — second interface (prove the envelope)

The `NormalizedEnvelope` abstraction is only justified once a second source
exists. Add a Telegram (or email) adapter that maps inbound messages →
`content.submit` envelopes → `ingest`. "sabnyx, remember this: …" This is
the cheapest way to validate that the substrate's central abstraction earns
its keep.

### Later (unchanged from roadmap, but explicitly *after* the loop works)

`domains/` graduation, scheduled daemon (mirror nanoclaw contracts),
public markdown surface, and — last — the ZK/x402 machinery. These are real
and fine; they just must not precede a working ingest/ask/reweave loop.

## 5. Concrete next step

Implement **Phase 1 + a minimal Phase 2 `ingest`** as the first vertical
slice, behind a human-approval gate, with full ledger instrumentation. That
single slice converts sabnyx from "typed markdown linter" to "self-
maintaining wiki" and validates every existing seam under real load. If it
holds, the rest of the plan is widening, not rearchitecting.

## 6. Reconciliation with the full jibot document

The sections above were written against `SPEC.md` (the local adaptation)
plus the Karpathy gist, because the jibot source was gated. The full source
document is now available. Three takeaways:

1. **The SPEC adaptation is faithful.** Three tiers, frontmatter-as-contract,
   the `description` field, the routing tree, the reweave pass, observations,
   the seven gates, and the NormalizedEnvelope all match the source. The
   `DEVIATION:` annotations (dropping `person`/`organization` types at stage
   0) are honest and reasonable. **The "continue, don't restart" verdict is
   reinforced** — sabnyx is a correct minimal subset, not a misreading.

2. **The source's own closing advice validates sabnyx's priorities:** *"the
   most important thing to get right is the routing decision tree and the
   description field. Everything else follows from those two."* sabnyx has
   both, done well. And the source's implementation checklist states **items
   1–5 give 80% of the value** — (1) three-tier dirs, (2) frontmatter +
   description, (3) routing tree, (4) triage, (5) health monitoring. sabnyx
   has 1–3 fully and 4–5 in *deterministic* form. So structurally sabnyx is
   most of the way through the high-value tier; what's missing is the
   *intelligence* inside triage/health and everything from item 6 on
   (reweave, observations-as-signal, currency, search, etc.).

3. **One correction to my earlier critique.** I called the deterministic
   `triage`/`promote` "stubs." The full document shows a deterministic tier
   is *intended*, not a placeholder: jibot's **heartbeat** runs a
   *conservative auto-promote* path with **no AI** (six deterministic gates +
   a trusted-source allowlist), and only escalates to **Claude AI triage**
   when ≥5 items are pending or at scheduled hours. So sabnyx's deterministic
   triage is the legitimate first tier; the gap is the *second* (LLM) tier
   layered above it — not that determinism was the wrong call.

### Concrete design details to fold into the plan

The full source supplies specifics worth adopting:

- **Two-speed triage** (revises Phase 2). Keep the deterministic
  auto-promote (promotable type, not temporal, required fields,
  description ≥30 chars, no duplicate, trusted source — all 6 must pass);
  add an **LLM triage** path that batches and fires on a threshold/schedule.
  Add a **trusted-source allowlist** to config. Cheaper and safer than
  routing everything through an LLM.
- **Type-specific extraction depth** (Phase 2 `ingest`). Classify the source
  by word count + URL pattern and scale extraction effort: shallow (<500w),
  standard (500–2,500), standard-plus (2,500–5,000), deep (>5,000w / arXiv,
  section-by-section). Avoids burning tokens on thin sources and
  under-extracting dense ones — pairs directly with `model_role` routing.
- **Model routing = rule-based scorer** (refines Phase 1). jibot's router is
  a ~14-dimension weighted scorer over the **user message only** (not the
  system prompt) → Haiku/Sonnet/Opus, with `[opus]`/`[haiku]` overrides;
  reported ~80% cost savings, <1ms, no ML. Implement `model_role` this way
  rather than as a static dict.
- **Semantic search specifically for reweave** (refines Phase 3 and ADR-0001's
  "grep until it breaks" stance). The source is explicit that at ~4,300 files
  grep/wikilink traversal *fails for discovery* — reweave's contradiction
  scan and bridge-link finding run on **vector/semantic search** to surface
  files that share no vocabulary. Karpathy's "<100k tokens, skip vectors"
  holds for *general retrieval*, but **reweave quality needs semantic search
  earlier than general Q&A does.** Plan a local index (QMD-style: keyword +
  vector + deep) around Phase 3/5, not "someday."
- **Knowledge-log is a grep contract** (Phase 5, Gate 3). Keep
  `ops/knowledge-log.md` append-only with a parseable line format
  (`# [ISO-timestamp] operation | title` + metadata) so
  `grep "ingest" | tail` works. sabnyx's current free-text bullets should
  adopt this shape.
- **`book-ingest` variant** (later). Chapter-level extraction (PyMuPDF TOC),
  each chapter **updating existing atlas pages** rather than only creating
  intake files — "compilation over indexing." A natural specialized `ingest`
  once the base loop works.
- **`ask` → page is jibot's `capture-to-jibrain`** (confirms Phase 4): the
  "file query answers back into the wiki" pattern is named explicitly.

### The dimension my plan under-weighted: persistent identity

The source's second half reframes the endgame: **jibot is a 23-year
persistent conversational identity** (same voice/memory across Twitter,
Slack, a MUD NPC, web, email, Signal/Telegram/WhatsApp, …) backed by the one
vault. sabnyx's roadmap has the *pieces* (Telegram adapter, public markdown
surface) but not the *framing* that they should present **one consistent
persona over many channels**, each a profile on a gateway. This doesn't
change the build order — the vault and the loop still come first — but it's
the "why" behind Phase 6, and worth stating as the north star.

### Alignment confirmed (no change needed)

- **Team surface as read-only projection.** sabnyx's "no multi-user vault;
  team surface is a read-only projection" matches jibot's Onyx deployment
  exactly ("vault is source of truth; Onyx is a read-only projection").
- **Four-plane ledger.** sabnyx's `LedgerEvent.plane` already mirrors
  opsctl's intent/event/projection/action; "segments are authority, SQLite
  rebuilds" matches ADR-0002.
- **Steal-don't-adopt Amplifier.** The source confirms jibot *runs on*
  Amplifier, but the recon's conclusion (steal recipe schema + model_role +
  skills layout; don't commit the runtime) stays sound for a solo vault.

### Suggested "not yet" list (borrowed from the source)

jibot's own "What We Don't Have Yet" is a good honesty template: adversarial
verification / confidence scoring, confidence *decay* (make `last_verified` a
decay function, not just a flag), cross-vault federation, and agent-*initiated*
follow-ups. Worth recording as explicit non-goals so they don't masquerade as
oversights.
