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

from .. import context, fm, ledger, llm
from ..paths import ATLAS, DATA_ROOT, INTAKE, ensure_tree
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
    """Consume pending reweave.proposed intents via reweave.reweave_one.

    Delegates the LLM call, guards, apply, and ledger event to the shared
    helper so the apply path lives in one place. tend's --dry-run skips the
    helper entirely (no LLM call), per DEC-01. Returns files reweaved.
    """
    from . import reweave as rw

    targets = rw._pending_targets()
    if not targets:
        console.print("[dim]reweave: no pending intents[/dim]")
        return 0

    console.print(f"reweave: [bold]{len(targets)}[/bold] pending target(s)")
    index = context.slug_index(ATLAS)
    reweaved = 0

    for tgt in targets:
        rel = str(tgt.relative_to(DATA_ROOT))
        console.print(f"  [bold]reweave[/bold] {rel}")

        if dry_run:
            console.print("    [dim]dry run: skipping LLM call[/dim]")
            continue

        result = rw.reweave_one(
            tgt, index, role="standard", actor=ACTOR_TEND, yes=yes, dry_run=False, console=console
        )
        if result is not None:
            reweaved += 1

    return reweaved


def _health_summary(console: Console) -> None:
    """Print a concise health table (reuses health.py's link scan)."""
    from . import health as health_mod

    idx = context.vault_slug_index()
    broken_links, _linked, _drift, _outgoing = health_mod.scan_wikilinks(idx)
    intake_count = len(list(INTAKE.glob("*.md")))
    atlas_count = sum(1 for p in ATLAS.rglob("*.md") if p.is_file())
    pending_obs = health_mod.pending_observation_count()

    t = Table(title="health summary")
    t.add_column("Check")
    t.add_column("Result", justify="right")
    t.add_row("intake queue", str(intake_count))
    t.add_row("atlas files", str(atlas_count))
    t.add_row("pending observations", str(pending_obs))
    t.add_row("broken wikilinks", str(len(broken_links)))
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
        except Exception as e:
            # Per-file robustness: one unreadable file must not abort the run.
            console.print(f"  [red]skip (unreadable)[/red] {p.name}: {e}")
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
                except Exception as e:
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

    # Passes 2 + 3 are the LLM-calling passes. Bound them under one per-run call
    # budget (fail-closed) so an unattended `tend --yes` loop with a misbehaving
    # prompt or a huge queue can't spin the provider without limit.
    llm_triaged = 0
    reweaved = 0
    with llm.call_budget():
        # -- pass 2: LLM triage --
        console.print(
            f"\n[bold]pass 2: LLM triage[/bold]  ({len(deferred)} deferred, threshold {threshold})"
        )

        if deferred and len(deferred) >= threshold:
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
                        # Provenance gate: Gate 6 (services.auto_promote_decision)
                        # blocks auto-promotion for an untrusted source_agent in
                        # the deterministic pass; the LLM tier must honour the
                        # same trust boundary (config.trusted_sources), or a
                        # prompt-injected telegram/web/r1 intake could ride an
                        # LLM "promote" verdict straight into atlas/ unattended.
                        try:
                            meta, _body, _kind = fm.load(path)
                        except fm.FrontmatterError as e:
                            # Fail closed: provenance can't be established from
                            # invalid frontmatter, and promote_file would refuse
                            # the file anyway. Report and continue (M3 contract).
                            console.print(f"  [red]promote failed:[/red] {e}")
                            continue
                        source_agent = meta.get("source_agent", "")
                        trusted_provenance = source_agent in trusted
                        if not trusted_provenance and yes:
                            console.print(
                                f"  [yellow]skip promote[/yellow] {item.slug}: untrusted "
                                f"provenance (source_agent={source_agent!r}; operator review "
                                f"needed)"
                            )
                            ledger.append_event(
                                plane="projection",
                                kind="triage.decided",
                                actor=ACTOR_TEND,
                                subject=str(path.relative_to(DATA_ROOT)),
                                payload={
                                    "slug": item.slug,
                                    "action": item.action,
                                    "reason": item.reason,
                                    "decision": "deferred_untrusted",
                                    "source_agent": source_agent,
                                },
                            )
                            continue

                        # Gate 5 re-check: the deferred queue systematically
                        # contains duplicate-slug deferrals, and the triage
                        # prompt never sees the atlas index — never let the
                        # LLM overwrite an existing atlas file.
                        if path.stem.lower() in atlas_slugs:
                            console.print(
                                f"  [yellow]skip promote[/yellow] {item.slug}: slug already "
                                f"exists in atlas (kept in intake)"
                            )
                            continue
                        console.print(
                            f"  [bold]LLM promote[/bold] {item.slug} → {item.atlas_type}  "
                            f"[dim]({item.reason})[/dim]"
                        )
                        try:
                            if trusted_provenance:
                                apply = yes or click.confirm(
                                    f"  apply promote for {item.slug}?", default=True
                                )
                            else:
                                apply = click.confirm(
                                    f"  untrusted provenance (source_agent={source_agent!r}) "
                                    f"— apply promote for {item.slug} anyway?",
                                    default=False,
                                )
                        except click.Abort:
                            # non-tty without --yes (cron): keep, don't crash the run
                            console.print(
                                "  [yellow]no interactive input; kept in intake "
                                "(use --yes for unattended runs)[/yellow]"
                            )
                            apply = False
                        if not apply:
                            continue
                        try:
                            promote_file(path, item.atlas_type, actor=ACTOR_TEND)
                            llm_triaged += 1
                            atlas_slugs.add(path.stem.lower())
                        except Exception as e:
                            console.print(f"  [red]promote failed:[/red] {e}")
                    elif item.action in ("keep", "discard"):
                        if item.action == "keep":
                            console.print(f"  [dim]keep[/dim] {item.slug}  ({item.reason})")
                        else:
                            console.print(
                                f"  [yellow]discard (noted, not deleted)[/yellow] {item.slug}  "
                                f"({item.reason})"
                            )
                        # Persist the decision (DEC-02: note it, do not delete) so
                        # unattended runs leave a record, not just console output.
                        ledger.append_event(
                            plane="projection",
                            kind="triage.decided",
                            actor=ACTOR_TEND,
                            subject=str(path.relative_to(DATA_ROOT)),
                            payload={
                                "slug": item.slug,
                                "action": item.action,
                                "reason": item.reason,
                            },
                        )
            else:
                console.print("  [dim]dry run: skipping LLM call[/dim]")
        else:
            console.print(
                f"  [dim]below threshold ({len(deferred)} < {threshold}); skipping LLM tier[/dim]"
            )

        # -- pass 3: reweave --
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
