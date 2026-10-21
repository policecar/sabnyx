"""Tests for ops/scripts/sabnyx-sync-tend.sh: the server heartbeat.

Runs the real script as a subprocess against real git repos (a bare "hub"
and a vault clone) with a fake `uv` on PATH that stands in for
`uv run sabnyx tend --yes`. No bash logic is mocked — only the one real
external dependency (the `tend` CLI call) is stubbed, exactly at the
boundary the script itself treats as opaque.

Covers the three outcomes of the commit-gating block (SPEC in the script's
header comment):
  - ledger-only diff, fresh HEAD  -> leave staged, exit 0, no commit/push
  - non-ledger diff               -> commit + push regardless of HEAD age
  - ledger-only diff, stale HEAD  -> commit + push anyway (bounds staleness)
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from sabnyx.paths import ROOT

SCRIPT = ROOT / "ops" / "scripts" / "sabnyx-sync-tend.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None,
    reason="requires bash and git on PATH",
)

GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@x"]


def _git(*args, cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *GIT_ID, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"git {args} failed: {result.stdout}{result.stderr}"
    return result


def _write_fake_uv(shim_dir: Path, flag_file: Path) -> None:
    """A fake `uv` that simulates `uv run sabnyx tend --yes`.

    Always appends one ledger line (tend.completed) to the vault's
    ops/events.jsonl; additionally edits vault/atlas/x.md when *flag_file*
    exists, so tests can opt into a "real" (non-ledger) change.
    """
    shim_dir.mkdir(parents=True, exist_ok=True)
    uv = shim_dir / "uv"
    uv.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        ': "${SABNYX_VAULT_DIR:?}"\n'
        'echo \'{"kind":"tend.completed"}\' >> "$SABNYX_VAULT_DIR/ops/events.jsonl"\n'
        f'if [ -f "{flag_file}" ]; then\n'
        '  printf "\\nupdated\\n" >> "$SABNYX_VAULT_DIR/vault/atlas/x.md"\n'
        "fi\n",
        encoding="utf-8",
    )
    uv.chmod(0o755)


def _setup(tmp_path: Path, *, committer_date: str | None = None) -> dict:
    """Build a bare hub, a vault clone with one commit, a fake code dir, and
    a PATH shim exposing a fake `uv`. Returns the pieces + a ready env dict."""
    hub = tmp_path / "hub"
    vault = tmp_path / "vault"
    code = tmp_path / "code"
    shim_dir = tmp_path / "bin"
    flag_file = tmp_path / "touch_atlas"
    home = tmp_path / "home"
    code.mkdir()
    home.mkdir()

    subprocess.run(["git", "init", "-q", "-b", "main", "--bare", str(hub)], check=True)
    _git("clone", "-q", str(hub), str(vault), cwd=tmp_path)

    (vault / "ops").mkdir(parents=True)
    (vault / "ops" / "events.jsonl").write_text("", encoding="utf-8")
    (vault / "vault" / "atlas").mkdir(parents=True)
    (vault / "vault" / "atlas" / "x.md").write_text("# x\n", encoding="utf-8")

    commit_env = {**os.environ}
    if committer_date is not None:
        commit_env["GIT_AUTHOR_DATE"] = committer_date
        commit_env["GIT_COMMITTER_DATE"] = committer_date

    _git("add", "-A", cwd=vault)
    _git("commit", "-q", "-m", "initial", cwd=vault, env=commit_env)
    _git("push", "-q", "-u", "origin", "main", cwd=vault)

    _write_fake_uv(shim_dir, flag_file)

    run_env = {
        **os.environ,
        "PATH": f"{shim_dir}:{os.environ['PATH']}",
        "HOME": str(home),
        "SABNYX_CODE_DIR": str(code),
        "SABNYX_VAULT_DIR": str(vault),
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x",
    }
    return {
        "hub": hub,
        "vault": vault,
        "code": code,
        "flag_file": flag_file,
        "env": run_env,
    }


def _run_script(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
    )


def _hub_log(hub: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(hub), "log", "--oneline", "--all"],
        capture_output=True,
        text=True,
    )
    return result.stdout + result.stderr


def test_ledger_only_change_leaves_staged_without_committing(tmp_path):
    ctx = _setup(tmp_path)
    vault, hub = ctx["vault"], ctx["hub"]
    head_before = _git("rev-parse", "HEAD", cwd=vault).stdout.strip()

    result = _run_script(ctx["env"])

    assert result.returncode == 0, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}\nhub log:\n{_hub_log(hub)}"
    )
    head_after = _git("rev-parse", "HEAD", cwd=vault).stdout.strip()
    assert head_after == head_before, f"HEAD moved; hub log:\n{_hub_log(hub)}"

    status = subprocess.run(
        ["git", "status", "--short"], cwd=vault, capture_output=True, text=True
    ).stdout
    assert "M  ops/events.jsonl" in status, f"unexpected status: {status!r}"

    # nothing was pushed
    hub_head = subprocess.run(
        ["git", "-C", str(hub), "rev-parse", "main"], capture_output=True, text=True
    ).stdout.strip()
    assert hub_head == head_before, f"hub advanced unexpectedly; hub log:\n{_hub_log(hub)}"


def test_non_ledger_change_commits_and_pushes(tmp_path):
    ctx = _setup(tmp_path)
    vault, hub, flag_file = ctx["vault"], ctx["hub"], ctx["flag_file"]
    flag_file.write_text("touch", encoding="utf-8")
    head_before = _git("rev-parse", "HEAD", cwd=vault).stdout.strip()

    result = _run_script(ctx["env"])

    assert result.returncode == 0, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}\nhub log:\n{_hub_log(hub)}"
    )
    head_after = _git("rev-parse", "HEAD", cwd=vault).stdout.strip()
    assert head_after != head_before, f"no new commit; hub log:\n{_hub_log(hub)}"

    message = _git("log", "-1", "--format=%s", cwd=vault).stdout.strip()
    assert message.startswith("tend "), f"unexpected message {message!r}"

    hub_head = subprocess.run(
        ["git", "-C", str(hub), "rev-parse", "main"], capture_output=True, text=True
    ).stdout.strip()
    assert hub_head == head_after, f"hub did not receive the push; hub log:\n{_hub_log(hub)}"


def test_stale_head_commits_ledger_only_change(tmp_path):
    # Backdate the initial commit by 2 days so the >24h staleness bound fires.
    stale_date = "2026-10-19T00:00:00+00:00"
    ctx = _setup(tmp_path, committer_date=stale_date)
    vault, hub = ctx["vault"], ctx["hub"]
    head_before = _git("rev-parse", "HEAD", cwd=vault).stdout.strip()

    result = _run_script(ctx["env"])

    assert result.returncode == 0, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}\nhub log:\n{_hub_log(hub)}"
    )
    head_after = _git("rev-parse", "HEAD", cwd=vault).stdout.strip()
    assert head_after != head_before, f"no new commit despite stale HEAD; hub log:\n{_hub_log(hub)}"

    message = _git("log", "-1", "--format=%s", cwd=vault).stdout.strip()
    assert message.startswith("tend "), f"unexpected message {message!r}"

    hub_head = subprocess.run(
        ["git", "-C", str(hub), "rev-parse", "main"], capture_output=True, text=True
    ).stdout.strip()
    assert hub_head == head_after, f"hub did not receive the push; hub log:\n{_hub_log(hub)}"


def test_script_is_executable_and_strict():
    assert SCRIPT.is_file()
    assert os.access(SCRIPT, os.X_OK), "script must be chmod +x (0755)"

    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "#!/usr/bin/env bash"

    # The first non-comment, non-blank line must be `set -euo pipefail` --
    # guards against someone editing in comments/logic ahead of strict mode.
    first_code_line = next(
        line for line in lines[1:] if line.strip() and not line.strip().startswith("#")
    )
    assert first_code_line == "set -euo pipefail"
