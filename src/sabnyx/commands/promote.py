"""`sabnyx promote` — move a file from intake/ to atlas/ with a reweave prompt."""

from __future__ import annotations

import sys
from datetime import date as date_cls
from pathlib import Path

import click
from rich.console import Console

from .. import fm
from ..paths import DATA_ROOT, INTAKE, ensure_tree
from ..services import promote_file


@click.command("promote")
@click.argument("source", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--as",
    "atlas_type",
    type=click.Choice(["concept", "reference", "idea", "person", "org", "project"]),
    required=True,
    help="Atlas type to promote as (concept/reference/idea, or entity: person/org/project).",
)
@click.option(
    "--last-verified",
    default=None,
    help="ISO date; required for references to time-sensitive content.",
)
def promote_cmd(source: Path, atlas_type: str, last_verified: str | None) -> None:
    """Promote an intake file to atlas/<atlas_type>/<slug>.md."""
    ensure_tree()
    console = Console()

    # Validate at the CLI boundary: a bad date must fail here, not surface
    # post-write as an opaque rollback from promote_file.
    if last_verified is not None:
        try:
            date_cls.fromisoformat(last_verified)
        except ValueError:
            console.print(
                f"[red]--last-verified must be an ISO date (YYYY-MM-DD), "
                f"got {last_verified!r}[/red]"
            )
            sys.exit(2)

    source = source.resolve()
    if INTAKE not in source.parents:
        console.print(f"[red]{source} is not under vault/intake/[/red]")
        sys.exit(2)

    try:
        _meta, _body, kind = fm.load(source)
    except fm.FrontmatterError as e:
        console.print(f"[red]source frontmatter invalid: {e}[/red]")
        sys.exit(1)

    if kind != "intake":
        console.print(f"[red]expected intake file, got {kind}[/red]")
        sys.exit(1)

    from ..paths import ATLAS_TYPE_DIRS

    dest_dir = ATLAS_TYPE_DIRS[atlas_type]
    dest = dest_dir / source.name
    if dest.exists():
        console.print(
            f"[yellow]target exists: {dest.relative_to(DATA_ROOT)}[/yellow]\n"
            f"Consider reweaving into the existing file instead of promoting."
        )
        sys.exit(1)

    try:
        result = promote_file(source, atlas_type, last_verified=last_verified, actor="sabnyx-cli")
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(3)

    console.print(f"[green]promoted[/green] {result.relative_to(DATA_ROOT)}")
    console.print("[dim]reweave.proposed appended to ledger[/dim]")
