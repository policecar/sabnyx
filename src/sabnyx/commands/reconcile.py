"""`sabnyx reconcile` — diff the ledger against the filesystem.

The ledger is an append-only audit of *agent* actions, not an event-sourced
mirror of the vault: git is the source of truth, and a manual file edit (or a
crash mid-promote, or a multi-machine merge) produces no event. This command
answers the first question after any incident — "does the ledger match the
vault?" — by comparing `atlas.promoted` events against the files under atlas/.

Read-only: it mutates nothing. Exits non-zero only when the ledger references
a promoted file that no longer exists (a real inconsistency), so it is safe to
wire into a health check.
"""

from __future__ import annotations

import sys

import click
from rich.console import Console
from rich.table import Table

from .. import services
from ..paths import DATA_ROOT


@click.command("reconcile")
def reconcile_cmd() -> None:
    """Report atlas files with no promote event, and promote events whose file is gone."""
    console = Console()
    r = services.reconcile_atlas()

    table = Table(title="ledger ⇆ filesystem reconciliation")
    table.add_column("Check")
    table.add_column("Result", justify="right")
    table.add_row("atlas files without a promote event", str(len(r.unledgered)))
    table.add_row("promote events with a missing file", str(len(r.orphaned)))
    console.print(table)

    if r.unledgered:
        console.print(
            "\n[yellow]atlas files with no atlas.promoted event[/yellow] "
            "[dim](manual edit, pre-ledger, or merged from another machine):[/dim]"
        )
        for rel in r.unledgered[:20]:
            console.print(f"  {rel}")
        if len(r.unledgered) > 20:
            console.print(f"  ... and {len(r.unledgered) - 20} more")

    if r.orphaned:
        console.print(
            "\n[red]atlas.promoted events whose file is missing[/red] "
            "[dim](deleted/moved outside the pipeline — the ledger is now wrong):[/dim]"
        )
        for rel in r.orphaned[:20]:
            console.print(f"  {rel}")
        if len(r.orphaned) > 20:
            console.print(f"  ... and {len(r.orphaned) - 20} more")

    if r.clean:
        console.print(f"\n[green]clean[/green] — ledger and atlas/ agree (under {DATA_ROOT}).")

    # Orphaned events are a genuine inconsistency; unledgered files are expected
    # (manual authoring is legitimate), so they don't fail the check.
    if r.orphaned:
        sys.exit(1)
