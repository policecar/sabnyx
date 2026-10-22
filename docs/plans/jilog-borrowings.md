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
- re-running any observation producer on an unchanged vault files zero new
  observations and emits zero new `observation.created` events;
- an observation archived as `wontfix` / `duplicate` is never re-filed; one
  resolved as `done` whose signal recurs is re-filed with `supersedes`;
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

Ordered by value ÷ effort. B1–B3 are the core; B4–B5 are independent
follow-ons; B6 lists what was looked at and consciously deferred.

### B1 — Stable observation identity

jilog: content hash over length-prefixed fields + a lifecycle ledger so the
same signal is counted once (`signal_ledger.rs`).

sabnyx adaptation — observations *are* files that persist through `archived`,
so the lifecycle ledger is the observation set itself plus `ops/events.jsonl`;
no TSV sidecar.

- `schemas.ObservationFrontmatter` gains `obs_id: str` (`sha256:<hex>`,
  required on new files; optional on legacy files for compatibility).
- `observation_id(category, involves, target=None) -> str` in `util.py` or a
  new `observations.py`: SHA-256 of the canonical JSON of
  `{"category", "involves": sorted, "target"}` — same canonicalisation as
  `ledger.content_hash` (sorted keys, no whitespace), so it is stable across
  Python versions and matches `opsctl` if it ever needs to compute it.
  Length-prefixing is unnecessary once the input is canonical JSON; note that
  in the docstring so nobody re-adds FNV.
- `observations.index(status=...) -> dict[obs_id, Path]` built on the
  `observations()` reader from the companion plan. Legacy files without
  `obs_id` get one computed on read (never written back silently).
- `writers.write_observation` sets `obs_id`, refuses to write when the id is
  already present in the pending index, and returns the existing path instead.
- Replace the ad-hoc dedupe in `commands/scan.py` (file-pair set, ~L50) and
  `commands/entities.py` (`*-gap-<slug>.md` glob, ~L180) with the index.
- Health reports duplicate `obs_id`s across pending files (Gate 7 warning).

Files: `schemas.py`, `writers.py`, `commands/scan.py`, `commands/entities.py`,
`commands/health.py`, new `observations.py` (or fold into the companion
plan's module), tests `test_observations.py`, extend `test_scan.py`,
`test_entities.py`.

### B2 — Resolution kind and reopen policy

jilog: a recurring signal reopens a closed issue only when `closed_reason ==
done`; `wontfix` / `duplicate` / `superseded` and a *missing* reason are left
alone (`trackers/kata.rs:82-101`).

- Companion plan's `resolution: str` stays; add
  `resolution_kind: done | wontfix | duplicate | obsolete` (required whenever
  `resolved` is present on a new transition; legacy = absent = treated as
  "missing reason" → never reopen, matching jilog's schema-drift rule).
- `observations.recurrence_policy(obs_id) -> "new" | "reopen" | "suppressed"`:
  pending → `suppressed` (already tracked); archived/promoted with kind `done`
  → `reopen`; any other kind or missing kind → `suppressed`.
- `reopen` writes a *new* observation file (never un-archives the old one —
  observations are append-only like the ledger) with
  `supersedes: <old obs_id>` in frontmatter and payload; `obs_id` is the same
  hash (identity is the signal, not the file), so the index must key pending
  files only and keep resolved files in a secondary map.
- Suppressed recurrences are counted, not written: one `observation.suppressed`
  event per producer run with `{obs_id, count}` so the ledger shows a signal
  that keeps firing against a `wontfix`.
- `sabnyx observations resolve|archive` (companion plan) take `--kind`.

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

## Sequencing

```
B1 ──► B2 ──► (companion plan: observations resolve/archive gets --kind)
B3  independent; do first if the fixture vault needs work anyway
B4  independent; after feedback-telemetry if that lands first
B5  independent; last — most new surface
```

One PR per borrowing. B1 and B2 may share a PR if B2 stays small.

## Handoff — running this with dew

Fast workflow (Plan → Build → Verify) per borrowing; the full six-stage
workflow is overkill for any single item here. Discover/Design are already
done by this document and the recon note.

From a worktree (never on `main`):

```sh
git worktree add -b feat/obs-identity .worktrees/obs-identity main
cd .worktrees/obs-identity
```

then `/dew new` → name `obs-identity` (or `golden-reports`, `tend-detectors`,
`review-requests`), kind *major new feature in an existing codebase*,
**fast** workflow. In the Plan stage, hand the stage skill this file's section
(B1, B3, …) plus `docs/plans/observations-and-age-triage.md` as the
requirements source; the Plan artifact should be a refinement of the section,
not a rediscovery. Build is gated by the success criteria at the top of this
file translated into tests first. Verify runs `uv run pytest`,
`uv run sabnyx lint`, and — for B3 — a second `pytest` run proving
`UPDATE_GOLDEN=1` fails.

Agent brief (if delegating instead of running dew interactively): objective =
one borrowing by id; files = those listed under the item; output = PR-ready
branch with tests, a `what / why / what did not change` summary, and any
deviation from this plan named explicitly. Instance specifics never enter
tracked files (AGENTS.md §9).
