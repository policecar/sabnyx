"""Promotion recovery: a committed promotion never loses its reweave work (R05).

`promote_file` writes the atlas page, then appends `atlas.promoted`, then
`reweave.proposed`, then unlinks the intake source. Those are separate
operations, so a failure can stop the sequence at any boundary. The
invariant these tests pin down:

  Once `atlas.promoted` is on disk for a path, the reweave queue holds that
  path until a later `reweave.completed` (or `atlas.removed`) for it, whether
  or not the separate `reweave.proposed` intent was ever written.

The queue is derived by replaying the ledger, so recovery needs no repair
write: the next tend (or `sabnyx reweave`) just finds the page. Failures are
injected at the real ledger file's `Path.open` and at `ledger._fsync`, never
by replacing `append_event`, so the ADR 0008 failure classes are the real ones.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from sabnyx import fm, ledger, services
from sabnyx.commands import reweave as rw
from sabnyx.schemas import ReweaveChangeset, TriageBatch

_REAL_OPEN = Path.open
_REAL_UNLINK = Path.unlink

DESCRIPTION = "a synthetic bookmark about recovery after interrupted promotion"


def _bookmark(tmp_vault, slug: str = "interrupted-promotion-bookmark") -> Path:
    p = tmp_vault["INTAKE"] / f"{slug}.md"
    p.write_text(
        "---\n"
        "type: bookmark\n"
        f"description: {DESCRIPTION}\n"
        "source: manual\n"
        "source_url: https://example.net/resource\n"
        "status: draft\n"
        "source_agent: sabnyx-ingest\n"
        "created: 2026-10-29T12:00:00+00:00\n"
        "---\n"
        f"# {slug}\n\nbody\n",
        encoding="utf-8",
    )
    return p


def _dest(tmp_vault, source: Path) -> Path:
    return tmp_vault["ATLAS_REFERENCES"] / source.name


def _rel(tmp_vault, p: Path) -> str:
    return str(p.relative_to(tmp_vault["DATA_ROOT"]))


def _kinds() -> list[str]:
    return [e.kind for e in ledger.replay()]


def _ledger_bytes() -> bytes:
    return ledger.EVENTS_JSONL.read_bytes() if ledger.EVENTS_JSONL.exists() else b""


def _track_kind(mp: pytest.MonkeyPatch) -> list[str]:
    """Record the kind of the event append_event is about to write.

    append_event builds its LedgerEvent immediately before opening the file,
    so at open/fsync time this names the event being appended. Only the model
    is wrapped; append_event and its I/O run for real.
    """
    current: list[str] = []

    class Recording(ledger.LedgerEvent):
        def __init__(self, **data: Any) -> None:
            super().__init__(**data)
            current[:] = [data["kind"]]

    mp.setattr(ledger, "LedgerEvent", Recording)
    return current


def _fail_append_open(mp: pytest.MonkeyPatch, kind: str, exc: BaseException) -> None:
    """Raise `exc` from the ledger's append-mode open for the first `kind`
    event: a pre-write failure, so no byte of that event reaches the file.
    Keyed by kind, so unrelated events appended earlier don't shift it."""
    current = _track_kind(mp)
    fired = False

    def guarded(self: Path, *a: Any, **k: Any) -> Any:
        nonlocal fired
        is_append = a[:1] == ("ab",) or k.get("mode") == "ab"
        if self == ledger.EVENTS_JSONL and is_append and current == [kind] and not fired:
            fired = True
            raise exc
        return _REAL_OPEN(self, *a, **k)

    mp.setattr(Path, "open", guarded)


def _fail_file_fsync(mp: pytest.MonkeyPatch, kind: str) -> None:
    """Raise OSError from the ledger file's fsync for the first `kind` event:
    the full line is already on disk, so append_event raises LedgerWriteError."""
    current = _track_kind(mp)
    real = ledger._fsync
    fired = False

    def guarded(fd: int) -> None:
        nonlocal fired
        if not stat.S_ISDIR(os.fstat(fd).st_mode) and current == [kind] and not fired:
            fired = True
            raise OSError(5, "injected: fsync EIO after the line was written")
        real(fd)

    mp.setattr(ledger, "_fsync", guarded)


