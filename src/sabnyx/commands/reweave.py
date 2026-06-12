"""`sabnyx reweave` — backward enrichment after promotion (plan §Phase 3).

Consumes the `reweave.proposed` intents that `promote` has been emitting
since stage 0. For each target (a recently promoted atlas file):

  1. assemble context: the target in full, every other atlas file as a
     description + body excerpt, pending observations
  2. one structured LLM call -> ReweaveChangeset
  3. human approval gate (unless --yes), then apply: additions to
     `sabnyx:related` marker blocks ONLY (prose is never touched) and
     observation files for contradictions/gaps/connections
  4. emit `reweave.completed` so the intent is consumed

Deterministic guards backstop the model: proposed links are dropped
unless the slug exists, is not a self-link, and is not already linked.

jibot's link priority order is carried through: bridge (connect
unconnected clusters) > enrichment > correction > low.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click
from rich.console import Console

from .. import context, fm, ledger, llm, paths, writers
from ..schemas import ReweaveChangeset
from ..util import extract_wikilinks, get_marker_block, set_marker_block

ACTOR = "sabnyx-reweave"

_TASK_SYSTEM = """You are the reweave agent for this vault: the backward pass that runs
after a file is promoted to atlas/. Knowledge compounds through connection, not
accumulation — connecting 5 existing concepts is worth more than 5 new files.

Given the target (recently promoted) and the rest of the atlas, produce a
ReweaveChangeset. Rules, in addition to the SPEC and ROUTING contract above:

- related_additions edit `sabnyx:related` marker blocks ONLY. `file` is the
  slug of the atlas file to edit — other files that should link to the target
  (backward links), and/or the target itself (outbound links). Check both
  directions: does A link to B? does B link to A?
- Only propose slugs that appear in the material provided. Never invent files.
- Priority order: bridge (connects previously unconnected clusters — highest
  value) > enrichment (file gains context) > correction (new information
  updates old). Skip pairs that already share 3+ connections; skip links that
  already exist.
- Contradiction scan: if the target makes a claim that conflicts with an
  existing atlas file (roles, dates, stances, mechanisms), propose an
  observation with category: contradiction, BOTH files in `involves`, and a
  suggested resolution in the body. This is how the vault detects stale claims.
- Gaps and recurring unlinked co-occurrences may become gap / connection
  observations. Be conservative; propose nothing if nothing qualifies.
