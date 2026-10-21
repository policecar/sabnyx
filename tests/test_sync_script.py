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

...plus the hardening added on review: honouring SABNYX_DATA_DIR (the same
variable sabnyx.paths reads), a flock(1) lock against overlapping runs, a
push-rejected retry, and never leaving the clone mid-rebase on failure.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from sabnyx.paths import DATA_ROOT, EVENTS_JSONL, ROOT

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
        ': "${SABNYX_DATA_DIR:?}"\n'
        'echo \'{"kind":"tend.completed"}\' >> "$SABNYX_DATA_DIR/ops/events.jsonl"\n'
        f'if [ -f "{flag_file}" ]; then\n'
        '  printf "\\nupdated\\n" >> "$SABNYX_DATA_DIR/vault/atlas/x.md"\n'
        "fi\n",
        encoding="utf-8",
    )
    uv.chmod(0o755)


def _write_fake_uv_with_race(shim_dir: Path, hub: Path, *, conflicting: bool) -> None:
    """A fake `uv` that also simulates a competing writer racing the hub.

    Like `_write_fake_uv`, it appends the ledger line and makes a non-ledger
    change (so the commit-gate always trips) -- but it ALSO clones the hub
    into a throwaway directory and pushes a second, independent commit
    straight to it, from inside the `tend` step. That lands the competing
    commit on the hub strictly *after* the script's own initial
    `git pull --rebase --autostash` (already past by the time `tend` runs)
    and *before* the script's own push -- the only way to deterministically
    force `push_with_retry` to see a rejected push without real concurrency.

    When *conflicting* is True, the race commit and the local change rewrite
    the same line of vault/atlas/x.md, so the retry's own
    `git pull --rebase` also fails (push-side conflict). Otherwise the race
    commit touches a different file, so the retry's rebase is clean.
    """
    shim_dir.mkdir(parents=True, exist_ok=True)
    uv = shim_dir / "uv"
    if conflicting:
        local_edit = 'printf "local-change\\n" > "$SABNYX_DATA_DIR/vault/atlas/x.md"\n'
        race_edit = 'printf "race-change\\n" > "$RACE_DIR/vault/atlas/x.md"\n'
    else:
        local_edit = 'printf "local-change\\n" > "$SABNYX_DATA_DIR/vault/atlas/x.md"\n'
        race_edit = 'printf "race\\n" > "$RACE_DIR/race-only.md"\n'
    race_dir = shim_dir.parent / "race-clone"
    uv.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        ': "${SABNYX_DATA_DIR:?}"\n'
        'echo \'{"kind":"tend.completed"}\' >> "$SABNYX_DATA_DIR/ops/events.jsonl"\n'
        + local_edit
        + f'RACE_DIR="{race_dir}"\n'
        'rm -rf "$RACE_DIR"\n'
        f'git -c user.name=t -c user.email=t@x clone -q "{hub}" "$RACE_DIR" >/dev/null 2>&1\n'
        + race_edit
        + 'git -C "$RACE_DIR" -c user.name=t -c user.email=t@x add -A\n'
        'git -C "$RACE_DIR" -c user.name=t -c user.email=t@x commit -q -m "race commit" '
        ">/dev/null\n"
        'git -C "$RACE_DIR" -c user.name=t -c user.email=t@x push -q origin main\n'
        'rm -rf "$RACE_DIR"\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)


