"""Validated writers shared by LLM-driven commands.

The model proposes; this module slugifies, writes, validates, rolls
back on schema failure, and ledgers. References `paths` by module
attribute so tests can redirect the vault wholesale.
"""

from __future__ import annotations

from datetime import date as date_cls

from rich.console import Console

from . import fm, ledger, paths
from .schemas import ProposedObservation
from .util import slugify


def write_observation(ob: ProposedObservation, *, actor: str, console: Console) -> str | None:
    """Write one observation file under intake/.observations/.

    Returns the path relative to ROOT, or None if skipped/rolled back.
    """
    today = date_cls.today().isoformat()
    slug = slugify(ob.title)
    path = paths.OBSERVATIONS / f"{today}-{ob.category}-{slug}.md"
    if path.exists():
        console.print(f"  [yellow]skip (exists)[/yellow] {path.relative_to(paths.ROOT)}")
        return None

    meta = {
        "type": "observation",
        "category": ob.category,
        "observed": today,
        "source_agent": actor,
        "status": "pending",
        "involves": ob.involves,
    }
    fm.write(path, meta, f"# {ob.title}\n\n{ob.body.strip()}\n")
    try:
        fm.load(path)  # belt + suspenders
    except fm.FrontmatterError as e:
        console.print(f"  [red]invalid, rolled back:[/red] {e}")
        path.unlink()
        return None

    rel = str(path.relative_to(paths.ROOT))
    ledger.append_event(
        kind="observation.created",
        actor=actor,
        subject=rel,
        payload={"category": ob.category, "slug": slug, "involves": ob.involves},
    )
    console.print(f"  [yellow]wrote[/yellow] {rel}")
    return rel
