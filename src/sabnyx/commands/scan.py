"""`sabnyx scan` — semantic contradiction scan across the atlas (Phase 5).

The semantic half of Gate 5 (currency / stale-claim detection): the
deterministic half lives in `health`. jibot runs this inside reweave via
QMD semantic search; sabnyx runs it standalone over the whole atlas (or
a slice), since grep-era retrieval can't surface conflicting claims that
share no vocabulary.

  1. assemble atlas excerpts
  2. one structured LLM call -> ContradictionScan
  3. dedupe against pending contradiction observations (by file pair)
  4. human approval gate, then write category: contradiction observations

An observation is cheap to write and expensive to miss; the human still
resolves each one. Writing nothing when the atlas is coherent is the
expected common case.
"""

from __future__ import annotations

import click
from rich.console import Console

from .. import context, fm, ledger, llm, paths, retrieval, writers
from ..paths import ensure_tree
from ..schemas import ContradictionScan, ModelRole

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


def _pending_contradiction_pairs() -> set[frozenset[str]]:
    """File-pair sets already covered by a pending contradiction observation,
    so re-scans don't pile up duplicates."""
    pairs: set[frozenset[str]] = set()
    for p in paths.OBSERVATIONS.glob("*.md"):
        try:
            meta, _body, _ = fm.load(p)
        except Exception:
            continue
        if meta.get("category") == "contradiction" and meta.get("status") == "pending":
            involves = meta.get("involves") or []
            if involves:
                pairs.add(frozenset(involves))
    return pairs


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

    known = _pending_contradiction_pairs()
    fresh = [f for f in result.findings if frozenset(f.involves) not in known]
    dup = len(result.findings) - len(fresh)

    ledger.append_event(
        plane="projection",
        kind="scan.completed",
        actor=ACTOR,
        subject="vault/atlas/",
        payload={
            "files_scanned": len(index),
            "findings": len(result.findings),
            "fresh": len(fresh),
            "duplicates": dup,
        },
    )

    console.print(f"\n[bold]{result.summary}[/bold]")
    if dup:
        console.print(f"[dim]{dup} finding(s) already tracked — skipped[/dim]")
    for f in fresh:
        console.print(f"  [yellow]contradiction[/yellow] {', '.join(f.involves)} — {f.title}")
    if not fresh:
        console.print("[green]no new contradictions[/green]")
        return

    if dry_run:
        console.print("[dim]dry run: nothing written[/dim]")
        return
    if not yes and not click.confirm("\nfile these observations?", default=True):
        console.print("[dim]declined; nothing written[/dim]")
        return

    written = 0
    for f in fresh:
        f.category = "contradiction"  # enforce regardless of model
        if writers.write_observation(f, actor=ACTOR, console=console):
            written += 1
    console.print(f"\n[green]filed {written} contradiction observation(s)[/green]")