def _setup(
    tmp_path: Path,
    *,
    committer_date: str | None = None,
    race: bool | None = None,
) -> dict:
    """Build a bare hub, a vault clone with one commit, a fake code dir, and
    a PATH shim exposing a fake `uv`. Returns the pieces + a ready env dict.

    *race* selects the fake `uv`: None (default) is the plain shim gated by
    `flag_file`; True/False installs `_write_fake_uv_with_race` instead,
    with a conflicting/clean competing commit respectively (see its
    docstring).
    """
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

    if race is None:
        _write_fake_uv(shim_dir, flag_file)
    else:
        _write_fake_uv_with_race(shim_dir, hub, conflicting=race)

    run_env = {
        **os.environ,
        "PATH": f"{shim_dir}:{os.environ['PATH']}",
        "HOME": str(home),
        "SABNYX_CODE_DIR": str(code),
        "SABNYX_DATA_DIR": str(vault),
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


def test_ledger_exclude_path_matches_paths_py():
    """The `:(exclude)…` pathspec for the ledger must track sabnyx.paths.

    Computed from EVENTS_JSONL relative to DATA_ROOT rather than hardcoded,
    so if the ledger ever moves (e.g. under a differently-named ops dir)
    this test fails instead of the script silently committing it every run.
    """
    relpath = EVENTS_JSONL.relative_to(DATA_ROOT).as_posix()
    script_text = SCRIPT.read_text(encoding="utf-8")
    assert f":(exclude){relpath}" in script_text, (
        f"script's ledger exclude pathspec must match sabnyx.paths "
        f"(EVENTS_JSONL relative to DATA_ROOT = {relpath!r}); script:\n{script_text}"
    )


@pytest.mark.skipif(
    shutil.which("flock") is None,
    reason="flock(1) not available on this platform (e.g. macOS); exercised on Linux CI",
)
def test_lock_blocks_concurrent_run(tmp_path):
    """Holding the lock file (as a concurrent run would) makes the script
    exit 0 immediately, with no commit -- an overlapping timer tick is not
    an error."""
    import fcntl

    ctx = _setup(tmp_path)
    vault, hub = ctx["vault"], ctx["hub"]
    head_before = _git("rev-parse", "HEAD", cwd=vault).stdout.strip()

    lock_path = vault / ".sync-tend.lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        result = _run_script(ctx["env"])

        assert result.returncode == 0, (
            f"stdout={result.stdout!r} stderr={result.stderr!r}\nhub log:\n{_hub_log(hub)}"
        )
        assert "another run holds" in result.stderr, result.stderr

        head_after = _git("rev-parse", "HEAD", cwd=vault).stdout.strip()
        assert head_after == head_before, (
            f"lock holder's run must not commit; hub log:\n{_hub_log(hub)}"
        )
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def test_push_retry_recovers_from_a_concurrent_hub_update(tmp_path):
    """A competing writer lands on the hub (on an unrelated file) strictly
    between the script's own initial pull and its own push -- injected via
    the fake `uv` shim mid-`tend`, since a push landed *before* the script
    starts is already absorbed by the script's existing initial
    `git pull --rebase --autostash` and never reaches the new retry code.
    The retry's rebase is clean; both commits end up on the hub and the
    local clone is left exactly in sync with it.
    """
    ctx = _setup(tmp_path, race=False)
    vault, hub = ctx["vault"], ctx["hub"]
    head_before = _git("rev-parse", "HEAD", cwd=vault).stdout.strip()

    result = _run_script(ctx["env"])

    assert result.returncode == 0, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}\nhub log:\n{_hub_log(hub)}"
    )
    log = _hub_log(hub)
    assert "race commit" in log, f"hub missing the competing commit:\n{log}"
    assert "tend " in log, f"hub missing the tend commit:\n{log}"

    status = subprocess.run(
        ["git", "status", "-sb"], cwd=vault, capture_output=True, text=True
    ).stdout
    assert "[ahead" not in status, f"local clone left ahead of the hub: {status!r}"

    head_after = _git("rev-parse", "HEAD", cwd=vault).stdout.strip()
    assert head_after != head_before
    hub_head = subprocess.run(
        ["git", "-C", str(hub), "rev-parse", "main"], capture_output=True, text=True
    ).stdout.strip()
    assert head_after == hub_head, f"local HEAD != hub HEAD; hub log:\n{log}"


def test_push_retry_gives_up_cleanly_on_a_conflicting_hub_update(tmp_path):
    """When the competing commit (injected the same way, mid-`tend`) touches
    the exact same line the local change does, the retry's own
    `git pull --rebase` also conflicts. The script must exit non-zero
    (so the systemd unit fails visibly) and must NOT leave the clone
    mid-rebase."""
    ctx = _setup(tmp_path, race=True)
    vault, hub = ctx["vault"], ctx["hub"]
    head_before = _git("rev-parse", "HEAD", cwd=vault).stdout.strip()

    result = _run_script(ctx["env"])

    assert result.returncode != 0, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}\nhub log:\n{_hub_log(hub)}"
    )
    assert "push rejected and the rebase-retry failed" in result.stderr, result.stderr

    head_after = _git("rev-parse", "HEAD", cwd=vault).stdout.strip()
    assert head_after != head_before, "the local tend commit should still exist locally"

    status = subprocess.run(
        ["git", "status", "--short"], cwd=vault, capture_output=True, text=True
    ).stdout
    assert "rebase in progress" not in status.lower(), status
    assert not (vault / ".git" / "rebase-merge").exists()
    assert not (vault / ".git" / "rebase-apply").exists()


def test_pull_rebase_conflict_aborts_cleanly(tmp_path):
    """A committed local change that conflicts with a competing hub commit
    makes the script's *initial* `git pull --rebase --autostash` fail. The
    script must abort that rebase and exit non-zero rather than leaving the
    clone wedged mid-rebase for the next run to trip over."""
    ctx = _setup(tmp_path)
    vault, hub = ctx["vault"], ctx["hub"]

    # A committed local change, one commit ahead of the hub.
    (vault / "vault" / "atlas" / "x.md").write_text("local-change\n", encoding="utf-8")
    _git("add", "-A", cwd=vault)
    _git("commit", "-q", "-m", "local edit", cwd=vault)

    # A competing push to the hub that touches the exact same line.
    competitor = tmp_path / "competitor"
    _git("clone", "-q", str(hub), str(competitor), cwd=tmp_path)
    (competitor / "vault" / "atlas" / "x.md").write_text("competing-change\n", encoding="utf-8")
    _git("add", "-A", cwd=competitor)
    _git("commit", "-q", "-m", "competing edit", cwd=competitor)
    _git("push", "-q", "origin", "main", cwd=competitor)

    result = _run_script(ctx["env"])

    assert result.returncode != 0, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}\nhub log:\n{_hub_log(hub)}"
    )
    assert "pull/rebase failed" in result.stderr, result.stderr

    status = subprocess.run(
        ["git", "status", "--short"], cwd=vault, capture_output=True, text=True
    ).stdout
    assert "rebase in progress" not in status.lower(), status
    assert not (vault / ".git" / "rebase-merge").exists()
    assert not (vault / ".git" / "rebase-apply").exists()


