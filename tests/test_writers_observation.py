"""write_observation outcomes and ledger_suppressed (IDD §6 writers.py, §10, D4, D5, D10, P1, P3).

Real files under tmp_vault; the resolver reads real atlas pages. The clock
seam (ledger.today) is pinned so filenames are deterministic. Console output
is captured by a recording sink so the exact markup lines can be asserted.
Ledger assertions read the real ledger via ledger.replay().
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError
from rich.markup import escape

from sabnyx import fm, ledger, paths
from sabnyx import observations as obs
from sabnyx.schemas import ProposedObservation
from sabnyx.writers import WriteOutcome, ledger_suppressed, write_observation

TODAY = date(2026, 6, 20)
TRUSTED = "sabnyx-scan"


class _Lines:
    """Console sink: records each print's markup string verbatim."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def print(self, *args, **_kwargs) -> None:
        self.lines.append(" ".join(str(a) for a in args))


@pytest.fixture
def vault(tmp_vault, monkeypatch):
    monkeypatch.setattr(ledger, "today", lambda: TODAY)
    for name in ("alpha-concept", "beta-concept", "gamma-concept", "delta-concept"):
        _page(tmp_vault["ATLAS_CONCEPTS"], name)
    return tmp_vault


def _page(dir_: Path, name: str) -> None:
    fm.write(
        dir_ / f"{name}.md",
        {"type": "concept", "description": "a synthetic page for the writer tests, long enough"},
        f"# {name}\n",
    )


def _contradiction(
    a: str = "alpha-concept", b: str = "beta-concept", title: str = ""
) -> ProposedObservation:
    return ProposedObservation(
        category="contradiction",
        title=title or f"{a} vs {b}",
        body=f"{a} says X; {b} says not-X.",
        involves=[f"{a}.md", f"{b}.md"],
    )


def _base(ob: ProposedObservation) -> str:
    from sabnyx.util import slugify

    return f"{TODAY.isoformat()}-{ob.category}-{slugify(ob.title)}"


def _rel(p: Path) -> str:
    return str(p.relative_to(paths.DATA_ROOT))


def _seed(
    name: str,
    *,
    involves: list[str],
    kind: str | None = None,
    source_agent: str = TRUSTED,
    category: str = "contradiction",
) -> Path:
    """An existing observation file; `kind` None is pending, else closed with that kind."""
    meta: dict = {
        "type": "observation",
        "category": category,
        "observed": "2026-06-12",
        "source_agent": source_agent,
        "status": "pending",
        "involves": involves,
    }
    if kind is not None:
        meta |= {
            "status": "promoted" if kind == "done" else "archived",
            "resolved": "2026-06-13",
            "resolution": f"closed as {kind}",
            "resolution_kind": kind,
        }
    p = paths.OBSERVATIONS / name
    fm.write(p, meta, f"# {name}\n\nseeded\n")
    return p


def _write(ob: ProposedObservation, **kw) -> tuple[WriteOutcome, _Lines]:
    lines = _Lines()
    kw.setdefault("producer", "scan")
    kw.setdefault("actor", TRUSTED)
    out = write_observation(ob, console=lines, **kw)  # ty: ignore[invalid-argument-type]
    return out, lines


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _events(kind: str) -> list:
    return [e for e in ledger.replay() if e.kind == kind]


def _listing() -> list[str]:
    return sorted(p.name for p in paths.OBSERVATIONS.iterdir())


# -- 1. validation first -------------------------------------------------------------