def _empty_changeset(**kwargs: Any) -> Any:
    """Stub for llm.parse_structured: reweave gets an empty changeset (so it
    completes without prompting), triage gets an empty batch."""
    if kwargs.get("output_format") is TriageBatch:
        return TriageBatch(items=[])
    assert kwargs.get("output_format") is ReweaveChangeset
    return ReweaveChangeset(summary="nothing qualifies")


def _missing_intent_state(tmp_vault) -> tuple[Path, Path]:
    """Promote with the intent append's open failing: atlas page and
    atlas.promoted committed, reweave.proposed never written."""
    source = _bookmark(tmp_vault)
    with pytest.MonkeyPatch.context() as mp:
        _fail_append_open(
            mp, "reweave.proposed", PermissionError(13, "injected: open before intent append")
        )
        with pytest.raises(PermissionError, match="before intent append"):
            services.promote_file(source, "reference")
    return source, _dest(tmp_vault, source)


# -- the normal path ------------------------------------------------------------------


def test_normal_promotion_queues_once_and_completion_consumes_it(tmp_vault):
    source = _bookmark(tmp_vault)
    dest = services.promote_file(source, "reference")

    assert not source.exists()
    assert _kinds() == ["atlas.promoted", "reweave.proposed"]
    # Two events say "pending" for one path; the queue lists it once.
    assert rw._pending_targets() == [dest]

    ledger.append_event(
        kind="reweave.completed", actor="t", subject=_rel(tmp_vault, dest), payload={}
    )
    assert rw._pending_targets() == []
    assert rw._pending_targets() == []  # a replay is not a requeue


# -- the reported defect: intent append fails before writing --------------------------


def test_missing_intent_is_recovered_by_replay(tmp_vault):
    """The R05 reproduction. Before the fix the page was promoted, reconcile
    was clean, retry was refused, and the queue was empty: lost work."""
    source, dest = _missing_intent_state(tmp_vault)

    assert dest.is_file()  # no source lost
    assert source.is_file()
    assert _kinds() == ["atlas.promoted"]  # intent never written
    # Reconcile is a file/event check: the promoted file exists and its event
    # does, so it is clean. It does not claim the workflow finished.
    assert services.reconcile_atlas().clean
    # The queue still finds the work, without any repair write.
    before = _ledger_bytes()
    assert rw._pending_targets() == [dest]
    assert rw._pending_targets() == [dest]  # restart: same answer, no duplicate
    assert _ledger_bytes() == before


def test_retry_after_missing_intent_changes_nothing(tmp_vault):
    """Retrying the promotion is refused and touches no file and no event, so
    a repeated recovery can't duplicate the promotion or eat operator edits."""
    source, dest = _missing_intent_state(tmp_vault)
    dest.write_text(dest.read_text(encoding="utf-8") + "\noperator note\n", encoding="utf-8")
    snapshot = (dest.read_bytes(), source.read_bytes(), _ledger_bytes())

    for _ in range(2):
        with pytest.raises(RuntimeError, match="already exists"):
            services.promote_file(source, "reference")
        assert (dest.read_bytes(), source.read_bytes(), _ledger_bytes()) == snapshot

    assert rw._pending_targets() == [dest]


def test_reweave_cmd_completes_recovered_work_once(tmp_vault, monkeypatch):
    """`sabnyx reweave` picks up the recovered page, completes it, keeps the
    operator's prose, and a second run finds nothing to do."""
    _source, dest = _missing_intent_state(tmp_vault)
    dest.write_text(dest.read_text(encoding="utf-8") + "\noperator note\n", encoding="utf-8")
    monkeypatch.setattr(rw.llm, "parse_structured", _empty_changeset)

    first = CliRunner().invoke(rw.reweave_cmd, ["--yes"])
    assert first.exit_code == 0, first.output
    assert "pending reweave targets: 1" in first.output
    assert _kinds() == ["atlas.promoted", "reweave.completed"]
    assert "operator note" in dest.read_text(encoding="utf-8")

    before = _ledger_bytes()
    second = CliRunner().invoke(rw.reweave_cmd, ["--yes"])
    assert second.exit_code == 0, second.output
    assert "nothing to do" in second.output
    assert _ledger_bytes() == before  # completed work is not requeued


