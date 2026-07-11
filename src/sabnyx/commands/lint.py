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
from ..paths import DATA_ROOT, OBSERVATIONS, SOURCES, VAULT
from ..util import walk_vault_md


def _lintable(path: Path) -> bool:
    """A schema-bearing vault .md file.

    Excludes operational `_review` files and raw `sources/` (no schema applies).
    Vault-membership scoping is applied separately by `--staged` (git reports
    repo-wide changes); explicitly-passed files are linted wherever they live.
    """
    return (
        path.suffix == ".md"
        and "_review" not in path.parts
        and SOURCES not in path.resolve().parents
    )


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

        # The vault lives in the DATA repo (SABNYX_DATA_DIR may point away
        # from the code repo) — the pre-commit gate must query THAT index,
        # not the code repo's, or it silently lints nothing.
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
            capture_output=True,
            text=True,
            cwd=DATA_ROOT,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or "unknown git error"
            console.print(f"[red]could not inspect staged files:[/red] {detail}")
            sys.exit(1)
        targets = []
        for rel in result.stdout.splitlines():
            p = DATA_ROOT / rel
            if p.is_file() and VAULT in p.parents and _lintable(p):
                targets.append(p)
    elif files:
        targets = [f for f in files if _lintable(f)]
    else:
        targets = walk_vault_md(VAULT)

    if not targets:
        console.print("[dim]no vault .md files to lint[/dim]")
        sys.exit(0)

    failures: list[tuple[Path, list[str]]] = []
    skipped = 0
    for path in targets:
        errs: list[str] = []
        try:
            meta, _body, kind = fm.load(path)
        except fm.FrontmatterError as e:
            failures.append((path, e.errors))
            continue
        except ValueError:
            # No schema tier applies (domains/ carries its own per-topic
            # schema, SPEC §1) — skip with a note, like sources/ (ADR-0005).
            skipped += 1
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

    if skipped:
        console.print(f"[dim]{skipped} file(s) skipped (no schema tier, e.g. domains/)[/dim]")

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
