"""Validated writers shared by LLM-driven commands.

The model proposes; this module slugifies, writes, validates, rolls
back on schema failure, and ledgers. References `paths` by module
attribute so tests can redirect the vault wholesale.
"""

from __future__ import annotations

from datetime import date as date_cls

from rich.console import Console

from . import fm, ledger, paths
from .schemas import ProposedIntake, ProposedObservation
from .util import slugify


def _write_checked(path, meta: dict, body: str, *, console: Console) -> str | None:
    """Skip-if-exists, write, re-validate, roll back on schema failure.

    Returns the DATA_ROOT-relative path string on success, or None if the file
    already existed or failed validation (in which case it is unlinked). The
    rollback is the bit you least want to maintain in two copies; callers keep
    their own meta/ledger/messaging.
    """
    if path.exists():
        console.print(f"  [yellow]skip (exists)[/yellow] {path.relative_to(paths.DATA_ROOT)}")
        return None
    fm.write(path, meta, body)
    try:
        fm.load(path)  # belt + suspenders
    except fm.FrontmatterError as e:
        console.print(f"  [red]invalid, rolled back:[/red] {e}")
        path.unlink()
        return None
    return str(path.relative_to(paths.DATA_ROOT))


def write_intake(
    pi: ProposedIntake,
    *,
    actor: str,
    source: str,
    source_url: str | None = None,
    trailer: str | None = None,
    console: Console,
) -> str | None:
    """Write one intake file from a model proposal.

    `trailer` is an optional provenance HTML comment appended to the body
    (reserved syntax, docs/reserved-syntax.md). Returns the path relative
    to ROOT, or None if skipped/rolled back.
    """
    slug = slugify(pi.title)
    path = paths.INTAKE / f"{slug}.md"

    meta: dict = {
        "type": pi.type,
        "description": pi.description,
        "source": source,
        "tags": pi.tags,
        "status": "draft",
        "created": ledger.now().isoformat(),
        "source_agent": actor,
    }
    if source_url:
        meta["source_url"] = source_url
    body = f"# {pi.title}\n\n{pi.body.strip()}\n" + (f"\n{trailer}\n" if trailer else "")
    rel = _write_checked(path, meta, body, console=console)
    if rel is None:
        return None

    ledger.append_event(
        kind="intake.created",
        actor=actor,
        subject=rel,
        payload={"type": pi.type, "description": pi.description, "slug": slug},
    )
    console.print(f"  [green]wrote[/green] {rel}")
    return rel


def append_knowledge_log(operation: str, title: str, fields: dict[str, str | None]) -> None:
    """Gate 3 (materia): grep-able provenance line, jibot's log contract.
    `grep "^# \\[" knowledge-log.md | grep ingest` finds ingestions."""
    log = paths.KNOWLEDGE_LOG
    log.parent.mkdir(parents=True, exist_ok=True)
    if not log.exists():
        log.write_text("# Knowledge log\n\nAppend-only provenance trail.\n\n", encoding="utf-8")
    with log.open("a", encoding="utf-8") as f:
        f.write(f"# [{ledger.now().isoformat()}] {operation} | {title}\n")
        for k, v in fields.items():
            if v:
                f.write(f"  {k}: {v}\n")


def write_observation(ob: ProposedObservation, *, actor: str, console: Console) -> str | None:
    """Write one observation file under intake/.observations/.

    Returns the path relative to ROOT, or None if skipped/rolled back.
    """
    today = date_cls.today().isoformat()
    slug = slugify(ob.title)
    path = paths.OBSERVATIONS / f"{today}-{ob.category}-{slug}.md"

    meta = {
        "type": "observation",
        "category": ob.category,
        "observed": today,
        "source_agent": actor,
        "status": "pending",
        "involves": ob.involves,
    }
    rel = _write_checked(path, meta, f"# {ob.title}\n\n{ob.body.strip()}\n", console=console)
    if rel is None:
        return None

    ledger.append_event(
        kind="observation.created",
        actor=actor,
        subject=rel,
        payload={"category": ob.category, "slug": slug, "involves": ob.involves},
    )
    console.print(f"  [yellow]wrote[/yellow] {rel}")
    return rel
