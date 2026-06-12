"""`sabnyx ingest` — the core operation: source -> structured knowledge.

The first LLM-in-the-loop command (plan §Phase 2). One pass:

  1. capture the raw source into vault/sources/ (immutable, ledgered)
  2. classify extraction depth by word count / URL (jibot tiers)
  3. assemble vault context: atlas + intake description indexes
  4. one structured LLM call -> IngestChangeset (intake files + observations)
  5. human approval gate (unless --yes), then apply through the
     validated writers, ledger every mutation, append knowledge-log

Containment: ingest writes to intake/ and sources/ only — never atlas/.
Promotion stays a separate, deliberate act (triage/promote), exactly the
defense-in-depth the jibot pipeline prescribes.
"""

from __future__ import annotations

import sys
from datetime import date as date_cls
from pathlib import Path

import click
from rich.console import Console

from .. import fm, ledger, llm, sources
from ..paths import ATLAS, INTAKE, KNOWLEDGE_LOG, OBSERVATIONS, ROOT, ensure_tree
from ..schemas import IngestChangeset
from ..util import slugify

ACTOR = "sabnyx-ingest"

_DEPTH_GUIDANCE = {
    "shallow": "This is a short source. Extract concisely: usually one intake file "
    "with a tight summary. Do not pad.",
    "standard": "Standard-length source. Extract the core claims and anything the "
    "operator would want to find again.",
    "standard-plus": "Dense source. Extract thoroughly: distinct concepts may warrant "
    "separate intake files.",
    "deep": "Long-form source (paper/book-length). Extract section by section: key "
    "points, argument structure, and distinct concepts as separate intake files.",
}

_TASK_SYSTEM = """You are the intake agent for this vault. Given one raw source and an
index of what the vault already contains, produce an IngestChangeset.

Rules, in addition to the SPEC and ROUTING contract above:

- Propose intake files only. You never write to atlas/; promotion is a separate
  human-gated step. This is a containment boundary, not a suggestion.
- The `description` field is the most load-bearing field in the vault: 30-200
  chars, distinct from the title, answering "what is this and why would I
  search for it?". Dense, specific, no marketing language.
- Check the atlas index before proposing anything. If the source's content is
  already covered by an existing atlas entry, do NOT duplicate it — propose an
  observation (category: connection) naming the existing file instead.
- If the source contradicts a claim suggested by an existing description,
  propose an observation (category: contradiction) naming the file.
- If the source references an entity the vault clearly lacks, you may note a
  gap observation. Be conservative: observations are cheap but not free.
- Bodies are plain markdown, no frontmatter, no leading H1 (the writer adds
  the title). Terse but not curt; specific over abstract.
- Titles become kebab-case slugs; keep them phrase-like, atemporal, no dates."""


def _index(dir_: Path, label: str) -> str:
    """Slug + description index of a vault directory, for filter-before-read."""
    lines: list[str] = []
    for p in sorted(dir_.rglob("*.md")):
        if OBSERVATIONS in p.parents:
            continue
        try:
            meta, _body, _kind = fm.load(p)
        except Exception:
            continue
        tags = ", ".join(meta.get("tags", []))
        lines.append(f"- {p.stem}: {meta.get('description', '')}" + (f" [{tags}]" if tags else ""))
    return f"## {label}\n" + ("\n".join(lines) if lines else "(empty)")


def _append_knowledge_log(operation: str, title: str, fields: dict[str, str | None]) -> None:
    """Gate 3 (materia): grep-able provenance line, jibot's log contract.
    `grep "^# \\[" knowledge-log.md | grep ingest` finds ingestions."""
    KNOWLEDGE_LOG.parent.mkdir(parents=True, exist_ok=True)
    if not KNOWLEDGE_LOG.exists():
        KNOWLEDGE_LOG.write_text(
            "# Knowledge log\n\nAppend-only provenance trail.\n\n", encoding="utf-8"
        )
    with KNOWLEDGE_LOG.open("a", encoding="utf-8") as f:
        f.write(f"# [{ledger.now().isoformat()}] {operation} | {title}\n")
        for k, v in fields.items():
            if v:
                f.write(f"  {k}: {v}\n")


