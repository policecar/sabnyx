"""vault_lock: the SAME flock(2) lock file the bash heartbeat
(ops/scripts/sabnyx-sync-tend.sh -- `flock -n 9` on
$SABNYX_DATA_DIR/.sync-tend.lock) holds for the duration of a `tend` run,
so a long-running writer (serve/serve-http) and the heartbeat can never
interleave vault writes.

Cases 1-3 contend across a real subprocess (the lock is a kernel object,
not a Python one -- an in-process-only test would prove nothing about the
bash interop this exists for). Case 4 is the serve-level integration: a
mocked VaultLocked must surface as a clear reply/503, never a crash.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import sys
import time

import pytest

from sabnyx import paths as paths_mod
from sabnyx.util import VaultLocked, vault_lock

LOCK_NAME = ".sync-tend.lock"


def _lock_path(data_root):
    return data_root / LOCK_NAME


def _wait_until_externally_locked(lock_path, timeout: float = 2.0) -> bool:
    """Poll until some OTHER process holds an exclusive flock on lock_path.

    Avoids a race against a subprocess that hasn't acquired the lock yet.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return True
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            time.sleep(0.02)
    return False


_PROBE = (
    "import fcntl, os, sys\n"
    "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)\n"
    "try:\n"
    "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
    "except BlockingIOError:\n"
    "    sys.exit(11)\n"
    "else:\n"
    "    fcntl.flock(fd, fcntl.LOCK_UN)\n"
    "    sys.exit(0)\n"
    "finally:\n"
    "    os.close(fd)\n"
)

_HOLDER = (
    "import fcntl, os, sys, time\n"
    "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)\n"
    "fcntl.flock(fd, fcntl.LOCK_EX)\n"
    "time.sleep(float(sys.argv[2]))\n"
)


def _probe(lock_path) -> int:
    return subprocess.run([sys.executable, "-c", _PROBE, str(lock_path)]).returncode


# -- 1: two processes contending -----------------------------------------------


def test_vault_lock_acquires_cleanly_when_free(tmp_path, monkeypatch):
    monkeypatch.setattr(paths_mod, "DATA_ROOT", tmp_path)
    with vault_lock(timeout=1.0):
        pass  # must not raise


def test_subprocess_blocked_while_held_then_succeeds_after_release(tmp_path, monkeypatch):
    monkeypatch.setattr(paths_mod, "DATA_ROOT", tmp_path)
    lock_path = _lock_path(tmp_path)

    with vault_lock(timeout=1.0):
        assert _probe(lock_path) == 11, "subprocess should find the lock held (EWOULDBLOCK)"

    assert _probe(lock_path) == 0, "subprocess should acquire once vault_lock released it"


# -- 2: timeout path ------------------------------------------------------------


def test_timeout_raises_vault_locked_in_about_the_requested_time(tmp_path, monkeypatch):
    monkeypatch.setattr(paths_mod, "DATA_ROOT", tmp_path)
    lock_path = _lock_path(tmp_path)

    proc = subprocess.Popen([sys.executable, "-c", _HOLDER, str(lock_path), "5"])
    try:
        assert _wait_until_externally_locked(lock_path), "subprocess never acquired the lock"

        start = time.monotonic()
        with pytest.raises(VaultLocked, match="vault is busy"), vault_lock(timeout=0.5):
            pass
        elapsed = time.monotonic() - start
        assert 0.4 <= elapsed <= 2.0, f"expected ~0.5s timeout, took {elapsed:.2f}s"
    finally:
        proc.kill()
        proc.wait()


# -- 3: bash flock(1) interop ---------------------------------------------------


@pytest.mark.skipif(
    shutil.which("flock") is None,
    reason="flock(1) not available on this platform (e.g. macOS); exercised on Linux CI",
)
def test_bash_flock_sees_python_held_lock_and_succeeds_after_release(tmp_path, monkeypatch):
    monkeypatch.setattr(paths_mod, "DATA_ROOT", tmp_path)
    lock_path = _lock_path(tmp_path)

    with vault_lock(timeout=1.0):
        blocked = subprocess.run(["flock", "-n", str(lock_path), "true"])
        assert blocked.returncode != 0

    freed = subprocess.run(["flock", "-n", str(lock_path), "true"])
    assert freed.returncode == 0


# -- 4: serve-level integration (VaultLocked never crashes the process) --------


def test_serve_telegram_ingest_replies_busy_on_vault_locked(tmp_vault, monkeypatch):
    from sabnyx.commands import serve

    def _always_busy(timeout: float = 120.0):
        raise VaultLocked("vault is busy (another writer...); try again shortly")

    monkeypatch.setattr(serve, "vault_lock", _always_busy)

    reply = serve._ingest("buy milk", title="buy milk", source_kind="manual", source_url=None)

    assert "vault is busy" in reply
    assert not list(tmp_vault["INTAKE"].glob("*.md"))


def test_serve_http_ingest_returns_503_on_vault_locked(tmp_vault, monkeypatch):
    from sabnyx.commands import serve_http

    def _always_busy(timeout: float = 120.0):
        raise VaultLocked("vault is busy (another writer...); try again shortly")

    monkeypatch.setattr(serve_http, "vault_lock", _always_busy)

    status, body = serve_http.route(
        "POST", "/ingest", b'{"text":"remember buy milk"}', "Bearer k1", {"k1"}
    )

    assert status == 503
    assert "vault is busy" in body["error"]
    assert not list(tmp_vault["INTAKE"].glob("*.md"))
