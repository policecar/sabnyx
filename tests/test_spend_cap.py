"""Tests for the rolling-window ingress LLM spend cap (spend.py).

Pure-ledger cases (1-6) exercise `check_ingress_cap` directly with a pinned
clock and backdated `llm.requested` events. Cases 7-8 are the service- and
serve-level integration: a tripped cap must leave no artifact behind and
must surface as a clear reply / 429, never a crash. Case 9 is the
write-amplification guard: a refusal storm (the exact load the cap exists
for) must not itself grow the ledger unboundedly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from rich.console import Console

from sabnyx import ledger
from sabnyx.spend import SpendCapExceeded, check_ingress_cap

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _requested(
    actor: str, *, minutes_ago: float, subject: str = "ask:x", ref: datetime = NOW
) -> None:
    """Backdate one llm.requested event `minutes_ago` minutes before `ref`."""
    ledger.append_event(
        plane="intent",
        kind="llm.requested",
        actor=actor,
        subject=subject,
        payload={"role": "fast", "model": "m", "provider": "p", "schema": "AskAnswer"},
        ts=ref - timedelta(minutes=minutes_ago),
    )


def _set_cap(monkeypatch, max_calls: int, window_minutes: int = 60) -> None:
    monkeypatch.setenv("SABNYX_INGRESS_MAX_CALLS", str(max_calls))
    monkeypatch.setenv("SABNYX_INGRESS_WINDOW_MINUTES", str(window_minutes))


# -- 1: shared cap trips across ingress actors, ledgers an int-only event -----


def test_cap_trips_across_ingress_actors_and_ledgers_capped_event(tmp_vault, monkeypatch):
    _set_cap(monkeypatch, max_calls=3, window_minutes=60)
    for minutes_ago in (50, 40, 10):  # oldest first; all three within the window
        _requested("sabnyx-telegram", minutes_ago=minutes_ago)

    with pytest.raises(SpendCapExceeded, match=r"3 calls / 60 min"):
        check_ingress_cap("sabnyx-r1", now=NOW)  # different ingress actor, shared cap

    events = [e for e in ledger.replay() if e.kind == "ingress.capped"]
    assert len(events) == 1
    payload = events[0].payload
    assert payload == {
        "calls_in_window": 3,
        "max_calls": 3,
        "window_minutes": 60,
        # oldest call was 50min ago in a 60min window -> ages out in 10min
        "retry_after_seconds": 600,
    }
    assert all(isinstance(v, int) for v in payload.values())  # ADR-0002: no floats
    assert events[0].actor == "sabnyx-r1"


# -- 2: events older than the window don't count ------------------------------


def test_events_older_than_window_do_not_trip_cap(tmp_vault, monkeypatch):
    _set_cap(monkeypatch, max_calls=3, window_minutes=60)
    for minutes_ago in (70, 80, 90):  # all outside the 60-minute window
        _requested("sabnyx-telegram", minutes_ago=minutes_ago)

    check_ingress_cap("sabnyx-r1", now=NOW)  # must not raise
    assert "ingress.capped" not in [e.kind for e in ledger.replay()]


# -- 3: one under the limit never trips ---------------------------------------


def test_one_under_limit_does_not_trip(tmp_vault, monkeypatch):
    _set_cap(monkeypatch, max_calls=3, window_minutes=60)
    for minutes_ago in (50, 40):  # max_calls - 1
        _requested("sabnyx-telegram", minutes_ago=minutes_ago)

    check_ingress_cap("sabnyx-r1", now=NOW)  # must not raise
    assert "ingress.capped" not in [e.kind for e in ledger.replay()]


# -- 4: SABNYX_INGRESS_MAX_CALLS=0 disables the cap ---------------------------


def test_max_calls_zero_disables_cap_even_with_many_events(tmp_vault, monkeypatch):
    _set_cap(monkeypatch, max_calls=0, window_minutes=60)
    for _ in range(1000):
        _requested("sabnyx-telegram", minutes_ago=1)

    check_ingress_cap("sabnyx-telegram", now=NOW)  # must not raise
    assert "ingress.capped" not in [e.kind for e in ledger.replay()]


# -- 5: a non-ingress actor is never gated -------------------------------------


@pytest.mark.parametrize("actor", ["sabnyx-ingest", "sabnyx-tend"])
def test_non_ingress_actor_never_capped(tmp_vault, monkeypatch, actor):
    _set_cap(monkeypatch, max_calls=1, window_minutes=60)
    for minutes_ago in range(20):  # way over any reasonable cap
        _requested("sabnyx-telegram", minutes_ago=minutes_ago)

    check_ingress_cap(actor, now=NOW)  # must not raise regardless of ledger state
    assert "ingress.capped" not in [e.kind for e in ledger.replay()]


# -- 6: sabnyx-tend's own llm.requested events don't count toward the window --


def test_tend_requests_do_not_count_toward_ingress_window(tmp_vault, monkeypatch):
    _set_cap(monkeypatch, max_calls=2, window_minutes=60)
    for minutes_ago in (10, 20, 30):  # three tend calls, well over max_calls=2
        _requested("sabnyx-tend", minutes_ago=minutes_ago)

    check_ingress_cap("sabnyx-telegram", now=NOW)  # must not raise: tend doesn't count
    assert "ingress.capped" not in [e.kind for e in ledger.replay()]


# -- 7: services-level — a tripped cap leaves no artifact and no new request --


def test_ingest_text_raises_and_leaves_no_artifact_when_capped(tmp_vault, monkeypatch):
    from sabnyx import services

    _set_cap(monkeypatch, max_calls=1, window_minutes=60)
    _requested("sabnyx-telegram", minutes_ago=1)  # already at the cap
    # check_ingress_cap's `now` defaults to ledger.now() (real wall clock);
    # pin it to NOW so the backdated event above is seen as "in the window".
    monkeypatch.setattr(ledger, "now", lambda: NOW)

    def boom(**kwargs):
        raise AssertionError("parse_structured must not be called once capped")

    monkeypatch.setattr(services.llm, "parse_structured", boom)

    requested_before = sum(1 for e in ledger.replay() if e.kind == "llm.requested")

    with pytest.raises(SpendCapExceeded):
        services.ingest_text(
            text="buy milk",
            title="Buy milk",
            source_kind="manual",
            source_url=None,
            console=Console(),
            actor="sabnyx-telegram",
        )

    assert not list(tmp_vault["SOURCES"].glob("*.md"))  # no artifact written
    requested_after = sum(1 for e in ledger.replay() if e.kind == "llm.requested")
    assert requested_after == requested_before  # no new llm.requested event


def test_answer_question_raises_when_capped(tmp_vault, monkeypatch):
    from sabnyx import services

    _set_cap(monkeypatch, max_calls=1, window_minutes=60)
    _requested("sabnyx-r1", minutes_ago=1)  # already at the cap
    monkeypatch.setattr(ledger, "now", lambda: NOW)

    def boom(**kwargs):
        raise AssertionError("parse_structured must not be called once capped")

    monkeypatch.setattr(services.llm, "parse_structured", boom)

    with pytest.raises(SpendCapExceeded):
        services.answer_question(question="is the sky blue?", actor="sabnyx-telegram")


# -- 8: serve-level surfacing ---------------------------------------------------


def _always_capped(actor: str, *, now: datetime | None = None) -> None:
    raise SpendCapExceeded(
        "LLM spend cap reached (1 calls / 1 min across ingress channels); try again in ~1 min",
        retry_after_seconds=42,
    )


def test_serve_http_route_returns_429_with_retry_after_when_capped(tmp_vault, monkeypatch):
    from sabnyx import spend as spend_mod
    from sabnyx.commands import serve_http

    monkeypatch.setattr(spend_mod, "check_ingress_cap", _always_capped)

    status, body, headers = serve_http.route(
        "POST", "/ingest", b'{"text":"remember buy milk"}', "Bearer k1", {"k1"}
    )

    assert status == 429
    assert "spend cap" in body["error"]
    assert headers["Retry-After"] == "42"
    assert not list(tmp_vault["INTAKE"].glob("*.md"))
    # No wire.errored write for a capped request: spend.check_ingress_cap is
    # the record (at most once per tripped window); a second write per
    # refusal would defeat that bound under a refusal storm.
    assert "wire.errored" not in [e.kind for e in ledger.replay()]


def test_serve_telegram_ingest_replies_with_cap_message(tmp_vault, monkeypatch):
    from sabnyx import spend as spend_mod
    from sabnyx.commands import serve

    monkeypatch.setattr(spend_mod, "check_ingress_cap", _always_capped)

    reply = serve._ingest("buy milk", title="buy milk", source_kind="manual", source_url=None)

    assert "spend cap" in reply
    assert not list(tmp_vault["INTAKE"].glob("*.md"))


# -- 9: at most one ingress.capped write per tripped window --------------------


def test_repeated_refusals_within_one_window_ledger_only_once(tmp_vault, monkeypatch):
    """A refusal storm (the exact load the cap exists for) must not itself
    grow the ledger: 50 refused calls in the same window write exactly one
    ingress.capped event, not one per refusal."""
    _set_cap(monkeypatch, max_calls=3, window_minutes=60)
    for minutes_ago in (50, 40, 10):
        _requested("sabnyx-telegram", minutes_ago=minutes_ago)

    count_before = ledger.count()
    for _ in range(50):
        with pytest.raises(SpendCapExceeded):
            check_ingress_cap("sabnyx-r1", now=NOW)

    assert ledger.count() == count_before + 1
    events = [e for e in ledger.replay() if e.kind == "ingress.capped"]
    assert len(events) == 1


def test_fresh_trip_after_window_passes_ledgers_a_new_event(tmp_vault, monkeypatch):
    """Once the window carrying the original ingress.capped event has fully
    elapsed, a fresh trip is a new, distinct event — the once-per-window
    bound doesn't silently disable the cap forever."""
    window_minutes = 60
    _set_cap(monkeypatch, max_calls=3, window_minutes=window_minutes)
    for minutes_ago in (50, 40, 10):
        _requested("sabnyx-telegram", minutes_ago=minutes_ago)

    with pytest.raises(SpendCapExceeded):
        check_ingress_cap("sabnyx-r1", now=NOW)  # first trip -> one ingress.capped event (ts=NOW)

    # Move well past the window the first ingress.capped event belongs to,
    # and supply a fresh batch of in-window calls so the cap trips again
    # rather than simply going quiet.
    later = NOW + timedelta(minutes=window_minutes + 10)
    for minutes_ago in (50, 40, 10):
        _requested("sabnyx-r1", minutes_ago=minutes_ago, ref=later)

    with pytest.raises(SpendCapExceeded):
        check_ingress_cap("sabnyx-telegram", now=later)

    events = [e for e in ledger.replay() if e.kind == "ingress.capped"]
    assert len(events) == 2
    assert events[0].ts == NOW
    assert events[1].ts == later


def test_route_429_appends_no_wire_errored(tmp_vault, monkeypatch):
    """route()'s 429 branch must not write its own wire.errored — see
    test_serve_http_route_returns_429_with_retry_after_when_capped above,
    duplicated here under this section's name for the coordinator's ask."""
    from sabnyx import spend as spend_mod
    from sabnyx.commands import serve_http

    monkeypatch.setattr(spend_mod, "check_ingress_cap", _always_capped)

    status, _, _ = serve_http.route(
        "POST", "/ingest", b'{"text":"remember buy milk"}', "Bearer k1", {"k1"}
    )

    assert status == 429
    assert "wire.errored" not in [e.kind for e in ledger.replay()]
