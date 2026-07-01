"""Tests for Phase 1: the heartbeat — tend command, auto-promote gate, triage batch."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from sabnyx import fm, ledger
from sabnyx.schemas import TriageBatch, TriageItem
from sabnyx.services import auto_promote_decision

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _good_meta(
    intake_type: str = "bookmark",
    *,
    description: str = "a long enough description that satisfies the 30 char limit here",
    source_agent: str = "sabnyx-ingest",
) -> dict:
    return {
        "type": intake_type,
        "description": description,
        "source": "web",
        "status": "draft",
        "created": datetime.now(UTC).isoformat(),
        "source_agent": source_agent,
    }


def _make_intake(
    intake_dir: Path, slug: str, meta: dict, body: str = "# body\n\ncontent\n"
) -> Path:
    p = intake_dir / f"{slug}.md"
    fm.write(p, meta, body)
    return p


def _make_atlas(dir_: Path, slug: str, body: str = "# durable\n\noriginal content\n") -> Path:
    p = dir_ / f"{slug}.md"
    fm.write(
        p,
        {
            "type": "concept",
            "description": "an existing durable atlas file that must never be overwritten",
            "tags": [],
            "status": "final",
            "promoted_from": None,
            "promoted_date": None,
            "last_verified": None,
            "proof": None,
        },
        body,
    )
    return p


# ---------------------------------------------------------------------------
# config.trusted_sources
# ---------------------------------------------------------------------------


class TestTrustedSources:
    def test_default_returns_sabnyx_ingest(self, monkeypatch):
        monkeypatch.delenv("SABNYX_TRUSTED_SOURCES", raising=False)
        from sabnyx.config import trusted_sources

        assert trusted_sources() == {"sabnyx-ingest"}

    def test_env_override_single(self, monkeypatch):
        monkeypatch.setenv("SABNYX_TRUSTED_SOURCES", "my-agent")
        from sabnyx.config import trusted_sources

        assert trusted_sources() == {"my-agent"}

    def test_env_override_multiple(self, monkeypatch):
        monkeypatch.setenv("SABNYX_TRUSTED_SOURCES", "agent-a, agent-b, agent-c")
        from sabnyx.config import trusted_sources

        assert trusted_sources() == {"agent-a", "agent-b", "agent-c"}

    def test_empty_env_returns_default(self, monkeypatch):
        monkeypatch.setenv("SABNYX_TRUSTED_SOURCES", "")
        from sabnyx.config import trusted_sources

        assert trusted_sources() == {"sabnyx-ingest"}


# ---------------------------------------------------------------------------
# auto_promote_decision — pure function, table-style tests
# ---------------------------------------------------------------------------


class TestAutoPromoteDecision:
    TRUSTED = {"sabnyx-ingest"}

    def _decide(self, meta, kind="intake", *, atlas_slugs=None, source_slug="my-bookmark"):
        return auto_promote_decision(
            meta,
            kind,
            trusted=self.TRUSTED,
            atlas_slugs=atlas_slugs or set(),
            source_slug=source_slug,
        )

    # -- Gate 1 / 2: promotable type ----------------------------------------

    def test_bookmark_is_promotable_as_reference(self):
        d = self._decide(_good_meta("bookmark"))
        assert d.promotable is True
        assert d.atlas_type == "reference"
        assert d.reasons == []

    def test_paper_is_promotable_as_reference(self):
        d = self._decide(_good_meta("paper"))
        assert d.promotable is True
        assert d.atlas_type == "reference"

    def test_note_deferred_to_llm_tier(self):
        d = self._decide(_good_meta("note"))
        assert d.promotable is False
        assert d.atlas_type is None
        assert any("LLM tier" in r for r in d.reasons)

    def test_thought_deferred_to_llm_tier(self):
        d = self._decide(_good_meta("thought"))
        assert d.promotable is False
        assert any("LLM tier" in r for r in d.reasons)

    def test_meeting_extract_never_promoted(self):
        d = self._decide(_good_meta("meeting-extract"))
        assert d.promotable is False
        assert any("temporal" in r for r in d.reasons)

    def test_observation_kind_never_promoted(self):
        d = self._decide(_good_meta("bookmark"), kind="observation")
        assert d.promotable is False

    # -- Gate 3: required fields --------------------------------------------

    def test_missing_source_fails_gate3(self):
        meta = _good_meta("bookmark")
        del meta["source"]
        d = self._decide(meta)
        assert d.promotable is False
        assert any("missing required fields" in r for r in d.reasons)
        assert "source" in " ".join(d.reasons)

    def test_missing_description_fails_gate3(self):
        meta = _good_meta("bookmark")
        del meta["description"]
        d = self._decide(meta)
        assert d.promotable is False

    def test_missing_status_fails_gate3(self):
        meta = _good_meta("bookmark")
        del meta["status"]
        d = self._decide(meta)
        assert d.promotable is False

    # -- Gate 4: description length -----------------------------------------

    def test_short_description_fails_gate4(self):
        meta = _good_meta("bookmark", description="too short")
        d = self._decide(meta)
        assert d.promotable is False
        assert any("description too short" in r for r in d.reasons)

    def test_exactly_30_chars_passes_gate4(self):
        meta = _good_meta("bookmark", description="a" * 30)
        d = self._decide(meta)
        assert d.promotable is True

    # -- Gate 5: no duplicate slug ------------------------------------------

    def test_duplicate_slug_fails_gate5(self):
        d = self._decide(
            _good_meta("bookmark"), source_slug="my-bookmark", atlas_slugs={"my-bookmark"}
        )
        assert d.promotable is False
        assert any("already exists in atlas" in r for r in d.reasons)

    def test_different_slug_passes_gate5(self):
        d = self._decide(
            _good_meta("bookmark"), source_slug="my-new-bookmark", atlas_slugs={"other"}
        )
        assert d.promotable is True

    # -- Gate 6: trusted source_agent ---------------------------------------

    def test_untrusted_agent_fails_gate6(self):
        d = self._decide(_good_meta("bookmark", source_agent="unknown-agent"))
        assert d.promotable is False
        assert any("not in trusted set" in r for r in d.reasons)

    def test_trusted_agent_passes_gate6(self):
        d = self._decide(_good_meta("bookmark", source_agent="sabnyx-ingest"))
        assert d.promotable is True

    def test_custom_trusted_set(self):
        meta = _good_meta("bookmark", source_agent="my-custom-agent")
        d = auto_promote_decision(
            meta,
            "intake",
            trusted={"my-custom-agent"},
            atlas_slugs=set(),
            source_slug="slug",
        )
        assert d.promotable is True

    # -- multiple failures accumulate ---------------------------------------

    def test_multiple_gate_failures_reported(self):
        meta = _good_meta("bookmark", description="short", source_agent="untrusted")
        del meta["source"]
        d = self._decide(meta)
        assert d.promotable is False
        assert len(d.reasons) >= 2


# ---------------------------------------------------------------------------
# services.promote_file
# ---------------------------------------------------------------------------


class TestPromoteFile:
    def test_promote_creates_atlas_file_and_removes_intake(self, tmp_vault):
        from sabnyx.services import promote_file

        intake_dir = tmp_vault["INTAKE"]
        meta = _good_meta("bookmark")
        p = _make_intake(intake_dir, "my-bookmark", meta)

        result = promote_file(p, "reference", actor="test-actor")

        assert result.exists()
        assert not p.exists()  # intake removed

        atlas_meta, body, kind = fm.load(result)
        assert kind == "atlas"
        assert atlas_meta["type"] == "reference"
        assert atlas_meta["status"] == "final"
        assert "promoted_from" in atlas_meta
        assert "## Related" in body
        assert "BEGIN sabnyx:related" in body

    def test_promote_appends_knowledge_log(self, tmp_vault):
        from sabnyx.services import promote_file

        p = _make_intake(tmp_vault["INTAKE"], "log-test", _good_meta("bookmark"))
        promote_file(p, "reference", actor="test-actor")

        log = tmp_vault["KNOWLEDGE_LOG"].read_text()
        assert "log-test.md" in log
        assert "reference" in log

    def test_promote_emits_ledger_events(self, tmp_vault):
        from sabnyx.services import promote_file

        p = _make_intake(tmp_vault["INTAKE"], "ledger-test", _good_meta("bookmark"))
        promote_file(p, "reference", actor="test-actor")

        kinds = [e.kind for e in ledger.replay()]
        assert "atlas.promoted" in kinds
        assert "reweave.proposed" in kinds

    def test_promote_preserves_source_url_in_provenance(self, tmp_vault):
        from sabnyx.services import promote_file

        meta = _good_meta("bookmark")
        meta["source_url"] = "https://example.com/article"
        p = _make_intake(tmp_vault["INTAKE"], "url-test", meta)
        result = promote_file(p, "reference", actor="test-actor")

        body = result.read_text()
        assert "<!-- source-url: https://example.com/article -->" in body

    def test_promote_with_last_verified(self, tmp_vault):
        from sabnyx.services import promote_file

        p = _make_intake(tmp_vault["INTAKE"], "lv-test", _good_meta("bookmark"))
        result = promote_file(p, "reference", last_verified="2025-01-01", actor="test-actor")

        atlas_meta, _, _ = fm.load(result)
        assert str(atlas_meta["last_verified"]) == "2025-01-01"

    def test_promote_refuses_existing_destination(self, tmp_vault):
        """C1: a duplicate slug must never overwrite (or roll back) an
        existing atlas file — the durable tier is untouchable here."""
        from sabnyx.services import promote_file

        existing = _make_atlas(tmp_vault["ATLAS_REFERENCES"], "dupe")
        original = existing.read_text()
        p = _make_intake(tmp_vault["INTAKE"], "dupe", _good_meta("bookmark"))

        with pytest.raises(RuntimeError, match="refusing to overwrite"):
            promote_file(p, "reference", actor="test-actor")

        assert existing.read_text() == original  # atlas file byte-identical
        assert p.exists()  # intake source untouched
        assert "atlas.promoted" not in [e.kind for e in ledger.replay()]


class TestPromoteCli:
    def test_rejects_malformed_last_verified_before_writing(self, tmp_vault):
        """Validate the date at the CLI boundary, not as a post-write rollback."""
        from sabnyx.commands.promote import promote_cmd

        p = _make_intake(tmp_vault["INTAKE"], "lv-bad", _good_meta("bookmark"))
        result = CliRunner().invoke(
            promote_cmd, [str(p), "--as", "reference", "--last-verified", "not-a-date"]
        )
        assert result.exit_code == 2, result.output
        assert "ISO date" in result.output
        assert p.exists()
        assert not (tmp_vault["ATLAS_REFERENCES"] / "lv-bad.md").exists()


# ---------------------------------------------------------------------------
# tend command — auto-promote pass
# ---------------------------------------------------------------------------


def _run_tend(monkeypatch, args, input_text=None, triage_result=None):
    from sabnyx.commands import tend as tend_mod

    calls: list[dict] = []

    if triage_result is not None:

        def fake_parse_structured(**kwargs):
            calls.append(kwargs)
            return triage_result

        monkeypatch.setattr(tend_mod.triage_batch.__module__.split(".")[0], "_fake_unused", None)
        # Patch at the services level since triage_batch calls llm.parse_structured
        from sabnyx import services as svc_mod

        monkeypatch.setattr(svc_mod.llm, "parse_structured", fake_parse_structured)

    runner = CliRunner()
    result = runner.invoke(tend_mod.tend_cmd, args, input=input_text, catch_exceptions=False)
    return result, calls


class TestTendAutoPromote:
    def test_trusted_bookmark_auto_promoted(self, tmp_vault, monkeypatch):
        """A trusted bookmark satisfying all gates is auto-promoted, intake removed."""
        from sabnyx.commands import tend as tend_mod

        intake_dir = tmp_vault["INTAKE"]
        meta = _good_meta("bookmark", source_agent="sabnyx-ingest")
        _make_intake(intake_dir, "my-bookmark", meta)

        monkeypatch.setenv("SABNYX_TRUSTED_SOURCES", "sabnyx-ingest")

        runner = CliRunner()
        result = runner.invoke(
            tend_mod.tend_cmd,
            ["--no-reweave", "--no-health"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output

        # intake file removed
        assert not (intake_dir / "my-bookmark.md").exists()
        # atlas file created
        atlas_file = tmp_vault["ATLAS_REFERENCES"] / "my-bookmark.md"
        assert atlas_file.exists()

        # ledger has atlas.promoted
        kinds = [e.kind for e in ledger.replay()]
        assert "atlas.promoted" in kinds

    def test_untrusted_bookmark_deferred(self, tmp_vault, monkeypatch):
        from sabnyx.commands import tend as tend_mod

        intake_dir = tmp_vault["INTAKE"]
        meta = _good_meta("bookmark", source_agent="untrusted-agent")
        _make_intake(intake_dir, "untrusted-bookmark", meta)

        monkeypatch.setenv("SABNYX_TRUSTED_SOURCES", "sabnyx-ingest")

        runner = CliRunner()
        result = runner.invoke(
            tend_mod.tend_cmd,
            ["--no-reweave", "--no-health"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output

        # file still in intake
        assert (intake_dir / "untrusted-bookmark.md").exists()
        # not in atlas
        assert not (tmp_vault["ATLAS_REFERENCES"] / "untrusted-bookmark.md").exists()

    def test_note_deferred_not_promoted(self, tmp_vault, monkeypatch):
        from sabnyx.commands import tend as tend_mod

        meta = _good_meta("note", source_agent="sabnyx-ingest")
        _make_intake(tmp_vault["INTAKE"], "my-note", meta)

        monkeypatch.setenv("SABNYX_TRUSTED_SOURCES", "sabnyx-ingest")

        runner = CliRunner()
        result = runner.invoke(
            tend_mod.tend_cmd,
            ["--no-reweave", "--no-health"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert (tmp_vault["INTAKE"] / "my-note.md").exists()


class TestTendDryRun:
    def test_dry_run_writes_nothing(self, tmp_vault, monkeypatch):
        from sabnyx.commands import tend as tend_mod

        intake_dir = tmp_vault["INTAKE"]
        meta = _good_meta("bookmark", source_agent="sabnyx-ingest")
        _make_intake(intake_dir, "dry-run-bookmark", meta)

        monkeypatch.setenv("SABNYX_TRUSTED_SOURCES", "sabnyx-ingest")

        runner = CliRunner()
        result = runner.invoke(
            tend_mod.tend_cmd,
            ["--dry-run", "--no-reweave", "--no-health"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output

        # intake file still present (dry run writes nothing)
        assert (intake_dir / "dry-run-bookmark.md").exists()
        # no atlas file created
        assert not (tmp_vault["ATLAS_REFERENCES"] / "dry-run-bookmark.md").exists()

        # no atlas.promoted event (dry run writes no ledger mutations either,
        # except the tend.completed projection event)
        kinds = [e.kind for e in ledger.replay()]
        assert "atlas.promoted" not in kinds

    def test_dry_run_skips_llm_triage(self, tmp_vault, monkeypatch):
        from sabnyx import services as svc_mod
        from sabnyx.commands import tend as tend_mod

        # Put 5+ deferred items so threshold is met
        for i in range(6):
            meta = _good_meta("note", source_agent="sabnyx-ingest")
            _make_intake(tmp_vault["INTAKE"], f"note-{i}", meta)

        monkeypatch.setenv("SABNYX_TRUSTED_SOURCES", "sabnyx-ingest")
        calls: list = []

        def fake_llm(**kwargs):
            calls.append(kwargs)
            return TriageBatch(items=[])

        monkeypatch.setattr(svc_mod.llm, "parse_structured", fake_llm)

        runner = CliRunner()
        result = runner.invoke(
            tend_mod.tend_cmd,
            ["--dry-run", "--threshold", "5", "--no-reweave", "--no-health"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        # no LLM call on dry run
        assert calls == []


# ---------------------------------------------------------------------------
# tend command — LLM triage tier
# ---------------------------------------------------------------------------


class TestTendLLMTriage:
    def test_llm_tier_triggered_at_threshold(self, tmp_vault, monkeypatch):
        from sabnyx import services as svc_mod
        from sabnyx.commands import tend as tend_mod

        # Put exactly threshold (5) notes (deferred, since notes go to LLM)
        for i in range(5):
            meta = _good_meta("note", source_agent="sabnyx-ingest")
            _make_intake(tmp_vault["INTAKE"], f"note-{i}", meta)

        monkeypatch.setenv("SABNYX_TRUSTED_SOURCES", "sabnyx-ingest")

        calls: list = []
        batch = TriageBatch(
            items=[
                TriageItem(slug=f"note-{i}", action="keep", atlas_type=None, reason="not ready")
                for i in range(5)
            ]
        )

        def fake_llm(**kwargs):
            calls.append(kwargs)
            return batch

        monkeypatch.setattr(svc_mod.llm, "parse_structured", fake_llm)

        runner = CliRunner()
        result = runner.invoke(
            tend_mod.tend_cmd,
            ["--threshold", "5", "--yes", "--no-reweave", "--no-health"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        # LLM was called (one call for the triage batch)
        assert len(calls) == 1
        assert calls[0]["output_format"] is TriageBatch

    def test_llm_tier_not_triggered_below_threshold(self, tmp_vault, monkeypatch):
        from sabnyx import services as svc_mod
        from sabnyx.commands import tend as tend_mod

        # Only 3 deferred items, threshold=5
        for i in range(3):
            meta = _good_meta("note", source_agent="sabnyx-ingest")
            _make_intake(tmp_vault["INTAKE"], f"note-{i}", meta)

        monkeypatch.setenv("SABNYX_TRUSTED_SOURCES", "sabnyx-ingest")

        calls: list = []

        def fake_llm(**kwargs):
            calls.append(kwargs)
            return TriageBatch(items=[])

        monkeypatch.setattr(svc_mod.llm, "parse_structured", fake_llm)

        runner = CliRunner()
        result = runner.invoke(
            tend_mod.tend_cmd,
            ["--threshold", "5", "--yes", "--no-reweave", "--no-health"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        # No LLM call below threshold
        assert calls == []

    def test_llm_promote_decision_applies_with_yes(self, tmp_vault, monkeypatch):
        from sabnyx import services as svc_mod
        from sabnyx.commands import tend as tend_mod

        for i in range(5):
            meta = _good_meta("note", source_agent="sabnyx-ingest")
            _make_intake(tmp_vault["INTAKE"], f"note-{i}", meta)

        # LLM decides to promote note-0 as a concept
        batch = TriageBatch(
            items=[
                TriageItem(
                    slug="note-0", action="promote", atlas_type="concept", reason="durable insight"
                ),
                *[
                    TriageItem(slug=f"note-{i}", action="keep", atlas_type=None, reason="not ready")
                    for i in range(1, 5)
                ],
            ]
        )

        monkeypatch.setenv("SABNYX_TRUSTED_SOURCES", "sabnyx-ingest")

        def fake_llm(**kwargs):
            return batch

        monkeypatch.setattr(svc_mod.llm, "parse_structured", fake_llm)

        runner = CliRunner()
        result = runner.invoke(
            tend_mod.tend_cmd,
            ["--threshold", "5", "--yes", "--no-reweave", "--no-health"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output

        # note-0 promoted to atlas/concepts
        assert (tmp_vault["ATLAS_CONCEPTS"] / "note-0.md").exists()
        assert not (tmp_vault["INTAKE"] / "note-0.md").exists()

    def test_llm_discard_does_not_delete_file(self, tmp_vault, monkeypatch):
        from sabnyx import services as svc_mod
        from sabnyx.commands import tend as tend_mod

        for i in range(5):
            meta = _good_meta("note", source_agent="sabnyx-ingest")
            _make_intake(tmp_vault["INTAKE"], f"note-{i}", meta)

        batch = TriageBatch(
            items=[
                TriageItem(slug="note-0", action="discard", atlas_type=None, reason="noise"),
                *[
                    TriageItem(slug=f"note-{i}", action="keep", atlas_type=None, reason="ok")
                    for i in range(1, 5)
                ],
            ]
        )

        monkeypatch.setenv("SABNYX_TRUSTED_SOURCES", "sabnyx-ingest")

        def fake_llm(**kwargs):
            return batch

        monkeypatch.setattr(svc_mod.llm, "parse_structured", fake_llm)

        runner = CliRunner()
        result = runner.invoke(
            tend_mod.tend_cmd,
            ["--threshold", "5", "--yes", "--no-reweave", "--no-health"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output

        # discard is noted only — file still present
        assert (tmp_vault["INTAKE"] / "note-0.md").exists()
        assert "discard (noted, not deleted)" in result.output

    def test_llm_promote_of_duplicate_slug_is_skipped(self, tmp_vault, monkeypatch):
        """C1: gate-5 deferrals reach triage and the LLM never sees the atlas
        index — a 'promote' for an existing slug must be skipped, not applied."""
        from sabnyx import services as svc_mod
        from sabnyx.commands import tend as tend_mod

        existing = _make_atlas(tmp_vault["ATLAS_CONCEPTS"], "note-0")
        original = existing.read_text()
        for i in range(5):
            _make_intake(tmp_vault["INTAKE"], f"note-{i}", _good_meta("note"))

        batch = TriageBatch(
            items=[
                TriageItem(slug="note-0", action="promote", atlas_type="concept", reason="dup"),
                *[
                    TriageItem(slug=f"note-{i}", action="keep", atlas_type=None, reason="ok")
                    for i in range(1, 5)
                ],
            ]
        )
        monkeypatch.setenv("SABNYX_TRUSTED_SOURCES", "sabnyx-ingest")
        monkeypatch.setattr(svc_mod.llm, "parse_structured", lambda **kw: batch)

        runner = CliRunner()
        result = runner.invoke(
            tend_mod.tend_cmd,
            ["--threshold", "5", "--yes", "--no-reweave", "--no-health"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert "skip promote" in result.output

        assert existing.read_text() == original  # atlas file untouched
        assert (tmp_vault["INTAKE"] / "note-0.md").exists()  # kept in intake
        assert "atlas.promoted" not in [e.kind for e in ledger.replay()]

    def test_keep_and_discard_decisions_are_ledgered(self, tmp_vault, monkeypatch):
        """DEC-02: 'note it' must survive an unattended run — as ledger events,
        not console output alone."""
        from sabnyx import services as svc_mod
        from sabnyx.commands import tend as tend_mod

        for i in range(5):
            _make_intake(tmp_vault["INTAKE"], f"note-{i}", _good_meta("note"))

        batch = TriageBatch(
            items=[
                TriageItem(slug="note-0", action="discard", atlas_type=None, reason="noise"),
                *[
                    TriageItem(slug=f"note-{i}", action="keep", atlas_type=None, reason="ok")
                    for i in range(1, 5)
                ],
            ]
        )
        monkeypatch.setenv("SABNYX_TRUSTED_SOURCES", "sabnyx-ingest")
        monkeypatch.setattr(svc_mod.llm, "parse_structured", lambda **kw: batch)

        runner = CliRunner()
        result = runner.invoke(
            tend_mod.tend_cmd,
            ["--threshold", "5", "--yes", "--no-reweave", "--no-health"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output

        decided = [e for e in ledger.replay() if e.kind == "triage.decided"]
        assert len(decided) == 5
        by_slug = {e.payload["slug"]: e for e in decided}
        assert by_slug["note-0"].payload["action"] == "discard"
        assert by_slug["note-0"].payload["reason"] == "noise"
        assert by_slug["note-0"].subject == "vault/intake/note-0.md"
        assert by_slug["note-1"].payload["action"] == "keep"


class TestTendRobustness:
    def test_malformed_yaml_intake_does_not_abort_run(self, tmp_vault, monkeypatch):
        """M3: one unparseable file defers; the rest of the run proceeds."""
        from sabnyx.commands import tend as tend_mod

        (tmp_vault["INTAKE"] / "broken.md").write_text(
            "---\ntype: note\ntags: [unclosed\n---\nbody\n", encoding="utf-8"
        )
        _make_intake(tmp_vault["INTAKE"], "good-bookmark", _good_meta("bookmark"))
        monkeypatch.setenv("SABNYX_TRUSTED_SOURCES", "sabnyx-ingest")

        runner = CliRunner()
        result = runner.invoke(
            tend_mod.tend_cmd, ["--no-reweave", "--no-health"], catch_exceptions=False
        )
        assert result.exit_code == 0, result.output
        assert "skip (invalid)" in result.output
        # the good file was still promoted and the run completed
        assert (tmp_vault["ATLAS_REFERENCES"] / "good-bookmark.md").exists()
        assert "tend complete" in result.output
        assert "tend.completed" in [e.kind for e in ledger.replay()]

    def test_llm_promote_of_invalid_file_reports_and_continues(self, tmp_vault, monkeypatch):
        """M3: promote_file raising FrontmatterError (not RuntimeError) in
        pass 2 must not kill the run before tend.completed."""
        from sabnyx import services as svc_mod
        from sabnyx.commands import tend as tend_mod

        for i in range(4):
            _make_intake(tmp_vault["INTAKE"], f"note-{i}", _good_meta("note"))
        bad_meta = _good_meta("note", description="too short")  # schema-invalid
        bad = _make_intake(tmp_vault["INTAKE"], "bad-note", bad_meta)

        batch = TriageBatch(
            items=[
                TriageItem(slug="bad-note", action="promote", atlas_type="concept", reason="r"),
                *[
                    TriageItem(slug=f"note-{i}", action="keep", atlas_type=None, reason="ok")
                    for i in range(4)
                ],
            ]
        )
        monkeypatch.setenv("SABNYX_TRUSTED_SOURCES", "sabnyx-ingest")
        monkeypatch.setattr(svc_mod.llm, "parse_structured", lambda **kw: batch)

        runner = CliRunner()
        result = runner.invoke(
            tend_mod.tend_cmd,
            ["--threshold", "5", "--yes", "--no-reweave", "--no-health"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert "promote failed" in result.output
        assert bad.exists()  # nothing destructive happened to the source
        assert "tend.completed" in [e.kind for e in ledger.replay()]

    def test_promote_prompt_without_tty_keeps_and_completes(self, tmp_vault, monkeypatch):
        """Non-tty without --yes (cron): skip/keep with a note, don't Abort."""
        from sabnyx import services as svc_mod
        from sabnyx.commands import tend as tend_mod

        for i in range(5):
            _make_intake(tmp_vault["INTAKE"], f"note-{i}", _good_meta("note"))

        batch = TriageBatch(
            items=[
                TriageItem(slug="note-0", action="promote", atlas_type="concept", reason="r"),
                *[
                    TriageItem(slug=f"note-{i}", action="keep", atlas_type=None, reason="ok")
                    for i in range(1, 5)
                ],
            ]
        )
        monkeypatch.setenv("SABNYX_TRUSTED_SOURCES", "sabnyx-ingest")
        monkeypatch.setattr(svc_mod.llm, "parse_structured", lambda **kw: batch)

        runner = CliRunner()
        # no --yes and no input stream: click.confirm hits EOF
        result = runner.invoke(
            tend_mod.tend_cmd,
            ["--threshold", "5", "--no-reweave", "--no-health"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert "no interactive input" in result.output
        assert (tmp_vault["INTAKE"] / "note-0.md").exists()  # kept, not promoted
        assert "tend.completed" in [e.kind for e in ledger.replay()]

    def test_threshold_zero_with_empty_queue_makes_no_llm_call(self, tmp_vault, monkeypatch):
        from sabnyx import services as svc_mod
        from sabnyx.commands import tend as tend_mod

        calls: list = []

        def fake_llm(**kwargs):
            calls.append(kwargs)
            return TriageBatch(items=[])

        monkeypatch.setattr(svc_mod.llm, "parse_structured", fake_llm)

        runner = CliRunner()
        result = runner.invoke(
            tend_mod.tend_cmd,
            ["--threshold", "0", "--yes", "--no-reweave", "--no-health"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert calls == []  # zero deferred items → no LLM call, even at threshold 0


# ---------------------------------------------------------------------------
# tend.completed ledger event
# ---------------------------------------------------------------------------


class TestTendLedger:
    def test_tend_completed_event_emitted(self, tmp_vault, monkeypatch):
        from sabnyx.commands import tend as tend_mod

        meta = _good_meta("bookmark", source_agent="sabnyx-ingest")
        _make_intake(tmp_vault["INTAKE"], "ledger-bm", meta)

        monkeypatch.setenv("SABNYX_TRUSTED_SOURCES", "sabnyx-ingest")

        runner = CliRunner()
        result = runner.invoke(
            tend_mod.tend_cmd,
            ["--no-reweave", "--no-health"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output

        events = list(ledger.replay())
        completed = [e for e in events if e.kind == "tend.completed"]
        assert len(completed) == 1
        ev = completed[0]
        assert ev.payload["auto_promoted"] == 1
        assert ev.payload["dry_run"] is False

    def test_dry_run_writes_nothing(self, tmp_vault, monkeypatch):
        """Dry-run must make NO mutations: no promote, no ledger event."""
        from sabnyx.commands import tend as tend_mod

        meta = _good_meta("bookmark", source_agent="sabnyx-ingest")
        intake = _make_intake(tmp_vault["INTAKE"], "dry-bm", meta)

        monkeypatch.setenv("SABNYX_TRUSTED_SOURCES", "sabnyx-ingest")
        events_before = ledger.count()

        runner = CliRunner()
        result = runner.invoke(
            tend_mod.tend_cmd,
            ["--dry-run", "--no-reweave", "--no-health"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output

        # nothing promoted, source untouched, ledger unchanged
        assert intake.exists()
        assert ledger.count() == events_before
        assert not [e for e in ledger.replay() if e.kind == "tend.completed"]
        assert not [e for e in ledger.replay() if e.kind == "atlas.promoted"]
