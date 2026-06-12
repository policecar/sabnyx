"""`sabnyx ask` — query the compiled vault; good answers become pages.

Phase 4 of the plan. One pass:

  1. route the question through the rule-based scorer (`[fast]`/`[deep]`
     override in the question itself, jibot iblai-router style)
  2. assemble context: atlas excerpts + intake description index
  3. one structured LLM call -> AskAnswer: a grounded answer citing
     [[wikilinks]], plus an optional capture proposal
  4. if the model flagged durable, novel insight, offer to file it into
     intake/ (Karpathy: "good answers become new pages rather than
     disappearing into chat history"; jibot's capture-to-jibrain)

An answer is a projection of the vault, so the ledger event sits on the
projection plane. A capture is a normal intake mutation and goes through
the same validated writer the ingest pass uses.
"""

from __future__ import annotations

import sys
from datetime import date as date_cls

import click
from rich.console import Console
from rich.markdown import Markdown

from .. import context, ledger, llm, paths, writers
from ..paths import ensure_tree
from ..schemas import AskAnswer
from ..util import slugify

ACTOR = "sabnyx-ask"

_TASK_SYSTEM = """You are the surfacing agent for this vault: you answer the operator's
questions from the compiled knowledge provided. Produce an AskAnswer.

Rules, in addition to the SPEC and ROUTING contract above:

- Ground the answer in the vault. Cite every claim drawn from a vault file as a
  [[wikilink]] to its slug, inline where the claim is made, and list those
  slugs in `cited`. Only cite slugs that appear in the material provided.
- If the vault does not cover (part of) the question, say so plainly and keep
  any general-knowledge additions clearly marked as such. Never imply vault
  support for an unsupported claim.
- Set `capture` ONLY when answering produced durable, novel insight: a
  synthesis across files, a connection not yet recorded, a decision. A capture
  must add something no existing file's description already covers. Simple
  lookups, restatements of one file, and negative results get capture = null.
- A capture follows intake conventions: type is usually `thought` (synthesis,
  decision) or `note`; description 30-200 chars, distinct from the title,
  dense; body terse, specific, with [[wikilinks]] to the files it builds on.
- Answer voice: terse but not curt; specific over abstract; no filler."""


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

    chosen_role = role or llm.role_for_message(question)
    console.print(
        f"[dim]role: {chosen_role}, model: {llm.model_for(chosen_role)}[/dim]"  # type: ignore[arg-type]
    )

    index = context.slug_index(paths.ATLAS)
    user = (
        "## Atlas (durable knowledge; excerpts)\n"
        f"{context.excerpts(index)}\n\n"
        f"{context.description_index(paths.INTAKE, 'Intake queue (drafts, not yet authoritative)')}\n\n"
        f"## Question\n{question}"
    )

    result = llm.parse_structured(
        task_system=_TASK_SYSTEM,
        user=user,
        output_format=AskAnswer,
        role=chosen_role,  # type: ignore[arg-type]
        actor=ACTOR,
        purpose=f"ask:{slugify(question)[:60]}",
    )

    console.print()
    console.print(Markdown(result.answer))
    if result.cited:
        console.print(f"\n[dim]cited: {', '.join(f'[[{s}]]' for s in result.cited)}[/dim]")

    ledger.append_event(
        plane="projection",
        kind="ask.answered",
        actor=ACTOR,
        subject=f"ask:{slugify(question)[:60]}",
        payload={
            "question_hash": ledger.content_hash({"q": question}),
            "cited": result.cited,
            "capture_proposed": result.capture is not None,
        },
    )

    # -- capture: the answer becomes a page ------------------------------------------
    if result.capture is None or capture_flag is False:
        return

    pi = result.capture
    console.print(
        f"\n[bold]capture proposed:[/bold] {slugify(pi.title)}.md ({pi.type})\n"
        f"  [dim]{pi.description}[/dim]"
    )
    accept = capture_flag is True or yes or click.confirm("file into intake/?", default=True)
    if not accept:
        console.print("[dim]declined; answer not captured[/dim]")
        return

    rel = writers.write_intake(
        pi,
        actor=ACTOR,
        source="manual",
        trailer=f"<!-- source-id:sabnyx-ask:{date_cls.today().isoformat()} -->",
        console=console,
    )
    if rel:
        writers.append_knowledge_log(
            "ask-capture",
            pi.title,
            {"question": question[:200], "file": rel},
        )
        console.print("[green]captured[/green] — triage will route it like any intake")