@pytest.mark.parametrize(
    ("kw", "field"),
    [({"target": "Not A Slug"}, "target"), ({"producer": "bogus"}, "producer")],
)
def test_invalid_beats_exists_and_reads_no_index(vault, monkeypatch, kw, field):
    ob = _contradiction()
    squatter = paths.OBSERVATIONS / f"{_base(ob)}.md"
    squatter.write_text("occupied\n", encoding="utf-8")
    before = _snapshot(vault["DATA_ROOT"])
    events = ledger.count()

    def boom(*_a, **_k):
        raise AssertionError("invalid metadata must not read the index")

    monkeypatch.setattr(obs, "load_all", boom)
    monkeypatch.setattr(obs, "resolver", boom)
    out, lines = _write(ob, **kw)

    assert out.kind == "invalid"  # not exists, though the base name is taken
    assert out.path is None
    assert out.error is not None
    assert field in out.error
    # escaped: a pydantic message can carry '[a-z0-9]', which rich reads as markup
    assert lines.lines == [f"  [red]invalid:[/red] {escape(out.error)}"]
    assert _snapshot(vault["DATA_ROOT"]) == before
    assert ledger.count() == events


# -- 2. one test per outcome kind ----------------------------------------------------


def test_kind_written_identity_contradiction(vault):
    ob = _contradiction()
    out, lines = _write(ob)

    assert out.kind == "written"
    assert out.path == paths.OBSERVATIONS / f"{_base(ob)}.md"
    assert out.obs_id is not None
    assert out.obs_id.startswith("sha256:")
    assert out.dry_run is False
    assert out.error is None
    meta, body, _ = fm.load(out.path)
    assert meta["producer"] == "scan"
    assert meta["source_agent"] == TRUSTED
    assert str(meta["observed"]) == TODAY.isoformat()  # the UTC clock seam
    assert "obs_id" not in meta
    assert "supersedes" not in meta
    assert "target" not in meta
    assert body.startswith("# alpha-concept vs beta-concept")
    (ev,) = _events("observation.created")
    assert ev.subject == out.rel
    assert ev.actor == TRUSTED
    assert ev.payload["producer"] == "scan"
    assert ev.payload["obs_id"] == out.obs_id
    assert "supersedes" not in ev.payload
    assert lines.lines == [f"  [yellow]wrote[/yellow] {out.rel}"]


def test_kind_reopened_done_head_then_already_pending(vault):
    head = _seed(
        "2026-06-12-contradiction-old.md", involves=["beta-concept", "alpha-concept"], kind="done"
    )
    ob = _contradiction()
    out, lines = _write(ob)

    assert out.kind == "reopened"
    assert out.path == paths.OBSERVATIONS / f"{_base(ob)}.md"
    meta, _, _ = fm.load(out.path)
    assert meta["supersedes"] == _rel(head)
    assert meta["status"] == "pending"
    assert "obs_id" not in meta
    (ev,) = _events("observation.created")  # a reopen stays an observation.created
    assert ev.payload["supersedes"] == _rel(head)
    assert ev.payload["obs_id"] == out.obs_id
    assert lines.lines == [f"  [yellow]reopened[/yellow] {out.rel} (supersedes {_rel(head)})"]

    again, _ = _write(ob)
    assert again.kind == "already_pending"
    assert again.path == out.path
    assert again.obs_id == out.obs_id


@pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "real"])
def test_reopen_validates_supersedes_before_either_exit(vault, monkeypatch, dry_run):
    # Probe at the policy seam: a Reopen whose head rel is not a valid
    # `supersedes`. Today the reader makes that impossible by provenance; the
    # writer must refuse it by construction, on the dry-run path too.
    import dataclasses

    _seed(
        "2026-06-12-contradiction-old.md", involves=["beta-concept", "alpha-concept"], kind="done"
    )
    (head,) = obs.observations("promoted")
    forged = dataclasses.replace(head, rel="vault/atlas/not-an-observation.md")
    monkeypatch.setattr(obs, "recurrence_policy", lambda _idx, _oid: obs.Reopen(forged))
    before, events = _snapshot(vault["DATA_ROOT"]), ledger.count()

    out, lines = _write(_contradiction(), dry_run=dry_run)

    assert out.kind == "invalid"
    assert out.path is None
    assert out.obs_id is not None
    assert "supersedes" in out.error
    assert out.dry_run is dry_run
    assert lines.lines == ([] if dry_run else [f"  [red]invalid:[/red] {escape(out.error)}"])
    assert _snapshot(vault["DATA_ROOT"]) == before
    assert ledger.count() == events


