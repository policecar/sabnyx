"""`sabnyx observations` — list, resolve and archive observations (IDD §6 CLI).

  list     pending observations by default, oldest first, one plain line each.
  resolve  mark one done (status promoted), with a reason.
  archive  dismiss one (wontfix, duplicate or obsolete; status archived),
           with a reason.

Every mutation goes through writers.transition_observation (actor
sabnyx-cli); this module builds no frontmatter itself. A refusal exits 1;
infrastructure errors propagate. References ``paths`` by module attribute so
tests redirect the vault.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal, get_args

import click
from rich.console import Console
from rich.markup import escape

from .. import ledger, paths, writers
from .. import observations as obs
from ..paths import ensure_tree
from ..schemas import ObservationCategory, ObservationStatus, ResolutionKind

ACTOR = "sabnyx-cli"

# Help for FILE in resolve and archive (D12).
_FILE_HELP = (
    "FILE is an observation file. A relative FILE is resolved against the data "
    "root (DATA_ROOT), not the current directory, so a path pasted from "
    "`sabnyx observations list` works; this differs from `promote`, which "
    "resolves its SOURCE against the current directory."
)


@click.group("observations")
def observations_cmd() -> None:
    """List, resolve and archive observations under vault/intake/.observations/."""


@observations_cmd.command("list")
@click.option("--category", type=click.Choice(get_args(ObservationCategory)), default=None)
@click.option(
    "--status",
    type=click.Choice([*get_args(ObservationStatus), "all"]),
    default="pending",
    show_default=True,
)
def list_cmd(category: str | None, status: ObservationStatus | Literal["all"]) -> None:
    """One line per observation, sorted by (observed, filename).

    Columns: rel, category, observed, age in days, the id (12 hex, or — for
    an observation with no identity), target, involves.
    """
    ensure_tree()
    console = Console()
    records = [o for o in obs.observations(status) if category is None or o.fm.category == category]
    if not records:
        console.print(f"[dim]no {status} observations[/dim]")
        return
    resolver = obs.resolver()
    today = ledger.today()
    for o in sorted(records, key=lambda o: (o.fm.observed, o.path.name)):
        oid = obs.obs_id(o.fm, resolver)
        cols = [
            o.rel,
            o.fm.category,
            o.fm.observed.isoformat(),
            f"{(today - o.fm.observed).days}d",
            oid.removeprefix("sha256:")[:12] if oid else "—",
            o.fm.target or "—",
            ", ".join(o.fm.involves) or "—",
        ]
        console.print("  ".join(escape(c) for c in cols), soft_wrap=True, highlight=False)
    console.print(f"[dim]{len(records)} {status}[/dim]")


def _transition(file: str, *, kind: ResolutionKind, reason: str) -> None:
    ensure_tree()
    path = Path(file)
    if not path.is_absolute():
        path = paths.DATA_ROOT / path  # D12: not the cwd
    try:
        t = writers.transition_observation(path, kind=kind, reason=reason, actor=ACTOR)
    except writers.ObservationRefused as e:
        click.echo(f"refused ({e.reason}): {e.detail}", err=True)
        sys.exit(1)
    verb = "resolved" if kind == "done" else "archived"
    Console().print(
        f"[green]{verb}[/green] {escape(t.rel)} → {t.new_status} ({t.kind})",
        soft_wrap=True,
        highlight=False,
    )


@observations_cmd.command("resolve", epilog=_FILE_HELP)
@click.argument("file")
@click.option("--reason", required=True, help="Why it is done (one line).")
def resolve_cmd(file: str, reason: str) -> None:
    """Mark the observation FILE done (status promoted)."""
    _transition(file, kind="done", reason=reason)


@observations_cmd.command("archive", epilog=_FILE_HELP)
@click.argument("file")
@click.option(
    "--kind",
    type=click.Choice(["wontfix", "duplicate", "obsolete"]),
    required=True,
    help="Why it is dismissed. (done is `resolve`.)",
)
@click.option("--reason", required=True, help="The detail (one line).")
def archive_cmd(file: str, kind: ResolutionKind, reason: str) -> None:
    """Dismiss the observation FILE (status archived)."""
    _transition(file, kind=kind, reason=reason)
