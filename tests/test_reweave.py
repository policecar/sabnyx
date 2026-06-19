"""Tests for marker blocks, pending-intent resolution, and the reweave command."""

from __future__ import annotations

from click.testing import CliRunner

from sabnyx import fm, ledger, util
from sabnyx.schemas import ProposedObservation, RelatedAddition, ReweaveChangeset

# -- marker blocks -----------------------------------------------------------------


def test_marker_block_roundtrip():
    body = "intro\n\n## Related\n\n<!-- BEGIN sabnyx:related -->\n<!-- END sabnyx:related -->\n"
    assert util.get_marker_block(body, "related") == ""
    updated = util.set_marker_block(body, "related", ["- [[a]]", "- [[b]]"])
    assert util.get_marker_block(updated, "related") == "- [[a]]\n- [[b]]"
    # everything outside the block is preserved byte-for-byte
    assert updated.startswith("intro\n\n## Related\n\n")
    assert updated.endswith("<!-- END sabnyx:related -->\n")
    # idempotent replace, not append
    again = util.set_marker_block(updated, "related", ["- [[a]]"])
    assert util.get_marker_block(again, "related") == "- [[a]]"
    assert again.count("BEGIN sabnyx:related") == 1


def test_marker_block_appended_when_missing():
    body = "# Title\n\nprose only\n"
    updated = util.set_marker_block(body, "related", ["- [[x]]"], heading="Related")
    assert updated.startswith("# Title\n\nprose only\n")
    assert "## Related" in updated
    assert util.get_marker_block(updated, "related") == "- [[x]]"


# -- vault scaffolding ----------------------------------------------------------------


def _make_atlas_file(dir_, slug, description, body="", related=True):
    meta = {
        "type": "concept",
        "description": description,
        "tags": [],
        "status": "final",
        "promoted_from": None,
        "promoted_date": "2026-06-12",
        "last_verified": "2026-06-12",
        "proof": None,
    }
    block = "\n\n## Related\n\n<!-- BEGIN sabnyx:related -->\n<!-- END sabnyx:related -->\n"
    fm.write(dir_ / f"{slug}.md", meta, f"# {slug}\n\n{body}" + (block if related else "\n"))
    return dir_ / f"{slug}.md"


def _propose_reweave(rel_subject):
    ledger.append_event(
        plane="intent",
        kind="reweave.proposed",
        actor="sabnyx-cli",
        subject=rel_subject,
        payload={"prompt": "..."},
    )


# -- pending resolution ------------------------------------------------------------------


def test_pending_targets_consumed_by_completion(tmp_vault):
    from sabnyx.commands import reweave as rw

    target = _make_atlas_file(
        tmp_vault["ATLAS_CONCEPTS"], "alpha", "a concept about alpha things, long enough to pass"
    )
    rel = str(target.relative_to(tmp_vault["DATA_ROOT"]))
    _propose_reweave(rel)
    assert [str(p) for p in rw._pending_targets()] == [str(target)]

    ledger.append_event(kind="reweave.completed", actor="t", subject=rel, payload={})
    assert rw._pending_targets() == []


def test_pending_targets_skips_deleted_files(tmp_vault):
    from sabnyx.commands import reweave as rw

    _propose_reweave("vault/atlas/concepts/gone.md")
    assert rw._pending_targets() == []


# -- end-to-end (faked LLM) -----------------------------------------------------------------


CANNED = ReweaveChangeset(
    summary="alpha bridges to beta; gamma contradiction noted.",
    related_additions=[
        # backward link: beta gains a link to the target
        RelatedAddition(file="beta", add_links=["alpha"], reason="same topic", priority="bridge"),
        # outbound link on the target itself + guards: self-link, unknown slug
        RelatedAddition(
            file="alpha",
            add_links=["beta", "alpha", "does-not-exist"],
            reason="bidirectional",
        ),
        # duplicate: beta already links alpha after first addition? (separate file: gamma
        # already links beta in prose, so the add should be dropped)
        RelatedAddition(file="gamma", add_links=["beta"], reason="dup test"),
    ],
    observations=[
        ProposedObservation(
            category="contradiction",
            title="alpha vs gamma claim",
            body="alpha says X, gamma says not-X. Suggested resolution: verify source.",
            involves=["alpha.md", "gamma.md"],
        )
    ],
)