def test_kind_already_pending(vault):
    pending = _seed(
        "2026-06-12-contradiction-old.md",
        involves=["alpha-concept", "beta-concept"],
        source_agent="manual",
    )
    files, events = _listing(), ledger.count()
    out, lines = _write(_contradiction())

    assert out.kind == "already_pending"
    assert out.path == pending
    assert out.obs_id is not None
    assert _listing() == files
    assert ledger.count() == events
    assert lines.lines == [f"  [dim]skip (pending)[/dim] {_rel(pending)}"]


def test_kind_suppressed(vault):
    head = _seed(
        "2026-06-12-contradiction-old.md",
        involves=["alpha-concept", "beta-concept"],
        kind="wontfix",
    )
    files, events = _listing(), ledger.count()
    out, lines = _write(_contradiction())

    assert out.kind == "suppressed"
    assert out.path == head
    assert out.resolution_kind == "wontfix"
    assert out.obs_id is not None
    assert _listing() == files
    assert ledger.count() == events  # write_observation never ledgers a suppression
    assert lines.lines == [f"  [dim]skip (suppressed: wontfix)[/dim] {_rel(head)}"]


def test_kind_exists_prose_collision_at_base(vault):
    ob = ProposedObservation(category="connection", title="alpha relates to beta", body="prose")
    squatter = paths.OBSERVATIONS / f"{_base(ob)}.md"
    squatter.write_text("occupied\n", encoding="utf-8")
    events = ledger.count()
    out, lines = _write(ob, producer="ingest", actor="sabnyx-ingest")

    assert out.kind == "exists"
    assert out.path == squatter
    assert out.obs_id is None
    assert squatter.read_text(encoding="utf-8") == "occupied\n"
    assert not (paths.OBSERVATIONS / f"{_base(ob)}-2.md").exists()  # prose takes no suffix
    assert ledger.count() == events
    assert lines.lines == [f"  [yellow]skip (exists)[/yellow] {_rel(squatter)}"]


def test_kind_invalid(vault):
    files, events = _listing(), ledger.count()
    out, lines = _write(_contradiction(), producer="bogus")

    assert out.kind == "invalid"
    assert out.path is None
    assert out.error is not None
    assert "producer" in out.error
    with pytest.raises(ValueError, match="no path"):
        _ = out.rel
    assert _listing() == files
    assert ledger.count() == events
    assert len(lines.lines) == 1
    assert lines.lines[0].startswith("  [red]invalid:[/red] ")


def test_untrusted_actor_is_prose_and_ignores_a_dismissed_head(vault):
    # D14: an untrusted source gets no id, so a wontfix head does not apply.
    _seed(
        "2026-06-12-contradiction-old.md",
        involves=["alpha-concept", "beta-concept"],
        kind="wontfix",
    )
    out, _ = _write(_contradiction(), producer="ingest", actor="telegram-agent")
    assert out.kind == "written"
    assert out.obs_id is None
    assert "obs_id" not in _events("observation.created")[0].payload


# -- 3. the -N suffix ----------------------------------------------------------------


def test_suffix_base_occupied_lands_on_2(vault):
    ob = _contradiction()
    (paths.OBSERVATIONS / f"{_base(ob)}.md").write_text("occupied\n", encoding="utf-8")
    out, lines = _write(ob)
    assert out.kind == "written"
    assert out.path == paths.OBSERVATIONS / f"{_base(ob)}-2.md"
    assert out.path.exists()
    assert lines.lines == [f"  [yellow]wrote[/yellow] {out.rel}"]


def test_suffix_prose_squatter_on_2_lands_on_3(vault):
    ob = _contradiction()
    (paths.OBSERVATIONS / f"{_base(ob)}.md").write_text("occupied\n", encoding="utf-8")
    squatter = _seed(
        f"{_base(ob)}-2.md", involves=["x"], source_agent="telegram-agent", category="connection"
    )
    out, _ = _write(ob)
    assert out.kind == "written"
    assert out.path == paths.OBSERVATIONS / f"{_base(ob)}-3.md"
    assert fm.load(squatter)[0]["category"] == "connection"  # untouched