def test_tend_recovers_missing_intent_on_a_later_run(tmp_vault, monkeypatch):
    """The ordinary heartbeat: run 1 dies between the two appends; run 2
    reweaves the page. The stranded intake source is deferred, never deleted."""
    from sabnyx.commands import tend as tend_mod

    source = _bookmark(tmp_vault)
    dest = _dest(tmp_vault, source)
    with pytest.MonkeyPatch.context() as mp:
        _fail_append_open(
            mp, "reweave.proposed", PermissionError(13, "injected: open before intent append")
        )
        run1 = CliRunner().invoke(
            tend_mod.tend_cmd, ["--no-reweave", "--no-health"], catch_exceptions=False
        )
    assert run1.exit_code == 0, run1.output
    assert "promote failed" in run1.output
    assert dest.is_file()
    assert source.is_file()
    assert "reweave.proposed" not in _kinds()

    monkeypatch.setattr(rw.llm, "parse_structured", _empty_changeset)
    run2 = CliRunner().invoke(tend_mod.tend_cmd, ["--yes", "--no-health"], catch_exceptions=False)
    assert run2.exit_code == 0, run2.output
    assert "1 pending target" in run2.output
    completed = [e for e in ledger.replay() if e.kind == "reweave.completed"]
    assert [e.subject for e in completed] == [_rel(tmp_vault, dest)]
    assert source.is_file()  # residual: reported by the gate-5 deferral
    assert "already exists in atlas" in run2.output
    assert _kinds().count("atlas.promoted") == 1


# -- adjacent failure boundaries -----------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [PermissionError(13, "injected: open before promotion append"), KeyboardInterrupt()],
    ids=["oserror", "ctrl-c"],
)
def test_failure_before_promotion_append_rolls_back_and_retry_succeeds(tmp_vault, exc):
    """Nothing reached the ledger (ADR 0008 pre-write class), so the page this
    call wrote is removed and the promotion can simply be retried."""
    source = _bookmark(tmp_vault)
    original = source.read_bytes()
    with pytest.MonkeyPatch.context() as mp:
        _fail_append_open(mp, "atlas.promoted", exc)
        with pytest.raises(type(exc)):
            services.promote_file(source, "reference")

    assert not _dest(tmp_vault, source).exists()
    assert source.read_bytes() == original
    assert _ledger_bytes() == b""
    assert services.reconcile_atlas().clean
    assert rw._pending_targets() == []

    dest = services.promote_file(source, "reference")
    assert rw._pending_targets() == [dest]
    assert _kinds() == ["atlas.promoted", "reweave.proposed"]


def _fail_unlink(mp: pytest.MonkeyPatch, target: Path) -> None:
    def guarded(self: Path, *a: Any, **k: Any) -> None:
        if self == target:
            raise PermissionError(13, "injected: unlink new page")
        _REAL_UNLINK(self, *a, **k)

    mp.setattr(Path, "unlink", guarded)


def test_failed_rollback_reports_both_errors_and_keeps_source(tmp_vault):
    """If removing the new page also fails, the error names both failures and
    the leftover state, which reconcile reports as an unledgered page."""
    source = _bookmark(tmp_vault)
    original = source.read_bytes()
    dest = _dest(tmp_vault, source)

    with pytest.MonkeyPatch.context() as mp:
        _fail_append_open(
            mp, "atlas.promoted", PermissionError(13, "injected: open before promotion append")
        )
        _fail_unlink(mp, dest)
        with pytest.raises(RuntimeError, match="unledgered") as excinfo:
            services.promote_file(source, "reference")

    assert isinstance(excinfo.value.__cause__, PermissionError)  # the undo error
    assert "before promotion append" in str(excinfo.value.__cause__.__context__)
    assert dest.is_file()
    assert source.read_bytes() == original
    assert _ledger_bytes() == b""
    assert services.reconcile_atlas().unledgered == [_rel(tmp_vault, dest)]


