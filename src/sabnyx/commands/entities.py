"""`sabnyx entities` — the lightweight entity layer (Phase 3).

People, orgs, and projects are first-class atlas pages, but the machinery
around them is deliberately small: entity *pages* plus an `aliases` list, a
gap detector ("referenced N× with no page"), and an alias/dup collision
report. No Senzing-scale identity resolution, no people CRM — that whole
half stays an explicit non-goal.

Three read-mostly subcommands:

  list     — show every entity page (person/org/project) and its aliases.
  gaps     — recurring broken wikilinks (≥ threshold distinct referrers,
             no resolving page) become `gap` observations the operator can
             act on. Deduped across runs; `--dry-run` writes nothing.
  resolve  — report tokens (slug or alias) claimed by two+ different entity
             pages — the signal that two pages should merge or an alias
             clashes. Report only; merging stays a human decision.

@decision DEC-01: entities is read-mostly; only `gaps` mutates (gap obs)
@rationale: Lightweight by design. Detection (gaps/resolve) is cheap and
  safe to run on the heartbeat; the destructive half (merging, deleting)
  is left to the operator, mirroring tend's "discard = note, never delete".
@decision DEC-02: wikilink targets are canonicalized via slugify for gaps
@rationale: `[[Jane Doe]]` and `[[jane-doe]]` name the same missing entity;
  slugifying both the target and the existing-slug set merges them into one
  gap instead of two, and resolves the link if `jane-doe.md` exists.
"""

from __future__ import annotations

import click
from rich.console import Console
from rich.table import Table

from .. import context, fm, ledger, paths, writers
from ..paths import ENTITY_TYPES, ensure_tree
from ..schemas import ProposedObservation
from ..util import extract_wikilinks, slugify, walk_vault_md

ACTOR_ENTITIES = "sabnyx-entities"


# -- shared helpers ------------------------------------------------------------


def _entity_pages() -> list[tuple[str, str, dict]]:
    """Every atlas page whose type is an entity type.

    Returns (slug, atlas_type, meta) tuples sorted by (type, slug). Scans all
    of atlas/ by frontmatter type rather than by directory, so a page lands
    here regardless of which subdir it sits in.
    """
    out: list[tuple[str, str, dict]] = []
    for p in paths.ATLAS.rglob("*.md"):
        if not p.is_file():
            continue
        try:
            meta, _body, kind = fm.load(p)
        except fm.FrontmatterError:
            continue
        if kind == "atlas" and meta.get("type") in ENTITY_TYPES:
            out.append((p.stem, str(meta.get("type")), meta))
    return sorted(out, key=lambda t: (t[1], t[0].lower()))


def _broken_link_counts() -> dict[str, list[str]]:
    """Map canonical missing-target slug -> sorted distinct referrer rels.

    A target is "missing" when it resolves to no page via the shared
    resolution index (file stems, slugified spaced names, and `aliases`
    frontmatter — the same rules health uses). Each referencing file counts
    once per target (distinct referrers, not raw occurrences — N different
    files naming a target is the real "this entity matters" signal).
    Observation files are not scanned (so a gap observation's own
    `[[target]]` never re-counts itself)."""
    resolution = context.link_resolution_index(context.vault_slug_index())
    counts: dict[str, set[str]] = {}
    for p in walk_vault_md(paths.VAULT):
        if paths.OBSERVATIONS in p.parents:
            continue
        try:
            _meta, body, _kind = fm.load(p)
        except fm.FrontmatterError:
            continue
        except ValueError:
            continue  # no schema tier (domains/): its slug still resolves via the index
        rel = str(p.relative_to(paths.DATA_ROOT))
        # canonicalize + dedup targets within this file
        targets = {slugify(t) for t in extract_wikilinks(body) if t.strip()}
        for key in targets:
            if key and context.resolve_wikilink(key, resolution) is None:
                counts.setdefault(key, set()).add(rel)
    return {key: sorted(refs) for key, refs in counts.items()}


# -- command group -------------------------------------------------------------


@click.group("entities")
def entities_cmd() -> None:
    """Lightweight entity layer: list, gaps, resolve."""


@entities_cmd.command("list")
@click.option(
    "--type",
    "type_",
    type=click.Choice(sorted(ENTITY_TYPES)),
    default=None,
    help="Only show this entity type.",
)
def list_cmd(type_: str | None) -> None:
    """List entity pages (person/org/project) and their aliases."""
    ensure_tree()
    console = Console()

    pages = _entity_pages()
    if type_:
        pages = [t for t in pages if t[1] == type_]

    if not pages:
        console.print("[dim]no entity pages yet[/dim]")
        return

    table = Table(title="entities")
    table.add_column("type")
    table.add_column("slug")
    table.add_column("aliases")
    table.add_column("description", overflow="fold")
    for slug, atlas_type, meta in pages:
        aliases = ", ".join(meta.get("aliases", []) or [])
        table.add_row(atlas_type, slug, aliases or "[dim]—[/dim]", meta.get("description", ""))
    console.print(table)
    console.print(f"[dim]{len(pages)} entity page(s)[/dim]")


def _gap_action(out: writers.WriteOutcome) -> str:
    """The table's action cell for one candidate's outcome."""
    if out.kind == "written":
        return "would file gap" if out.dry_run else "filed gap"
    if out.kind == "reopened":
        return "would reopen gap" if out.dry_run else "reopened gap"
    if out.kind == "suppressed":
        return f"[dim]suppressed ({out.resolution_kind})[/dim]"
    if out.kind in ("already_pending", "exists"):
        return "exists" if out.dry_run else "[dim]exists[/dim]"
    return "[yellow]skipped[/yellow]"  # invalid: write_observation printed why