def test_suffix_bound_99_gives_invalid(vault):
    ob = _contradiction()
    (paths.OBSERVATIONS / f"{_base(ob)}.md").write_text("occupied\n", encoding="utf-8")
    for n in range(2, 100):
        (paths.OBSERVATIONS / f"{_base(ob)}-{n}.md").write_text("occupied\n", encoding="utf-8")
    files, events = _listing(), ledger.count()
    out, lines = _write(ob)

    assert out.kind == "invalid"
    assert out.path is None
    assert out.obs_id is not None
    assert out.error is not None
    assert "-99" in out.error
    assert _listing() == files
    assert not (paths.OBSERVATIONS / f"{_base(ob)}-100.md").exists()
    assert ledger.count() == events
    assert lines.lines[0].startswith("  [red]invalid:[/red] ")


# -- 4. dry run ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("head_kind", "expected"), [(None, "written"), ("done", "reopened"), ("wontfix", "suppressed")]
)
def test_dry_run_touches_neither_disk_nor_ledger(vault, head_kind, expected):
    if head_kind is not None:
        _seed(
            "2026-06-12-contradiction-old.md",
            involves=["alpha-concept", "beta-concept"],
            kind=head_kind,
        )
    ob = _contradiction()
    before = _snapshot(vault["DATA_ROOT"])
    events = list(ledger.replay())
    out, lines = _write(ob, dry_run=True)

    assert out.kind == expected
    assert out.dry_run is True
    assert out.obs_id is not None
    if expected != "suppressed":
        assert out.path == paths.OBSERVATIONS / f"{_base(ob)}.md"
        assert not out.path.exists()
    assert _snapshot(vault["DATA_ROOT"]) == before
    assert list(ledger.replay()) == events
    assert lines.lines == []

    real, _ = _write(ob)  # the preview did not drift from the real call
    assert (real.kind, real.path, real.obs_id) == (out.kind, out.path, out.obs_id)


# -- 5. ledger_suppressed ------------------------------------------------------------


def _suppressed_events():
    return _events("observation.suppressed")


def test_ledger_suppressed_one_event_per_id_per_run(vault):
    head = _seed(
        "2026-06-12-contradiction-old.md",
        involves=["alpha-concept", "beta-concept"],
        kind="wontfix",
    )
    outs = [_write(_contradiction(title=f"take {n}"))[0] for n in range(3)]
    assert [o.kind for o in outs] == ["suppressed"] * 3

    assert ledger_suppressed(outs, actor="sabnyx-scan") == 1
    (ev,) = _suppressed_events()
    assert ev.plane == "projection"
    assert ev.actor == "sabnyx-scan"
    assert ev.subject == _rel(head)
    assert ev.payload == {"obs_id": outs[0].obs_id, "count": 3, "kind": "wontfix"}


def test_ledger_suppressed_two_ids_two_events(vault):
    h1 = _seed(
        "2026-06-12-contradiction-ab.md", involves=["alpha-concept", "beta-concept"], kind="wontfix"
    )
    h2 = _seed(
        "2026-06-12-contradiction-gd.md",
        involves=["gamma-concept", "delta-concept"],
        kind="obsolete",
    )
    outs = [
        _write(_contradiction("gamma-concept", "delta-concept"))[0],
        _write(_contradiction())[0],
        _write(_contradiction("delta-concept", "gamma-concept"))[0],
    ]
    assert ledger_suppressed(outs, actor="sabnyx-scan") == 2
    evs = _suppressed_events()
    assert [(e.subject, e.payload["count"], e.payload["kind"]) for e in evs] == [
        (_rel(h2), 2, "obsolete"),  # first-seen order
        (_rel(h1), 1, "wontfix"),
    ]
    assert evs[0].payload["obs_id"] != evs[1].payload["obs_id"]


