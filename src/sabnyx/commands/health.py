"""`sabnyx health` — orphans, broken links, schema drift, queue stagnation."""

from __future__ import annotations

import sys
from pathlib import Path

import click
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from .. import context, fm, ledger, tend_detectors
from .. import observations as obs
from ..paths import ATLAS, DATA_ROOT, INTAKE
from ..util import age_days, extract_wikilinks

# Gate 5: jibot flags time-sensitive content whose last_verified is absent
# or older than 180 days. references are the time-sensitive type at stage 0;
# concepts/ideas/entities don't go stale on a clock, so they are exempt.
CURRENCY_DAYS = 180
CURRENCY_TYPES = frozenset({"reference"})


def scan_wikilinks(
    idx: dict[str, Path],
) -> tuple[list[tuple[Path, str]], set[str], list[tuple[Path, str]], set[str]]:
    """One pass over the index → (broken_links, linked_slugs, schema_drift,
    has_outgoing).

    Targets resolve through context.link_resolution_index, so slugified
    spaced names and `aliases` frontmatter both count. broken_links: (file,
    raw target) per unresolvable wikilink. linked_slugs: canonical slugs
    that received a link. has_outgoing: slugs whose body links to at least
    one resolving target (dead-end detection). Files no schema tier covers
    (domains/ has its own per-topic schema) are skipped — they still count
    as link targets via ``idx``, but are not schema drift. Shared by
    `health` and `tend` so the scan lives in one place.
    """
    resolution = context.link_resolution_index(idx)
    broken_links: list[tuple[Path, str]] = []
    linked_slugs: set[str] = set()
    schema_drift: list[tuple[Path, str]] = []
    has_outgoing: set[str] = set()
    for slug, path in idx.items():
        try:
            _meta, body, _kind = fm.load(path)
        except fm.FrontmatterError as e:
            schema_drift.append((path, str(e.errors)))
            continue
        except ValueError:
            continue  # unclassifiable (e.g. domains/): its own schema, not drift
        for target in extract_wikilinks(body):
            resolved = context.resolve_wikilink(target, resolution)
            if resolved is not None:
                linked_slugs.add(resolved)
                has_outgoing.add(slug)
            else:
                broken_links.append((path, target))
    return broken_links, linked_slugs, schema_drift, has_outgoing


ANOMALIES_HEADING = "tend anomalies (as of the last recorded tend run)"


def print_anomalies(console: Console, anomalies: list[tend_detectors.Anomaly]) -> None:
    """The anomaly section shared by `health` and `tend`. Findings describe
    the latest *recorded* runs: while tend is stopped, this shows neither
    recovery nor new failure. Subjects are ledger data, so escaped."""
    console.print(f"\n[yellow]{ANOMALIES_HEADING}:[/yellow]")
    if not anomalies:
        console.print("  none")
    for a in anomalies:
        span = (
            f"{a.count} consecutive runs"
            if a.count == a.window and a.detector != "ceiling_hit"
            else f"{a.count} of the last {a.window} runs"
        )
        console.print(f"  {a.detector}  {escape(a.subject)}  ({span})", soft_wrap=True)


def _console() -> Console:
    """The command's console. A seam: tests swap in a fixed-width, colourless
    console so the report is byte-stable."""
    return Console()


def pending_observation_count() -> int:
    """Pending observations, through the shared reader — the same count
    vault_pulse, triage, reweave and tend see."""
    return len(obs.observations())


