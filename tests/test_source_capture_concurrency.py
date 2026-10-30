"""Raw-source capture stays write-once under concurrency and failure (R02).

Why this matters: vault/sources/ holds the immutable originals every
re-extraction and contradiction check reads (ADR 0005). Before the fix,
`capture` checked `exists()` and then called `Path.write_text`, which
truncates. Two processes capturing different bodies under one title could
both see the name free; both reported success and ledgered a capture, but
only one body survived. CLI ingest does not take the daemon's vault lock, so
serial request handling in one server never protected against this.

The concurrent cases run two real child processes (tests/_source_capture_
child.py) that share one redirected DATA_ROOT and are paused at the
publication boundary, so the interleaving is forced every run rather than
hoped for. The failure cases run in-process.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from sabnyx import ledger, paths, sources

CHILD = Path(__file__).with_name("_source_capture_child.py")
JOIN_TIMEOUT_S = 30.0
FAKE_DIGEST = "ab" * 32


def _source_files() -> list[str]:
    """Every entry in sources/, hidden temp files included (only ensure_tree's
    placeholder is ignored)."""
    return sorted(p.name for p in sources.SOURCES.iterdir() if p.name != ".gitkeep")


def _captured_subjects() -> list[str]:
    return sorted(e.subject for e in ledger.replay() if e.kind == "source.captured")


def _diagnostics() -> str:
    return f"sources={_source_files()} captured={_captured_subjects()}"


def _run_workers(tmp_path: Path, workers: dict[str, dict[str, str]]) -> dict[str, dict]:
    """Start one child per worker, join each with a bounded wait, and return
    its parsed JSON result. Any timeout or crash fails with both children's
    output and the vault state, so a hung barrier is diagnosable."""
    sync = tmp_path / "sync"
    sync.mkdir()
    base_env = {
        **os.environ,
        # Explicit, not inherited: the child resolves DATA_ROOT at import
        # time, and must land in this test's vault, never the live one.
        "SABNYX_DATA_DIR": str(paths.DATA_ROOT),
        "CAPTURE_SYNC_DIR": str(sync),
        "CAPTURE_PARTIES": ",".join(workers),
    }
    procs = {
        name: subprocess.Popen(
            [sys.executable, str(CHILD)],
            env={**base_env, "CAPTURE_WORKER": name, **env},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for name, env in workers.items()
    }
    outputs: dict[str, tuple[int | None, str, str]] = {}
    try:
        for name, proc in procs.items():
            try:
                out, err = proc.communicate(timeout=JOIN_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = proc.communicate()
            outputs[name] = (proc.returncode, out, err)
    finally:
        for proc in procs.values():
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    report = "\n".join(
        f"[{n}] rc={rc}\nstdout={out!r}\nstderr={err}" for n, (rc, out, err) in outputs.items()
    )
    assert all(rc == 0 for rc, _, _ in outputs.values()), f"{report}\n{_diagnostics()}"
    return {n: json.loads(out.strip().splitlines()[-1]) for n, (_, out, _) in outputs.items()}


def _worker(text: str, *, mode: str, gate: str = "", reuse: bool = False, **extra: str) -> dict:
    return {
        "CAPTURE_TEXT": text,
        "CAPTURE_SLUG": "same-title",
        "CAPTURE_MODE": mode,
        "CAPTURE_GATE_NAME": gate,
        "CAPTURE_REUSE": "1" if reuse else "0",
        **extra,
    }


def _rel(name: str) -> str:
    return str((sources.SOURCES / name).relative_to(paths.DATA_ROOT))


# -- concurrent captures ----------------------------------------------------------


def test_concurrent_different_bodies_on_free_base_name_both_survive(tmp_vault, tmp_path):
    # Both processes select `same-title.md` while it is free, then publish
    # together. Exactly one may own the name; the other must move to its
    # content-hash name, not truncate the winner.
    results = _run_workers(
        tmp_path,
        {
            "a": _worker("content-a", mode="rendezvous", gate="same-title.md"),
            "b": _worker("content-b", mode="rendezvous", gate="same-title.md"),
        },
    )
    assert {r["status"] for r in results.values()} == {"ok"}, results
    names = {w: r["path"] for w, r in results.items()}
    assert len(set(names.values())) == 2, f"{names} {_diagnostics()}"
    assert "same-title.md" in names.values()
    for worker, name in names.items():
        assert (sources.SOURCES / name).read_bytes() == f"content-{worker}".encode()
    assert _captured_subjects() == sorted(_rel(n) for n in names.values())
    assert _source_files() == sorted(names.values()), "temp residue or extra artifact"


def test_concurrent_different_bodies_on_occupied_suffixed_name_both_survive(tmp_vault, tmp_path):
    # The base name is already taken, so both fall through to the hash-suffix
    # loop; a forced shared digest makes them contend for the SAME suffixed
    # name. The loser must widen the suffix, not overwrite.
    sources.capture(text="content-0", slug="same-title", actor="pytest")
    short = f"same-title-{FAKE_DIGEST[:8]}.md"
    results = _run_workers(
        tmp_path,
        {
            w: _worker(
                f"content-{w}", mode="rendezvous", gate=short, CAPTURE_FAKE_DIGEST=FAKE_DIGEST
            )
            for w in ("a", "b")
        },
    )
    names = {w: r["path"] for w, r in results.items()}
    assert sorted(names.values()) == sorted([short, f"same-title-{FAKE_DIGEST[:16]}.md"]), (
        f"{results} {_diagnostics()}"
    )
    for worker, name in names.items():
        assert (sources.SOURCES / name).read_bytes() == f"content-{worker}".encode()
    assert (sources.SOURCES / "same-title.md").read_bytes() == b"content-0"
    assert len(_captured_subjects()) == 3
    assert len(_source_files()) == 3, _diagnostics()


def test_concurrent_identical_bodies_with_reuse_share_one_artifact(tmp_vault, tmp_path):
    # Two ingests of the same source (e.g. a retry racing the original) must
    # converge on one retained artifact and one capture event.
    results = _run_workers(
        tmp_path,
        {
            w: _worker("same body", mode="rendezvous", gate="same-title.md", reuse=True)
            for w in ("a", "b")
        },
    )
    assert [r["status"] for r in results.values()] == ["ok", "ok"], results
    assert {r["path"] for r in results.values()} == {"same-title.md"}
    assert (sources.SOURCES / "same-title.md").read_bytes() == b"same body"
    assert _captured_subjects() == [_rel("same-title.md")]
    assert _source_files() == ["same-title.md"]


def test_concurrent_identical_bodies_without_reuse_exactly_one_succeeds(tmp_vault, tmp_path):
    results = _run_workers(
        tmp_path,
        {w: _worker("same body", mode="rendezvous", gate="same-title.md") for w in ("a", "b")},
    )
    statuses = sorted(r["status"] for r in results.values())
    assert statuses == ["exists", "ok"], f"{results} {_diagnostics()}"
    assert (sources.SOURCES / "same-title.md").read_bytes() == b"same body"
    assert _captured_subjects() == [_rel("same-title.md")]
    assert _source_files() == ["same-title.md"]


def test_reuse_never_returns_a_partially_written_source(tmp_vault, tmp_path):
    # Worker a stops halfway through writing its bytes; b captures the same
    # body meanwhile. If a's half-written file were visible under the real
    # name, b would either "reuse" truncated bytes or file a spurious
    # hash-suffixed duplicate. Both must end on one complete artifact.
    body = "".join(f"line {i:05d}\n" for i in range(8000))  # ~88 KiB
    results = _run_workers(
        tmp_path,
        {
            "a": _worker(body, mode="pause-midwrite", reuse=True),
            "b": _worker(body, mode="after-paused", reuse=True),
        },
    )
    assert {r["path"] for r in results.values()} == {"same-title.md"}, f"{results} {_diagnostics()}"
    assert (sources.SOURCES / "same-title.md").read_bytes() == body.encode()
    assert _captured_subjects() == [_rel("same-title.md")]
    assert _source_files() == ["same-title.md"]


# -- failed / interrupted publication ---------------------------------------------


@pytest.mark.parametrize(
    "exc", [OSError(errno.ENOSPC, "No space left on device"), KeyboardInterrupt()]
)
def test_interrupted_write_publishes_nothing_and_keeps_existing_source(tmp_vault, monkeypatch, exc):
    first = sources.capture(text="content-0", slug="same-title", actor="pytest")
    real_write = os.write

    def failing_write(fd, data):
        real_write(fd, bytes(data)[:1])  # a partial write is on disk...
        raise exc  # ...when the disk fills / the operator hits ctrl-c

    # A scoped patch: monkeypatch.undo() would also revert tmp_vault's paths.
    with monkeypatch.context() as m:
        m.setattr(os, "write", failing_write)
        with pytest.raises((OSError, KeyboardInterrupt)) as ei:
            sources.capture(text="content-b", slug="same-title", actor="pytest")
    assert type(ei.value) is type(exc)

    assert first.read_bytes() == b"content-0"
    assert _source_files() == ["same-title.md"], "partial artifact or temp residue left"
    assert _captured_subjects() == [_rel("same-title.md")], "a failed write was ledgered"


def test_failed_publication_never_deletes_another_writers_artifact(tmp_vault, monkeypatch):
    # A contender publishes the name, then our own publication fails with a
    # real error (not EEXIST). Cleanup must remove only our private temp
    # file; the contender's artifact is not ours to delete.
    contender = sources.SOURCES / ".contender"
    contender.write_bytes(b"contender body")
    real_link = os.link

    def contended_link(src, dst, *a, **kw):
        real_link(contender, dst)
        raise OSError(errno.EIO, "I/O error")

    with monkeypatch.context() as m:
        m.setattr(os, "link", contended_link)
        with pytest.raises(OSError, match="I/O error"):
            sources.capture(text="ours", slug="same-title", actor="pytest")
    contender.unlink()

    assert (sources.SOURCES / "same-title.md").read_bytes() == b"contender body"
    assert _source_files() == ["same-title.md"], "temp residue left"
    assert _captured_subjects() == [], "a failed publication was ledgered"


# -- sequential invariants the new publication path must keep -----------------------


def test_new_source_gets_umask_mode_not_private_temp_mode(tmp_vault):
    # Published via a private temp file: the artifact must still get the
    # mode a plain write would (0o644 under umask 022), not mkstemp's 0600.
    prev = os.umask(0o022)
    try:
        p = sources.capture(text="body", slug="mode", actor="pytest")
    finally:
        os.umask(prev)
    assert stat.S_IMODE(p.stat().st_mode) == 0o644
    assert p.stat().st_nlink == 1, "private temp link left behind"


def test_identity_compares_exact_bytes_including_crlf(tmp_vault):
    # Identity is the stored bytes. A text-mode read would fold CRLF to LF
    # and never match, filing a duplicate of an identical CRLF source.
    sources.capture(text="a\r\nb\r\n", slug="crlf", actor="pytest")
    with pytest.raises(FileExistsError, match="already captured"):
        sources.capture(text="a\r\nb\r\n", slug="crlf", actor="pytest")
    assert _source_files() == ["crlf.md"]


def test_full_width_hash_collision_falls_back_to_serial(tmp_vault, monkeypatch):
    # Every hash width occupied by different content: the defensive numeric
    # serial must still publish rather than loop or overwrite.
    from types import SimpleNamespace

    digest = SimpleNamespace(hexdigest=lambda: FAKE_DIGEST)
    monkeypatch.setattr(sources, "hashlib", SimpleNamespace(sha256=lambda _d: digest))
    paths_ = [sources.capture(text=f"c{i}", slug="s", actor="pytest") for i in range(10)]
    names = [p.name for p in paths_]
    assert names[0] == "s.md"
    assert names[-1] == f"s-{FAKE_DIGEST}-2.md"
    assert len(set(names)) == 10
    for i, p in enumerate(paths_):
        assert p.read_bytes() == f"c{i}".encode()
