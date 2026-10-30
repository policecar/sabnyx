"""Ledger↔filesystem reconciliation and promote-ordering invariants (Fix #7)."""

from __future__ import annotations

from click.testing import CliRunner

from sabnyx import ledger, services
from sabnyx.commands.reconcile import reconcile_cmd


def _make_intake(tmp_vault, slug: str) -> None:
    (tmp_vault["INTAKE"] / f"{slug}.md").write_text(
        "---\n"
        "type: bookmark\n"
        "source_url: https://example.com/source\n"
        "description: a sufficiently long description for promotion tests\n"
        "source: web\n"
        "status: draft\n"
        "source_agent: sabnyx-ingest\n"
        "created: 2026-06-19T00:00:00+00:00\n"
        "---\n"
        f"# {slug}\n\nbody\n",
        encoding="utf-8",
    )


def test_promote_ledgers_before_unlink(tmp_vault):
    """The atlas.promoted event is the commit point: it lands, the source is
    gone, and the dest exists — and the ledger order has the move recorded."""
    _make_intake(tmp_vault, "alpha")
    src = tmp_vault["INTAKE"] / "alpha.md"

    dest = services.promote_file(src, "reference")

    assert dest.exists()
    assert not src.exists()  # source removed after the ledger commit
    kinds = [e.kind for e in ledger.replay()]
    assert "atlas.promoted" in kinds
    assert "reweave.proposed" in kinds
    # atlas.promoted precedes the unlink-dependent knowledge-log write; reweave
    # intent is emitted alongside it so a crash can't lose it.
    assert kinds.index("atlas.promoted") < kinds.index("reweave.proposed")


def test_reconcile_clean_after_promote(tmp_vault):
    _make_intake(tmp_vault, "beta")
    services.promote_file(tmp_vault["INTAKE"] / "beta.md", "reference")

    r = services.reconcile_atlas()
    assert r.clean
    assert r.unledgered == []
    assert r.orphaned == []


def test_reconcile_detects_unledgered_file(tmp_vault):
    # A manually-authored atlas file has no atlas.promoted event.
    (tmp_vault["ATLAS_CONCEPTS"] / "hand-written.md").write_text(
        "---\ntype: concept\ndescription: manually authored, never promoted via the pipeline\n"
        "status: final\n---\n# hand written\n",
        encoding="utf-8",
    )
    r = services.reconcile_atlas()
    assert "vault/atlas/concepts/hand-written.md" in r.unledgered
    assert r.orphaned == []


def test_reconcile_detects_orphaned_event(tmp_vault):
    _make_intake(tmp_vault, "gamma")
    dest = services.promote_file(tmp_vault["INTAKE"] / "gamma.md", "reference")
    # Delete the promoted file outside the pipeline -> the event is now orphaned.
    dest.unlink()

    r = services.reconcile_atlas()
    assert "vault/atlas/references/gamma.md" in r.orphaned


def test_reconcile_cmd_exits_nonzero_on_orphan(tmp_vault):
    _make_intake(tmp_vault, "delta")
    dest = services.promote_file(tmp_vault["INTAKE"] / "delta.md", "reference")
    dest.unlink()

    result = CliRunner().invoke(reconcile_cmd, [])
    assert result.exit_code == 1, result.output
    assert "missing" in result.output


def test_reconcile_cmd_clean_exits_zero(tmp_vault):
    result = CliRunner().invoke(reconcile_cmd, [])
    assert result.exit_code == 0, result.output
    assert "clean" in result.output


def test_atlas_removed_compensates_deleted_file(tmp_vault):
    """A legitimate deletion is acknowledged with a compensating atlas.removed
    event (SPEC §10) — reconcile stops expecting the file, in ledger order."""
    _make_intake(tmp_vault, "epsilon")
    dest = services.promote_file(tmp_vault["INTAKE"] / "epsilon.md", "reference")
    dest.unlink()
    assert services.reconcile_atlas().orphaned == ["vault/atlas/references/epsilon.md"]

    ledger.append_event(
        kind="atlas.removed",
        actor="operator",
        subject="vault/atlas/references/epsilon.md",
        payload={"reason": "superseded"},
    )
    r = services.reconcile_atlas()
    assert r.orphaned == []
    assert r.clean

    result = CliRunner().invoke(reconcile_cmd, [])
    assert result.exit_code == 0, result.output