def _run_reweave(monkeypatch, args, input_text=None):
    from sabnyx.commands import reweave as rw

    calls: list[dict] = []

    def fake_parse_structured(**kwargs):
        calls.append(kwargs)
        return CANNED

    monkeypatch.setattr(rw.llm, "parse_structured", fake_parse_structured)
    runner = CliRunner()
    result = runner.invoke(rw.reweave_cmd, args, input=input_text)
    return result, calls


def test_reweave_pending_applies_guarded_changeset(tmp_vault, monkeypatch):
    concepts = tmp_vault["ATLAS_CONCEPTS"]
    target = _make_atlas_file(concepts, "alpha", "a concept about alpha things, long enough yes")
    _make_atlas_file(concepts, "beta", "a concept about beta things, also long enough to pass")
    _make_atlas_file(
        concepts,
        "gamma",
        "a concept about gamma things, long enough to pass too",
        body="prose already links [[beta]] here.\n",
    )
    rel = str(target.relative_to(tmp_vault["DATA_ROOT"]))
    _propose_reweave(rel)

    result, calls = _run_reweave(monkeypatch, ["--yes"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]["role"] == "standard"

    # beta gained the backward link
    _, beta_body, _ = fm.load(concepts / "beta.md")
    assert "[[alpha]]" in (util.get_marker_block(beta_body, "related") or "")
    # target gained only the legit outbound link (self + unknown dropped)
    _, alpha_body, _ = fm.load(concepts / "alpha.md")
    assert util.get_marker_block(alpha_body, "related") == "- [[beta]]"
    # gamma untouched: link already present in prose
    _, gamma_body, _ = fm.load(concepts / "gamma.md")
    assert util.get_marker_block(gamma_body, "related") == ""

    # contradiction observation written
    obs = list(tmp_vault["OBSERVATIONS"].glob("*-contradiction-*.md"))
    assert len(obs) == 1
    meta, _, _ = fm.load(obs[0])
    assert meta["involves"] == ["alpha.md", "gamma.md"]

    # ledger: intent consumed, edits recorded
    kinds = [e.kind for e in ledger.replay()]
    assert kinds.count("atlas.reweaved") == 2
    assert kinds[-1] == "reweave.completed"

    # second run: nothing pending
    result2, calls2 = _run_reweave(monkeypatch, [])
    assert result2.exit_code == 0
    assert calls2 == []
    assert "nothing to do" in result2.output


def test_reweave_dry_run_writes_nothing(tmp_vault, monkeypatch):
    concepts = tmp_vault["ATLAS_CONCEPTS"]
    target = _make_atlas_file(concepts, "alpha", "a concept about alpha things, long enough yes")
    _make_atlas_file(concepts, "beta", "a concept about beta things, also long enough to pass")
    _make_atlas_file(concepts, "gamma", "a concept about gamma things, long enough to pass too")
    _propose_reweave(str(target.relative_to(tmp_vault["DATA_ROOT"])))

    result, calls = _run_reweave(monkeypatch, ["--dry-run"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    _, beta_body, _ = fm.load(concepts / "beta.md")
    assert util.get_marker_block(beta_body, "related") == ""
    # intent NOT consumed on dry run
    from sabnyx.commands import reweave as rw

    assert len(rw._pending_targets()) == 1


def test_reweave_explicit_target_rejects_non_atlas(tmp_vault, monkeypatch, tmp_path):
    from sabnyx.commands import reweave as rw

    stray = tmp_path / "stray.md"
    stray.write_text("not atlas")
    runner = CliRunner()
    result = runner.invoke(rw.reweave_cmd, [str(stray)])
    assert result.exit_code == 2
    assert "not under vault/atlas/" in " ".join(result.output.split())
