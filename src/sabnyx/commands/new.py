"""`sabnyx new` — create an intake file with a valid frontmatter skeleton."""

from __future__ import annotations

import sys
from datetime import date as date_cls

import click

from .. import fm, ledger
from ..paths import DATA_ROOT, INTAKE, OBSERVATIONS, ensure_tree
from ..util import slugify


@click.command("new")
@click.option(
    "--type",
    "type_",
    type=click.Choice(["bookmark", "paper", "note", "thought", "observation"]),
    required=True,
    help="Content type. 'observation' routes to vault/intake/.observations/.",
)
@click.option("--title", prompt=True, help="Short title (becomes the slug).")
@click.option("--description", prompt=True, help="30-200 chars: what is this and why.")
@click.option("--source-url", default=None, help="Required for bookmark/paper.")
@click.option(
    "--source-date",
    default=None,
    help="ISO date of the source itself (e.g. paper publication date).",
)
@click.option("--tags", default="", help="Comma-separated tags.")
@click.option(
    "--category",
    type=click.Choice(["gap", "contradiction", "connection", "friction", "structural"]),
    default=None,
    help="For type=observation only.",
)
@click.pass_context
def new_cmd(
    ctx: click.Context,
    type_: str,
    title: str,
    description: str,
    source_url: str | None,
    source_date: str | None,
    tags: str,
    category: str | None,
) -> None:
    """Create an intake file with a valid frontmatter skeleton."""
    ensure_tree()
    slug = slugify(title)
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]

    if type_ == "observation":
        if not category:
            click.echo("--category is required for observation", err=True)
            sys.exit(2)
        # observation filenames are date-prefixed
        today = date_cls.today().isoformat()
        path = OBSERVATIONS / f"{today}-{category}-{slug}.md"
        meta = {
            "type": "observation",
            "category": category,
            "observed": today,
            "source_agent": "sabnyx-cli",
            "status": "pending",
            "involves": [],
        }
        body = f"# {title}\n\n{description}\n"
    else:
        if type_ in ("bookmark", "paper") and not source_url:
            click.echo(f"--source-url is required for {type_}", err=True)
            sys.exit(2)
        path = INTAKE / f"{slug}.md"
        meta: dict = {
            "type": type_,
            "description": description,
            "source": "manual",
            "tags": tag_list,
            "status": "draft",
            "created": ledger.now().isoformat(),
            "source_agent": "sabnyx-cli",
        }
        if source_url:
            meta["source_url"] = source_url
        if source_date:
            meta["source_date"] = source_date
        body = f"# {title}\n\n"

    if path.exists():
        click.echo(f"refusing to overwrite: {path}", err=True)
        sys.exit(1)

    fm.write(path, meta, body)

    # validate what we just wrote (belt + suspenders)
    try:
        fm.load(path)
    except fm.FrontmatterError as e:
        click.echo(f"WROTE INVALID FILE, deleting: {e}", err=True)
        path.unlink()
        sys.exit(3)

    ledger.append_event(
        kind=f"{'observation' if type_ == 'observation' else 'intake'}.created",
        actor="sabnyx-cli",
        subject=str(path.relative_to(DATA_ROOT)),
        payload={
            "type": meta["type"],
            "description": description if type_ != "observation" else None,
            "category": category,
            "slug": slug,
        },
    )
    click.echo(str(path))