def test_ledger_suppressed_zero_emits_nothing(vault):
    ob = ProposedObservation(category="connection", title="prose", body="b")
    outs = [_write(_contradiction())[0], _write(ob)[0], _write(ob)[0]]
    assert [o.kind for o in outs] == ["written", "written", "exists"]
    events = ledger.count()
    assert ledger_suppressed(outs, actor="sabnyx-scan") == 0
    assert ledger_suppressed([], actor="sabnyx-scan") == 0
    assert ledger.count() == events


def test_ledger_suppressed_skips_dry_run_outcomes(vault):
    _seed(
        "2026-06-12-contradiction-old.md",
        involves=["alpha-concept", "beta-concept"],
        kind="wontfix",
    )
    dry = [_write(_contradiction(), dry_run=True)[0] for _ in range(2)]
    assert [(o.kind, o.dry_run) for o in dry] == [("suppressed", True)] * 2
    events = ledger.count()
    assert ledger_suppressed(dry, actor="sabnyx-scan") == 0
    assert ledger.count() == events

    real = _write(_contradiction())[0]
    assert ledger_suppressed([*dry, real], actor="sabnyx-scan") == 1
    assert _suppressed_events()[0].payload["count"] == 1


# -- 6. tokens never open files ------------------------------------------------------

_OPEN_TRACK: dict = {"on": False, "seen": []}
_HOOK_INSTALLED = False


def _audit(event: str, args: tuple) -> None:
    if not _OPEN_TRACK["on"] or event != "open":
        return
    target = args[0]
    if isinstance(target, (str, bytes, os.PathLike)):
        _OPEN_TRACK["seen"].append(os.fsdecode(target))


def test_pathlike_involves_open_nothing_outside_vault_and_ops(vault, monkeypatch):
    global _HOOK_INSTALLED
    if not _HOOK_INSTALLED:  # audit hooks cannot be removed: install once, gate by flag
        sys.addaudithook(_audit)
        _HOOK_INSTALLED = True
    root = vault["DATA_ROOT"].resolve()
    sentinels = [root / ".env", root / "vault" / ".env", root / "vault" / "intake" / ".env"]
    for s in sentinels:
        s.write_text("SENTINEL=1\n", encoding="utf-8")
    _page(vault["ATLAS_CONCEPTS"], "b")
    # cwd-relative resolution of '../../.env' would land on vault/.env
    monkeypatch.chdir(paths.OBSERVATIONS)

    ob = ProposedObservation(
        category="contradiction", title="env vs b", body="x", involves=["../../.env", "b"]
    )
    _OPEN_TRACK["seen"] = []
    _OPEN_TRACK["on"] = True
    try:
        out, _ = _write(ob)
    finally:
        _OPEN_TRACK["on"] = False

    assert out.kind == "written"
    assert out.obs_id is not None  # identity path ran: not vacuous
    opened = [Path(p) if os.path.isabs(p) else Path(os.getcwd()) / p for p in _OPEN_TRACK["seen"]]
    under = [p.resolve() for p in opened if p.resolve().is_relative_to(root)]
    assert under, "tracking saw no opens under the data root: the probe is vacuous"
    assert not set(under) & set(sentinels)
    allowed = (root / "vault", root / "ops")
    assert [p for p in under if not any(p.is_relative_to(a) for a in allowed)] == []
    assert all(s.read_text(encoding="utf-8") == "SENTINEL=1\n" for s in sentinels)


# -- 7. no provenance from model output ----------------------------------------------


@pytest.mark.parametrize("field", ["source_agent", "supersedes", "producer", "target"])
def test_proposed_observation_forbids_provenance_fields(field):
    with pytest.raises(ValidationError):
        ProposedObservation(
            **{
                "category": "contradiction",
                "title": "t",
                "body": "b",
                field: "vault/intake/.observations/x.md",
            }
        )
