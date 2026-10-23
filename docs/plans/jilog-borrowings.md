# Plan: borrowings from jilog — deterministic signal hygiene

Status: proposed
Depends on: `observations-and-age-triage.md` (shares the `resolution` field
and the `observations()` reader; B2 needs its transition helper)
Source: `docs/recon/jilog-and-ai-agent-learnings.md`
Scope: Python only; no new LLM pass; every mutation ledgered

## Outcome

Make sabnyx's self-maintenance loop *idempotent over time*: the same signal
observed on many heartbeats is counted once, a deliberately dismissed signal
stays dismissed, the operator-facing reports are byte-stable and under test,
and a misbehaving heartbeat shows up in the ledger instead of the bill.

Success means, mechanically:

- `scan`, `entities gaps`, and `reweave` agree on what "already observed"
  means — one `observation_id()` helper, no per-command globs;
- re-running `entities gaps` or `scan` on an unchanged vault files zero new
  observations and emits zero new `observation.created` events (the first
  run after merge may file; the second must not). Prose observations
  (manual `new`, `ingest`, `reweave` connections/clusters) are outside this
  claim — see B1;
- an observation archived as `wontfix` / `duplicate` / `obsolete` is never
  re-filed; one resolved as `done` whose signal recurs is re-filed with
  `supersedes`;
- `tend --dry-run` and `health` outputs on the fixture vault match committed
  golden files; `UPDATE_GOLDEN=1` regenerates and the regen run *fails*;
- a heartbeat that trips a detector appends one `tend.anomaly` event and
  `health` lists it.

## Non-goals

