"""Heartbeat anomaly detectors over synthetic ledger streams — no vault.

Why these matter: a heartbeat that misbehaves (a slug the LLM keeps
re-deciding, a run budget hit again and again, the same failure every tick)
is otherwise invisible — each tick exits and the next one starts clean. The
detectors are the only memory across ticks, so their edges (threshold,
streak break, window, run boundary) are what decide whether the operator
ever hears about it.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sabnyx import ledger
from sabnyx import tend_detectors as td
from sabnyx.schemas import LedgerEvent
from sabnyx.services import ACTOR_TEND

T0 = datetime(2026, 1, 1, tzinfo=UTC)
SLUG = "vault/intake/made-up-heron.md"
OTHER = "vault/intake/made-up-kettle.md"


class Stream:
    """Builds a synthetic ledger: one call per event, timestamps advance."""

    def __init__(self) -> None:
        self.events: list[LedgerEvent] = []
        self.t = T0

    def add(
        self,
        kind: str,
        subject: str = "vault/",
        actor: str = ACTOR_TEND,
        gap: timedelta = timedelta(minutes=15),
        **payload: object,
    ) -> Stream:
        return self._append(kind, subject, actor, gap, dict(payload))

    def _append(self, kind: str, subject: str, actor: str, gap: timedelta, payload: dict) -> Stream:
        self.t += gap
        self.events.append(
            LedgerEvent(
                ts=self.t,
                plane="projection",
                kind=kind,
                actor=actor,
                subject=subject,
                payload=payload,
                content_hash=ledger.content_hash(payload),
            )
        )
        return self

    def keep(self, subject: str = SLUG) -> Stream:
        return self.add("triage.decided", subject, decision="keep")

    def promote_failed(self, subject: str = SLUG) -> Stream:
        return self.add("triage.decided", subject, decision="promote_failed")

    def reweave_failed(self, stem: str = "made-up-heron") -> Stream:
        return self.add("llm.failed", f"reweave:{stem}", error="RuntimeError")

    def completed(self, gap: timedelta = timedelta(minutes=15)) -> Stream:
        return self.add("tend.completed", gap=gap)

    def budget_abort(self) -> Stream:
        payload = {"error": "LLMBudgetError", "pass": 2}
        return self._append("tend.aborted", "vault/", ACTOR_TEND, timedelta(minutes=15), payload)

    def other_abort(self) -> Stream:
        payload = {"error": "ValueError", "pass": 3}
        return self._append("tend.aborted", "vault/", ACTOR_TEND, timedelta(minutes=15), payload)


def keep_runs(n: int, s: Stream | None = None) -> Stream:
    s = s or Stream()
    for _ in range(n):
        s.keep().completed()
    return s


# ---------------------------------------------------------------------------
# runs(): the unit every detector counts in
# ---------------------------------------------------------------------------


def test_runs_empty_ledger_is_no_runs() -> None:
    assert td.runs([]) == []
    assert td.detect([]) == []
    assert td.trips([]) == []


def test_runs_split_at_completed_and_aborted() -> None:
    s = Stream().keep().completed().promote_failed().budget_abort()
    rs = td.runs(s.events)
    assert [r.boundary.kind for r in rs] == ["tend.completed", "tend.aborted"]
    assert [len(r.events) for r in rs] == [1, 1]


def test_runs_exclude_dangling_tail() -> None:
    # A run in progress (or a dry run: it writes no boundary) is not a run.
    s = keep_runs(3).keep()
    assert len(td.runs(s.events)) == 3
    assert td.detect(s.events) == []  # the 4th keep is not yet a run


def test_runs_ignore_other_actors() -> None:
    # An operator `sabnyx reweave` failure, or a non-tend boundary-shaped
    # event, must not leak into the heartbeat's runs.
    s = Stream().keep().add("llm.failed", "reweave:x", actor="sabnyx-cli").completed()
    s.add("tend.completed", actor="sabnyx-cli")
    rs = td.runs(s.events)
    assert len(rs) == 1
    assert [e.kind for e in rs[0].events] == ["triage.decided"]


def test_gap_between_runs_changes_nothing() -> None:
    # Runs are counted, not clocked: a 40-minute skipped tick neither breaks
    # a streak nor fires on its own.
    s = keep_runs(2)
    s.keep().completed(gap=timedelta(minutes=40))
    s.keep().completed()
    assert td.detect(s.events) == [td.Anomaly("stuck_slug", SLUG, 4, 4)]
    assert td.detect(Stream().completed().completed(gap=timedelta(minutes=40)).events) == []


def test_keep_counted_once_per_run() -> None:
    # A folded run (a crashed tick without a boundary merged into the next)
    # may carry the same subject twice; it still counts as one run.
    s = Stream()
    for _ in range(3):
        s.keep().keep().completed()
    assert td.detect(s.events) == []


# ---------------------------------------------------------------------------
# stuck_slug
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("n", "expected"), [(3, []), (4, [(4, 4)]), (6, [(6, 6)])])
def test_stuck_slug_threshold(n: int, expected: list[tuple[int, int]]) -> None:
    got = td.stuck_slug(td.runs(keep_runs(n).events))
    assert [(a.count, a.window) for a in got] == expected
    assert all(a.subject == SLUG and a.detector == "stuck_slug" for a in got)


def test_stuck_slug_streak_broken_one_run_back() -> None:
    s = keep_runs(3)
    s.completed()  # run without the keep
    keep_runs(3, s)
    assert td.stuck_slug(td.runs(s.events)) == []


def test_stuck_slug_only_trailing() -> None:
    # Stuck once, now fine: quiet.
    s = keep_runs(5).completed()
    assert td.stuck_slug(td.runs(s.events)) == []


def test_stuck_slug_ignores_other_decisions() -> None:
    s = Stream()
    for _ in range(4):
        s.add("triage.decided", SLUG, decision="discard").completed()
    assert td.stuck_slug(td.runs(s.events)) == []


# ---------------------------------------------------------------------------
# ceiling_hit
# ---------------------------------------------------------------------------


def test_ceiling_two_in_last_five_fires() -> None:
    s = Stream().budget_abort().completed().completed().budget_abort().completed()
    assert td.ceiling_hit(td.runs(s.events)) == [td.Anomaly("ceiling_hit", "vault/", 2, 5)]


def test_ceiling_one_hit_is_quiet() -> None:
    s = Stream().budget_abort().completed().completed()
    assert td.ceiling_hit(td.runs(s.events)) == []


def test_ceiling_hit_in_run_six_falls_out_of_window() -> None:
    s = Stream().budget_abort()  # run 6 counting back
    s.budget_abort()  # run 5
    for _ in range(4):
        s.completed()
    assert td.ceiling_hit(td.runs(s.events)) == []
    s2 = Stream().budget_abort().budget_abort()
    for _ in range(3):
        s2.completed()
    assert td.ceiling_hit(td.runs(s2.events)) == [td.Anomaly("ceiling_hit", "vault/", 2, 5)]


def test_ceiling_window_shorter_than_five_runs() -> None:
    s = Stream().budget_abort().budget_abort()
    assert td.ceiling_hit(td.runs(s.events)) == [td.Anomaly("ceiling_hit", "vault/", 2, 2)]


def test_ceiling_ignores_other_abort_errors() -> None:
    s = Stream().other_abort().other_abort().budget_abort()
    assert td.ceiling_hit(td.runs(s.events)) == []


# ---------------------------------------------------------------------------
# repeat_failure
# ---------------------------------------------------------------------------


def test_repeat_promote_failed_three_runs() -> None:
    s = Stream()
    for _ in range(3):
        s.promote_failed().completed()
    assert td.repeat_failure(td.runs(s.events)) == [td.Anomaly("repeat_failure", SLUG, 3, 3)]


def test_repeat_reweave_failure_three_runs() -> None:
    s = Stream()
    for _ in range(3):
        s.reweave_failed().other_abort()
    got = td.repeat_failure(td.runs(s.events))
    assert got == [td.Anomaly("repeat_failure", "reweave:made-up-heron", 3, 3)]


def test_repeat_failure_two_runs_is_quiet() -> None:
    s = Stream().promote_failed().completed().promote_failed().completed()
    assert td.repeat_failure(td.runs(s.events)) == []


def test_repeat_failure_mixed_subjects_is_quiet() -> None:
    s = Stream().promote_failed(SLUG).completed()
    s.promote_failed(OTHER).completed()
    s.reweave_failed().completed()
    assert td.repeat_failure(td.runs(s.events)) == []


def test_repeat_failure_ignores_non_reweave_llm_failures() -> None:
    s = Stream()
    for _ in range(3):
        s.add("llm.failed", "triage:batch", error="RuntimeError").completed()
    assert td.repeat_failure(td.runs(s.events)) == []


# ---------------------------------------------------------------------------
# detect(): the union, sorted and stable
# ---------------------------------------------------------------------------


def test_detect_sorted_and_stable() -> None:
    s = Stream()
    for _ in range(4):
        s.keep(OTHER).keep(SLUG).promote_failed(SLUG).budget_abort()
    first = td.detect(s.events)
    assert first == td.detect(list(s.events))
    assert first == sorted(first)
    assert [a.detector for a in first] == [
        "ceiling_hit",
        "repeat_failure",
        "stuck_slug",
        "stuck_slug",
    ]


# ---------------------------------------------------------------------------
# trips(): one event per onset, keyed on (detector, subject)
# ---------------------------------------------------------------------------


def test_trips_stuck_slug_onset_persistence_clearance_recurrence() -> None:
    s = keep_runs(3)
    assert td.trips(s.events) == []  # below threshold
    s.keep().completed()
    assert td.trips(s.events) == [td.Anomaly("stuck_slug", SLUG, 4, 4)]  # newly tripped
    for _ in range(2):  # count 5, 6: still present, changed count
        s.keep().completed()
        assert td.trips(s.events) == []
        assert td.detect(s.events)[0].count > 4
    s.completed()  # cleared
    assert td.trips(s.events) == []
    assert td.detect(s.events) == []
    for _ in range(3):
        s.keep().completed()
        assert td.trips(s.events) == []
    s.keep().completed()  # recurs after clearance
    assert td.trips(s.events) == [td.Anomaly("stuck_slug", SLUG, 4, 4)]


def test_trips_ceiling_hits_move_inside_window_without_retrip() -> None:
    s = Stream().budget_abort().budget_abort()
    assert td.trips(s.events) == [td.Anomaly("ceiling_hit", "vault/", 2, 2)]
    s.budget_abort()  # 3 hits, window 3
    assert td.trips(s.events) == []
    s.completed().completed()  # 3 hits, window 5
    assert td.trips(s.events) == []
    assert td.detect(s.events) == [td.Anomaly("ceiling_hit", "vault/", 3, 5)]


def test_trips_persisting_with_same_count_does_not_trip() -> None:
    # Ceiling stays at exactly (2, 5) as one old hit slides out of the window
    # and a new one arrives: the finding persists, nothing trips.
    s = Stream().budget_abort().budget_abort().completed().completed().completed()
    assert td.detect(s.events) == [td.Anomaly("ceiling_hit", "vault/", 2, 5)]
    s.budget_abort()
    assert td.detect(s.events) == [td.Anomaly("ceiling_hit", "vault/", 2, 5)]
    assert td.trips(s.events) == []


def test_trips_ceiling_clears_when_hit_leaves_window() -> None:
    s = Stream().budget_abort().completed().completed().completed().budget_abort()
    assert td.detect(s.events) == [td.Anomaly("ceiling_hit", "vault/", 2, 5)]
    s.completed()  # the first abort leaves the window: one hit left
    assert td.detect(s.events) == []
    assert td.trips(s.events) == []
    s.budget_abort()  # back to two in five: recurs, trips again
    assert td.trips(s.events) == [td.Anomaly("ceiling_hit", "vault/", 2, 5)]


def test_trips_ignore_dangling_tail() -> None:
    # The tail after the last boundary is no run, so it cannot trip.
    s = keep_runs(3).keep()
    assert td.trips(s.events) == []


# ---------------------------------------------------------------------------
# structure
# ---------------------------------------------------------------------------


def test_module_imports_no_path_shell_or_model_code() -> None:
    """Structural check only: the module's own imports name none of the
    modules that build paths, run commands or call a model. It does not
    prove untrusted subjects never reach those (a transitive import could);
    that is verified through the data flow in the security review."""
    src = Path(td.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported |= {a.name for a in node.names}
    forbidden = {"paths", "subprocess", "llm", "os", "shutil", "pathlib"}
    assert not imported & forbidden, imported & forbidden
