"""Telegram adapter: the second NormalizedEnvelope source after the CLI.

Long-poll transport over httpx (already a dependency — no SDK). Inbound
Telegram updates are normalized into the same NormalizedEnvelope every
other interface produces, so the policy/handler layer is channel-agnostic
— adding a third channel is one more normalizer, nothing downstream.

Config:
    SABNYX_TELEGRAM_TOKEN   bot token from @BotFather (required)
    SABNYX_TELEGRAM_ALLOW   comma-separated trusted sender user IDs
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime

import httpx

from ..schemas import NormalizedEnvelope

API = "https://api.telegram.org"


def token() -> str:
    tok = os.environ.get("SABNYX_TELEGRAM_TOKEN", "")
    if not tok:
        raise ValueError("SABNYX_TELEGRAM_TOKEN is not set")
    return tok


def allowlist() -> set[str]:
    """Trusted sender user IDs (jibot's trusted-source pattern)."""
    raw = os.environ.get("SABNYX_TELEGRAM_ALLOW", "")
    return {s.strip() for s in raw.split(",") if s.strip()}


def _redacted(e: Exception, method: str) -> RuntimeError:
    """Rebuild an httpx (or JSON-decode) error without the request URL — the
    URL embeds the bot token, and serve.py prints/ledgers these exceptions on
    poll/send failures. Keep only the status code (or error type) and the API
    method."""
    status = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else None
    detail = f"HTTP {status}" if status is not None else type(e).__name__
    return RuntimeError(f"telegram {method} failed: {detail}")


def get_updates(offset: int | None, timeout: int = 30) -> list[dict]:
    """Long-poll for updates. Returns the raw `result` array."""
    params: dict = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    try:
        # http timeout must outlast the long-poll window
        resp = httpx.get(f"{API}/bot{token()}/getUpdates", params=params, timeout=timeout + 10)
        resp.raise_for_status()
        # resp.json() stays inside the guard: a non-JSON 200 (proxy error page)
        # raises ValueError, which must also be redacted, not leaked raw.
        return resp.json().get("result", [])
    except (httpx.HTTPError, ValueError) as e:
        redacted = _redacted(e, "getUpdates")
    # Re-raise OUTSIDE the except handler so __context__ is never set to the
    # token-bearing httpx error: a logger that walks the exception chain
    # (ignoring __suppress_context__) still cannot reach the token.
    raise redacted


def send_message(chat_id: int | str, text: str) -> None:
    """Send a reply. Telegram caps messages at 4096 chars; truncate."""
    body = {"chat_id": chat_id, "text": text[:4000]}
    try:
        httpx.post(f"{API}/bot{token()}/sendMessage", json=body, timeout=30).raise_for_status()
        return
    except httpx.HTTPError as e:
        redacted = _redacted(e, "sendMessage")
    raise redacted  # outside the handler: no token-bearing error in __context__


def envelope_for_update(update: dict) -> NormalizedEnvelope | None:
    """Map one Telegram update to a NormalizedEnvelope, or None if it is
    not a text message we handle."""
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return None
    text = msg.get("text")
    if not text:
        return None

    frm = msg.get("from", {})
    actor_raw = str(frm.get("id", ""))
    update_id = str(update.get("update_id", ""))
    env_id = "sha256:" + hashlib.sha256(f"telegram|{update_id}".encode()).hexdigest()
    ts = (
        datetime.fromtimestamp(msg.get("date", 0), tz=UTC) if msg.get("date") else datetime.now(UTC)
    )

    return NormalizedEnvelope(
        envelope_id=env_id,
        source="telegram",
        source_id=update_id,
        ts=ts,
        actor_raw=actor_raw,
        actor_canonical=frm.get("username"),
        kind="message.received",
        payload={
            "text": text,
            "chat_id": msg.get("chat", {}).get("id"),
            "message_id": msg.get("message_id"),
            "from_username": frm.get("username"),
        },
    )