- No session-transcript reading, no digest of the operator's coding sessions
  (that is jilog's job; run it beside sabnyx if ever wanted).
- No SQLite projection, no UUIDv7, no segment files, no spool. `opsctl`'s flat
  JSONL stays the ledger (ADR-0002).
- No LLM judging observations. Dedupe and reopen are hash-and-enum logic.
- No change to SPEC gates or ROUTING.

## Borrowings

Ordered by value ÷ effort; grouped into three dew cycles under Sequencing
below. B6 lists what was looked at and consciously deferred.

### B1 — Stable observation identity

jilog: content hash over length-prefixed fields + a lifecycle ledger so the
same signal is counted once (`signal_ledger.rs`).

sabnyx adaptation — observations *are* files that persist through `archived`,
so the lifecycle ledger is the observation set itself plus `ops/events.jsonl`;
no TSV sidecar.

Refined in Cycle 1 Discover. Identity is narrower than first drafted: only
signals the code can name mechanically get one; a false merge silently drops
a signal, which is worse than a duplicate the operator archives in seconds.

- Identity is a property of the *file*, not of who wrote it. The `signal` is
  derivable or absent:
  - `contradiction` with exactly two `involves` → the sorted pair;
  - `gap` with `target` set → the target;
  - `connection` / `friction` / `structural`, a contradiction naming ≠ 2
    files, a gap without `target` → no signal, no identity (prose). Prose
    observations get the lifecycle commands but no dedupe and no reopen.
  Titles are never hashed: two rewordings are not "the same" in any way the
  code can defend.
- `observation_id(category, signal) -> str | None` in `observations.py`:
  SHA-256 of the canonical JSON of `{"category", "signal"}` — same
  canonicalisation as `ledger.content_hash` (sorted keys, no whitespace).
  Length-prefixing is unnecessary once the input is canonical JSON; note that
  in the docstring so nobody re-adds FNV. No `producer` in the hash: actor
  labels are not producers (`tend` writes as itself when running `reweave`),
  and two producers of one signal *should* merge. `producer` is a fixed-enum
  audit field in frontmatter, outside the hash.
- Each signal component is normalised before hashing: canonical slug via the
  alias-aware slug index when it resolves, else `slugify` of the value with
  path and `.md` stripped. Identity is therefore f(file, vault state). It can
  shift when a page or alias is created — the moment a gap closes — so the
  drift coincides with resolution rather than causing a false re-file. This
  is deliberate; do not "fix" it.
- `obs_id` is **computed on every read and never stored** in frontmatter. A
  stored-or-nothing id would treat every existing file as prose and re-file
  pairs `scan` already suppresses today. No backfill, no lint-equality rule.
  The id *is* written into ledger payloads (`observation.created`,
  `.resolved`, `.suppressed`) as an immutable point-in-time snapshot, so
  `opsctl query` can follow one signal across files.
- `observations.index()` built on the companion plan's `observations()`
  reader: pending files keyed by `obs_id`, resolved files in a secondary map
  (B2).
- `writers.write_observation` computes the id when the signal is derivable,
  consults the pending index, and returns the existing path instead of
  writing a duplicate. Prompts ask reweave/ingest for exactly two `involves`
  on contradictions; the writer accepts any count and assigns identity only
  at two (strictness would only drop signals).
- Replace the ad-hoc dedupe in `commands/scan.py` (file-pair set, ~L50) and
  `commands/entities.py` (`*-gap-<slug>.md` glob, ~L180) with the index.
  `scan`'s behaviour is unchanged: a pending contradiction on the pair from
  any writer still suppresses it. `entities gaps` sets `target` on new gaps.
- Nice-to-have: health reports duplicate `obs_id`s across pending files
  (Gate 7 warning).

Files: `schemas.py`, `writers.py`, `commands/scan.py`, `commands/entities.py`,
`commands/health.py`, new `observations.py` (or fold into the companion
plan's module), tests `test_observations.py`, extend `test_scan.py`,
`test_entities.py`.

### B2 — Resolution kind and reopen policy

jilog: a recurring signal reopens a closed issue only when `closed_reason ==
done`; `wontfix` / `duplicate` / `superseded` and a *missing* reason are left
alone (`trackers/kata.rs:82-101`).

Refined in Cycle 1 Discover:

- Companion plan's `resolution: str` stays; add
  `resolution_kind: done | wontfix | duplicate | obsolete` (required whenever
  `resolved` is present on a new transition; legacy = absent = treated as
  "missing reason" → never reopen, matching jilog's schema-drift rule).
- `status` is **derived from the kind**, not a second axis: `done` →
  `promoted`; `wontfix` / `duplicate` / `obsolete` → `archived`. Schema
  validation rejects a mismatch. Commands: `resolve FILE --reason` (always
  `done`) and `archive FILE --kind wontfix|duplicate|obsolete --reason`;
  `archive --kind done` is refused. This supersedes the companion plan's
  "archive" for deterministic gap closure: closing a gap is a *resolve*
  (`done`), so a gap whose page later disappears is re-filed.
- `observations.recurrence_policy(obs_id) -> "new" | "reopen" | "suppressed"`:
  pending → `suppressed` (already tracked); resolved with kind `done` →
  `reopen`; any other kind or missing kind → `suppressed`.
- `reopen` writes a *new* observation file (never un-archives the old one —
  observations are append-only like the ledger). `supersedes` holds the old
  file's **vault-relative path** in frontmatter and payload — not its
  `obs_id`, which equals the new file's (identity is the signal). Only the
  recurrence path may set `supersedes`; the manual commands refuse it, so a
  reopen chain cannot be hand-forged.
- A same-day reopen collides with the old filename
  (`<date>-<category>-<slug>.md`); the writer appends a deterministic `-2`,
  `-3`, never a timestamp (Cycle 2 goldens).
- Suppressed recurrences are counted, not written: one `observation.suppressed`
  event per signal per producer run with `{obs_id, count}`, emitted only when
  `count > 0`, so the ledger shows a signal that keeps firing against a
  `wontfix` while a quiet heartbeat stays silent.
- SPEC.md §3 observation schema gains `target`, `producer`, `resolution`,
  `resolution_kind`, `supersedes` and the derived status rule in the same PR
  (no `obs_id` field — it is computed). All new fields are optional on
  existing files.

Files: `schemas.py`, `writers.py` / `observations.py`, companion plan's
command module, `test_observations.py`.

### B3 — Golden tests for operator-facing reports

jilog: byte-identical digest under test; `UPDATE_GOLDEN=1` regenerates and the
regen run never passes (`tests/golden_digest.rs`).

- Tests build vaults per-test via the `tmp_vault` fixture
  (`tests/conftest.py:170`, tmp_path + monkeypatched module paths); there is
  no committed fixture directory. Add one small deterministic *builder*
  (`tests/golden_vault.py`: a fixed set of intake/atlas/observation files with
  fixed `created` dates) on top of `tmp_vault` rather than checking a vault in.
- Make `tend --dry-run` and `health` output deterministic: fixed clock by
  monkeypatching `ledger.now` (the only clock seam — `ran it`), stable sort
  order for every listed item, no absolute paths (relative to vault root),
  `Console(width=100, force_terminal=False, no_color=True)`.
- `tests/golden/tend-dry-run.txt`, `tests/golden/health.txt`; one helper
  `assert_golden(name, actual)` that on `UPDATE_GOLDEN=1` rewrites the file
  and then `pytest.fail("golden regenerated — rerun without UPDATE_GOLDEN")`.
- Scope creep guard: golden tests cover the *report text* only; behavioural
  tests stay as they are. If a golden breaks on a legitimate change, regen is
  a one-line deliberate act in its own commit.

Files: `tests/golden/`, `tests/conftest.py` (helper + clock fixture),
`commands/tend.py`, `commands/health.py` (determinism only — no behaviour
change; if a behaviour change is needed, surface it).

### B4 — Heartbeat anomaly detectors

jilog: pure-function detectors with hard-coded thresholds over an event stream
(`health.rs`: stuck loop ≥4, runaway ≥150, storms per window).

- `tend_detectors.py` (or inside `commands/tend.py`): functions
  `(events: list[LedgerEvent]) -> list[Anomaly]` over the last N
  `tend.completed` runs read from `ops/events.jsonl`. Start with three:
  1. **stuck slug** — same intake slug triaged `keep` in ≥ 4 consecutive runs;
  2. **ceiling hit** — `SABNYX_MAX_LLM_CALLS` reached in ≥ 2 of the last 5
     runs;
  3. **repeat failure** — same `promote failed` / `reweave failed` subject in
     ≥ 3 consecutive runs.
  Thresholds are module constants with a one-line rationale each.
- `tend` appends one `tend.anomaly` event per finding `{detector, subject,
  count, window}`; `health` prints pending anomalies (those newer than the
  last `tend.completed` with zero anomalies).
- Deterministic, no model, reads the ledger only; runs in `--dry-run` too
  (it writes nothing there, just prints).

Files: new `tend_detectors.py`, `commands/tend.py`, `commands/health.py`,
`test_tend_detectors.py` (synthetic event lists; no vault needed).
Coordinate with `feedback-telemetry.md`: detectors should read the
`triage.decided` outcome field once it lands; until then key on existing
`triage.*` kinds.

### B5 — `_review/requests/`: the agent asks instead of guessing

ai-agent-learnings (`collaborative-knowledge-domains.md:122`): a `requests/`
folder so the maintainer can park a question for a human.

- Unattended `tend --yes` today has one move when a gate blocks: skip and log
  (`noninteractive` outcome in the telemetry plan). Add a second: write
  `vault/_review/requests/<obs-or-slug>.md` with frontmatter
  `{type: request, asked_by, asked, about: <path>, question, options: [...],
  status: open | answered}` and the proposed action verbatim.
- `sabnyx requests list` / `requests answer FILE --choose N`: answering
  applies the proposal through the *same* `services.py` path the interactive
  prompt would have, then marks the request `answered` and ledgers
  `request.answered`.
- `_review/` is operational, never knowledge (SPEC §5); lint must accept the
  `request` type there and reject it anywhere else.
- Deliberately small: no notification channel, no Telegram push. `health`
  lists open requests; that is the inbox.

Files: `schemas.py` (`RequestFrontmatter`), `commands/lint.py` containment
rule, new `commands/requests.py`, `commands/tend.py` (emit on blocked
decision), `test_requests.py`.

### B6 — Looked at, deferred (with the trigger that would un-defer)

- **Incremental `opsctl verify` with watermark + gap backfill**: full rehash
  is fine at ~70 events/day for years. Trigger: `opsctl verify` > 1 s.
- **No-clobber hard-link publish**: only when a non-git sync can race the
  appender. Trigger: adopting Syncthing or a second concurrent writer host.
- **Domain `CONTEXT.md` with live counters**: trigger: the first
  `domains/<topic>/` graduation (ROUTING "When to graduate").
- **Pluggable Reader trait**: sabnyx's adapters (`adapters/cli.py`,
  `telegram.py`, `r1.py`) already share `services.py`; a formal Protocol is
  Rule-of-Three material when a fourth channel appears.

## Sequencing — three dew cycles

B1–B5 are not one feature, and two of them fight when built together, so
they run as three cycles. Within a cycle, independent nodes may fan out
(dew's DAG); across cycles, one PR each.

### Cycle 1 — `observation-lifecycle` (B1 + B2 + the companion plan)

One coherent change to the observation data model and its readers:

1. `observations-and-age-triage.md`: `observations()` reader,
   `transition_observation`, `sabnyx observations list|resolve|archive`,
   deterministic gap closure. (Its age trigger already shipped in PR #25 as
   `tend --max-age-days`; out of this cycle.)
2. **B1** `obs_id` + `observations.index()`; rewire `scan` / `entities`
   dedupe onto it.
3. **B2** `resolution_kind`, `recurrence_policy`, reopen-with-`supersedes`,
   `observation.suppressed` events; `--kind` on resolve/archive.

B2 cannot exist without (1)'s transition helper, and B1's index *is* the
reader (1) calls for — splitting them would mean building the same module
twice. (1) and B1 are parallelizable; B2 follows both. Workflow: **full**
(six stages) — this is a schema change plus a new command group and it
deserves Demonstrate — or a chunky fast cycle if the operator prefers.

### Cycle 2 — `report-hygiene` (B4 then B3)

B4 changes what `tend` and `health` print; B3 freezes that output in golden
files. Build B3 first and the goldens regenerate the moment B4 lands. So:
B4 detectors → make outputs deterministic → B3 goldens, in that order, one
cycle. Workflow: **fast**. Independent of Cycle 1 except that `health`
output includes observation counts — run after Cycle 1 merges, or accept one
golden regen.

Also in this cycle (assigned from Cycle 1's deferred list): `reconcile`
asserts that every non-pending observation has an `observation.resolved`
event for its `rel` — the file/ledger mismatch a failed restore can leave
behind. ~15 read-only lines.

Threat note: detectors read ledger payloads that carry model-derived fields
(slugs, reasons). They are data for counting only — never interpolated into
a path, a shell, or a prompt. Golden fixtures use made-up slugs (AGENTS §9).

### Cycle 3 — `review-requests` (B5), if at all

Most new surface (new frontmatter type, new command, a lint containment
rule, `tend` writing into `_review/`) and the least-proven value. Kept
alone so it can be dropped without touching the others. Workflow: **fast**.
Prerequisites: `feedback-telemetry.md`'s `noninteractive` outcome, so the
request is emitted exactly where that outcome is recorded; and Cycle 1's
identity-eligibility gate (`observations.IDENTITY_SOURCES`) merged, so an
untrusted observation can neither suppress nor trigger anything a request
acts on.

Threat note — this cycle executes model output on operator say-so, so the
design must guarantee that what the operator saw is exactly what runs:

- `options` is a closed enum of actions (`promote-as-<atlas type>` / `keep` /
  `discard`), never free text the answer path interprets;
- the request file stores `proposal_hash = content_hash(proposal)` at write
  time; `requests answer` recomputes it and refuses on mismatch, so a
  hand-edited or tampered request fails closed;
- request files are written only by `tend` and mutated only by `answer`;
  lint rejects `type: request` outside `_review/`;
- the answer path applies the action through the same `services.py` function
  the interactive prompt would have called — no second apply path.

B6 builds nothing.

If one cycle is non-negotiable: B1 + B2 + B4, leaving B3 and B5 out. B3 is
the one item that must never share a cycle with anything that changes
report output.

## Handoff — running this with dew

Discover is done by this document and the recon note; each cycle starts from
the section above plus the files it names.

One worktree per cycle, never on `main`:

```sh
git worktree add -b feat/observation-lifecycle .worktrees/observation-lifecycle main
cd .worktrees/observation-lifecycle
```

then `/dew new` → name = the cycle name above, kind *major new feature in an
existing codebase*, workflow as stated per cycle. In Plan (fast) or
Discover/Design (full), hand the stage skill this file's cycle section and
the plan(s) it names as the requirements source; the stage artifact should
refine them, not rediscover them. Build is gated by the success criteria at
the top of this file translated into tests first. Verify runs
`uv run pytest`, `uv run sabnyx lint`, and — for Cycle 2 — a second `pytest`
run proving `UPDATE_GOLDEN=1` fails.

`.dew/` is gitignored in this repo: dew artifacts are ephemeral working state,
and the durable record is this plan, the PR, and the ADR if a cycle produces a
decision worth one. Use `/dew pause` before leaving a worktree; the context
snapshot lives in the worktree, not in git.

### Threat pass — every cycle

Treat all model output and all remote-ingress content (`sabnyx-telegram`,
`sabnyx-r1`, `sabnyx-ingest-web`) as attacker-controlled. Concretely:

- every new read of such content is a trust boundary: validate at the edge,
  resolve names through the slug index, never build a `Path` or a command
  from it, load YAML with `safe_load` only;
- trust follows provenance, classified by `source_agent` (set by the adapter,
  never by model output). Each trusted action keeps its own allowlist —
  auto-promote uses `SABNYX_TRUSTED_SOURCES`, observation identity uses
  `observations.IDENTITY_SOURCES` (Cycle 1, D14) — because one list carrying
  two trust decisions lets a widening for one silently widen the other. A
  design that lets untrusted content block, suppress, or trigger a trusted
  action needs such a gate or an explicit accepted-risk note;
- the Design artifact carries a short *threat pass* section listing the new
  boundaries and what bounds each (a gate, a cap, a refusal by construction);
- run `/security-review` on the branch before Verify closes; the review's
  findings are part of Verify's evidence, not optional reading.

AGENTS §2 roles are convention at stage 0 and hooks are discipline, not
security (ADR 0004); the provenance gate and OS sandboxing are the real
boundaries, so designs must not lean on the roles table for safety.

Agent brief (if delegating instead of running dew interactively): objective =
one borrowing by id; files = those listed under the item; output = PR-ready
branch with tests, a `what / why / what did not change` summary, and any
deviation from this plan named explicitly. Instance specifics never enter
tracked files (AGENTS.md §9).
