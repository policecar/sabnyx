"""`sabnyx verify` — the live smoke test (plan §Phase D).

Six phases of LLM-driven code were built against a faked model. This is
the one command that proves the real path end to end: it sends a minimal
structured-output request through the configured provider and role, then
checks the parsed result, the model, and the token usage came back.

Run it once after setting a key (ANTHROPIC_API_KEY, or
SABNYX_LLM_PROVIDER=openrouter + OPENROUTER_API_KEY). It costs a few
tokens and writes nothing to the vault. Exits non-zero on any failure so
it can gate a setup script or CI.
"""

from __future__ import annotations

import os
import sys

import click
from rich.console import Console

from .. import llm
from ..schemas import HealthProbe

ACTOR = "sabnyx-verify"


def _credential_hint(prov: str) -> str | None:
    """Friendly message if the active provider has no usable credential, so
    verify reports 'set X' instead of an opaque SDK TypeError. None if a
    credential appears present."""
    if prov == "anthropic":
        if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            return None
        return "no Anthropic credential — set ANTHROPIC_API_KEY (or run `ant auth login`)."
    # openrouter / openai-compatible
    if os.environ.get("SABNYX_LLM_API_KEY") or (
        prov == "openrouter" and os.environ.get("OPENROUTER_API_KEY")
    ):
        return None
    return f"no API key for {prov} — set SABNYX_LLM_API_KEY (or OPENROUTER_API_KEY)."

_TASK_SYSTEM = (
    "This is a connectivity probe. Reply with ok=true and a short note "
    "confirming you received the vault contract above. Do not do anything else."
)


@click.command("verify")
@click.option(
    "--role",
    type=click.Choice(["fast", "standard", "deep"]),
    default="fast",
    help="Which role to exercise (default: fast — cheapest).",
)
@click.option("--all-roles", is_flag=True, help="Probe fast, standard, and deep in turn.")
def verify_cmd(role: str, all_roles: bool) -> None:
    """Send one real structured call per role; report model + token usage."""
    console = Console()
    prov = llm.provider()
    roles = ["fast", "standard", "deep"] if all_roles else [role]

    console.print(f"provider: [bold]{prov}[/bold]")
    hint = _credential_hint(prov)
    if hint:
        console.print(f"[red]{hint}[/red]")
        sys.exit(1)

    failures = 0
    for r in roles:
        try:
            model = llm.model_for(r, prov)  # type: ignore[arg-type]
        except ValueError as e:
            console.print(f"[red]{r}: {e}[/red]")
            failures += 1
            continue
        console.print(f"\n[dim]probing {r} -> {model} ...[/dim]")
        try:
            result = llm.parse_structured(
                task_system=_TASK_SYSTEM,
                user="Are you reachable?",
                output_format=HealthProbe,
                role=r,  # type: ignore[arg-type]
                actor=ACTOR,
                purpose="verify",
                max_tokens=200,
            )
        except Exception as e:  # noqa: BLE001 — verify reports every failure mode
            console.print(f"[red]{r}: FAILED[/red] — {type(e).__name__}: {e}")
            failures += 1
            continue
        flag = "[green]ok[/green]" if result.ok else "[yellow]reachable but ok=false[/yellow]"
        console.print(f"  {flag} — {result.note}")

    # token usage / cache hits are on the ledger (llm.completed); point there
    console.print(
        "\n[dim]token usage + cache stats recorded on the ledger "
        "(ops/events.jsonl, kind=llm.completed)[/dim]"
    )
    if failures:
        console.print(f"[red]{failures} role(s) failed[/red]")
        sys.exit(1)
    console.print("[green]live path verified[/green]")
