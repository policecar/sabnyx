"""Rolling-window LLM spend cap for the ingress daemons (serve / serve-http).

llm.py's `call_budget()` is a per-run ceiling the long-lived daemons were
deliberately excluded from (see its docstring): there is no single "run" to
bound when a process polls forever. This module is the daemon-side
complement — a rolling-window cap on calls triggered by an externally
reachable channel (Telegram, the R1 HTTP ingress), independent of process
lifetime.

Configuration (env vars, see config.py):
    SABNYX_INGRESS_MAX_CALLS       calls allowed per window (default 60;
                                    <= 0 disables the cap)
    SABNYX_INGRESS_WINDOW_MINUTES  window size in minutes (default 60)
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from . import ledger
from .config import ingress_max_calls, ingress_window_minutes

# Actors whose LLM calls are externally triggerable without an operator in
# the loop — the set this cap governs. sabnyx-ingest (CLI) and sabnyx-tend
# (the heartbeat) are operator-initiated/operator-scheduled and stay
# governed solely by llm.call_budget.
INGRESS_ACTORS = frozenset({"sabnyx-telegram", "sabnyx-r1"})


class SpendCapExceeded(RuntimeError):
    """Raised when the rolling ingress LLM-call cap has been reached.

    The message is end-user-safe (no paths/exception detail) and fit to
    show verbatim in a Telegram reply or an HTTP error body.
    `retry_after_seconds` lets an HTTP caller set a ``Retry-After`` header.
    """

    def __init__(self, message: str, *, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


def _message(max_calls: int, window_minutes: int, retry_after_seconds: int) -> str:
    retry_minutes = max(1, math.ceil(retry_after_seconds / 60))
    return (
        f"LLM spend cap reached ({max_calls} calls / {window_minutes} min across "
        f"ingress channels); try again in ~{retry_minutes} min"
    )


def check_ingress_cap(actor: str, *, now: datetime | None = None) -> None:
    """Raise `SpendCapExceeded` if `actor` has hit the rolling ingress cap.

    No-op if `actor` is not in `INGRESS_ACTORS` or the cap is disabled
    (`SABNYX_INGRESS_MAX_CALLS <= 0`). `now` defaults to `ledger.now()`
    (UTC, tz-aware) and exists so tests can pin the clock.

    `ingress.capped` is ledgered at most once per tripped window, not once
    per refusal: a flood of calls from a capped (or compromised) sender has
    no LLM call to slow it down, so without this a refusal storm would grow
    the ledger — and the replay every ingress request already does — by two
    lines per request indefinitely. The event means "the cap tripped at
    some point in this window", not "this request was refused"; callers
    that need the latter already get it via the raised exception.

    @decision DEC-01: gate in services.py (this function's callers), not in
    llm.parse_structured.
    @rationale: parse_structured runs AFTER services.ingest_text has already
    called sources.capture, which writes an artifact. Gating earlier — as
    the first statement of services.ingest_text/answer_question — means a
    capped request leaves no artifact and no llm.requested event, just the
    (at most one per window) ingress.capped event below. Putting the gate in
    parse_structured would also start throttling sabnyx-tend and the ingest
    CLI, which this cap is not meant to cover; they stay governed solely by
    llm.call_budget.

    @decision DEC-02: counts llm.requested, not llm.completed.
    @rationale: a call that errors out still spent the attempt (provider
    latency/quota, and on some providers partial billing) — only a call
    that was never attempted (refused here, or by llm.call_budget) should
    be free. Counting llm.completed would let a channel that reliably
    triggers provider failures dodge the cap indefinitely. The counted unit
    is requests to the LLM tier, not provider calls: `parse_structured`
    emits exactly one llm.requested per request, but a schema-repair re-ask
    (llm.py's `_MAX_REPAIR_ATTEMPTS`) is a second full-price provider call
    that this cap never sees — each counted request can cost up to
    ``1 + _MAX_REPAIR_ATTEMPTS`` provider calls. Kept as-is (counting
    provider calls directly would mean charging the cap from inside the
    repair-retry loop, a bigger change for a secondary cost multiplier);
    size `SABNYX_INGRESS_MAX_CALLS` with that multiplier in mind.
    """
    if actor not in INGRESS_ACTORS:
        return
    max_calls = ingress_max_calls()
    if max_calls <= 0:
        return

    window_minutes = ingress_window_minutes()
    window = timedelta(minutes=window_minutes)
    now = now or ledger.now()
    cutoff = now - window

    # One full ledger replay per ingress request. Acceptable at the request
    # rates Telegram/R1 produce (at most a handful per minute): a full
    # replay of the live ledger (11.9k events) measures 17 ms. If the ledger
    # grows large enough for that to matter, the optimisation is a tail read
    # (seek backward from EOF until past `cutoff`) rather than this full
    # scan. This replay also finds the most recent ingress.capped event, so
    # a refusal storm doesn't add a second write per request on top of the
    # read; services.py calls this function too (DEC-01), so a non-capped
    # request pays this replay twice — intended, see DEC-01.
    #
    # `event.ts > cutoff` (not `>=`): a call exactly `window` old has fully
    # aged out, not merely reached the boundary.
    in_window: list[datetime] = []
    last_capped_ts: datetime | None = None
    for event in ledger.replay():
        if event.kind == "llm.requested" and event.actor in INGRESS_ACTORS and event.ts > cutoff:
            in_window.append(event.ts)
        elif event.kind == "ingress.capped":
            last_capped_ts = event.ts  # replay is chronological: last one wins

    if len(in_window) < max_calls:
        return

    # The cap lifts only once enough EXCESS calls have aged out to bring the
    # count back under max_calls — not when the single oldest call ages out.
    # With N calls in window and a cap of max_calls, the (N - max_calls)-th
    # oldest call (0-indexed: N - max_calls) is the last one that must age
    # out; `window` after ITS timestamp is when the count finally drops
    # below max_calls. Using the single oldest call instead would understate
    # the wait whenever more than max_calls calls landed in one window (a
    # lowered-then-restarted cap, or two daemons both passing the check at
    # max_calls - 1 concurrently — no lock around check+emit, which is fine,
    # but the arithmetic must cope with the resulting overshoot).
    sorted_in_window = sorted(in_window)
    lift_at = sorted_in_window[len(in_window) - max_calls] + window
    retry_after_seconds = max(1, math.ceil((lift_at - now).total_seconds()))

    if last_capped_ts is not None and last_capped_ts > cutoff:
        # The cap is still exceeded and this window's trip is already
        # ledgered — refuse again without a second write (see docstring).
        # Order matters: this check sits AFTER the count check, so a trip
        # never locks the channel out once enough calls have aged out.
        raise SpendCapExceeded(
            _message(max_calls, window_minutes, retry_after_seconds),
            retry_after_seconds=retry_after_seconds,
        )

    ledger.append_event(
        plane="event",
        kind="ingress.capped",
        actor=actor,
        subject="llm/",
        payload={
            "calls_in_window": len(in_window),
            "max_calls": max_calls,
            "window_minutes": window_minutes,
            "retry_after_seconds": retry_after_seconds,
        },
        # Explicit ts=now (not ledger.append_event's real-wall-clock default):
        # the suppression check above compares this event's ts against a
        # cutoff derived from `now`, so it must be stamped with the same
        # clock reading the rest of this call used, not the instant the
        # write happens to land.
        ts=now,
    )
    raise SpendCapExceeded(
        _message(max_calls, window_minutes, retry_after_seconds),
        retry_after_seconds=retry_after_seconds,
    )