@entities_cmd.command("gaps")
@click.option(
    "--threshold",
    default=3,
    show_default=True,
    type=int,
    help="Min distinct referrers before a missing target becomes a gap.",
)
@click.option("--dry-run", is_flag=True, help="Show the gaps; write nothing.")
def gaps_cmd(threshold: int, dry_run: bool) -> None:
    """Recurring broken wikilinks → `gap` observations.

    A target referenced by >= threshold distinct vault files with no
    resolving page is a candidate entity gap. Each becomes one pending
    `gap` observation (deduped across runs). Use --dry-run to preview.
    """
    ensure_tree()
    console = Console()

    if dry_run:
        console.print("[bold yellow]dry run[/bold yellow] — no observations will be written")

    # Close first (D3): a pending gap whose target now has a page is resolved
    # (kind done), so it neither counts as pending nor shadows this scan.
    closure = writers.close_resolved_gaps(actor=ACTOR_ENTITIES, dry_run=dry_run, console=console)

    counts = _broken_link_counts()
    candidates = sorted(
        ((key, refs) for key, refs in counts.items() if len(refs) >= threshold),
        key=lambda kr: (-len(kr[1]), kr[0]),
    )

    # One write_observation per candidate, through the recurrence policy: a
    # gap filed by an earlier run is already_pending, a dismissed one is
    # suppressed, a fixed-and-back one is reopened. Dry runs ask the same
    # policy (D5), so the preview cannot drift from a real run.
    outcomes: list[writers.WriteOutcome] = []
    if not candidates:
        console.print(
            f"[dim]no gaps: no target referenced by >= {threshold} file(s) lacks a page[/dim]"
        )
    else:
        table = Table(title=f"entity gaps (>= {threshold} referrers, no page)")
        table.add_column("missing target")
        table.add_column("referrers", justify="right")
        table.add_column("action")
        for key, refs in candidates:
            body = (
                f"`[[{key}]]` is referenced by {len(refs)} vault file(s) but has no "
                f"page. Create an entity page (`sabnyx promote --as person|org|project`) "
                f"or fix the links.\n\n" + "\n".join(f"- {r}" for r in refs)
            )
            out = writers.write_observation(
                ProposedObservation(category="gap", title=key, body=body, involves=refs),
                producer="entities",
                actor=ACTOR_ENTITIES,
                target=key,  # _broken_link_counts keys are already slugs
                dry_run=dry_run,
                console=console,
            )
            outcomes.append(out)
            table.add_row(key, str(len(refs)), _gap_action(out))
        console.print(table)

    written = sum(1 for o in outcomes if o.kind in ("written", "reopened"))
    if not dry_run:
        if candidates:
            console.print(f"[green]{written}[/green] new gap observation(s) filed")
        # Ledgered on every real run, candidates or not: a run that only
        # closed gaps (their pages now exist, so their links resolve) is
        # still a run, and its closure count belongs in its summary.
        ledger.append_event(
            plane="projection",
            kind="entities.gaps_scanned",
            actor=ACTOR_ENTITIES,
            subject="vault/",
            payload={
                "threshold": threshold,
                "candidates": len(candidates),
                # A partition of candidates (Cycle 2 reads these):
                #   filed + already_tracked + suppressed + invalid == candidates
                "filed": written,
                "already_tracked": sum(
                    1 for o in outcomes if o.kind in ("already_pending", "exists")
                ),
                "suppressed": sum(1 for o in outcomes if o.kind == "suppressed"),
                "invalid": sum(1 for o in outcomes if o.kind == "invalid"),
                # Closure is separate from the candidate partition.
                "closed": len(closure.closed),
                "ambiguous": len(closure.ambiguous),
            },
        )
        writers.ledger_suppressed(outcomes, actor=ACTOR_ENTITIES)


@entities_cmd.command("resolve")
def resolve_cmd() -> None:
    """Report alias/slug collisions across entity pages.

    A collision is one token (a page slug or a slugified alias) claimed by
    two or more *different* entity pages — the signal that the pages name the
    same thing and should merge, or that an alias clashes with another page.
    Report only: merging is a human decision (lightweight, not Senzing).
    """
    ensure_tree()
    console = Console()

    pages = _entity_pages()
    # token -> {slug -> how it was claimed ("slug" | "alias:<raw>")}
    token_owners: dict[str, dict[str, str]] = {}
    for slug, _atlas_type, meta in pages:
        slug_key = slug.lower()
        token_owners.setdefault(slug_key, {})[slug] = "slug"
        for alias in meta.get("aliases", []) or []:
            akey = slugify(alias)
            if not akey or akey == slug_key:
                continue  # blank or self-alias: not a collision
            token_owners.setdefault(akey, {}).setdefault(slug, f"alias:{alias}")

    collisions = {tok: owners for tok, owners in token_owners.items() if len(owners) >= 2}

    if not collisions:
        console.print(f"[dim]no collisions across {len(pages)} entity page(s)[/dim]")
    else:
        table = Table(title="entity collisions")
        table.add_column("token")
        table.add_column("claimed by")
        for tok in sorted(collisions):
            owners = collisions[tok]
            claimants = "; ".join(f"{s} ({how})" for s, how in sorted(owners.items()))
            table.add_row(tok, claimants)
        console.print(table)
        console.print(
            f"[yellow]{len(collisions)} collision(s)[/yellow] — review and merge manually"
        )

    ledger.append_event(
        plane="projection",
        kind="entities.resolved",
        actor=ACTOR_ENTITIES,
        subject="vault/",
        payload={"entities": len(pages), "collisions": len(collisions)},
    )
