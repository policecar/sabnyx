"""Ledger content_hash contract — the Python half of the Python/Rust parity
guarantee (ADR-0002).

The cross-language hash test lives in opsctl (it shells out to Python). These
tests pin the same invariants from the Python gate so a regression in
``_canonical`` is caught by ``pytest``, not only by ``cargo test``:

  - determinism against hardcoded vectors (identical to the Rust test vectors)
  - non-ASCII stability (ensure_ascii escaping)
  - the no-floats / no-NaN contract that keeps the hash byte-compatible
"""

from __future__ import annotations

import math
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from sabnyx import ledger


# These expected values are byte-identical to the hardcoded vectors asserted in
# opsctl/src/ledger.rs (test_hash_*). If either side changes, both must change.
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"task": "tend", "status": "started"},
            "sha256:a1f2b81593a4865bd65cc621c82d60b87042fd86963fe94de77ccbe21093d2fe",
        ),
        (
            {"a": 1, "b": [1, 2, 3], "nested": {"z": 1, "y": 2}},
            "sha256:7b72446e66532f78b25981b5ac9df976562d763d83d37f4436ce673289b8c4d4",
        ),
        ({}, "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"),
    ],
)
def test_content_hash_matches_rust_vectors(payload: dict, expected: str) -> None:
    assert ledger.content_hash(payload) == expected


def test_content_hash_is_deterministic_and_key_order_independent() -> None:
    a = ledger.content_hash({"x": 1, "y": {"b": 2, "a": 3}})
    b = ledger.content_hash({"y": {"a": 3, "b": 2}, "x": 1})
    assert a == b


def test_content_hash_unicode_is_stable() -> None:
    # ensure_ascii=True path: BMP + supplementary (emoji) char.
    h = ledger.content_hash({"k": "café 日本語 🎉"})
    assert h.startswith("sha256:")
    assert h == ledger.content_hash({"k": "café 日本語 🎉"})


@pytest.mark.parametrize(
    "payload",
    [
        {"cost": 1.5},
        {"nested": {"v": 0.1}},
        {"items": [1, 2, 3.0]},
        {"big": 1e20},
    ],
)
def test_content_hash_rejects_floats(payload: dict) -> None:
    with pytest.raises(TypeError, match="float"):
        ledger.content_hash(payload)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_content_hash_rejects_non_finite(bad: float) -> None:
    with pytest.raises(TypeError, match="float"):
        ledger.content_hash({"v": bad})


def test_bools_are_allowed() -> None:
    # bool is a subclass of int but is NOT a float — must pass.
    h = ledger.content_hash({"ok": True, "no": False})
    assert h.startswith("sha256:")


def test_append_event_rejects_float_payload(tmp_vault) -> None:
    with pytest.raises(TypeError, match="float"):
        ledger.append_event(kind="test.event", actor="t", subject="s", payload={"ratio": 0.5})


# -- replay error reporting (parity with opsctl read_events) ------------------------


def test_replay_error_names_line_number_and_snippet(tmp_vault) -> None:
    ledger.append_event(kind="ok.one", actor="t", subject="s", payload={})
    with ledger.EVENTS_JSONL.open("a", encoding="utf-8") as f:
        f.write('{"this is": "not a ledger event"}\n')
    ledger.append_event(kind="ok.three", actor="t", subject="s", payload={})

    with pytest.raises(ledger.LedgerError, match=r"ledger line 2:") as ei:
        list(ledger.replay())
    assert "line starts:" in str(ei.value)
    assert "not a ledger event" in str(ei.value)


def test_replay_error_is_not_skipped_silently(tmp_vault) -> None:
    ledger.EVENTS_JSONL.parent.mkdir(parents=True, exist_ok=True)
    ledger.EVENTS_JSONL.write_text("garbage\n", encoding="utf-8")
    with pytest.raises(ledger.LedgerError, match="ledger line 1"):
        ledger.count()


# -- append atomicity: one line, even for large events -------------------------------


def test_append_large_event_is_one_line_and_replays(tmp_vault) -> None:
    big = "x" * 10_000  # well over PIPE_BUF-ish sizes a buffered writer may split
    ledger.append_event(kind="big.event", actor="t", subject="s", payload={"blob": big})
    data = ledger.EVENTS_JSONL.read_bytes()
    assert data.endswith(b"\n")
    assert data.count(b"\n") == 1  # exactly one line
    events = list(ledger.replay())
    assert len(events) == 1
    assert events[0].payload["blob"] == big