@click.command("health")
@click.option("--quick", is_flag=True, help="Skip the expensive checks.")
def health_cmd(quick: bool) -> None:
    """Report orphans, broken wikilinks, stale intake, ledger count."""
    console = _console()
    idx = context.vault_slug_index()

    orphans: list[Path] = []
    dead_ends: list[Path] = []
    stale_intake: list[tuple[Path, int]] = []

    broken_links, linked_slugs, schema_drift, has_outgoing = scan_wikilinks(idx)

    if not quick:
        for slug, p in idx.items():
            # Atlas files only: intake orphans are expected and uninteresting
            # until promotion. jibot: orphan = no links in either direction;
            # dead-end = receives links but links nowhere.
            if "atlas" not in p.parts:
                continue
            incoming = slug in linked_slugs
            outgoing = slug in has_outgoing
            if not incoming and not outgoing:
                orphans.append(p)
            elif incoming and not outgoing:
                dead_ends.append(p)

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
    # Sorted: the printed stale/untracked lists must not follow filesystem order.
    for p in sorted(ATLAS.rglob("*.md")):
        if not p.is_file():
            continue
        try:
            meta, body, kind = fm.load(p)
        except fm.FrontmatterError:
            continue
        if kind != "atlas":
            continue
        atlas_count += 1

        if meta.get("type") in CURRENCY_TYPES:
            age = age_days(meta.get("last_verified"))
            if age is None or age > CURRENCY_DAYS:
                stale_atlas.append((p, age))

        if not meta.get("promoted_from") and "<!-- source-" not in body:
            untracked.append(p)

    # Gate 6 COST: intake-to-atlas ratio (raw material piling up unrefined).
    intake_count = len(list(INTAKE.glob("*.md")))
    cost_ratio = intake_count / atlas_count if atlas_count else float(intake_count)

    # Observations: one parse; malformed files are reported, never counted.
    valid_obs, malformed_obs = obs.load_all()
    resolver = obs.resolver()
    integrity = obs.integrity_warnings(obs.index(valid_obs, resolver), resolver)
    pending_obs = sum(1 for o in valid_obs if o.fm.status == "pending")

    anomalies = tend_detectors.detect(ledger.replay())

    # Report
    table = Table(title="sabnyx health")
    table.add_column("Check")
    table.add_column("Result", justify="right")

    table.add_row("files indexed", str(len(idx)))
    table.add_row("intake queue", str(intake_count))
    table.add_row("atlas files", str(atlas_count))
    table.add_row("intake:atlas ratio (Gate 6 cost)", f"{cost_ratio:.2f}")
    table.add_row("pending observations", str(pending_obs))
    table.add_row("malformed observations", str(len(malformed_obs)))
    table.add_row("observation integrity warnings", str(len(integrity)))
    table.add_row("broken wikilinks", str(len(broken_links)))
    if not quick:
        table.add_row("atlas orphans", str(len(orphans)))
        table.add_row("atlas dead ends", str(len(dead_ends)))
    table.add_row("stale intake (>60d)", str(len(stale_intake)))
    table.add_row(f"stale atlas (Gate 5, >{CURRENCY_DAYS}d/unverified)", str(len(stale_atlas)))
    table.add_row("untracked provenance (Gate 3)", str(len(untracked)))
    table.add_row("schema drift", str(len(schema_drift)))
    table.add_row("ledger events", str(ledger.count()))
    table.add_row("tend anomalies", str(len(anomalies)))
    console.print(table)

    if anomalies:
        print_anomalies(console, anomalies)

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

    if integrity:
        # Warnings, not failures: the exit code does not change. Sorted, and
        # printed whole (no truncation): Cycle 2 freezes this text.
        console.print("\n[yellow]observation integrity:[/yellow]")
        for w in integrity:
            console.print(f"  {escape(w.text())}", soft_wrap=True)  # one line each

    if malformed_obs:
        console.print("\n[yellow]malformed observations (not counted):[/yellow]")
        for path, err in malformed_obs[:20]:
            console.print(f"  {path.relative_to(DATA_ROOT)}: {err}")
        if len(malformed_obs) > 20:
            console.print(f"  ... and {len(malformed_obs) - 20} more")

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
            "malformed_observations": len(malformed_obs),
            "integrity_warnings": len(integrity),
            "broken_links": len(broken_links),
            "orphans": len(orphans) if not quick else None,
            "dead_ends": len(dead_ends) if not quick else None,
            "stale_intake": len(stale_intake),
            "stale_atlas": len(stale_atlas),
            "untracked_provenance": len(untracked),
            "schema_drift": len(schema_drift),
            "quick": quick,
        },
    )

    if schema_drift:
        sys.exit(1)
