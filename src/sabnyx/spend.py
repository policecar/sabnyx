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


def check_ingress_cap(actor: str, *, now: datetime | None = None) -> None:
    """Raise `SpendCapExceeded` if `actor` has hit the rolling ingress cap.

    No-op if `actor` is not in `INGRESS_ACTORS` or the cap is disabled
    (`SABNYX_INGRESS_MAX_CALLS <= 0`). `now` defaults to `ledger.now()`
    (UTC, tz-aware) and exists so tests can pin the clock.

    @decision DEC-01: gate in services.py (this function's callers), not in
    llm.parse_structured.
    @rationale: parse_structured runs AFTER services.ingest_text has already
    called sources.capture, which writes an artifact. Gating earlier — as
    the first statement of services.ingest_text/answer_question — means a
    capped request leaves no artifact and no llm.requested event, just the
    ingress.capped event below. Putting the gate in parse_structured would
    also start throttling sabnyx-tend and the ingest CLI, which this cap is
    not meant to cover; they stay governed solely by llm.call_budget.

    @decision DEC-02: counts llm.requested, not llm.completed.
    @rationale: a call that errors out still spent the attempt (provider
    latency/quota, and on some providers partial billing) — only a call
    that was never attempted (refused here, or by llm.call_budget) should
    be free. Counting llm.completed would let a channel that reliably
    triggers provider failures dodge the cap indefinitely.
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
    # rates Telegram/R1 produce (at most a handful per minute); if the
    # ledger grows large enough for a full replay to matter, the
    # optimisation is a tail read (seek backward from EOF until past
    # `cutoff`) rather than this full scan.
    in_window: list[datetime] = [
        event.ts
        for event in ledger.replay()
        if event.kind == "llm.requested" and event.actor in INGRESS_ACTORS and event.ts >= cutoff
    ]

    if len(in_window) < max_calls:
        return

    oldest = min(in_window)
    retry_after_seconds = max(1, math.ceil((oldest + window - now).total_seconds()))
    retry_minutes = max(1, math.ceil(retry_after_seconds / 60))

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
    )
    raise SpendCapExceeded(
        f"LLM spend cap reached ({max_calls} calls / {window_minutes} min across "
        f"ingress channels); try again in ~{retry_minutes} min",
        retry_after_seconds=retry_after_seconds,
    )
