"""Append-only ledger at ops/events.jsonl.

Rules:
  - Never edit a past line. To correct, append a compensating event.
  - Never write without a content_hash.
  - Read via replay(), never by parsing lines ad-hoc.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime

from .paths import EVENTS_JSONL, OPS
from .schemas import LedgerEvent, LedgerPlane


def _canonical(payload: dict) -> bytes:
    """Canonical JSON bytes for hashing: sorted keys, no whitespace."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def content_hash(payload: dict) -> str:
    """SHA-256 of the canonical payload, prefixed for Lemma-compatibility."""
    h = hashlib.sha256(_canonical(payload)).hexdigest()
    return f"sha256:{h}"


def now() -> datetime:
    """UTC timestamp, tz-aware. Use this instead of datetime.now()."""
    return datetime.now(UTC)


def append_event(
    *,
    kind: str,
    actor: str,
    subject: str,
    payload: dict,
    plane: LedgerPlane = "event",
    ts: datetime | None = None,
) -> LedgerEvent:
    """Append one event to ops/events.jsonl and return the typed record."""
    OPS.mkdir(parents=True, exist_ok=True)
    event = LedgerEvent(
        ts=ts or now(),
        plane=plane,
        kind=kind,
        actor=actor,
        subject=subject,
        payload=payload,
        content_hash=content_hash(payload),
    )
    line = event.model_dump_json()
    # Atomic-ish append: single write() with newline, O_APPEND on POSIX
    # gives us atomicity for lines <= PIPE_BUF, which these always are.
    with EVENTS_JSONL.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    return event


def replay() -> Iterator[LedgerEvent]:
    """Yield every event in order. The only sanctioned read path."""
    if not EVENTS_JSONL.exists():
        return
    with EVENTS_JSONL.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield LedgerEvent.model_validate_json(line)


def count() -> int:
    """How many events on the ledger."""
    return sum(1 for _ in replay())
