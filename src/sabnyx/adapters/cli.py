"""CLI adapter: normalize shell invocations into NormalizedEnvelope.

The CLI is the first interface. Every `sabnyx <command>` can be seen as
one envelope: source=cli, kind="command.invoke", payload={argv, cwd}.

This lets us replay user commands from the ledger and later compare
CLI vs Telegram vs email events uniformly.
"""

from __future__ import annotations

import hashlib
import os
import sys
from datetime import UTC, datetime

from ..schemas import NormalizedEnvelope


def envelope_for_invocation(argv: list[str] | None = None) -> NormalizedEnvelope:
    """Build an envelope for the current CLI invocation."""
    argv = argv if argv is not None else sys.argv
    ts = datetime.now(UTC)
    actor_raw = os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"
    source_id = " ".join(argv)
    env_id_src = f"cli|{source_id}|{ts.isoformat()}".encode()
    env_id = "sha256:" + hashlib.sha256(env_id_src).hexdigest()
    return NormalizedEnvelope(
        envelope_id=env_id,
        source="cli",
        source_id=source_id,
        ts=ts,
        actor_raw=actor_raw,
        actor_canonical=actor_raw,
        kind="command.invoke",
        payload={"argv": argv, "cwd": os.getcwd()},
    )
