"""`sabnyx tend` — the heartbeat: two-speed triage + reweave + health.

One schedulable command that drives the self-maintaining vault cycle:

  1. Auto-promote pass  — deterministic gate, no LLM.
  2. LLM triage pass   — batched model call for deferred items (≥ threshold).
  3. Reweave pass       — consume pending reweave.proposed intents.
  4. Health pass        — print a concise audit summary.

Every mutation is ledgered. --dry-run writes nothing and makes no calls to
promote_file or the reweave apply path.

@decision DEC-01: tend is a thin orchestrator; all logic lives in services
@rationale: The four passes reuse services.promote_file, services.triage_batch,
  reweave._pending_targets/_apply_links, and health helpers. tend just coordinates
  them and handles --dry-run / --yes gating. This keeps the command testable
  without re-implementing logic.
@decision DEC-02: discard means "note it, do not delete"
@rationale: Auto-deletion of vault files without explicit operator action is too
  destructive. LLM "discard" decisions are surfaced as report lines only.
"""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from .. import context, fm, ledger
from ..paths import ATLAS, INTAKE, ensure_tree
from ..services import (
    ACTOR_TEND,
    AutoPromoteDecision,
    auto_promote_decision,
    promote_file,
    triage_batch,
)


def _atlas_slugs() -> set[str]:
    """All slugs currently in atlas/ (case-insensitive)."""
    return {p.stem.lower() for p in ATLAS.rglob("*.md") if p.is_file()}


def _intake_files() -> list[Path]:
    return sorted(p for p in INTAKE.glob("*.md") if p.is_file())


def _reweave_pending(*, dry_run: bool, yes: bool, console: Console) -> int:
    """Consume pending reweave.proposed intents.

    Reuses reweave.py's internal helpers directly so we don't duplicate the
    LLM call, guard logic, or ledger events. Returns count of files reweaved.
    """
    from . import reweave as rw

    targets = rw._pending_targets()
    if not targets:
        console.print("[dim]reweave: no pending intents[/dim]")
        return 0

    console.print(f"reweave: [bold]{len(targets)}[/bold] pending target(s)")
    idx = context.slug_index(ATLAS)
    reweaved = 0

    for tgt in targets:
        from .. import llm
        from ..schemas import ReweaveChangeset

        rel = str(tgt.relative_to(ATLAS.parent.parent))  # relative to ROOT
        console.print(f"  [bold]reweave[/bold] {rel}")

        if dry_run:
            console.print("    [dim]dry run: skipping LLM call[/dim]")
            continue

        changeset = llm.parse_structured(
            task_system=rw._TASK_SYSTEM,
            user=rw._context_for(tgt, idx),
            output_format=ReweaveChangeset,
            role="standard",
            actor=ACTOR_TEND,
            purpose=f"reweave:{tgt.stem}",
        )

        console.print(f"  [bold]Proposed:[/bold] {changeset.summary}")
        has_changes = bool(changeset.related_additions or changeset.observations)
        if has_changes and not yes and not click.confirm("  apply?", default=True):
            console.print("  [dim]declined; intent left pending[/dim]")
            continue

        modified = rw._apply_links(tgt, changeset, idx, console)
        from .. import writers

        obs_written: list[str] = []
        for ob in changeset.observations:
            written = writers.write_observation(ob, actor=ACTOR_TEND, console=console)
            if written:
                obs_written.append(written)

        ledger.append_event(
            kind="reweave.completed",
            actor=ACTOR_TEND,
            subject=rel,
            payload={
                "summary": changeset.summary,
                "files_linked": modified,
                "observations": obs_written,
            },
        )
        reweaved += 1

    return reweaved


def _health_summary(console: Console) -> None:
    """Print a concise health table (reuses health.py helpers)."""
    from . import health as health_mod

    idx = health_mod._slug_index()
    intake_count = len(list(INTAKE.glob("*.md")))
    atlas_count = sum(1 for p in ATLAS.rglob("*.md") if p.is_file())
    pending_obs = sum(1 for _ in (INTAKE / ".observations").glob("*.md"))
    broken = 0
    for path in idx.values():
        try:
            _meta, body, _kind = fm.load(path)
        except fm.FrontmatterError:
            continue
        from ..util import extract_wikilinks

        for target in extract_wikilinks(body):
            if target.lower().strip() not in idx:
                broken += 1

    t = Table(title="health summary")
    t.add_column("Check")
    t.add_column("Result", justify="right")
    t.add_row("intake queue", str(intake_count))
    t.add_row("atlas files", str(atlas_count))
    t.add_row("pending observations", str(pending_obs))
    t.add_row("broken wikilinks", str(broken))
    t.add_row("ledger events", str(ledger.count()))
    console.print(t)


