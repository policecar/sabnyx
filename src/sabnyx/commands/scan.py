"""`sabnyx scan` — semantic contradiction scan across the atlas (Phase 5).

The semantic half of Gate 5 (currency / stale-claim detection): the
deterministic half lives in `health`. jibot runs this inside reweave via
QMD semantic search; sabnyx runs it standalone over the whole atlas (or
a slice), since grep-era retrieval can't surface conflicting claims that
share no vocabulary.

  1. assemble atlas excerpts
  2. one structured LLM call -> ContradictionScan
  3. dry pass through the recurrence policy (write_observation, dry_run),
     with identical pairs deduped within the one LLM result
  4. human approval gate, then the real pass; dismissed signals are counted
     once per run (observation.suppressed)

An observation is cheap to write and expensive to miss; the human still
resolves each one. Writing nothing when the atlas is coherent is the
expected common case.
"""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console

from .. import context, ledger, llm, paths, retrieval, writers
from ..paths import ensure_tree
from ..schemas import ContradictionScan, ModelRole, ProposedObservation

ACTOR = "sabnyx-scan"

_TASK_SYSTEM = """You are the contradiction scanner for this vault. Given the atlas
(durable knowledge), find pairs of files that make genuinely conflicting claims
about the same entity, concept, role, date, stance, or mechanism. Produce a
ContradictionScan.

Rules, in addition to the SPEC and ROUTING contract above:

- A finding is a real contradiction, not a difference in topic or emphasis.
  Two files covering different aspects of one subject is NOT a contradiction.
  If you are not confident both files assert incompatible things, omit it.
- Each finding is an observation: category contradiction, BOTH file slugs in
  `involves` (with .md), a title naming the conflict, and a body stating each
  file's claim and a suggested resolution (which to verify, or how to
  reconcile). Cite the files as [[wikilinks]] in the body.
- Only reference slugs that appear in the material provided.
- Report nothing if the atlas is coherent. Precision over recall: a missed
  contradiction costs less than a false one the operator must chase down."""


@click.command("scan")
@click.option(
    "--role",
    type=click.Choice(["fast", "standard", "deep"]),
    default="deep",
    help="Model role (default: deep — contradiction detection rewards reasoning).",
)
@click.option("--yes", "-y", is_flag=True, help="Write findings without the approval prompt.")
@click.option("--dry-run", is_flag=True, help="Show findings; write nothing.")
def scan_cmd(role: ModelRole, yes: bool, dry_run: bool) -> None:
    """Scan the atlas for contradicting claims; file them as observations."""
    console = Console()
    ensure_tree()

    index = context.slug_index(paths.ATLAS)
    if len(index) < 2:
        console.print("[dim]fewer than 2 atlas files; nothing to scan[/dim]")
        return

    console.print(
        f"scanning [bold]{len(index)}[/bold] atlas files  "
        f"(role: {role}, model: {llm.model_for(role)})"
    )

    result = llm.parse_structured(
        task_system=_TASK_SYSTEM,
        user="## Atlas (durable knowledge; excerpts)\n"
        # Contradiction scan wants the whole atlas, not a query-ranked slice.
        + retrieval.get_retriever().retrieve(None, index),
        output_format=ContradictionScan,
        role=role,
        actor=ACTOR,
        purpose="scan:atlas-contradictions",
    )

    findings = list(result.findings)
    for f in findings:
        f.category = "contradiction"  # enforce regardless of model, before any id is computed

    # 1. Dry pass through the real policy (D5): what a real run would do with
    #    each finding, with nothing written and nothing ledgered.
    previews = [
        writers.write_observation(f, producer="scan", actor=ACTOR, dry_run=True, console=console)
        for f in findings
    ]
    # 2. Within-result dedupe (D5b). The model can name one pair twice, in two
    #    forms (alpha.md / alpha); against an empty folder both would preview as
    #    "written" while the real pass files one. Keyed by obs_id; prose (no
    #    id) by the file it would write, which a second prose write would find
    #    taken. So the count the operator confirms is the count the real pass
    #    files.
    plan: list[tuple[ProposedObservation, writers.WriteOutcome]] = []
    seen_ids: set[str] = set()
    planned_paths: set[Path] = set()
    for f, pv in zip(findings, previews, strict=True):
        if pv.obs_id is not None:
            if pv.obs_id in seen_ids:
                continue
            seen_ids.add(pv.obs_id)
        elif pv.path in planned_paths:
            continue
        if pv.kind in ("written", "reopened"):
            planned_paths.add(pv.path)
        plan.append((f, pv))
    fresh = [f for f, pv in plan if pv.kind in ("written", "reopened")]
    suppressed = [f for f, pv in plan if pv.kind == "suppressed"]
    invalid = [f for f, pv in plan if pv.kind == "invalid"]
    # The scan.completed counts PARTITION the findings (Cycle 2 reads them):
    #   fresh + duplicates + suppressed + invalid == findings
    # duplicates = in-result repeats + already_pending + exists (already tracked).
    dup = len(findings) - len(fresh) - len(suppressed) - len(invalid)

    # Dry runs are ledger-write-free: the llm.* events for the call that
    # really happened are appended by parse_structured and correctly stay.
    if not dry_run:
        ledger.append_event(
            plane="projection",
            kind="scan.completed",
            actor=ACTOR,
            subject="vault/atlas/",
            payload={
                "files_scanned": len(index),
                "findings": len(findings),
                "fresh": len(fresh),
                "duplicates": dup,
                "suppressed": len(suppressed),
                "invalid": len(invalid),
            },
        )

    console.print(f"\n[bold]{result.summary}[/bold]")
    if dup:
        console.print(f"[dim]{dup} finding(s) already tracked — skipped[/dim]")
    if suppressed:
        console.print(f"[dim]{len(suppressed)} finding(s) dismissed earlier — not filed[/dim]")
    for f in fresh:
        console.print(f"  [yellow]contradiction[/yellow] {', '.join(f.involves)} — {f.title}")
    if not fresh:
        console.print("[green]no new contradictions[/green]")
    if dry_run:
        if fresh:
            console.print("[dim]dry run: nothing written[/dim]")
        return

    confirmed = bool(fresh) and (
        yes or click.confirm(f"\nfile these {len(fresh)} observation(s)?", default=True)
    )
    if fresh and not confirmed:
        console.print("[dim]declined; nothing written[/dim]")

    # 3. Real pass: the confirmed findings, plus the suppressed ones. A
    #    suppressed write writes nothing, so running it needs no consent, and
    #    it is what lets this run count the dismissed signal it met (D4).
    outcomes = [
        writers.write_observation(f, producer="scan", actor=ACTOR, console=console)
        for f, pv in plan
        if (confirmed and pv.kind in ("written", "reopened")) or pv.kind == "suppressed"
    ]
    writers.ledger_suppressed(outcomes, actor=ACTOR)
    if confirmed:
        written = sum(1 for o in outcomes if o.kind in ("written", "reopened"))
        console.print(f"\n[green]filed {written} contradiction observation(s)[/green]")