def test_promotion_append_possibly_written_keeps_page_and_queues_it(tmp_vault):
    """LedgerWriteError on the first append: the line may be on disk (here it
    is), so the page must stay. Replay then sees the promotion and queues it."""
    source = _bookmark(tmp_vault)
    with pytest.MonkeyPatch.context() as mp:
        _fail_file_fsync(mp, "atlas.promoted")
        with pytest.raises(ledger.LedgerWriteError):
            services.promote_file(source, "reference")

    dest = _dest(tmp_vault, source)
    assert dest.is_file()
    assert source.is_file()
    assert _kinds() == ["atlas.promoted"]
    assert rw._pending_targets() == [dest]


def test_intent_append_possibly_written_keeps_everything(tmp_vault):
    """LedgerWriteError on the second append: both lines are on disk; the page
    and the source both stay, and the page is queued exactly once."""
    source = _bookmark(tmp_vault)
    with pytest.MonkeyPatch.context() as mp:
        _fail_file_fsync(mp, "reweave.proposed")
        with pytest.raises(ledger.LedgerWriteError):
            services.promote_file(source, "reference")

    dest = _dest(tmp_vault, source)
    assert dest.is_file()
    assert source.is_file()
    assert _kinds() == ["atlas.promoted", "reweave.proposed"]
    assert rw._pending_targets() == [dest]


def test_source_unlink_failure_keeps_source_and_queue(tmp_vault, monkeypatch):
    """Both events committed, then the unlink fails: the source stays (a
    reported residual), the page is queued, the knowledge log is not written."""
    source = _bookmark(tmp_vault)

    def guarded(self: Path, *a: Any, **k: Any) -> None:
        if self == source:
            raise PermissionError(13, "injected: unlink source")
        _REAL_UNLINK(self, *a, **k)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Path, "unlink", guarded)
        with pytest.raises(PermissionError, match="unlink source"):
            services.promote_file(source, "reference")

    dest = _dest(tmp_vault, source)
    assert dest.is_file()
    assert source.is_file()
    assert _kinds() == ["atlas.promoted", "reweave.proposed"]
    assert rw._pending_targets() == [dest]
    assert not tmp_vault["KNOWLEDGE_LOG"].exists()


# -- removal and re-promotion ---------------------------------------------------------


def test_removed_page_leaves_queue_and_repromotion_requeues(tmp_vault):
    dest = services.promote_file(_bookmark(tmp_vault), "reference")
    rel = _rel(tmp_vault, dest)

    dest.unlink()  # removed before its reweave ran: reweave.proposed is stale
    ledger.append_event(kind="atlas.removed", actor="operator", subject=rel, payload={})
    assert rw._pending_targets() == []

    # A hand-written page at the removed path is not a promotion: the stale
    # pre-removal intent must not resurface against it.
    fm.write(dest, {"type": "reference", "description": DESCRIPTION, "status": "final"}, "x\n")
    assert rw._pending_targets() == []
    dest.unlink()

    # A real re-promotion of the same path is new work.
    redo = services.promote_file(_bookmark(tmp_vault), "reference")
    assert redo == dest
    assert rw._pending_targets() == [dest]


def test_completion_then_repromotion_without_intent_requeues(tmp_vault):
    """A historical completion must not mask a later promotion, even when that
    later promotion's own intent append failed."""
    source = _bookmark(tmp_vault)
    dest = services.promote_file(source, "reference")
    rel = _rel(tmp_vault, dest)
    ledger.append_event(kind="reweave.completed", actor="t", subject=rel, payload={})
    dest.unlink()
    ledger.append_event(kind="atlas.removed", actor="operator", subject=rel, payload={})

    _source, again = _missing_intent_state(tmp_vault)
    assert again == dest
    assert rw._pending_targets() == [dest]
