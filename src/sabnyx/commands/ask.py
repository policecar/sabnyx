"""`sabnyx ask` — query the compiled vault; good answers become pages.

Phase 4 of the plan. Orchestration lives in services.py (shared with the
chat interface); this command owns routing display, rendering, and the
capture gate.

  1. route the question through the rule-based scorer (`[fast]`/`[deep]`
     override in the question itself, jibot iblai-router style)
  2. assemble context + answer, grounded in the vault, citing [[wikilinks]]
  3. if the model flagged durable, novel insight, offer to file it into
     intake/ (Karpathy: "good answers become new pages"; jibot's
     capture-to-jibrain)
"""

from __future__ import annotations

import sys

import click
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape

from .. import llm, services
from ..paths import ensure_tree
from ..util import slugify


@click.command("ask")
@click.argument("question")
@click.option(
    "--role",
    type=click.Choice(["fast", "standard", "deep"]),
    default=None,
    help="Override the router-chosen model role.",
)
@click.option(
    "--capture/--no-capture",
    "capture_flag",
    default=None,
    help="Force or suppress filing the answer into intake/ (default: ask).",
)
@click.option("--yes", "-y", is_flag=True, help="Accept a proposed capture without prompting.")
def ask_cmd(question: str, role: str | None, capture_flag: bool | None, yes: bool) -> None:
    """Answer a question from the vault, citing [[wikilinks]].

    Durable answers can be filed back into intake/ so the vault
    compounds from use, not just from ingestion.
    """
    console = Console()
    ensure_tree()

    if not question.strip():
        console.print("[red]empty question[/red]")
        sys.exit(2)

    result = services.answer_question(question=question, role_override=role)
    console.print(
        f"[dim]role: {result.role}, model: {llm.model_for(result.role)}[/dim]"  # type: ignore[arg-type]
    )

    answer = result.answer
    console.print()
    console.print(Markdown(answer.answer))
    if answer.cited:
        # escape so Rich renders the [[wikilink]] brackets literally
        cites = escape(", ".join(f"[[{s}]]" for s in answer.cited))
        console.print(f"\n[dim]cited: {cites}[/dim]")

    # -- capture: the answer becomes a page ------------------------------------------
    if answer.capture is None or capture_flag is False:
        return

    pi = answer.capture
    console.print(
        f"\n[bold]capture proposed:[/bold] {slugify(pi.title)}.md ({pi.type})\n"
        f"  [dim]{pi.description}[/dim]"
    )
    accept = capture_flag is True or yes or click.confirm("file into intake/?", default=True)
    if not accept:
        console.print("[dim]declined; answer not captured[/dim]")
        return

    rel = services.capture_answer(pi, question=question, console=console)
    if rel:
        console.print("[green]captured[/green] — triage will route it like any intake")
