"""`sabnyx ingest` — the core operation: source -> structured knowledge.

The first LLM-in-the-loop command (plan §Phase 2). One pass:

  1. capture the raw source into vault/sources/ (immutable, ledgered)
  2. classify extraction depth by word count / URL (jibot tiers)
  3. assemble vault context: atlas + intake description indexes
  4. one structured LLM call -> IngestChangeset (intake files + observations)
  5. human approval gate (unless --yes), then apply through the
     validated writers, ledger every mutation, append knowledge-log

Orchestration lives in services.py so the chat interface shares one write
path. This command owns input acquisition, the approval gate, and output.

Containment: ingest writes to intake/ and sources/ only — never atlas/.
Promotion stays a separate, deliberate act (triage/promote), exactly the
defense-in-depth the jibot pipeline prescribes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click
from rich.console import Console

from .. import llm, services, sources
from ..paths import ROOT, ensure_tree
from ..services import ACTOR_INGEST as ACTOR
from ..util import slugify


@click.command("ingest")
@click.argument("source_file", required=False, type=click.Path(exists=True, path_type=Path))
@click.option("--url", default=None, help="Ingest a URL instead of a local file.")
@click.option("--stdin", "use_stdin", is_flag=True, help="Read the source from stdin.")
@click.option("--title", default=None, help="Source title (required for --stdin).")
@click.option(
    "--role",
    type=click.Choice(["fast", "standard", "deep"]),
    default=None,
    help="Override the depth-derived model role.",
)
@click.option("--yes", "-y", is_flag=True, help="Apply without the approval prompt.")
@click.option("--dry-run", is_flag=True, help="Show the proposed changeset; write nothing.")
def ingest_cmd(
    source_file: Path | None,
    url: str | None,
    use_stdin: bool,
    title: str | None,
    role: str | None,
    yes: bool,
    dry_run: bool,
) -> None:
    """Ingest a source: capture raw, extract structured knowledge to intake/."""
    console = Console()
    ensure_tree()

    # -- 1. acquire the raw text ------------------------------------------------
    if sum(map(bool, (source_file, url, use_stdin))) != 1:
        console.print("[red]provide exactly one of: FILE, --url, --stdin[/red]")
        sys.exit(2)

    if url:
        text = sources.fetch_url(url)
        src_title = title or url
        src_kind = "web"
    elif use_stdin:
        if not title:
            console.print("[red]--title is required with --stdin[/red]")
            sys.exit(2)
        text = sys.stdin.read()
        src_title = title
        src_kind = "manual"
    else:
        assert source_file is not None
        text = source_file.read_text(encoding="utf-8", errors="replace")
        src_title = title or source_file.stem
        src_kind = "import"

    if not text.strip():
        console.print("[red]source is empty[/red]")
        sys.exit(1)

    # -- 2. capture raw -------------------------------------------------------------
    if not dry_run:
        try:
            src_path = sources.capture(text=text, slug=src_title, actor=ACTOR, source_url=url)
        except FileExistsError as e:
            console.print(f"[red]{e}[/red]")
            sys.exit(1)
        src_rel = str(src_path.relative_to(ROOT))
    else:
        src_rel = f"vault/sources/{slugify(src_title)}.md (dry run, not written)"

    # -- 3 + 4. propose (classify, assemble, extract) -------------------------------
    proposal = services.build_ingest_proposal(
        text=text,
        title=src_title,
        source_kind=src_kind,
        source_url=url,
        src_rel=src_rel,
        role_override=role,
        actor=ACTOR,
    )
    console.print(
        f"captured [bold]{src_rel}[/bold]  "
        f"(depth: {proposal.depth}, role: {proposal.role}, model: {llm.model_for(proposal.role)})"  # type: ignore[arg-type]
    )

    changeset = proposal.changeset
    console.print(f"\n[bold]Proposed:[/bold] {changeset.summary}\n")
    for pi in changeset.intake_files:
        console.print(f"  [green]+ intake[/green] {slugify(pi.title)}.md ({pi.type})")
        console.print(f"    [dim]{pi.description}[/dim]")
    for ob in changeset.observations:
        console.print(f"  [yellow]+ observation[/yellow] ({ob.category}) {slugify(ob.title)}")
    if not changeset.intake_files and not changeset.observations:
        console.print("  [dim]nothing to write — source already covered or empty[/dim]")

    # -- 5. approval gate ----------------------------------------------------------
    if dry_run:
        console.print("\n[dim]dry run: nothing written[/dim]")
        return
    if not changeset.intake_files and not changeset.observations:
        return
    if not yes and not click.confirm("\napply?", default=True):
        console.print("[dim]declined; raw source kept, nothing else written[/dim]")
        return

    # -- 6. apply through the validated writers --------------------------------------
    written = services.apply_ingest(
        proposal,
        title=src_title,
        source_kind=src_kind,
        source_url=url,
        src_rel=src_rel,
        console=console,
        actor=ACTOR,
    )
    console.print(
        f"\n[green]ingest complete[/green] ({len(written)} file(s)). "
        "next: [bold]sabnyx triage[/bold] when the queue warrants it."
    )
