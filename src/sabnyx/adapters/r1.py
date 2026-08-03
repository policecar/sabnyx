"""Rabbit R1 adapter: the third NormalizedEnvelope source after CLI and Telegram.

The R1 "Creation" is a static web app in the device's 240x282 WebView. It
captures voice through rabbit's on-device ASR (`CreationVoiceHandler` +
`onPluginMessage`/`sttEnded`) and POSTs the *transcript* — plain text — to our
HTTP ingress with a bearer key. This module owns the R1-specific concerns:
bearer-key trust and mapping a request into the same NormalizedEnvelope every
other interface produces, so the routing/action layer stays channel-agnostic.

The HTTP transport (the server loop) lives in commands/serve_http.py, mirroring
how serve.py owns the Telegram long-poll loop while telegram.py owns the
adapter. See docs/recon/rabbit-r1-surface-2026.md for the verified device
surface (notably: rabbit transcribes for us, so the server needs no Whisper).

Config:
    SABNYX_R1_KEYS   comma-separated bearer keys; one key ~ one operator/vault.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from datetime import UTC, datetime

from ..schemas import NormalizedEnvelope


def keys() -> set[str]:
    """Trusted bearer keys (jibot's trusted-source pattern, over HTTP)."""
    raw = os.environ.get("SABNYX_R1_KEYS", "")
    return {s.strip() for s in raw.split(",") if s.strip()}


def check_bearer(authorization: str | None, trusted: set[str]) -> bool:
    """Return True iff the Authorization header carries a trusted bearer key.

    Constant-time compare against each key (`hmac.compare_digest`) so a wrong
    key cannot be distinguished by timing. The scheme is case-insensitive
    ("Bearer"/"bearer"). An empty trusted set always denies — the server also
    refuses to start open, but this is the second line of defence.
    """
    if not authorization or not trusted:
        return False
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    token = parts[1].strip()
    if not token:
        return False
    # Compare bytes, not str: `hmac.compare_digest` raises TypeError on
    # non-ASCII str input, and this runs pre-auth on attacker-controlled
    # headers — a stray "Bearer café" must be a clean deny, not a crash.
    token_b = token.encode("utf-8")
    # Check every key without short-circuiting: `any(...)` would return on the
    # first match, so a valid token would run fewer comparisons than an invalid
    # one — a small timing tell on which token is valid. Fold instead.
    matched = False
    for k in trusted:
        if hmac.compare_digest(token_b, k.encode("utf-8")):
            matched = True
    return matched


def envelope_for_request(text: str, *, request_id: str | None = None) -> NormalizedEnvelope:
    """Map one R1 ingress request to a NormalizedEnvelope (source="web").

    `request_id` defaults to a content hash so identical retries dedup on the
    ledger; callers (and tests) may pass an explicit id for determinism.
    """
    text = text or ""
    rid = request_id or hashlib.sha256(text.encode()).hexdigest()[:16]
    env_id = "sha256:" + hashlib.sha256(f"r1|{rid}".encode()).hexdigest()
    return NormalizedEnvelope(
        envelope_id=env_id,
        source="web",
        source_id=rid,
        ts=datetime.now(UTC),
        actor_raw="r1",
        actor_canonical=None,
        kind="message.received",
        payload={"text": text},
    )
