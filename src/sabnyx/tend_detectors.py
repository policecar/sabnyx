"""Heartbeat anomaly detectors: pure functions over the ledger's tend runs.

A *run* is the sequence of tend-actor events that ends at one boundary
event (`tend.completed` or `tend.aborted`). Runs are counted, not clocked:
a skipped heartbeat tick leaves no boundary and is invisible, so a streak
spanning a wall-clock gap still counts and the gap alone fires nothing.

Three detectors, each reporting only a *trailing* condition (one that holds
in the newest run), so a problem that has since cleared stays quiet:

  stuck_slug      the same intake subject was triaged `keep` in
                  STUCK_KEEP_RUNS consecutive runs. tend's retriage memory
                  skips unchanged files, so this fires on an item whose
                  content keeps changing while the verdict stays `keep`,
                  or on a regression of that memory ("re-rolling a settled
                  keep on every heartbeat").
  ceiling_hit     CEILING_HITS of the last CEILING_WINDOW runs aborted on
                  the per-run LLM call budget.
  repeat_failure  the same subject failed in REPEAT_FAILURE_RUNS
                  consecutive runs: a `promote_failed` triage decision, or
                  an `llm.failed` on a `reweave:<stem>` call.

Known limit (accepted, not corrected): events of a run that died without a
boundary (interrupt, ledger write failure, process kill) fold into the next
recorded run. Each detector counts a subject at most once per run, so a fold
never double-counts within a run, but it can merge two heartbeats into one
run and make a streak read shorter than the heartbeats it spans.

Every input string is ledger data and is only compared and counted here:
never turned into a path, a shell argument or a prompt.

@decision DEC-001: Detectors are pure functions of the event list.
@rationale: tend, health and the tests all need the same answer from the
same ledger; purity makes that answer reproducible and unit-testable without
a vault, and keeps the detectors from becoming a write path.

@decision DEC-002: A finding's identity is (detector, subject).
@rationale: `trips()` compares keys, not whole Anomaly values, so a streak
growing from 4 to 5 does not re-trip. One `tend.anomaly` per onset instead
of one per heartbeat (~96/day for one stuck slug).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .schemas import LedgerEvent
from .services import ACTOR_TEND

# Four heartbeats (an hour at the 15-minute cadence) of the same `keep`
# verdict on content that keeps changing is a loop, not deliberation.
STUCK_KEEP_RUNS = 4
# One budget abort can be a burst; two within five runs is a pattern.
CEILING_HITS = 2
CEILING_WINDOW = 5
# A third consecutive failure on the same subject rules out a transient blip.
REPEAT_FAILURE_RUNS = 3

BOUNDARY_KINDS = frozenset({"tend.completed", "tend.aborted"})
# Subject of run-level findings: the same subject tend.completed carries.
RUN_SUBJECT = "vault/"


@dataclass(frozen=True, order=True)
class Anomaly:
    detector: str  # "ceiling_hit" | "repeat_failure" | "stuck_slug"
    subject: str  # ledger subject counted (intake rel | "reweave:<stem>" | "vault/")
    count: int  # streak length, or hits within the window
    window: int  # runs examined (streak detectors: == count)

    @property
    def key(self) -> tuple[str, str]:
        return (self.detector, self.subject)


@dataclass(frozen=True)
class Run:
    boundary: LedgerEvent  # tend.completed | tend.aborted
    events: tuple[LedgerEvent, ...]  # actor == ACTOR_TEND, since the previous boundary


def runs(events: Iterable[LedgerEvent]) -> list[Run]:
    """Split the ledger into tend runs, oldest first.

    Only ACTOR_TEND events count. Events after the last boundary (a run in
    progress, or a dry run, which writes no boundary) belong to no run yet.
    """
    out: list[Run] = []
    pending: list[LedgerEvent] = []
    for e in events:
        if e.actor != ACTOR_TEND:
            continue
        if e.kind in BOUNDARY_KINDS:
            out.append(Run(boundary=e, events=tuple(pending)))
            pending = []
        else:
            pending.append(e)
    return out


def _trailing_streaks(per_run: Sequence[frozenset[str]]) -> dict[str, int]:
    """For each subject in the newest run: how many consecutive runs,
    counting back from the newest, contain it."""
    if not per_run:
        return {}
    streaks: dict[str, int] = {}
    for subject in per_run[-1]:
        n = 0
        for subjects in reversed(per_run):
            if subject not in subjects:
                break
            n += 1
        streaks[subject] = n
    return streaks


def _streak_findings(
    detector: str, per_run: Sequence[frozenset[str]], threshold: int
) -> list[Anomaly]:
    return [
        Anomaly(detector, subject, n, n)
        for subject, n in _trailing_streaks(per_run).items()
        if n >= threshold
    ]


def _triaged(run: Run, decision: str) -> frozenset[str]:
    return frozenset(
        e.subject
        for e in run.events
        if e.kind == "triage.decided" and e.payload.get("decision") == decision
    )


def stuck_slug(runs: Sequence[Run]) -> list[Anomaly]:
    per_run = [_triaged(r, "keep") for r in runs]
    return _streak_findings("stuck_slug", per_run, STUCK_KEEP_RUNS)


def ceiling_hit(runs: Sequence[Run]) -> list[Anomaly]:
    window = runs[-CEILING_WINDOW:]
    hits = sum(
        1
        for r in window
        if r.boundary.kind == "tend.aborted" and r.boundary.payload.get("error") == "LLMBudgetError"
    )
    if hits >= CEILING_HITS:
        return [Anomaly("ceiling_hit", RUN_SUBJECT, hits, len(window))]
    return []


def _failed(run: Run) -> frozenset[str]:
    reweave_failures = frozenset(
        e.subject for e in run.events if e.kind == "llm.failed" and e.subject.startswith("reweave:")
    )
    return _triaged(run, "promote_failed") | reweave_failures


def repeat_failure(runs: Sequence[Run]) -> list[Anomaly]:
    per_run = [_failed(r) for r in runs]
    return _streak_findings("repeat_failure", per_run, REPEAT_FAILURE_RUNS)


def _detect_runs(rs: Sequence[Run]) -> list[Anomaly]:
    return sorted([*stuck_slug(rs), *ceiling_hit(rs), *repeat_failure(rs)])


def detect(events: Iterable[LedgerEvent]) -> list[Anomaly]:
    """Every current finding, sorted. Same events in, same list out."""
    return _detect_runs(runs(events))


def trips(events: Sequence[LedgerEvent]) -> list[Anomaly]:
    """Findings whose (detector, subject) key is new in the latest run.

    A key that was already present before the last boundary does not trip
    again, whatever its count or window now says; once it clears, a later
    recurrence trips again.
    """
    rs = runs(events)
    if not rs:
        return []
    before = {a.key for a in _detect_runs(rs[:-1])}
    return [a for a in _detect_runs(rs) if a.key not in before]
