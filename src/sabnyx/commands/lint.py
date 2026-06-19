"""`sabnyx lint` — Gates 1, 2, 4 (intent, precision, containment).

Exit codes:
    0  all gates pass
    1  gate failures found
    2  usage error
"""

from __future__ import annotations

import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from .. import fm
from ..paths import DATA_ROOT, OBSERVATIONS, ROOT, SOURCES, VAULT
from ..util import walk_vault_md


def _gate_4_containment(path: Path, meta: dict, kind: str) -> list[str]:
    """Gate 4: tier boundaries.

    intake/ must carry status: draft
    atlas/ must carry status: final
    observation must live under intake/.observations/
    """
    errors: list[str] = []
    status = meta.get("status")
    if kind == "intake" and status != "draft":
        errors.append(f"intake/ file has status={status!r}, expected 'draft'")
    if kind == "atlas" and status != "final":
        errors.append(f"atlas/ file has status={status!r}, expected 'final'")
    if meta.get("type") == "observation" and OBSERVATIONS not in path.parents:
        errors.append("observation file is not under vault/intake/.observations/")
    return errors


@click.command("lint")
@click.argument("files", nargs=-1, type=click.Path(exists=True, path_type=Path))
@click.option("--staged", is_flag=True, help="Only lint files staged for git commit.")
def lint_cmd(files: tuple[Path, ...], staged: bool) -> None:
    """Gates 1, 2, 4. Hard-fail on any violation."""
    console = Console()

    targets: list[Path]
    if staged:
        import subprocess

        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        targets = []
        for rel in result.stdout.splitlines():
            p = ROOT / rel
            if p.is_file() and p.suffix == ".md" and VAULT in p.parents:
                # Skip operational files and raw sources (no schema applies)
                if "_review" in p.parts or SOURCES in p.parents:
                    continue
                targets.append(p)
    elif files:
        targets = [
            f
            for f in files
            if f.suffix == ".md" and "_review" not in f.parts and SOURCES not in f.resolve().parents
        ]
    else:
        targets = walk_vault_md(VAULT)

    if not targets:
        console.print("[dim]no vault .md files to lint[/dim]")
        sys.exit(0)

    failures: list[tuple[Path, list[str]]] = []
    for path in targets:
        errs: list[str] = []
        try:
            meta, _body, kind = fm.load(path)
        except fm.FrontmatterError as e:
            failures.append((path, e.errors))
            continue
        except Exception as e:
            failures.append((path, [f"parse error: {e}"]))
            continue

        # Gate 1: intent (description present for typed content)
        if kind in ("intake", "atlas") and not meta.get("description"):
            errs.append("Gate 1 intent: missing description")

        # Gate 4: containment
        errs.extend(_gate_4_containment(path, meta, kind))

        if errs:
            failures.append((path, errs))

    if failures:
        table = Table(title="sabnyx lint — failures", show_lines=True)
        table.add_column("File", style="bold")
        table.add_column("Gate / error")
        for path, errs in failures:
            table.add_row(str(path.relative_to(DATA_ROOT)), "\n".join(f"- {e}" for e in errs))
        console.print(table)
        console.print(f"[red]{len(failures)} file(s) failed lint[/red]")
        sys.exit(1)

    console.print(f"[green]lint OK[/green] ({len(targets)} file(s))")
    sys.exit(0)
