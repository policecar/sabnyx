"""`sabnyx reweave` — backward enrichment after promotion (plan §Phase 3).

Consumes the reweave queue: every atlas file whose last reweave-relevant
ledger event is `atlas.promoted` or the `reweave.proposed` intent that
`promote` emits beside it (see _pending_targets). For each target:

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

from .. import context, fm, ledger, llm, paths, retrieval, writers
from .. import observations as obs
from ..schemas import ModelRole, ReweaveChangeset
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
  observation with category: contradiction, exactly two files in `involves`
  (the target and the conflicting file), and a suggested resolution in the
  body. This is how the vault detects stale claims.
- Gaps and recurring unlinked co-occurrences may become gap / connection
  observations. Be conservative; propose nothing if nothing qualifies.
- Reasons are one line, specific, no filler."""


# Events that put a subject in the reweave queue. `atlas.promoted` counts on
# its own: promote_file appends it and `reweave.proposed` separately, so a
# failure between the two appends would otherwise leave a committed promotion
# with no queued reweave and nothing that ever retries it (R05).
_QUEUES = ("atlas.promoted", "reweave.proposed")


def _pending_targets() -> list[Path]:
    """Atlas files with reweave work still owed, derived by replaying the ledger.

    Per subject, the last relevant event wins (the same rule as
    opsctl/src/project.rs):

      atlas.promoted, reweave.proposed  -> pending
      reweave.completed                 -> done
      atlas.removed                     -> forgotten

    So a promotion is queued even if its `reweave.proposed` append never
    landed; a completed reweave stays done until the path is promoted (or
    proposed) again; and an intent left behind by a removed page never
    resurfaces against a page later written to the same path by hand.
    The listing is read-only: recovery writes nothing, and replaying twice
    gives the same answer. A subject whose file is missing is skipped.
    """
    status: dict[str, bool] = {}  # subject -> pending?, insertion-ordered
    for e in ledger.replay():
        if e.kind in _QUEUES:
            status[e.subject] = True
        elif e.kind == "reweave.completed":
            status[e.subject] = False
        elif e.kind == "atlas.removed":
            status.pop(e.subject, None)
    out: list[Path] = []
    for subj, pending in status.items():
        p = paths.DATA_ROOT / subj
        if pending and p.is_file():
            out.append(p)
    return out


def _context_for(target: Path, index: dict[str, Path]) -> str:
    """Target in full; every other atlas file as description + excerpt;
    pending observations. Retrieval goes through the Retriever seam, ranking
    the rest of the atlas by overlap with the target page."""
    t_meta, t_body, _ = fm.load(target)
    query = " ".join([target.stem, t_meta.get("description", ""), " ".join(t_meta.get("tags", []))])
    parts = [
        f"## Target (recently promoted): {target.stem}",
        f"description: {t_meta.get('description', '')}",
        f"tags: {', '.join(t_meta.get('tags', []))}",
        "",
        t_body.strip(),
        "",
        "## Rest of the atlas (excerpts)",
        retrieval.get_retriever().retrieve(query, index, exclude=target)
        if len(index) > 1
        else "(the atlas contains no other files)",
    ]

    # Pending only: an archived (dismissed) signal must not reach the model.
    pending = obs.observations()
    if pending:
        parts.append("## Pending observations")
        for o in pending:
            involves = ", ".join(o.fm.involves)
            parts.append(
                f"- [{o.fm.category}] {o.path.stem}"
                + (f" (involves: {involves})" if involves else "")
            )
    return "\n".join(parts)


