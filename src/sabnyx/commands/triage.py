"""`sabnyx triage` — review intake queue, propose placements.

Stage 0: a listing + per-file recommendation written to
vault/_review/pending.md. Does not move files. Promotion is a
separate explicit command.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import click
from rich.console import Console

from .. import fm, ledger
from ..paths import DATA_ROOT, INTAKE, OBSERVATIONS, REVIEW, ensure_tree


def _age_days(created_iso: str | datetime) -> int:
    if isinstance(created_iso, str):
        dt = datetime.fromisoformat(created_iso.replace("Z", "+00:00"))
    else:
        dt = created_iso
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (datetime.now(UTC) - dt).days


def _intake_files() -> list[Path]:
    return sorted([p for p in INTAKE.glob("*.md") if p.is_file()])


def _obs_files() -> list[Path]:
    return sorted([p for p in OBSERVATIONS.glob("*.md") if p.is_file()])


def _recommend(meta: dict, kind: str) -> str:
    """Map an intake type to a likely atlas destination.

    Stage 0: heuristic, not authoritative.
    """
    if kind == "observation":
        return "keep in .observations until addressed"
    t = meta.get("type")
    if t == "bookmark":
        return "atlas/references/ (if worth keeping) OR discard"
    if t == "paper":
        return "atlas/references/ + extract a concept to atlas/concepts/"
    if t == "note":
        return "atlas/concepts/ OR atlas/references/ (depends on content)"
    if t == "thought":
        return "atlas/concepts/ if durable; discard if momentary"
    return "unknown type; manual review"


@click.command("triage")
def triage_cmd() -> None:
    """Write vault/_review/pending.md summarizing intake + observations."""
    ensure_tree()
    console = Console()

    intake = _intake_files()
    obs = _obs_files()

    lines: list[str] = []
    lines.append("# Triage queue")
    lines.append("")
    lines.append(f"Generated: {ledger.now().isoformat()}")
    lines.append("")
    lines.append(f"intake files: {len(intake)}   observations: {len(obs)}")
    lines.append("")

    if intake:
        lines.append("## Intake")
        lines.append("")
        for p in intake:
            try:
                meta, _body, kind = fm.load(p)
            except fm.FrontmatterError as e:
                lines.append(f"- [INVALID] {p.relative_to(DATA_ROOT)}: {e.errors}")
                continue
            age = _age_days(meta.get("created", ledger.now()))
            desc = meta.get("description", "")
            rec = _recommend(meta, kind)
            lines.append(
                f"- `{p.relative_to(DATA_ROOT)}` ({meta.get('type')}, age {age}d)  -> {rec}\n  > {desc}"
            )
        lines.append("")

    if obs:
        lines.append("## Observations (pending)")
        lines.append("")
        for p in obs:
            try:
                meta, _body, _kind = fm.load(p)
            except fm.FrontmatterError as e:
                lines.append(f"- [INVALID] {p.relative_to(DATA_ROOT)}: {e.errors}")
                continue
            lines.append(
                f"- `{p.relative_to(DATA_ROOT)}` "
                f"(category: {meta.get('category')}, status: {meta.get('status')})"
            )
        lines.append("")

    out = REVIEW / "pending.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    ledger.append_event(
        kind="triage.proposed",
        actor="sabnyx-cli",
        subject=str(out.relative_to(DATA_ROOT)),
        payload={
            "intake_count": len(intake),
            "observation_count": len(obs),
        },
    )
    console.print(f"[green]wrote[/green] {out.relative_to(DATA_ROOT)}")
    console.print(f"intake: [bold]{len(intake)}[/bold]   observations: [bold]{len(obs)}[/bold]")