# -- observations vs their ledger events (report-hygiene) ------------------------
# Why: an observation whose lifecycle the ledger never saw (dropped by hand,
# or its status hand-edited) is invisible to every ledger-based audit. These
# lists surface them. They warn and never fail: hand-dropping is legitimate
# (AGENTS §4), so a vault full of them must still reconcile with exit 0.


def _hand_drop(tmp_vault, slug: str, status: str = "pending") -> str:
    from sabnyx import fm

    p = tmp_vault["OBSERVATIONS"] / f"2026-06-12-gap-{slug}.md"
    meta = {
        "type": "observation",
        "category": "gap",
        "observed": "2026-06-12",
        "source_agent": "manual",
        "status": status,
        "involves": [],
    }
    if status != "pending":
        meta |= {"resolved": "2026-06-13", "resolution": "noted", "resolution_kind": "wontfix"}
    fm.write(p, meta, f"# {slug}\n\nnoted\n")
    fm.load(p)  # valid on disk: a listing means "unledgered", not "malformed"
    return p.relative_to(tmp_vault["DATA_ROOT"]).as_posix()


def _write_ledgered(slug: str):
    from rich.console import Console

    from sabnyx import writers
    from sabnyx.schemas import ProposedObservation

    out = writers.write_observation(
        ProposedObservation(category="gap", title=f"made up {slug}", body="noted"),
        producer="manual",
        actor="operator",
        console=Console(),
    )
    assert out.path is not None
    return out.path


def test_reconcile_observations_clean_when_empty(tmp_vault):
    o = services.reconcile_observations()
    assert o.pending_without_created == []
    assert o.resolved_without_resolved == []


def test_reconcile_observations_fully_ledgered_is_clean(tmp_vault):
    from sabnyx import writers

    _write_ledgered("made-up-otter")
    closed = _write_ledgered("made-up-quill")
    # Resolved in place: the rel does not change, so observation.resolved's
    # subject still matches the file.
    writers.transition_observation(closed, kind="wontfix", reason="no", actor="operator")
    o = services.reconcile_observations()
    assert o.pending_without_created == []
    assert o.resolved_without_resolved == []


def test_reconcile_lists_pending_without_created(tmp_vault):
    rel = _hand_drop(tmp_vault, "made-up-otter")
    o = services.reconcile_observations()
    assert o.pending_without_created == [rel]
    assert o.resolved_without_resolved == []

    result = CliRunner().invoke(reconcile_cmd, [])
    assert result.exit_code == 0, result.output
    assert rel in result.output


def test_reconcile_lists_closed_without_resolved(tmp_vault):
    rel = _hand_drop(tmp_vault, "made-up-quill", status="archived")
    o = services.reconcile_observations()
    assert o.resolved_without_resolved == [rel]
    assert o.pending_without_created == []

    result = CliRunner().invoke(reconcile_cmd, [])
    assert result.exit_code == 0, result.output
    assert rel in result.output


def test_reconcile_hand_edited_status_lists_only_missing_resolved(tmp_vault):
    """Ledgered as created, then closed by hand: created is on record, the
    resolution is not."""
    from sabnyx import fm

    path = _write_ledgered("made-up-otter")
    meta, body, _kind = fm.load(path)
    meta |= {
        "status": "archived",
        "resolved": "2026-06-13",
        "resolution": "noted",
        "resolution_kind": "wontfix",
    }
    fm.write(path, meta, body)
    rel = path.relative_to(tmp_vault["DATA_ROOT"]).as_posix()
    o = services.reconcile_observations()
    assert o.resolved_without_resolved == [rel]
    assert o.pending_without_created == []


def test_reconcile_observations_sorted(tmp_vault):
    rels = [_hand_drop(tmp_vault, s) for s in ("made-up-zebu", "made-up-auk", "made-up-mole")]
    assert services.reconcile_observations().pending_without_created == sorted(rels)


def test_reconcile_skips_malformed_observation(tmp_vault):
    (tmp_vault["OBSERVATIONS"] / "2026-06-12-gap-broken.md").write_text("---\nnope: [\n---\n")
    o = services.reconcile_observations()
    assert o.pending_without_created == []
    assert o.resolved_without_resolved == []