def _apply_links(
    target: Path, changeset: ReweaveChangeset, index: dict[str, Path], console: Console
) -> list[str]:
    """Apply marker-block link additions with deterministic guards.
    Returns relative paths of files actually modified.

    @decision DEC-RW-001: patch the raw file text, never load → fm.write.
    @rationale: a parse/serialize round trip drops YAML comments, reorders
      and requotes keys, turns CRLF into LF and strips the body's leading
      indentation (an indented code block becomes prose). Reweave may write
      marker blocks only (AGENTS §2, SPEC §8), so it reads the file with no
      newline translation, validates it with fm.load_text, edits only the
      body half of fm.split_text via set_marker_block, and checks that the
      frontmatter re-parses unchanged before the atomic write. Rendered
      lines take the newline of the file's first line.
    """
    modified: list[str] = []
    for add in changeset.related_additions:
        # index keys are lowercased slugs (context.slug_index)
        path = index.get(add.file.lower())
        if path is None:
            console.print(f"  [yellow]drop[/yellow] unknown file slug: {add.file}")
            continue
        # bytes → str without universal-newline translation (CRLF survives)
        raw = path.read_bytes().decode("utf-8")
        try:
            meta, body = fm.load_text(raw, fm.classify(path), path=path)
        except fm.FrontmatterError as e:
            console.print(f"  [red]skip (invalid frontmatter)[/red] {add.file}: {e.errors}")
            continue

        existing = {link.lower() for link in extract_wikilinks(body)}
        new_links: list[str] = []
        dropped: list[str] = []
        for link in add.add_links:
            key = link.lower()
            if key in index and key != add.file.lower() and key not in existing:
                new_links.append(link)
                existing.add(key)  # dedup repeats within one add_links list
            else:
                # Classified inline (not via `link not in new_links` after the
                # fact) so an exact duplicate in add_links -- e.g. ["Foo",
                # "Foo"] -- is still reported: the first "Foo" is kept, the
                # second is dropped as a duplicate, not silently uncounted.
                dropped.append(link)
        if dropped:
            console.print(f"  [dim]drop (unknown/self/duplicate): {', '.join(dropped)}[/dim]")
        if not new_links:
            continue

        head, raw_body = fm.split_text(raw)
        newline = "\r\n" if raw.split("\n", 1)[0].endswith("\r") else "\n"
        current = get_marker_block(raw_body, "related") or ""
        # split on "\n" only (not str.splitlines, which also breaks NEL/LS/PS)
        lines: list[str] = [ln.removesuffix("\r") for ln in current.split("\n") if ln.strip()]
        lines += [f"- [[{link}]]" for link in new_links]
        try:
            new_raw_body = set_marker_block(
                raw_body, "related", lines, heading="Related", newline=newline
            )
        except ValueError as e:
            # half-open or ambiguous marker block (manual edit damage): report
            # and skip the file rather than nest/duplicate markers (SPEC §8)
            console.print(f"  [red]skip (corrupt marker block)[/red] {add.file}: {e}")
            continue
        patched = head + new_raw_body
        after_meta, _ = fm.load_text(patched, fm.classify(path), path=path)
        if after_meta != meta:
            # unreachable by construction (only body bytes change); never
            # write a file whose metadata this edit would alter
            console.print(f"  [red]skip (patch altered frontmatter)[/red] {add.file}")
            continue
        fm.write_text_atomic(path, patched)

        rel = str(path.relative_to(paths.DATA_ROOT))
        modified.append(rel)
        ledger.append_event(
            kind="atlas.reweaved",
            actor=ACTOR,
            subject=rel,
            payload={
                "target": str(target.relative_to(paths.DATA_ROOT)),
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


def reweave_one(
    tgt: Path,
    index: dict[str, Path],
    *,
    role: ModelRole,
    actor: str,
    yes: bool,
    dry_run: bool,
    console: Console,
) -> tuple[list[str], list[str]] | None:
    """Reweave one atlas file: LLM proposal → approval gate → apply links and
    observations → ledger ``reweave.completed``.

    Returns ``(files_linked, observations_written)`` when changes are applied,
    or ``None`` on a dry run or when the operator declines (intent left
    pending). Callers own their per-target framing and decide whether to call
    this under --dry-run at all: tend skips it to avoid the LLM call, while
    reweave_cmd calls it to preview the changeset.
    """
    changeset = llm.parse_structured(
        task_system=_TASK_SYSTEM,
        user=_context_for(tgt, index),
        output_format=ReweaveChangeset,
        role=role,
        actor=actor,
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
        return None

    has_changes = bool(changeset.related_additions or changeset.observations)
    if has_changes and not yes:
        try:
            approved = click.confirm("apply?", default=True)
        except click.Abort:
            # non-tty without --yes (cron): leave pending, don't crash the run
            console.print("[yellow]no interactive input; intent left pending (use --yes)[/yellow]")
            return None
        if not approved:
            console.print("[dim]declined; intent left pending[/dim]")
            return None

    modified = _apply_links(tgt, changeset, index, console)
    obs_written: list[str] = []
    outcomes: list[writers.WriteOutcome] = []
    for ob in changeset.observations:
        out = writers.write_observation(ob, producer="reweave", actor=actor, console=console)
        outcomes.append(out)
        if out.kind in ("written", "reopened"):
            obs_written.append(out.rel)

    ledger.append_event(
        kind="reweave.completed",
        actor=actor,
        subject=str(tgt.relative_to(paths.DATA_ROOT)),
        payload={
            "summary": changeset.summary,
            "files_linked": modified,
            "observations": obs_written,
        },
    )
    # One run = one reweave_one call (IDD §6): dismissed signals it met are
    # counted once each, after the completion event.
    writers.ledger_suppressed(outcomes, actor=actor)
    return modified, obs_written


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
def reweave_cmd(target: Path | None, role: ModelRole, yes: bool, dry_run: bool) -> None:
    """Backward enrichment: link a promoted file into the existing graph.

    With no TARGET, reweaves every atlas file the ledger says is still owed
    one: promoted or proposed, and not completed since. With a TARGET,
    reweaves just that atlas file.
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
            console.print("[dim]no pending reweave work; nothing to do[/dim]")
            return
        console.print(f"pending reweave targets: [bold]{len(targets)}[/bold]")

    index = context.slug_index(paths.ATLAS)

    for tgt in targets:
        rel = str(tgt.relative_to(paths.DATA_ROOT))
        console.print(f"\n[bold]reweave[/bold] {rel}  (role: {role}, model: {llm.model_for(role)})")

        result = reweave_one(
            tgt, index, role=role, actor=ACTOR, yes=yes, dry_run=dry_run, console=console
        )
        if result is None:
            continue
        modified, obs_written = result
        console.print(
            f"[green]reweave complete[/green] "
            f"({len(modified)} file(s) linked, {len(obs_written)} observation(s))"
        )