@click.command("tend")
@click.option("--dry-run", is_flag=True, help="Show the plan; write nothing.")
@click.option("--yes", "-y", is_flag=True, help="Apply LLM triage decisions without prompting.")
@click.option(
    "--threshold",
    default=5,
    show_default=True,
    type=int,
    help="Min deferred items before calling the LLM triage tier.",
)
@click.option("--no-reweave", is_flag=True, help="Skip the reweave pass.")
@click.option("--no-health", is_flag=True, help="Skip the health summary.")
def tend_cmd(dry_run: bool, yes: bool, threshold: int, no_reweave: bool, no_health: bool) -> None:
    """Heartbeat: auto-promote → LLM triage → reweave → health.

    Runs the full self-maintenance cycle in one schedulable command.
    Use --dry-run to preview without writing anything.
    """
    ensure_tree()
    console = Console()

    if dry_run:
        console.print("[bold yellow]dry run[/bold yellow] — no mutations will be written")

    from ..config import trusted_sources

    trusted = trusted_sources()
    atlas_slugs = _atlas_slugs()
    intake = _intake_files()

    console.print(
        f"\n[bold]pass 1: auto-promote[/bold]  "
        f"({len(intake)} intake file(s), {len(atlas_slugs)} atlas slug(s))"
    )

    auto_promoted: list[Path] = []
    deferred: list[Path] = []

    for p in intake:
        try:
            meta, _body, kind = fm.load(p)
        except fm.FrontmatterError as e:
            console.print(f"  [red]skip (invalid)[/red] {p.name}: {e.errors}")
            deferred.append(p)
            continue

        decision: AutoPromoteDecision = auto_promote_decision(
            meta,
            kind,
            trusted=trusted,
            atlas_slugs=atlas_slugs,
            source_slug=p.stem.lower(),
        )

        if decision.promotable:
            assert decision.atlas_type is not None
            console.print(
                f"  [green]auto-promote[/green] {p.name} → {decision.atlas_type}"
                + (" [dim](dry run)[/dim]" if dry_run else "")
            )
            if not dry_run:
                try:
                    promote_file(p, decision.atlas_type, actor=ACTOR_TEND)
                    auto_promoted.append(p)
                    # Update atlas_slugs so subsequent files in this run don't
                    # get false-positive "no duplicate" passes.
                    atlas_slugs.add(p.stem.lower())
                except RuntimeError as e:
                    console.print(f"  [red]promote failed:[/red] {e}")
                    deferred.append(p)
            else:
                auto_promoted.append(p)
        else:
            reason_str = "; ".join(decision.reasons) if decision.reasons else "no reason"
            console.print(f"  [dim]defer[/dim] {p.name}  ({reason_str})")
            deferred.append(p)

    console.print(
        f"  auto-promoted: [bold]{len(auto_promoted)}[/bold]  "
        f"deferred: [bold]{len(deferred)}[/bold]"
    )

    # -- pass 2: LLM triage --
    llm_triaged = 0
    console.print(
        f"\n[bold]pass 2: LLM triage[/bold]  ({len(deferred)} deferred, threshold {threshold})"
    )

    if len(deferred) >= threshold:
        console.print(f"  calling LLM triage for {len(deferred)} item(s)…")
        if not dry_run:
            batch = triage_batch(deferred, role="standard")
            slug_to_path = {p.stem.lower(): p for p in deferred}

            for item in batch.items:
                path = slug_to_path.get(item.slug.lower())
                if path is None:
                    console.print(f"  [yellow]unknown slug from LLM:[/yellow] {item.slug!r}")
                    continue

                if item.action == "promote" and item.atlas_type:
                    console.print(
                        f"  [bold]LLM promote[/bold] {item.slug} → {item.atlas_type}  "
                        f"[dim]({item.reason})[/dim]"
                    )
                    if yes or click.confirm(f"  apply promote for {item.slug}?", default=True):
                        try:
                            promote_file(path, item.atlas_type, actor=ACTOR_TEND)
                            llm_triaged += 1
                            atlas_slugs.add(path.stem.lower())
                        except RuntimeError as e:
                            console.print(f"  [red]promote failed:[/red] {e}")
                elif item.action == "keep":
                    console.print(f"  [dim]keep[/dim] {item.slug}  ({item.reason})")
                elif item.action == "discard":
                    console.print(
                        f"  [yellow]discard (noted, not deleted)[/yellow] {item.slug}  "
                        f"({item.reason})"
                    )
        else:
            console.print("  [dim]dry run: skipping LLM call[/dim]")
    else:
        console.print(
            f"  [dim]below threshold ({len(deferred)} < {threshold}); skipping LLM tier[/dim]"
        )

    # -- pass 3: reweave --
    reweaved = 0
    if not no_reweave:
        console.print("\n[bold]pass 3: reweave[/bold]")
        reweaved = _reweave_pending(dry_run=dry_run, yes=yes, console=console)
    else:
        console.print("\n[dim]pass 3: reweave skipped (--no-reweave)[/dim]")

    # -- pass 4: health --
    if not no_health:
        console.print("\n[bold]pass 4: health[/bold]")
        _health_summary(console)
    else:
        console.print("\n[dim]pass 4: health skipped (--no-health)[/dim]")

    # -- summary ledger event (skipped in dry-run: dry-run writes nothing) --
    if not dry_run:
        ledger.append_event(
            plane="projection",
            kind="tend.completed",
            actor=ACTOR_TEND,
            subject="vault/",
            payload={
                "auto_promoted": len(auto_promoted),
                "llm_triaged": llm_triaged,
                "reweaved": reweaved,
                "dry_run": dry_run,
            },
        )

    console.print(
        f"\n[bold green]tend complete[/bold green]  "
        f"auto_promoted={len(auto_promoted)}  "
        f"llm_triaged={llm_triaged}  "
        f"reweaved={reweaved}" + ("  [dim](dry run)[/dim]" if dry_run else "")
    )