# -- append durability: fsync is the commit point ----------------------------------


def test_append_event_fsyncs_for_durability(tmp_vault, monkeypatch) -> None:
    # The append is the ledger's commit point, so it must be fsync'd to disk
    # before returning — not left in the OS page cache where a power loss
    # could drop it. Spying on os.fsync alone would be vacuous: it proves
    # *some* fsync happened, not that it hit the ledger file. Spy on
    # ledger._fsync (the helper that owns the real fsync/F_FULLFSYNC call)
    # and assert the fd it's handed resolves — by inode — to the ledger
    # file itself.
    #
    # The warm-up append puts the file in the "already exists" state so
    # this test isolates the per-append file fsync from the first-append
    # directory fsync, which has its own test below.
    ledger.append_event(kind="warm.up", actor="t", subject="s", payload={})
    ledger_ino = ledger.EVENTS_JSONL.stat().st_ino

    synced_inos: list[int] = []
    real_fsync = ledger._fsync

    def _spy(fd: int) -> None:
        synced_inos.append(os.fstat(fd).st_ino)
        real_fsync(fd)

    monkeypatch.setattr(ledger, "_fsync", _spy)
    event = ledger.append_event(kind="test.event", actor="t", subject="s", payload={"n": 1})

    assert synced_inos, "append_event must fsync the ledger file before returning"
    assert ledger_ino in synced_inos, (
        "fsync must hit the ledger file's own fd by inode, not an unrelated fd"
    )
    assert list(ledger.replay())[-1].content_hash == event.content_hash


def test_append_event_fsyncs_parent_dir_on_first_creation(tmp_vault, monkeypatch) -> None:
    # The very first append creates ops/events.jsonl, which also adds a new
    # directory entry to its parent. The file's own fsync only covers its
    # *contents* — without a directory fsync too, a crash right after
    # creation could leave that directory entry unrecoverable even though
    # the file's bytes are safely on disk. Prove it by inode: the fsync spy
    # must see a fd that stats back to the parent directory itself.
    assert not ledger.EVENTS_JSONL.exists(), "fixture must start with no ledger file"
    parent_ino = ledger.EVENTS_JSONL.parent.stat().st_ino

    dir_fsync_inos: list[int] = []
    real_fsync = ledger._fsync

    def _spy(fd: int) -> None:
        st = os.fstat(fd)
        if stat.S_ISDIR(st.st_mode):
            dir_fsync_inos.append(st.st_ino)
        real_fsync(fd)

    monkeypatch.setattr(ledger, "_fsync", _spy)
    ledger.append_event(kind="first.event", actor="t", subject="s", payload={})

    assert dir_fsync_inos, "first append must fsync a directory fd"
    assert parent_ino in dir_fsync_inos, (
        "the directory fsync must hit the ledger's own parent directory"
    )


def test_append_event_raises_on_short_write(tmp_vault, monkeypatch) -> None:
    # An unbuffered write(2) can legally report fewer bytes written than
    # requested without raising. If append_event trusted that count blindly,
    # a short write would get fsync'd and certified durable as a truncated,
    # corrupt line. Simulate the kernel under-reporting by wrapping the
    # ledger file's write() to lie about the byte count (data still lands on
    # disk in full — only the reported count is short, same as a real short
    # write would look from append_event's point of view).
    real_open = Path.open

    class _ShortWriteFile:
        def __init__(self, real_file: Any) -> None:
            self._real_file = real_file

        def write(self, data: bytes) -> int:
            self._real_file.write(data)
            return len(data) - 1

        def fileno(self) -> int:
            return self._real_file.fileno()

        def __enter__(self) -> _ShortWriteFile:
            return self

        def __exit__(self, *exc_info: object) -> bool:
            self._real_file.__exit__(*exc_info)
            return False

    def _fake_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        real_file = real_open(self, *args, **kwargs)
        if self == ledger.EVENTS_JSONL:
            return _ShortWriteFile(real_file)
        return real_file

    monkeypatch.setattr(Path, "open", _fake_open)

    with pytest.raises(ledger.LedgerError, match="short write"):
        ledger.append_event(kind="test.event", actor="t", subject="s", payload={"n": 1})