@click.command("ingest")
@click.argument("source_file", required=False, type=click.Path(exists=True, path_type=Path))
@click.option("--url", default=None, help="Ingest a URL instead of a local file.")
@click.option("--stdin", "use_stdin", is_flag=True, help="Read the source from stdin.")
@click.option("--title", default=None, help="Source title (required for --stdin).")
@click.option(
    "--role",
    type=click.Choice(["fast", "standard", "deep"]),
    default=None,
    help="Override the depth-derived model role.",
)
@click.option("--yes", "-y", is_flag=True, help="Apply without the approval prompt.")
@click.option("--dry-run", is_flag=True, help="Show the proposed changeset; write nothing.")
def ingest_cmd(
    source_file: Path | None,
    url: str | None,
    use_stdin: bool,
    title: str | None,
    role: str | None,
    yes: bool,
    dry_run: bool,
) -> None:
    """Ingest a source: capture raw, extract structured knowledge to intake/."""
    console = Console()
    ensure_tree()

    # -- 1. acquire the raw text ------------------------------------------------
    if sum(map(bool, (source_file, url, use_stdin))) != 1:
        console.print("[red]provide exactly one of: FILE, --url, --stdin[/red]")
        sys.exit(2)

    if url:
        text = sources.fetch_url(url)
        src_title = title or url
        src_kind = "web"
    elif use_stdin:
        if not title:
            console.print("[red]--title is required with --stdin[/red]")
            sys.exit(2)
        text = sys.stdin.read()
        src_title = title
        src_kind = "manual"
    else:
        assert source_file is not None
        text = source_file.read_text(encoding="utf-8", errors="replace")
        src_title = title or source_file.stem
        src_kind = "import"

    if not text.strip():
        console.print("[red]source is empty[/red]")
        sys.exit(1)

    # -- 2. capture raw + classify depth -----------------------------------------
    if not dry_run:
        try:
            src_path = sources.capture(text=text, slug=src_title, actor=ACTOR, source_url=url)
        except FileExistsError as e:
            console.print(f"[red]{e}[/red]")
            sys.exit(1)
        src_rel = str(src_path.relative_to(ROOT))
    else:
        src_rel = f"vault/sources/{slugify(src_title)}.md (dry run, not written)"

    depth = sources.classify_depth(text, source_url=url)
    chosen_role = role or llm.role_for_depth(depth)
    console.print(
        f"captured [bold]{src_rel}[/bold]  "
        f"(depth: {depth}, role: {chosen_role}, model: {llm.model_for(chosen_role)})"
    )

    # -- 3 + 4. context assembly and the LLM pass ---------------------------------
    user = (
        f"{_index(ATLAS, 'Atlas index (durable knowledge already in the vault)')}\n\n"
        f"{_index(INTAKE, 'Intake queue (already captured, awaiting triage)')}\n\n"
        f"## Extraction depth\n{_DEPTH_GUIDANCE[depth]}\n\n"
        f"## Source ({src_kind}" + (f", {url}" if url else "") + f": {src_title})\n\n{text}"
    )

    changeset = llm.parse_structured(
        task_system=_TASK_SYSTEM,
        user=user,
        output_format=IngestChangeset,
        role=chosen_role,  # type: ignore[arg-type]
        actor=ACTOR,
        purpose=f"ingest:{slugify(src_title)}",
    )

    ledger.append_event(
        plane="intent",
        kind="ingest.proposed",
        actor=ACTOR,
        subject=src_rel,
        payload={
            "summary": changeset.summary,
            "intake_count": len(changeset.intake_files),
            "observation_count": len(changeset.observations),
            "depth": depth,
            "role": chosen_role,
        },
    )

    # -- 5. approval gate ----------------------------------------------------------
    console.print(f"\n[bold]Proposed:[/bold] {changeset.summary}\n")
    for pi in changeset.intake_files:
        console.print(f"  [green]+ intake[/green] {slugify(pi.title)}.md ({pi.type})")
        console.print(f"    [dim]{pi.description}[/dim]")
    for ob in changeset.observations:
        console.print(f"  [yellow]+ observation[/yellow] ({ob.category}) {slugify(ob.title)}")
    if not changeset.intake_files and not changeset.observations:
        console.print("  [dim]nothing to write — source already covered or empty[/dim]")

    if dry_run:
        console.print("\n[dim]dry run: nothing written[/dim]")
        return
    if not changeset.intake_files and not changeset.observations:
        return
    if not yes and not click.confirm("\napply?", default=True):
        console.print("[dim]declined; raw source kept, nothing else written[/dim]")
        return

    # -- 6. apply through the validated writers --------------------------------------
    today = date_cls.today().isoformat()
    written: list[str] = []

    for pi in changeset.intake_files:
        slug = slugify(pi.title)
        path = INTAKE / f"{slug}.md"
        if path.exists():
            console.print(f"  [yellow]skip (exists)[/yellow] {path.relative_to(ROOT)}")
            continue
        meta: dict = {
            "type": pi.type,
            "description": pi.description,
            "source": src_kind,
            "tags": pi.tags,
            "status": "draft",
            "created": ledger.now().isoformat(),
            "source_agent": ACTOR,
        }
        if url:
            meta["source_url"] = url
        body = f"# {pi.title}\n\n{pi.body.strip()}\n\n<!-- source-id:sabnyx-sources:{Path(src_rel).name} -->\n"
        fm.write(path, meta, body)
        try:
            fm.load(path)  # belt + suspenders, same as `new`
        except fm.FrontmatterError as e:
            console.print(f"  [red]invalid, rolled back:[/red] {e}")
            path.unlink()
            continue
        rel = str(path.relative_to(ROOT))
        written.append(rel)
        ledger.append_event(
            kind="intake.created",
            actor=ACTOR,
            subject=rel,
            payload={"type": pi.type, "description": pi.description, "slug": slug},
        )
        console.print(f"  [green]wrote[/green] {rel}")

    for ob in changeset.observations:
        slug = slugify(ob.title)
        path = OBSERVATIONS / f"{today}-{ob.category}-{slug}.md"
        if path.exists():
            console.print(f"  [yellow]skip (exists)[/yellow] {path.relative_to(ROOT)}")
            continue
        meta = {
            "type": "observation",
            "category": ob.category,
            "observed": today,
            "source_agent": ACTOR,
            "status": "pending",
            "involves": ob.involves,
        }
        fm.write(path, meta, f"# {ob.title}\n\n{ob.body.strip()}\n")
        try:
            fm.load(path)
        except fm.FrontmatterError as e:
            console.print(f"  [red]invalid, rolled back:[/red] {e}")
            path.unlink()
            continue
        rel = str(path.relative_to(ROOT))
        written.append(rel)
        ledger.append_event(
            kind="observation.created",
            actor=ACTOR,
            subject=rel,
            payload={"category": ob.category, "slug": slug, "involves": ob.involves},
        )
        console.print(f"  [yellow]wrote[/yellow] {rel}")

    # -- gate 3: provenance log -------------------------------------------------------
    _append_knowledge_log(
        "ingest",
        src_title,
        {"source": src_rel, "url": url, "depth": depth, "files": ", ".join(written) or "none"},
    )
    ledger.append_event(
        kind="ingest.applied",
        actor=ACTOR,
        subject=src_rel,
        payload={"written": written, "skipped_or_failed": False},
    )
    console.print(
        f"\n[green]ingest complete[/green] ({len(written)} file(s)). "
        "next: [bold]sabnyx triage[/bold] when the queue warrants it."
    )