- Reasons are one line, specific, no filler."""


def _pending_targets() -> list[Path]:
    """Atlas files with a `reweave.proposed` intent not yet consumed by a
    `reweave.completed` event. The ledger is the queue."""
    proposed: dict[str, None] = {}  # insertion-ordered set
    done: set[str] = set()
    for e in ledger.replay():
        if e.kind == "reweave.proposed":
            proposed[e.subject] = None
        elif e.kind == "reweave.completed":
            done.add(e.subject)
    out: list[Path] = []
    for subj in proposed:
        if subj in done:
            continue
        p = paths.ROOT / subj
        if p.is_file():
            out.append(p)
    return out


def _context_for(target: Path, index: dict[str, Path]) -> str:
    """Target in full; every other atlas file as description + excerpt;
    pending observations. Grep-era retrieval — semantic search is phase 5."""
    t_meta, t_body, _ = fm.load(target)
    parts = [
        f"## Target (recently promoted): {target.stem}",
        f"description: {t_meta.get('description', '')}",
        f"tags: {', '.join(t_meta.get('tags', []))}",
        "",
        t_body.strip(),
        "",
        "## Rest of the atlas (excerpts)",
        context.excerpts(index, exclude=target)
        if len(index) > 1
        else "(the atlas contains no other files)",
    ]

    obs = sorted(paths.OBSERVATIONS.glob("*.md"))
    if obs:
        parts.append("## Pending observations")
        for p in obs:
            try:
                meta, _body, _ = fm.load(p)
            except Exception:
                continue
            involves = ", ".join(meta.get("involves", []))
            parts.append(
                f"- [{meta.get('category')}] {p.stem}"
                + (f" (involves: {involves})" if involves else "")
            )
    return "\n".join(parts)


def _apply_links(
    target: Path, changeset: ReweaveChangeset, index: dict[str, Path], console: Console
) -> list[str]:
    """Apply marker-block link additions with deterministic guards.
    Returns relative paths of files actually modified."""
    modified: list[str] = []
    for add in changeset.related_additions:
        path = index.get(add.file)
        if path is None:
            console.print(f"  [yellow]drop[/yellow] unknown file slug: {add.file}")
            continue
        try:
            meta, body, _ = fm.load(path)
        except fm.FrontmatterError as e:
            console.print(f"  [red]skip (invalid frontmatter)[/red] {add.file}: {e.errors}")
            continue

        existing = {link.lower() for link in extract_wikilinks(body)}
        new_links = [
            link
            for link in add.add_links
            if link in index and link != add.file and link.lower() not in existing
        ]
        dropped = [link for link in add.add_links if link not in new_links]
        if dropped:
            console.print(f"  [dim]drop (unknown/self/duplicate): {', '.join(dropped)}[/dim]")
        if not new_links:
            continue

        current = get_marker_block(body, "related")
        lines = [ln for ln in (current or "").splitlines() if ln.strip()]
        lines += [f"- [[{link}]]" for link in new_links]
        new_body = set_marker_block(body, "related", lines, heading="Related")
        fm.write(path, meta, new_body)

        rel = str(path.relative_to(paths.ROOT))
        modified.append(rel)
        ledger.append_event(
            kind="atlas.reweaved",
            actor=ACTOR,
            subject=rel,
            payload={
                "target": str(target.relative_to(paths.ROOT)),
                "added_links": new_links,
                "priority": add.priority,
                "reason": add.reason,
            },
        )
        console.print(
            f"  [green]linked[/green] {rel} += {', '.join(f'[[{link}]]' for link in new_links)} "
            f"[dim]({add.priority}: {add.reason})[/dim]"
        )
    return modified


@click.command("reweave")
@click.argument("target", required=False, type=click.Path(exists=True, path_type=Path))
@click.option(
    "--role",
    type=click.Choice(["fast", "standard", "deep"]),
    default="standard",
    help="Model role (default: standard).",
)
@click.option("--yes", "-y", is_flag=True, help="Apply without the approval prompt.")
@click.option("--dry-run", is_flag=True, help="Show the proposed changeset; write nothing.")
def reweave_cmd(target: Path | None, role: str, yes: bool, dry_run: bool) -> None:
    """Backward enrichment: link a promoted file into the existing graph.

    With no TARGET, consumes every pending reweave.proposed intent from
    the ledger. With a TARGET, reweaves just that atlas file.
    """
    console = Console()

    if target is not None:
        target = target.resolve()
        if paths.ATLAS not in target.parents:
            console.print(f"[red]{target} is not under vault/atlas/[/red]")
            sys.exit(2)
        targets = [target]
    else:
        targets = _pending_targets()
        if not targets:
            console.print("[dim]no pending reweave.proposed intents; nothing to do[/dim]")
            return
        console.print(f"pending reweave targets: [bold]{len(targets)}[/bold]")

    index = context.slug_index(paths.ATLAS)

    for tgt in targets:
        rel = str(tgt.relative_to(paths.ROOT))
        console.print(f"\n[bold]reweave[/bold] {rel}  (role: {role}, model: {llm.model_for(role)})")

        changeset = llm.parse_structured(
            task_system=_TASK_SYSTEM,
            user=_context_for(tgt, index),
            output_format=ReweaveChangeset,
            role=role,  # type: ignore[arg-type]
            actor=ACTOR,
            purpose=f"reweave:{tgt.stem}",
        )

        console.print(f"[bold]Proposed:[/bold] {changeset.summary}")
        for add in changeset.related_additions:
            console.print(
                f"  [green]+ links[/green] {add.file} += "
                f"{', '.join(add.add_links)} [dim]({add.priority}: {add.reason})[/dim]"
            )
        for ob in changeset.observations:
            console.print(f"  [yellow]+ observation[/yellow] ({ob.category}) {ob.title}")
        if not changeset.related_additions and not changeset.observations:
            console.print("  [dim]nothing qualifies — graph already coherent[/dim]")

        if dry_run:
            console.print("[dim]dry run: nothing written[/dim]")
            continue

        has_changes = bool(changeset.related_additions or changeset.observations)
        if has_changes and not yes and not click.confirm("apply?", default=True):
            console.print("[dim]declined; intent left pending[/dim]")
            continue

        modified = _apply_links(tgt, changeset, index, console)
        obs_written: list[str] = []
        for ob in changeset.observations:
            written = writers.write_observation(ob, actor=ACTOR, console=console)
            if written:
                obs_written.append(written)

        ledger.append_event(
            kind="reweave.completed",
            actor=ACTOR,
            subject=rel,
            payload={
                "summary": changeset.summary,
                "files_linked": modified,
                "observations": obs_written,
            },
        )
        console.print(
            f"[green]reweave complete[/green] "
            f"({len(modified)} file(s) linked, {len(obs_written)} observation(s))"
        )
