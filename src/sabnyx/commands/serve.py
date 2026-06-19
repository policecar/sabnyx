"""`sabnyx serve` — drive the vault from Telegram (plan §Phase 6).

A long-poll loop that turns the second NormalizedEnvelope source into
vault actions. The envelope abstraction finally earns its keep: this
handler is channel-agnostic — it acts on envelopes, not on Telegram
payloads.

Trusted-sender model (the operator's choice): only SABNYX_TELEGRAM_ALLOW
user IDs are honored, and their captures auto-apply into intake/ (still
disposable, still triaged before atlas). Everything else is logged to the
ledger as wire.rejected and ignored — no reply, so the bot is not a spam
target.

Message routing (predictable, no LLM):
    /start | /help                             -> greeting (no capture)
    /ask <q>  | /q <q> | text ending in '?'   -> ask
    /status | /health                          -> health summary
    a bare http(s):// URL                      -> ingest the URL
    /remember <text> | /capture <text> | other -> ingest the text (capture)
"""

from __future__ import annotations

import io
import re
import time

import click
from rich.console import Console

from .. import ledger, services
from ..adapters import telegram
from ..paths import ensure_tree
from ..schemas import NormalizedEnvelope

ACTOR = "sabnyx-telegram"
_URL_RE = re.compile(r"^https?://\S+$")
# Matches /start or /help with an optional @botname suffix and/or a trailing argument.
# Examples: /start, /help, /start@your-bot, /start somedeeplink, /help@your-bot
_GREET_RE = re.compile(r"^/(start|help)(@\S+)?(\s.*)?$", re.IGNORECASE)
_QUIET = Console(file=io.StringIO())  # writers want a console; swallow their output

_GREETING = (
    "👋 sabnyx — your vault over Telegram.\n"
    "• send a URL or text to capture it\n"
    "• /ask <question> — answer grounded in your vault\n"
    "• /status — vault health\n"
    "(/start and /help just show this.)"
)


def classify(text: str) -> tuple[str, str]:
    """Map a message to (action, argument). Pure; no side effects."""
    t = text.strip()
    low = t.lower()
    parts = t.split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    if _GREET_RE.match(t):
        return "greet", ""
    if low.startswith(("/ask", "/q ")) or low == "/q":
        return "ask", arg
    if low.startswith(("/status", "/health")):
        return "status", ""
    if low.startswith(("/remember", "/capture")):
        return "ingest_text", arg
    if _URL_RE.match(t):
        return "ingest_url", t
    if t.endswith("?"):
        return "ask", t
    return "ingest_text", t


def handle(env: NormalizedEnvelope) -> str:
    """Act on one trusted envelope; return the reply text. Writes are
    auto-applied (trusted sender). All paths reuse services.py — the same
    write path the CLI uses."""
    text = env.payload.get("text", "")
    action, arg = classify(text)

    ledger.append_event(
        plane="event",
        kind="wire.received",
        actor=ACTOR,
        subject=f"telegram:{env.source_id}",
        payload={"action": action, "actor": env.actor_raw, "kind": env.kind},
    )

    if action == "greet":
        return _GREETING

    if action == "status":
        return _status()

    if action == "ask":
        if not arg:
            return "ask what? send a question, or '/ask <question>'."
        result = services.answer_question(question=arg, actor=ACTOR)
        reply = result.answer.answer
        if result.answer.cited:
            reply += "\n\ncited: " + ", ".join(f"[[{s}]]" for s in result.answer.cited)
        if result.answer.capture is not None:
            rel = services.capture_answer(
                result.answer.capture, question=arg, console=_QUIET, actor=ACTOR
            )
            if rel:
                reply += f"\n\n(captured to {rel})"
        return reply

    # ingest_url / ingest_text
    if action == "ingest_url":
        try:
            body = telegram_fetch(arg)
        except Exception as e:  # noqa: BLE001 — report fetch failure back to the user
            return f"couldn't fetch that URL: {e}"
        title, source_kind, source_url = arg, "web", arg
    else:
        if not arg:
            return "nothing to remember — send some text after /remember."
        body, title, source_kind, source_url = arg, services.title_from(arg), "manual", None

    return _ingest(body, title=title, source_kind=source_kind, source_url=source_url)


def telegram_fetch(url: str) -> str:
    """Indirection point so tests can stub URL fetching."""
    from .. import sources

    return sources.fetch_url(url)


def _ingest(text: str, *, title: str, source_kind: str, source_url: str | None) -> str:
    try:
        head, written = services.ingest_text(
            text=text,
            title=title,
            source_kind=source_kind,
            source_url=source_url,
            console=_QUIET,
            actor=ACTOR,
        )
    except FileExistsError:
        return f"already captured something titled '{title}'."
    if written:
        return f"{head}\n\nfiled {len(written)}: " + ", ".join(written)
    return f"{head}\n\n(nothing new filed — already covered)"


def _status() -> str:
    """A terse health pulse, computed without the full health command."""
    p = services.vault_pulse()
    return f"intake: {p['intake']}  atlas: {p['atlas']}  pending observations: {p['observations']}"


def _dispatch_batch(updates: list[dict], trusted: set[str]) -> int:
    """Handle one batch of raw updates; return the next offset."""
    next_offset = 0
    for update in updates:
        next_offset = max(next_offset, int(update.get("update_id", 0)) + 1)
        env = telegram.envelope_for_update(update)
        if env is None:
            continue
        chat_id = env.payload.get("chat_id")
        if trusted and env.actor_raw not in trusted:
            ledger.append_event(
                plane="event",
                kind="wire.rejected",
                actor=ACTOR,
                subject=f"telegram:{env.source_id}",
                payload={"actor": env.actor_raw, "reason": "not in allowlist"},
            )
            continue
        try:
            reply = handle(env)
        except Exception as e:  # noqa: BLE001 — never let one message kill the loop
            reply = f"sorry, that failed: {e}"
            ledger.append_event(
                plane="event",
                kind="wire.errored",
                actor=ACTOR,
                subject=f"telegram:{env.source_id}",
                payload={"error": str(e)},
            )
        if chat_id is not None:
            telegram.send_message(chat_id, reply)
    return next_offset


@click.command("serve")
@click.option("--once", is_flag=True, help="Drain the current backlog and exit (cron/testing).")
@click.option("--timeout", default=30, help="Long-poll timeout in seconds.")
def serve_cmd(once: bool, timeout: int) -> None:
    """Run the Telegram interface: long-poll, route, act, reply."""
    console = Console()
    ensure_tree()

    trusted = telegram.allowlist()
    if not trusted:
        console.print(
            "[yellow]warning:[/yellow] SABNYX_TELEGRAM_ALLOW is empty — "
            "every sender would be trusted. Refusing to run open."
        )
        raise SystemExit(2)

    console.print(f"[green]serving[/green] (trusted senders: {len(trusted)}). ctrl-c to stop.")
    offset: int | None = None
    while True:
        try:
            updates = telegram.get_updates(offset, timeout=timeout)
        except Exception as e:  # noqa: BLE001 — transient network; back off and retry
            console.print(f"[dim]poll error: {e}; retrying in 5s[/dim]")
            time.sleep(5)
            continue
        if updates:
            offset = _dispatch_batch(updates, trusted)
        if once:
            break
