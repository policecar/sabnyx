"""Ledger↔filesystem reconciliation and promote-ordering invariants (Fix #7)."""

from __future__ import annotations

from click.testing import CliRunner

from sabnyx import ledger, services
from sabnyx.commands.reconcile import reconcile_cmd


def _make_intake(tmp_vault, slug: str) -> None:
    (tmp_vault["INTAKE"] / f"{slug}.md").write_text(
        "---\n"
        "type: bookmark\n"
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
