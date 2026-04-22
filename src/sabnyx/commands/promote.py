"""`sabnyx promote` — move a file from intake/ to atlas/ with a reweave prompt."""

from __future__ import annotations

import sys
from datetime import date as date_cls
from pathlib import Path

import click
from rich.console import Console

from .. import fm, ledger
from ..paths import ATLAS_TYPE_DIRS, INTAKE, KNOWLEDGE_LOG, ROOT, ensure_tree


@click.command("promote")
@click.argument("source", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--as",
    "atlas_type",
    type=click.Choice(["concept", "reference", "idea"]),
    required=True,
    help="Atlas type to promote as.",
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

    source = source.resolve()
    if INTAKE not in source.parents:
        console.print(f"[red]{source} is not under vault/intake/[/red]")
        sys.exit(2)

    try:
        meta, body, kind = fm.load(source)
    except fm.FrontmatterError as e:
        console.print(f"[red]source frontmatter invalid: {e}[/red]")
        sys.exit(1)

    if kind != "intake":
        console.print(f"[red]expected intake file, got {kind}[/red]")
        sys.exit(1)

    dest_dir = ATLAS_TYPE_DIRS[atlas_type]
    dest = dest_dir / source.name
    if dest.exists():
        console.print(
            f"[yellow]target exists: {dest.relative_to(ROOT)}[/yellow]\n"
            f"Consider reweaving into the existing file instead of promoting."
        )
        sys.exit(1)

    today = date_cls.today().isoformat()
    new_meta = {
        "type": atlas_type,
        "description": meta["description"],
        "tags": meta.get("tags", []),
        "status": "final",
        "promoted_from": str(source.relative_to(ROOT)),
        "promoted_date": today,
        "last_verified": last_verified or today,
        "proof": None,
    }

    # Preserve source provenance as HTML comments in the body (round-trip).
    provenance_lines = []
    if src_url := meta.get("source_url"):
        provenance_lines.append(f"<!-- source-url: {src_url} -->")
    if src_date := meta.get("source_date"):
        provenance_lines.append(f"<!-- source-date: {src_date} -->")
    if meta.get("source"):
        provenance_lines.append(f"<!-- source-kind: {meta['source']} -->")
    provenance = ("\n".join(provenance_lines) + "\n\n") if provenance_lines else ""

    # Add an empty Related marker block for future reweave passes.
    related_block = "\n\n## Related\n\n<!-- BEGIN sabnyx:related -->\n<!-- END sabnyx:related -->\n"

    new_body = provenance + body.rstrip() + related_block

    fm.write(dest, new_meta, new_body)

    # Validate what we wrote, before touching the source
    try:
        fm.load(dest)
    except fm.FrontmatterError as e:
        console.print(f"[red]wrote invalid atlas file, rolling back: {e}[/red]")
        dest.unlink()
        sys.exit(3)

    # Remove the intake source
    source.unlink()

    # Provenance log (Gate 3 materia)
    KNOWLEDGE_LOG.parent.mkdir(parents=True, exist_ok=True)
    if not KNOWLEDGE_LOG.exists():
        KNOWLEDGE_LOG.write_text(
            "# Knowledge log\n\nAppend-only provenance trail.\n\n", encoding="utf-8"
        )
    with KNOWLEDGE_LOG.open("a", encoding="utf-8") as f:
        f.write(
            f"- {today} promote: `{source.relative_to(ROOT)}` -> "
            f"`{dest.relative_to(ROOT)}` ({atlas_type})\n"
        )

    ledger.append_event(
        kind="atlas.promoted",
        actor="sabnyx-cli",
        subject=str(dest.relative_to(ROOT)),
        payload={
            "from": str(source.relative_to(ROOT)),
            "as": atlas_type,
            "description": meta["description"],
            "source_url": meta.get("source_url"),
        },
    )

    # Emit a reweave prompt as a separate event (not an action yet).
    ledger.append_event(
        plane="intent",
        kind="reweave.proposed",
        actor="sabnyx-cli",
        subject=str(dest.relative_to(ROOT)),
        payload={
            "description": meta["description"],
            "tags": meta.get("tags", []),
            "prompt": (
                "now that this file exists, what existing atlas files should "
                "link to it or be updated by it? look for contradictions and "
                "bridge links; propose additions to the Related marker block "
                "and write observations for conflicts."
            ),
        },
    )

    console.print(f"[green]promoted[/green] {dest.relative_to(ROOT)}")
    console.print("[dim]reweave.proposed appended to ledger[/dim]")
