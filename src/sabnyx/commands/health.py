"""`sabnyx health` — orphans, broken links, schema drift, queue stagnation."""

from __future__ import annotations

import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from .. import context, fm, ledger
from ..paths import ATLAS, DATA_ROOT, INTAKE, OBSERVATIONS
from ..util import age_days, extract_wikilinks

# Gate 5: jibot flags time-sensitive content whose last_verified is absent
# or older than 180 days. references are the time-sensitive type at stage 0.
CURRENCY_DAYS = 180


def scan_wikilinks(
    idx: dict[str, Path],
) -> tuple[list[tuple[Path, str]], set[str], list[tuple[Path, str]]]:
    """One pass over the index → (broken_links, linked_slugs, schema_drift).

    broken_links: (file, raw target) per wikilink whose lowercased target is
    not a known slug. linked_slugs: targets that resolved (for orphan
    detection). schema_drift: (file, errors) for files that fail to parse.
    Shared by `health` and `tend` so the scan lives in one place.
    """
    broken_links: list[tuple[Path, str]] = []
    linked_slugs: set[str] = set()
    schema_drift: list[tuple[Path, str]] = []
    for path in idx.values():
        try:
            _meta, body, _kind = fm.load(path)
        except fm.FrontmatterError as e:
            schema_drift.append((path, str(e.errors)))
            continue
        for target in extract_wikilinks(body):
            tslug = target.lower().strip()
            if tslug in idx:
                linked_slugs.add(tslug)
            else:
                broken_links.append((path, target))
    return broken_links, linked_slugs, schema_drift


@click.command("health")
@click.option("--quick", is_flag=True, help="Skip the expensive checks.")
def health_cmd(quick: bool) -> None:
    """Report orphans, broken wikilinks, stale intake, ledger count."""
    console = Console()
    idx = context.vault_slug_index()

    orphans: list[Path] = []
    stale_intake: list[tuple[Path, int]] = []

    broken_links, linked_slugs, schema_drift = scan_wikilinks(idx)

    if not quick:
        for slug, p in idx.items():
            # An orphan has no incoming links AND is in atlas/ (intake orphans
            # are expected and uninteresting until promotion).
            if "atlas" in p.parts and slug not in linked_slugs:
                orphans.append(p)

    # Stale intake: > 60 days in intake/ is a stagnation signal.
    for p in INTAKE.glob("*.md"):
        try:
            meta, _body, _kind = fm.load(p)
        except fm.FrontmatterError:
            continue
        age = age_days(meta.get("created"))
        if age is not None and age > 60:
            stale_intake.append((p, age))

    # Gate 5 CURRENCY + Gate 3 MATERIA, over atlas files.
    stale_atlas: list[tuple[Path, int | None]] = []  # last_verified absent or > CURRENCY_DAYS
    untracked: list[Path] = []  # no provenance: not promoted, no source comment
    atlas_count = 0
    for p in ATLAS.rglob("*.md"):
        if not p.is_file():
            continue
        try:
            meta, body, kind = fm.load(p)
        except fm.FrontmatterError:
            continue
        if kind != "atlas":
            continue
        atlas_count += 1

        age = age_days(meta.get("last_verified"))
        if age is None or age > CURRENCY_DAYS:
            stale_atlas.append((p, age))

        if not meta.get("promoted_from") and "<!-- source-" not in body:
            untracked.append(p)

    # Gate 6 COST: intake-to-atlas ratio (raw material piling up unrefined).
    intake_count = len(list(INTAKE.glob("*.md")))
    cost_ratio = intake_count / atlas_count if atlas_count else float(intake_count)

    # Observations
    pending_obs = sum(1 for _ in OBSERVATIONS.glob("*.md"))

    # Report
    table = Table(title="sabnyx health")
    table.add_column("Check")
    table.add_column("Result", justify="right")

    table.add_row("files indexed", str(len(idx)))
    table.add_row("intake queue", str(intake_count))
    table.add_row("atlas files", str(atlas_count))
    table.add_row("intake:atlas ratio (Gate 6 cost)", f"{cost_ratio:.2f}")
    table.add_row("pending observations", str(pending_obs))
    table.add_row("broken wikilinks", str(len(broken_links)))
    if not quick:
        table.add_row("atlas orphans", str(len(orphans)))
    table.add_row("stale intake (>60d)", str(len(stale_intake)))
    table.add_row(f"stale atlas (Gate 5, >{CURRENCY_DAYS}d/unverified)", str(len(stale_atlas)))
    table.add_row("untracked provenance (Gate 3)", str(len(untracked)))
    table.add_row("schema drift", str(len(schema_drift)))
    table.add_row("ledger events", str(ledger.count()))
    console.print(table)

    if broken_links:
        console.print("\n[yellow]broken wikilinks:[/yellow]")
        for path, target in broken_links[:20]:
            console.print(f"  {path.relative_to(DATA_ROOT)}  -> [[{target}]]")
        if len(broken_links) > 20:
            console.print(f"  ... and {len(broken_links) - 20} more")

    if stale_atlas:
        console.print("\n[yellow]stale atlas (Gate 5, verify or refresh):[/yellow]")
        for path, age in stale_atlas[:20]:
            label = "never verified" if age is None else f"{age}d"
            console.print(f"  {path.relative_to(DATA_ROOT)}  ({label})")
        if len(stale_atlas) > 20:
            console.print(f"  ... and {len(stale_atlas) - 20} more")

    if untracked:
        console.print("\n[yellow]untracked provenance (Gate 3):[/yellow]")
        for path in untracked[:20]:
            console.print(f"  {path.relative_to(DATA_ROOT)}")
        if len(untracked) > 20:
            console.print(f"  ... and {len(untracked) - 20} more")

    if schema_drift:
        console.print("\n[red]schema drift:[/red]")
        for path, errs in schema_drift[:10]:
            console.print(f"  {path.relative_to(DATA_ROOT)}: {errs}")

    # Ledger event for the audit
    ledger.append_event(
        plane="projection",
        kind="health.audited",
        actor="sabnyx-cli",
        subject="vault/",
        payload={
            "files": len(idx),
            "intake": intake_count,
            "atlas": atlas_count,
            # A string, not a float: hashed ledger payloads forbid floats so the
            # content_hash stays byte-compatible with opsctl (ADR-0002).
            "cost_ratio": f"{cost_ratio:.3f}",
            "observations": pending_obs,
            "broken_links": len(broken_links),
            "orphans": len(orphans) if not quick else None,
            "stale_intake": len(stale_intake),
            "stale_atlas": len(stale_atlas),
            "untracked_provenance": len(untracked),
            "schema_drift": len(schema_drift),
            "quick": quick,
        },
    )

    if schema_drift:
        sys.exit(1)
