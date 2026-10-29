"""Child process for tests/test_source_capture_concurrency.py (not a test module).

Runs one `sources.capture` in its own interpreter, against the DATA_ROOT the
parent passes in `SABNYX_DATA_DIR`, and pauses it at a chosen boundary so two
real processes interleave deterministically instead of by luck:

  rendezvous     at the first attempt to publish the name CAPTURE_GATE_NAME
                 (os.link / os.open / Path.write_text / Path.write_bytes onto
                 it), signal "<worker>.ready" and wait until every worker in
                 CAPTURE_PARTIES is ready. Fires once, so a retry on the next
                 candidate name runs freely and cannot deadlock on the gate.
  pause-midwrite on the first os.write into any file under sources/, write
                 half the chunk, signal "paused", wait for "released".
  after-paused   wait for "paused" before capturing; signal "released" once
                 the capture has returned or raised.

The hooks match a publication *boundary*, not an implementation detail of
the fixed code, so the same harness also drives the unsafe variants the
regression must catch (exists-then-write, O_EXCL-reserve-then-write).

Prints one JSON line: {"status": "ok"|"exists", "path": ...}. Any other
failure exits non-zero with the traceback on stderr.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

GATE_TIMEOUT_S = 10.0


def main() -> int:
    data_dir = Path(os.environ["SABNYX_DATA_DIR"]).resolve()
    from sabnyx import paths, sources

    # Fail fast rather than ever touch a vault the parent did not hand us.
    if paths.DATA_ROOT.resolve() != data_dir or sources.SOURCES.resolve() != (
        data_dir / "vault" / "sources"
    ):
        print(f"DATA_ROOT not redirected: {paths.DATA_ROOT} != {data_dir}", file=sys.stderr)
        return 3
    source_dir = sources.SOURCES.resolve()

    worker = os.environ["CAPTURE_WORKER"]
    sync = Path(os.environ["CAPTURE_SYNC_DIR"])
    mode = os.environ["CAPTURE_MODE"]
    gate_name = os.environ.get("CAPTURE_GATE_NAME", "")
    parties = [p for p in os.environ.get("CAPTURE_PARTIES", "").split(",") if p]

    def signal(name: str) -> None:
        (sync / name).touch()

    def wait_for(*names: str) -> None:
        deadline = time.monotonic() + GATE_TIMEOUT_S
        while not all((sync / n).exists() for n in names):
            if time.monotonic() > deadline:
                present = sorted(p.name for p in sync.iterdir())
                raise RuntimeError(f"{worker}: gate timeout waiting for {names}; present={present}")
            time.sleep(0.005)

    def in_sources(p: object) -> bool:
        try:
            return Path(os.fsdecode(p)).resolve().parent == source_dir  # type: ignore[arg-type]
        except TypeError:
            return False

    fired = False

    def publish_gate(target: object) -> None:
        nonlocal fired
        if (
            mode == "rendezvous"
            and not fired
            and in_sources(target)
            and Path(os.fsdecode(target)).name == gate_name  # type: ignore[arg-type]
        ):
            fired = True
            signal(f"{worker}.ready")
            wait_for(*(f"{p}.ready" for p in parties))

    real_link, real_open, real_write = os.link, os.open, os.write
    real_write_text, real_write_bytes = Path.write_text, Path.write_bytes
    source_fds: set[int] = set()

    def link(src, dst, *a, **kw):  # type: ignore[no-untyped-def]
        publish_gate(dst)
        return real_link(src, dst, *a, **kw)

    def open_(path, flags, *a, **kw):  # type: ignore[no-untyped-def]
        if flags & os.O_CREAT:
            publish_gate(path)
        fd = real_open(path, flags, *a, **kw)
        if in_sources(path):
            source_fds.add(fd)
        return fd

    paused = False

    def write(fd, data):  # type: ignore[no-untyped-def]
        nonlocal paused
        if mode == "pause-midwrite" and not paused and fd in source_fds and len(data) > 1:
            paused = True
            n = real_write(fd, bytes(data)[: len(data) // 2])
            signal("paused")
            wait_for("released")
            return n
        return real_write(fd, data)

    def write_text(self, *a, **kw):  # type: ignore[no-untyped-def]
        publish_gate(self)
        return real_write_text(self, *a, **kw)

    def write_bytes(self, *a, **kw):  # type: ignore[no-untyped-def]
        publish_gate(self)
        return real_write_bytes(self, *a, **kw)

    os.link, os.open, os.write = link, open_, write
    Path.write_text, Path.write_bytes = write_text, write_bytes  # type: ignore[method-assign]

    fake = os.environ.get("CAPTURE_FAKE_DIGEST")
    if fake:
        # Force different bodies onto the same hash-suffixed names, so the
        # suffix-selection loop itself is contended (a real short-hash
        # collision is not something a test can produce on demand).
        digest = SimpleNamespace(hexdigest=lambda: fake)
        sources.hashlib = SimpleNamespace(sha256=lambda _data: digest)  # type: ignore[assignment]

    if mode == "after-paused":
        wait_for("paused")
    try:
        try:
            path = sources.capture(
                text=os.environ["CAPTURE_TEXT"],
                slug=os.environ["CAPTURE_SLUG"],
                actor="pytest-concurrency",
                reuse_existing=os.environ.get("CAPTURE_REUSE") == "1",
            )
        except FileExistsError as e:
            print(json.dumps({"worker": worker, "status": "exists", "error": str(e)}))
            return 0
    finally:
        if mode == "after-paused":
            signal("released")
    print(json.dumps({"worker": worker, "status": "ok", "path": path.name}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