def test_lock_file_never_staged_by_add(tmp_path):
    """The lock file lives at the vault repo root; `git add -A` must never
    pick it up, even if it happens to exist when the gate runs. It stays an
    untracked on-disk file (`?? .sync-tend.lock`) -- that's fine, it just
    must never be staged or committed."""
    ctx = _setup(tmp_path)
    vault, hub = ctx["vault"], ctx["hub"]
    ctx["flag_file"].write_text("touch", encoding="utf-8")
    (vault / ".sync-tend.lock").write_text("", encoding="utf-8")

    result = _run_script(ctx["env"])

    assert result.returncode == 0, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}\nhub log:\n{_hub_log(hub)}"
    )
    status = subprocess.run(
        ["git", "status", "--short"], cwd=vault, capture_output=True, text=True
    ).stdout
    assert "?? .sync-tend.lock" in status, f"expected it untracked, got: {status!r}"
    assert "A  .sync-tend.lock" not in status, f"lock file was staged: {status!r}"

    tracked = subprocess.run(["git", "ls-files"], cwd=vault, capture_output=True, text=True).stdout
    assert ".sync-tend.lock" not in tracked.splitlines(), "lock file must never be committed"


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


def test_tilde_in_data_dir_is_expanded_like_paths_py(tmp_path):
    """`SABNYX_DATA_DIR=~/...` works for the Python CLI (paths.py expanduser);
    the script must accept the same spelling — review #22 round 2, finding 2."""
    ctx = _setup(tmp_path)
    vault, hub, flag_file = ctx["vault"], ctx["hub"], ctx["flag_file"]
    flag_file.write_text("touch", encoding="utf-8")
    env = {**ctx["env"], "HOME": str(tmp_path), "SABNYX_DATA_DIR": "~/vault"}

    result = _run_script(env)

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "data dir not found" not in result.stderr
    head = _git("rev-parse", "HEAD", cwd=vault).stdout.strip()
    hub_head = _git("rev-parse", "main", cwd=hub).stdout.strip()
    assert hub_head == head, f"hub did not receive the push; hub log:\n{_hub_log(hub)}"


def test_syncs_the_checked_out_branch_not_a_hardcoded_main(tmp_path):
    """A clone on a branch other than `main` must pull from and push to *that*
    branch; previously `push origin main` could exit 0 while the new commit
    never reached the hub — review #22 round 2, finding 1."""
    ctx = _setup(tmp_path)
    vault, hub, flag_file = ctx["vault"], ctx["hub"], ctx["flag_file"]
    _git("checkout", "-q", "-b", "work", cwd=vault)
    _git("push", "-q", "-u", "origin", "work", cwd=vault)
    flag_file.write_text("touch", encoding="utf-8")
    main_before = _git("rev-parse", "main", cwd=hub).stdout.strip()

    result = _run_script(ctx["env"])

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    head = _git("rev-parse", "HEAD", cwd=vault).stdout.strip()
    assert _git("rev-parse", "work", cwd=hub).stdout.strip() == head, (
        f"hub 'work' did not receive the commit; hub log:\n{_hub_log(hub)}"
    )
    assert _git("rev-parse", "main", cwd=hub).stdout.strip() == main_before, (
        f"hub 'main' moved although the clone was on 'work'; hub log:\n{_hub_log(hub)}"
    )
    status = _git("status", "-sb", cwd=vault).stdout.splitlines()[0]
    assert "[ahead" not in status, status


def test_detached_head_is_refused(tmp_path):
    ctx = _setup(tmp_path)
    vault, hub = ctx["vault"], ctx["hub"]
    _git("checkout", "-q", "--detach", cwd=vault)
    hub_before = _hub_log(hub)

    result = _run_script(ctx["env"])

    assert result.returncode == 1, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "detached HEAD" in result.stderr
    assert _hub_log(hub) == hub_before


def test_tilde_in_code_dir_is_expanded_too(tmp_path):
    """Same as the data-dir case, for the sibling variable — review #22 round 3."""
    ctx = _setup(tmp_path)
    (tmp_path / "home-code").mkdir()
    env = {**ctx["env"], "HOME": str(tmp_path), "SABNYX_CODE_DIR": "~/home-code"}

    result = _run_script(env)

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "code dir not found" not in result.stderr
