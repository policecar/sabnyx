"""`sabnyx health` — orphans, broken links, schema drift, queue stagnation."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from .. import fm, ledger
from ..paths import INTAKE, OBSERVATIONS, ROOT, VAULT
from ..util import extract_wikilinks, walk_vault_md


def _slug_index() -> dict[str, Path]:
    """Map slug -> path for every file under vault/ (excl. observations)."""
    idx: dict[str, Path] = {}
    for p in walk_vault_md(VAULT):
        if OBSERVATIONS in p.parents:
            continue
        idx[p.stem.lower()] = p
    return idx


def _age_days(ts: datetime | str | None) -> int | None:
    if ts is None:
        return None
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (datetime.now(UTC) - ts).days


@click.command("health")
@click.option("--quick", is_flag=True, help="Skip the expensive checks.")
def health_cmd(quick: bool) -> None:
    """Report orphans, broken wikilinks, stale intake, ledger count."""
    console = Console()
    idx = _slug_index()

    broken_links: list[tuple[Path, str]] = []
    orphans: list[Path] = []
    stale_intake: list[tuple[Path, int]] = []
    schema_drift: list[tuple[Path, str]] = []

    linked_slugs: set[str] = set()
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
        age = _age_days(meta.get("created"))
        if age is not None and age > 60:
            stale_intake.append((p, age))

    # Observations
    pending_obs = sum(1 for _ in OBSERVATIONS.glob("*.md"))

    # Report
    table = Table(title="sabnyx health")
    table.add_column("Check")
    table.add_column("Result", justify="right")

    table.add_row("files indexed", str(len(idx)))
    table.add_row("intake queue", str(len(list(INTAKE.glob("*.md")))))
    table.add_row("pending observations", str(pending_obs))
    table.add_row("broken wikilinks", str(len(broken_links)))
    if not quick:
        table.add_row("atlas orphans", str(len(orphans)))
    table.add_row("stale intake (>60d)", str(len(stale_intake)))
    table.add_row("schema drift", str(len(schema_drift)))
    table.add_row("ledger events", str(ledger.count()))
    console.print(table)

    if broken_links:
        console.print("\n[yellow]broken wikilinks:[/yellow]")
        for path, target in broken_links[:20]:
            console.print(f"  {path.relative_to(ROOT)}  -> [[{target}]]")
        if len(broken_links) > 20:
            console.print(f"  ... and {len(broken_links) - 20} more")

    if schema_drift:
        console.print("\n[red]schema drift:[/red]")
        for path, errs in schema_drift[:10]:
            console.print(f"  {path.relative_to(ROOT)}: {errs}")

    # Ledger event for the audit
    ledger.append_event(
        plane="projection",
        kind="health.audited",
        actor="sabnyx-cli",
        subject="vault/",
        payload={
            "files": len(idx),
            "intake": len(list(INTAKE.glob("*.md"))),
            "observations": pending_obs,
            "broken_links": len(broken_links),
            "orphans": len(orphans) if not quick else None,
            "stale_intake": len(stale_intake),
            "schema_drift": len(schema_drift),
            "quick": quick,
        },
    )

    if schema_drift:
        sys.exit(1)
