"""transition_observation (IDD §6 writers.py, §10, §10a, D11, D15, P2).

Real files under tmp_vault, written as raw bytes so CRLF and exact layout
are under the test's control. ledger.today is pinned. Ledger failures are
injected at the OS boundary only (the real ledger file object, ledger._fsync,
Path.mkdir/open, LedgerEvent construction), never by replacing append_event,
inside a scoped MonkeyPatch so tmp_vault's redirects stay in place.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Any

import pytest

# The OS-boundary injection helpers from the ledger's own failure-class table
# (tests/test_ledger.py): one definition of "short write", "close EIO", ...
from test_ledger import _inject

from sabnyx import fm, ledger, paths
from sabnyx import observations as obs
from sabnyx.schemas import ObservationFrontmatter
from sabnyx.writers import ObservationRefused, Transition, transition_observation

TODAY = date(2026, 6, 20)
ACTOR = "sabnyx-cli"

PENDING = [
    "type: observation",
    "category: friction",
    "observed: 2026-06-12",
    "source_agent: manual",
    "status: pending",
]
BODY = ["", "# alpha friction", "", "The alpha-concept page is hard to find."]
RESOLVED = [
    "status: promoted",
    "resolved: 2026-06-20",
    "resolution: fixed it",
    "resolution_kind: done",
]


@pytest.fixture
def vault(tmp_vault, monkeypatch):
    monkeypatch.setattr(ledger, "today", lambda: TODAY)
    for name in ("alpha-concept", "beta-concept"):
        fm.write(
            tmp_vault["ATLAS_CONCEPTS"] / f"{name}.md",
            {"type": "concept", "description": "a synthetic page for the transition tests"},
            f"# {name}\n",
        )
    # Steady state: the ledger exists with one line.
    ledger.append_event(kind="seed", actor="t", subject="s", payload={"n": 0})
    return tmp_vault


def _doc(fm_lines: list[str], body: list[str] = BODY, nl: str = "\n") -> bytes:
    return (nl.join(["---", *fm_lines, "---", *body]) + nl).encode("utf-8")


def _after(fm_lines: list[str], resolved: list[str] = RESOLVED) -> list[str]:
    """What the patcher must make of `fm_lines`: the status line replaced in
    place (or inserted after category), the other three appended."""
    status, *appended = resolved
    out = list(fm_lines)
    if "status: pending" in out:
        out[out.index("status: pending")] = status
    else:
        out.insert(out.index(next(x for x in out if x.startswith("category:"))) + 1, status)
    return out + appended


def _put(name: str, data: bytes, folder: Path | None = None) -> Path:
    p = (folder or paths.OBSERVATIONS) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _lines() -> int:
    return ledger.EVENTS_JSONL.read_bytes().count(b"\n")


def _resolve(path: Path, reason: str = "fixed it", kind: Any = "done", **kw: Any) -> Transition:
    return transition_observation(path, kind=kind, reason=reason, actor=ACTOR, **kw)


def _assert_refused(
    path: Path, expected: str, *, files: list[Path], reason: str = "fixed it"
) -> ObservationRefused:
    """Refused with `expected`; every file byte-identical; 0 ledger lines."""
    before = {f: f.read_bytes() for f in files}
    lines = _lines()
    with pytest.raises(ObservationRefused) as ei:
        _resolve(path, reason=reason)
    assert ei.value.reason == expected, str(ei.value)
    assert {f: f.read_bytes() for f in files} == before
    assert _lines() == lines
    return ei.value


def _events(kind: str = "observation.resolved") -> list:
    return [e for e in ledger.replay() if e.kind == kind]


# -- (1) path check (§10a) ------------------------------------------------------------


def test_symlink_inside_folder_to_a_file_outside_is_refused_outside(vault):
    target = _put("loose.md", b"just some text\n", vault["INTAKE"])
    link = paths.OBSERVATIONS / "link.md"
    link.symlink_to(target)
    _assert_refused(link, "outside", files=[target])


def test_symlink_inside_folder_to_a_valid_pending_observation_elsewhere_is_refused_outside(
    vault,
):
    target = _put("2026-06-12-friction-alpha.md", _doc(PENDING), vault["DATA_ROOT"] / "elsewhere")
    link = paths.OBSERVATIONS / "2026-06-12-friction-alpha.md"
    link.symlink_to(target)
    _assert_refused(link, "outside", files=[target])
    assert link.is_symlink()


def test_dotdot_path_escaping_the_folder_is_refused_outside(vault):
    x = _put("x.md", _doc(PENDING), vault["INTAKE"])  # exists, and is a valid observation
    _assert_refused(paths.OBSERVATIONS / ".." / "x.md", "outside", files=[x])


def test_missing_file_is_refused_not_found(vault):
    _assert_refused(paths.OBSERVATIONS / "nope.md", "not_found", files=[])


def test_directory_named_md_is_refused_not_found(vault):
    (paths.OBSERVATIONS / "dir.md").mkdir()
    _assert_refused(paths.OBSERVATIONS / "dir.md", "not_found", files=[])


def test_non_md_file_inside_the_folder_is_refused_outside(vault):
    p = _put("2026-06-12-friction-alpha.txt", _doc(PENDING))
    _assert_refused(p, "outside", files=[p])


def test_link_outside_the_folder_pointing_inside_patches_the_target_and_stays_a_link(vault):
    real = _put("2026-06-12-friction-alpha.md", _doc(PENDING))
    link = vault["INTAKE"] / "shortcut.md"
    link.symlink_to(real)

    t = _resolve(link)

    assert link.is_symlink()
    assert link.resolve() == real.resolve()
    assert t.path == real.resolve()
    assert t.rel == "vault/intake/.observations/2026-06-12-friction-alpha.md"
    assert real.read_bytes() == _doc(_after(PENDING))
    assert [e.subject for e in _events()] == [t.rel]


# -- (2) refusal reasons and check order (§10) ------------------------------------------


def test_symlink_loop_is_refused_not_found_not_a_traceback(vault):
    # a.md -> b.md -> a.md inside the folder. 3.13's non-strict resolve()
    # returns the path unresolved (is_file False); 3.11/3.12 raise
    # RuntimeError. Pinned: not_found, links untouched, 0 ledger lines.
    folder = paths.OBSERVATIONS
    folder.mkdir(parents=True, exist_ok=True)
    a, b = folder / "2026-06-12-gap-a.md", folder / "2026-06-12-gap-b.md"
    a.symlink_to(b.name)
    b.symlink_to(a.name)
    lines = _lines()
    with pytest.raises(ObservationRefused) as ei:
        _resolve(a)
    assert ei.value.reason == "not_found"
    assert a.is_symlink()
    assert b.is_symlink()
    assert os.readlink(a) == b.name
    assert _lines() == lines


def test_resolve_raising_on_a_loop_is_refused_not_found(vault, monkeypatch):
    # The 3.11/3.12 behaviour, simulated: resolve() raises instead of returning.
    p = _put("a.md", _doc(PENDING))
    real_resolve = Path.resolve

    def looping(self: Path, *a: Any, **k: Any) -> Path:
        if self.name == p.name:
            raise RuntimeError(f"Symlink loop from {self!r}")
        return real_resolve(self, *a, **k)

    monkeypatch.setattr(Path, "resolve", looping)
    with pytest.raises(ObservationRefused) as ei:
        _resolve(p)
    assert ei.value.reason == "not_found"
    assert "Symlink loop" in ei.value.detail


@pytest.mark.parametrize("reason", ["a\udc80b", "\udcff", "fixed \ud800 it"])
def test_lone_surrogate_reason_is_refused_unencodable_reason(vault, reason):
    p = _put("a.md", _doc(PENDING))
    _assert_refused(p, "unencodable_reason", files=[p], reason=reason)


def test_order_unencodable_reason_beats_not_pending(vault):
    p = _put("a.md", _doc(_after(PENDING)))
    _assert_refused(p, "unencodable_reason", files=[p], reason="a\udc80b")


@pytest.mark.parametrize("reason", ["", "   \t "])
def test_empty_reason_is_refused_empty_reason(vault, reason):
    p = _put("a.md", _doc(PENDING))
    _assert_refused(p, "empty_reason", files=[p], reason=reason)


# All eight characters str.splitlines breaks on; "\n" in r catches only LF/CRLF.
_BREAKS = ["\n", "\r", "\r\n", "\x0b", "\x0c", "\x85", " ", " "]


@pytest.mark.parametrize("brk", _BREAKS, ids=[repr(b) for b in _BREAKS])
def test_multiline_reason_is_refused_for_every_line_break_character(vault, brk):
    p = _put("a.md", _doc(PENDING))
    _assert_refused(p, "multiline_reason", files=[p], reason=f"first{brk}second")


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(_doc([*PENDING, "involves: [unclosed"]), id="bad-yaml"),
        pytest.param(_doc([*PENDING[:1], "category: bogus", *PENDING[2:]]), id="schema-invalid"),
        pytest.param(_doc(PENDING) + b"\xff\xfe not utf-8\n", id="non-utf8"),
        pytest.param(b"no frontmatter at all\n", id="no-frontmatter"),
        # Parses (YAML keeps the last duplicate) and validates, but the patcher
        # cannot pick the status line: PatchError is refused malformed.
        pytest.param(_doc([*PENDING, "status: pending"]), id="patch-error-two-status-lines"),
    ],
)
def test_malformed_file_is_refused_malformed(vault, data):
    p = _put("a.md", data)
    _assert_refused(p, "malformed", files=[p])


def test_patch_error_detail_is_carried_in_the_refusal(vault):
    p = _put("a.md", _doc([*PENDING, "status: pending"]))
    refused = _assert_refused(p, "malformed", files=[p])
    assert "cannot patch safely" in refused.detail
    assert "more than one column-0 'status' line" in refused.detail


@pytest.mark.parametrize(
    "resolved",
    [
        RESOLVED,
        [
            "status: archived",
            "resolved: 2026-06-15",
            "resolution: not needed",
            "resolution_kind: wontfix",
        ],
    ],
    ids=["promoted", "archived"],
)
def test_non_pending_file_is_refused_not_pending(vault, resolved):
    p = _put("a.md", _doc(_after(PENDING, resolved)))
    _assert_refused(p, "not_pending", files=[p])


def test_order_outside_beats_empty_reason(vault):
    p = _put("a.txt", _doc(PENDING))
    _assert_refused(p, "outside", files=[p], reason="")


def test_order_empty_reason_beats_malformed(vault):
    p = _put("a.md", b"\xff not utf-8\n")
    _assert_refused(p, "empty_reason", files=[p], reason=" ")


def test_order_malformed_beats_not_pending(vault):
    lines = _after(PENDING)
    lines[lines.index("category: friction")] = "category: bogus"
    p = _put("a.md", _doc(lines))
    _assert_refused(p, "malformed", files=[p])


def test_order_multiline_reason_beats_not_pending(vault):
    p = _put("a.md", _doc(_after(PENDING)))
    _assert_refused(p, "multiline_reason", files=[p], reason="a\nb")


# -- (3) raw bytes: the patcher's copy guarantee survives the transition (D11) ------------


def _body_bytes(data: bytes, nl: bytes) -> bytes:
    """Everything after the closing fence line."""
    return data.split(nl + b"---" + nl, 1)[1]


def test_crlf_file_keeps_every_byte_except_the_patched_lines(vault):
    p = _put("a.md", _doc(PENDING, nl="\r\n"))
    before = p.read_bytes()

    _resolve(p)

    after = p.read_bytes()
    assert after == _doc(_after(PENDING), nl="\r\n")
    assert _body_bytes(after, b"\r\n") == _body_bytes(before, b"\r\n")
    assert after.count(b"\n") == after.count(b"\r\n") > 0  # no bare LF anywhere
    assert len(_events()) == 1


@pytest.mark.parametrize(
    ("fm_lines", "body", "reason", "resolution_line"),
    [
        pytest.param(PENDING, BODY, "fixed it", "resolution: fixed it", id="lf"),
        pytest.param(
            [x for x in PENDING if not x.startswith("status:")],
            BODY,
            "fixed it",
            "resolution: fixed it",
            id="no-status-line",
        ),
        pytest.param(
            PENDING,
            ["", "# tricky body", "status: pending", "---", "category: friction", "---", "end"],
            "fixed it",
            "resolution: fixed it",
            id="body-with-status-and-fences",
        ),
        pytest.param(
            PENDING,
            BODY,
            "note status: done here",
            "resolution: 'note status: done here'",
            id="reason-containing-status",
        ),
    ],
)
def test_transition_changes_only_the_owned_frontmatter_lines(
    vault, fm_lines, body, reason, resolution_line
):
    p = _put("a.md", _doc(fm_lines, body))
    before = p.read_bytes()

    _resolve(p, reason=reason)

    resolved = [
        "status: promoted",
        "resolved: 2026-06-20",
        resolution_line,
        "resolution_kind: done",
    ]
    after = p.read_bytes()
    assert after == _doc(_after(fm_lines, resolved), body)
    assert _body_bytes(after, b"\n") == _body_bytes(before, b"\n")
    meta, _body, _kind = fm.load(p)
    assert meta["status"] == "promoted"
    assert meta["resolution"] == reason
    assert len(_events()) == 1


# -- seed rows: the event, dry run -------------------------------------------------------


def test_prose_event_carries_no_obs_id_and_no_target(vault):
    p = _put("a.md", _doc(PENDING))
    t = _resolve(p, kind="obsolete", reason="  page was removed  ")

    (e,) = _events()
    assert (e.plane, e.actor, e.subject) == ("event", ACTOR, t.rel)
    assert e.payload == {
        "category": "friction",
        "old_status": "pending",
        "new_status": "archived",
        "kind": "obsolete",
        "resolution": "page was removed",
    }
    assert t == Transition(
        path=p.resolve(),
        rel="vault/intake/.observations/a.md",
        obs_id=None,
        old_status="pending",
        new_status="archived",
        kind="obsolete",
        resolution="page was removed",
        resolved=TODAY,
    )


def _expected_id(lines: list[str]) -> str:
    meta = fm.load_text(_doc(lines).decode(), "observation", path=Path("x.md"))[0]
    oid = obs.obs_id(ObservationFrontmatter.model_validate(meta), obs.resolver())
    assert oid is not None
    return oid


def test_identity_gap_event_carries_obs_id_and_target(vault):
    lines = [
        "type: observation",
        "category: gap",
        "observed: 2026-06-12",
        "source_agent: sabnyx-entities",
        "status: pending",
        "target: beta-widget",
    ]
    oid = _expected_id(lines)
    p = _put("gap.md", _doc(lines))

    t = _resolve(p, kind="wontfix", reason="not worth a page")

    (e,) = _events()
    assert e.payload == {
        "category": "gap",
        "old_status": "pending",
        "new_status": "archived",
        "kind": "wontfix",
        "resolution": "not worth a page",
        "obs_id": oid,
        "target": "beta-widget",
    }
    assert t.obs_id == oid


def test_identity_contradiction_event_carries_obs_id_without_target(vault):
    lines = [
        "type: observation",
        "category: contradiction",
        "observed: 2026-06-12",
        "source_agent: sabnyx-scan",
        "status: pending",
        "involves:",
        "- alpha-concept.md",
        "- beta-concept.md",
    ]
    oid = _expected_id(lines)
    p = _put("c.md", _doc(lines))

    _resolve(p)

    (e,) = _events()
    assert set(e.payload) == {
        "category",
        "old_status",
        "new_status",
        "kind",
        "resolution",
        "obs_id",
    }
    assert e.payload["obs_id"] == oid


def test_dry_run_writes_nothing_and_returns_the_would_be_transition(vault):
    p = _put("a.md", _doc(PENDING))
    before, lines = p.read_bytes(), _lines()

    t = _resolve(p, kind="duplicate", reason="same as another", dry_run=True)

    assert (t.dry_run, t.new_status, t.kind, t.resolved) == (True, "archived", "duplicate", TODAY)
    assert p.read_bytes() == before
    assert _lines() == lines


# -- (4) restore rule (§10, D15) ---------------------------------------------------------


def _inject_pre(mp: pytest.MonkeyPatch, name: str) -> None:
    """Pre-write ledger failures: nothing can have reached events.jsonl."""
    if name == "open":
        _inject(mp, "open")  # PermissionError on ledger.EVENTS_JSONL only
    elif name == "event-build":
        # Fires only inside append_event's event construction: obs_id and the
        # resolved date use content_hash / today, which stay real.
        def bad_event(*a: Any, **k: Any) -> Any:
            raise TypeError("injected: event build")

        mp.setattr(ledger, "LedgerEvent", bad_event)
    elif name == "mkdir":
        # Only ledger.OPS: write_text_atomic mkdirs the observation's parent too.
        real_mkdir = Path.mkdir

        def bad_mkdir(self: Path, *a: Any, **k: Any) -> None:
            if self == ledger.OPS:
                raise PermissionError(13, "injected: mkdir")
            real_mkdir(self, *a, **k)

        mp.setattr(Path, "mkdir", bad_mkdir)
    else:
        raise AssertionError(name)


@pytest.mark.parametrize(
    ("injection", "exc_type"),
    [("open", PermissionError), ("event-build", TypeError), ("mkdir", PermissionError)],
)
def test_pre_write_ledger_failure_restores_the_file_and_a_retry_succeeds(
    vault, injection, exc_type
):
    p = _put("a.md", _doc(PENDING, nl="\r\n"))
    before, lines = p.read_bytes(), _lines()

    with pytest.MonkeyPatch.context() as mp:
        _inject_pre(mp, injection)
        with pytest.raises(exc_type, match="injected") as ei:
            _resolve(p)
    assert not isinstance(ei.value, ledger.LedgerWriteError)

    assert p.read_bytes() == before  # written, then restored byte for byte
    assert _lines() == lines

    _resolve(p)  # no injection: the retry works
    assert len(_events()) == 1
    assert p.read_bytes() == _doc(_after(PENDING), nl="\r\n")


@pytest.mark.parametrize("injection", ["short", "write-eio-partial", "fsync", "close"])
def test_post_write_ledger_failure_leaves_the_file_resolved_and_a_retry_is_refused(
    vault, injection
):
    p = _put("a.md", _doc(PENDING))
    lines = _lines()

    with pytest.MonkeyPatch.context() as mp:
        _inject(mp, injection)
        with pytest.raises(ledger.LedgerWriteError):
            _resolve(p)

    # No restore: the line may be on disk, so the file must agree with it.
    assert p.read_bytes() == _doc(_after(PENDING))
    meta, _body, _kind = fm.load(p)  # strictly valid
    assert meta["status"] == "promoted"
    if injection in ("fsync", "close"):
        assert _lines() == lines + 1  # the full line is on disk

    _assert_refused(p, "not_pending", files=[p])


def _chain(e: BaseException | None) -> list[BaseException]:
    seen: list[BaseException] = []
    stack = [e]
    while stack:
        x = stack.pop()
        if x is None or any(x is s for s in seen):
            continue
        seen.append(x)
        stack += [x.__cause__, x.__context__]
    return seen


def test_failed_restore_raises_chained_and_leaves_the_file_resolved_with_no_event(vault):
    p = _put("a.md", _doc(PENDING))
    lines = _lines()
    real_write = fm.write_text_atomic
    calls: list[Path] = []

    def write_then_fail(path: Path, text: str) -> None:
        calls.append(path)
        if len(calls) == 2:  # the restore
            raise OSError(5, "injected: restore EIO")
        real_write(path, text)

    with pytest.MonkeyPatch.context() as mp:
        _inject(mp, "open")
        mp.setattr(fm, "write_text_atomic", write_then_fail)
        with pytest.raises(RuntimeError, match="no ledger event") as ei:
            _resolve(p)

    chain = _chain(ei.value)
    assert any(isinstance(x, PermissionError) and "injected: open" in str(x) for x in chain)
    assert any(isinstance(x, OSError) and "injected: restore" in str(x) for x in chain)
    # Documented leftover: resolved on disk, nothing in the ledger (the
    # signature Cycle 2 reconcile detects).
    assert fm.load(p)[0]["status"] == "promoted"
    assert _lines() == lines
    assert calls == [p.resolve(), p.resolve()]


def test_strict_revalidation_failure_raises_and_writes_nothing(vault):
    p = _put("a.md", _doc(PENDING))
    before, lines = p.read_bytes(), _lines()

    def invalid_patch(raw: str, updates: dict) -> str:
        # Parseable YAML, but status promoted without resolved/resolution/kind.
        return raw.replace("status: pending", "status: promoted")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fm, "patch_frontmatter", invalid_patch)
        with pytest.raises(RuntimeError, match="strict validation"):
            _resolve(p)

    assert p.read_bytes() == before
    assert _lines() == lines
