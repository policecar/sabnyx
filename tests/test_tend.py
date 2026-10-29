"""Tests for Phase 1: the heartbeat — tend command, auto-promote gate, triage batch."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
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
    meta = {
        "type": intake_type,
        "description": description,
        "source": "web",
        "status": "draft",
        "created": datetime.now(UTC).isoformat(),
        "source_agent": source_agent,
    }
    if intake_type in ("bookmark", "paper"):
        meta["source_url"] = "https://example.com/source"  # schema-required (Gate 2)
    return meta


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


def _backdate_intake_created(tmp_vault, paths: list[Path], *, days: int) -> None:
    """Ledger an intake.created event `days` ago for each path, so age-triage
    tests don't need to sleep or monkeypatch time globally."""
    ts = datetime.now(UTC) - timedelta(days=days)
    for p in paths:
        ledger.append_event(
            kind="intake.created",
            actor="sabnyx-cli",
            subject=str(p.relative_to(tmp_vault["DATA_ROOT"])),
            payload={"type": "note", "description": "x", "slug": p.stem},
            ts=ts,
        )


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
        # one shared format for every operation: the Gate 3 grep contract
        # `grep "^# \[" knowledge-log.md | grep promote` must find promotions
        assert any(ln.startswith("# [") and "promote | log-test" in ln for ln in log.splitlines())

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


class TestTendLLMProvenanceGate:
    """The LLM triage tier must honour the same source_agent trust gate as
    the deterministic auto-promote pass (Gate 6) — an LLM "promote" verdict
    for an untrusted intake (telegram/web/r1 provenance) must never reach
    atlas/ unattended."""

    def test_untrusted_batch_all_deferred_not_promoted(self, tmp_vault, monkeypatch):
        from sabnyx import services as svc_mod
        from sabnyx.commands import tend as tend_mod

        for i in range(5):
            meta = _good_meta("note", source_agent="sabnyx-telegram")
            _make_intake(tmp_vault["INTAKE"], f"note-{i}", meta)

        batch = TriageBatch(
            items=[
                TriageItem(
                    slug=f"note-{i}", action="promote", atlas_type="concept", reason="looks durable"
                )
                for i in range(5)
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

        # nothing promoted; every file remains in intake
        assert not any(tmp_vault["ATLAS_CONCEPTS"].glob("*.md"))
        for i in range(5):
            assert (tmp_vault["INTAKE"] / f"note-{i}.md").exists()
        assert "atlas.promoted" not in [e.kind for e in ledger.replay()]

        # a ledger event per file records the untrusted deferral
        deferred_events = [
            e
            for e in ledger.replay()
            if e.kind == "triage.decided" and e.payload.get("decision") == "deferred_untrusted"
        ]
        assert len(deferred_events) == 5
        for e in deferred_events:
            assert e.payload["source_agent"] == "sabnyx-telegram"

    def test_trusted_batch_promotes(self, tmp_vault, monkeypatch):
        """Positive twin: trusted source_agent still promotes via the LLM tier."""
        from sabnyx import services as svc_mod
        from sabnyx.commands import tend as tend_mod

        for i in range(5):
            meta = _good_meta("note", source_agent="sabnyx-ingest")
            _make_intake(tmp_vault["INTAKE"], f"note-{i}", meta)

        batch = TriageBatch(
            items=[
                TriageItem(
                    slug=f"note-{i}", action="promote", atlas_type="concept", reason="looks durable"
                )
                for i in range(5)
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

        for i in range(5):
            assert (tmp_vault["ATLAS_CONCEPTS"] / f"note-{i}.md").exists()
            assert not (tmp_vault["INTAKE"] / f"note-{i}.md").exists()
        assert [e.kind for e in ledger.replay()].count("atlas.promoted") == 5

    def test_mixed_batch_only_trusted_promote(self, tmp_vault, monkeypatch):
        from sabnyx import services as svc_mod
        from sabnyx.commands import tend as tend_mod

        for i in range(3):
            meta = _good_meta("note", source_agent="sabnyx-ingest")
            _make_intake(tmp_vault["INTAKE"], f"trusted-{i}", meta)
        for i in range(3):
            meta = _good_meta("note", source_agent="sabnyx-r1")
            _make_intake(tmp_vault["INTAKE"], f"untrusted-{i}", meta)

        batch = TriageBatch(
            items=[
                TriageItem(
                    slug=f"trusted-{i}",
                    action="promote",
                    atlas_type="concept",
                    reason="durable",
                )
                for i in range(3)
            ]
            + [
                TriageItem(
                    slug=f"untrusted-{i}",
                    action="promote",
                    atlas_type="concept",
                    reason="durable",
                )
                for i in range(3)
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

        for i in range(3):
            assert (tmp_vault["ATLAS_CONCEPTS"] / f"trusted-{i}.md").exists()
            assert not (tmp_vault["INTAKE"] / f"trusted-{i}.md").exists()
        for i in range(3):
            assert not (tmp_vault["ATLAS_CONCEPTS"] / f"untrusted-{i}.md").exists()
            assert (tmp_vault["INTAKE"] / f"untrusted-{i}.md").exists()


def _keep_all_from_call(**kwargs) -> TriageBatch:
    """Fake parse_structured: 'keep' every slug actually sent in this call.

    Parses the slugs out of the rendered `user` prompt rather than hardcoding
    a fixed batch, so it stays correct across re-triage runs that send a
    shrinking subset of the original deferred queue.
    """
    slugs = re.findall(r"- slug: (\S+)", kwargs["user"])
    return TriageBatch(
        items=[TriageItem(slug=s, action="keep", atlas_type=None, reason="ok") for s in slugs]
    )


class TestTendRetriageMemory:
    """Pass 2 must remember prior triage.decided verdicts by content hash so
    an unattended tend loop doesn't re-roll a settled 'keep' every run."""

    def test_unchanged_items_skip_llm_on_rerun(self, tmp_vault, monkeypatch):
        from sabnyx import services as svc_mod
        from sabnyx.commands import tend as tend_mod

        for i in range(5):
            _make_intake(tmp_vault["INTAKE"], f"note-{i}", _good_meta("note"))
        monkeypatch.setenv("SABNYX_TRUSTED_SOURCES", "sabnyx-ingest")
        monkeypatch.setattr(svc_mod.llm, "parse_structured", _keep_all_from_call)

        runner = CliRunner()
        result = runner.invoke(
            tend_mod.tend_cmd,
            ["--threshold", "5", "--yes", "--no-reweave", "--no-health"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output

        decided = [e for e in ledger.replay() if e.kind == "triage.decided"]
        assert len(decided) == 5
        for e in decided:
            assert e.payload["content_hash"].startswith("sha256:")

        def fail_if_called(**kwargs):
            pytest.fail("LLM triage must not be called when nothing changed since last decision")

        monkeypatch.setattr(svc_mod.llm, "parse_structured", fail_if_called)

        result2 = runner.invoke(
            tend_mod.tend_cmd,
            ["--threshold", "5", "--yes", "--no-reweave", "--no-health"],
            catch_exceptions=False,
        )
        assert result2.exit_code == 0, result2.output
        assert "skipped 5 already-triaged item(s)" in result2.output

        # no new triage.decided events were appended
        decided_after = [e for e in ledger.replay() if e.kind == "triage.decided"]
        assert len(decided_after) == 5

    def test_only_changed_file_is_sent_on_rerun(self, tmp_vault, monkeypatch):
        from sabnyx import services as svc_mod
        from sabnyx.commands import tend as tend_mod

        paths = [
            _make_intake(tmp_vault["INTAKE"], f"note-{i}", _good_meta("note")) for i in range(5)
        ]
        monkeypatch.setenv("SABNYX_TRUSTED_SOURCES", "sabnyx-ingest")
        monkeypatch.setattr(svc_mod.llm, "parse_structured", _keep_all_from_call)

        runner = CliRunner()
        result = runner.invoke(
            tend_mod.tend_cmd,
            ["--threshold", "5", "--yes", "--no-reweave", "--no-health"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output

        # change note-0's content -> its content_hash no longer matches memory
        meta, body, _kind = fm.load(paths[0])
        fm.write(paths[0], meta, body + "\nedited after triage\n")

        calls: list = []

        def recording(**kwargs):
            calls.append(kwargs)
            return _keep_all_from_call(**kwargs)

        monkeypatch.setattr(svc_mod.llm, "parse_structured", recording)

        # threshold=1 so the single remaining (changed) item still clears the
        # LLM-tier gate even though 4 of the 5 deferred items were filtered.
        result2 = runner.invoke(
            tend_mod.tend_cmd,
            ["--threshold", "1", "--yes", "--no-reweave", "--no-health"],
            catch_exceptions=False,
        )
        assert result2.exit_code == 0, result2.output
        assert "skipped 4 already-triaged item(s)" in result2.output
        assert len(calls) == 1
        assert re.findall(r"- slug: (\S+)", calls[0]["user"]) == ["note-0"]

    def test_retriage_flag_forces_full_resend(self, tmp_vault, monkeypatch):
        from sabnyx import services as svc_mod
        from sabnyx.commands import tend as tend_mod

        for i in range(5):
            _make_intake(tmp_vault["INTAKE"], f"note-{i}", _good_meta("note"))
        monkeypatch.setenv("SABNYX_TRUSTED_SOURCES", "sabnyx-ingest")
        monkeypatch.setattr(svc_mod.llm, "parse_structured", _keep_all_from_call)

        runner = CliRunner()
        result = runner.invoke(
            tend_mod.tend_cmd,
            ["--threshold", "5", "--yes", "--no-reweave", "--no-health"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output

        calls: list = []

        def recording(**kwargs):
            calls.append(kwargs)
            return _keep_all_from_call(**kwargs)

        monkeypatch.setattr(svc_mod.llm, "parse_structured", recording)

        result2 = runner.invoke(
            tend_mod.tend_cmd,
            ["--threshold", "5", "--yes", "--no-reweave", "--no-health", "--retriage"],
            catch_exceptions=False,
        )
        assert result2.exit_code == 0, result2.output
        assert "already-triaged" not in result2.output
        assert len(calls) == 1
        assert len(re.findall(r"- slug: (\S+)", calls[0]["user"])) == 5

    def test_legacy_decision_without_content_hash_does_not_suppress(self, tmp_vault, monkeypatch):
        """A triage.decided event predating this feature (no content_hash in
        its payload) must count as 'no memory', never as a suppressing match."""
        from sabnyx.commands import tend as tend_mod

        paths = [
            _make_intake(tmp_vault["INTAKE"], f"note-{i}", _good_meta("note")) for i in range(5)
        ]
        monkeypatch.setenv("SABNYX_TRUSTED_SOURCES", "sabnyx-ingest")

        ledger.append_event(
            plane="projection",
            kind="triage.decided",
            actor="sabnyx-tend",
            subject=str(paths[0].relative_to(tmp_vault["DATA_ROOT"])),
            payload={"slug": "note-0", "action": "keep", "reason": "legacy decision, pre-hash"},
        )

        calls: list = []

        def recording(**kwargs):
            calls.append(kwargs)
            return _keep_all_from_call(**kwargs)

        from sabnyx import services as svc_mod

        monkeypatch.setattr(svc_mod.llm, "parse_structured", recording)

        runner = CliRunner()
        result = runner.invoke(
            tend_mod.tend_cmd,
            ["--threshold", "5", "--yes", "--no-reweave", "--no-health"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert "already-triaged" not in result.output
        assert len(calls) == 1
        assert len(re.findall(r"- slug: (\S+)", calls[0]["user"])) == 5


# ---------------------------------------------------------------------------
# _item_age_days — pure function, ledger -> frontmatter -> mtime priority
# ---------------------------------------------------------------------------


class TestItemAgeDays:
    def test_ledger_ts_wins_when_present(self, tmp_vault):
        """Tier 1: the ledger's intake.created ts, even if frontmatter's own
        `created` field disagrees."""
        from sabnyx.commands import tend as tend_mod

        p = _make_intake(tmp_vault["INTAKE"], "note-a", _good_meta("note"))
        now = datetime(2026, 1, 20, tzinfo=UTC)
        subject = str(p.relative_to(tmp_vault["DATA_ROOT"]))
        created_ts = {subject: now - timedelta(days=12, hours=12)}

        age = tend_mod._item_age_days(p, created_ts, now=now)
        assert age == pytest.approx(12.5, abs=1e-6)

    def test_frontmatter_created_used_when_no_ledger_entry(self, tmp_vault):
        """Tier 2a: frontmatter `created`, when there's no ledger memory for
        this subject (e.g. a file predating intake.created ledgering)."""
        from sabnyx.commands import tend as tend_mod

        now = datetime(2026, 1, 20, tzinfo=UTC)
        meta = _good_meta("note")
        meta["created"] = (now - timedelta(days=4)).isoformat()
        p = _make_intake(tmp_vault["INTAKE"], "note-b", meta)

        age = tend_mod._item_age_days(p, created_ts={}, now=now)
        assert age == pytest.approx(4.0, abs=1e-6)

    def test_frontmatter_source_date_used_when_created_absent(self, tmp_vault, monkeypatch):
        """Tier 2b: `source_date` fallback when `created` itself is absent
        from the loaded frontmatter dict.

        `IntakeFrontmatter.created` is schema-required, so a real file can
        never reach fm.load() with `created` missing — this exercises the
        _item_age_days fallback logic directly by stubbing fm.load's return,
        rather than asserting an unreachable on-disk state.
        """
        from sabnyx.commands import tend as tend_mod

        p = _make_intake(tmp_vault["INTAKE"], "note-c", _good_meta("note"))
        now = datetime(2026, 1, 20, tzinfo=UTC)
        fake_meta = {"source_date": (now - timedelta(days=9)).date()}
        monkeypatch.setattr(tend_mod.fm, "load", lambda path: (fake_meta, "", "intake"))

        age = tend_mod._item_age_days(p, created_ts={}, now=now)
        assert age == pytest.approx(9.0, abs=1e-6)

    def test_mtime_fallback_when_frontmatter_invalid(self, tmp_vault):
        """Tier 3: file mtime, last resort for invalid/malformed frontmatter.
        Invalid frontmatter must never feed its (untrusted) content into the
        age calculation — only the filesystem-level mtime."""
        import os

        from sabnyx.commands import tend as tend_mod

        p = tmp_vault["INTAKE"] / "broken.md"
        p.write_text("---\ntype: note\ntags: [unclosed\n---\nbody\n", encoding="utf-8")
        now = datetime.now(UTC)
        mtime = (now - timedelta(days=6)).timestamp()
        os.utime(p, (mtime, mtime))

        age = tend_mod._item_age_days(p, created_ts={}, now=now)
        assert age == pytest.approx(6.0, abs=0.01)

    def test_mtime_fallback_when_created_is_unparseable_string(self, tmp_vault, monkeypatch):
        """A non-ISO `created` string must fall to mtime, not raise and abort
        the whole tend run (PR #25 review)."""
        import os

        from sabnyx.commands import tend as tend_mod

        p = _make_intake(tmp_vault["INTAKE"], "note-d", _good_meta("note"))
        fake_meta = {"created": "not-a-date"}
        monkeypatch.setattr(tend_mod.fm, "load", lambda path: (fake_meta, "", "intake"))

        now = datetime.now(UTC)
        mtime = (now - timedelta(days=3)).timestamp()
        os.utime(p, (mtime, mtime))

        age = tend_mod._item_age_days(p, created_ts={}, now=now)
        assert age == pytest.approx(3.0, abs=0.01)


# ---------------------------------------------------------------------------
# tend pass 2 — age-based triage fallback (--max-age-days)
# ---------------------------------------------------------------------------


class TestTendAgeTriageFallback:
    """Below --threshold, pass 2 must still fire once the OLDEST item in
    to_triage (via _item_age_days) is >= --max-age-days. Age is a trigger
    over the whole queue (plan's triage_due semantics), not a per-item
    filter: once triggered, the entire (already-filtered) to_triage queue is
    sent in one batched call."""

    def test_fresh_items_below_threshold_no_age_trigger(self, tmp_vault, monkeypatch):
        from sabnyx import services as svc_mod
        from sabnyx.commands import tend as tend_mod

        for i in range(3):
            _make_intake(tmp_vault["INTAKE"], f"note-{i}", _good_meta("note"))
        monkeypatch.setenv("SABNYX_TRUSTED_SOURCES", "sabnyx-ingest")

        def fail_if_called(**kwargs):
            pytest.fail("LLM triage must not fire: below threshold, nothing is aged")

        monkeypatch.setattr(svc_mod.llm, "parse_structured", fail_if_called)

        runner = CliRunner()
        result = runner.invoke(
            tend_mod.tend_cmd,
            ["--threshold", "5", "--yes", "--no-reweave", "--no-health"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert "oldest deferred item" not in result.output
        assert "below threshold (3 < 5)" in result.output
        assert not [e for e in ledger.replay() if e.kind == "triage.decided"]

    def test_backdated_items_triaged_below_threshold_by_default_max_age(
        self, tmp_vault, monkeypatch
    ):
        from sabnyx import services as svc_mod
        from sabnyx.commands import tend as tend_mod

        paths = [
            _make_intake(tmp_vault["INTAKE"], f"note-{i}", _good_meta("note")) for i in range(3)
        ]
        _backdate_intake_created(tmp_vault, paths, days=10)
        monkeypatch.setenv("SABNYX_TRUSTED_SOURCES", "sabnyx-ingest")

        calls: list = []

        def recording(**kwargs):
            calls.append(kwargs)
            return _keep_all_from_call(**kwargs)

        monkeypatch.setattr(svc_mod.llm, "parse_structured", recording)

        runner = CliRunner()
        result = runner.invoke(
            tend_mod.tend_cmd,
            ["--threshold", "5", "--yes", "--no-reweave", "--no-health"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert "oldest deferred item is 10.0 day(s) old" in result.output
        assert "→ triaging 3 below threshold" in " ".join(result.output.split())
        assert len(calls) == 1
        assert len(re.findall(r"- slug: (\S+)", calls[0]["user"])) == 3

        decided = [e for e in ledger.replay() if e.kind == "triage.decided"]
        assert len(decided) == 3
        completed = [e for e in ledger.replay() if e.kind == "tend.completed"][-1]
        assert completed.payload["triage_trigger"] == "age"

    def test_max_age_days_zero_disables_age_fallback(self, tmp_vault, monkeypatch):
        from sabnyx import services as svc_mod
        from sabnyx.commands import tend as tend_mod

        paths = [
            _make_intake(tmp_vault["INTAKE"], f"note-{i}", _good_meta("note")) for i in range(3)
        ]
        _backdate_intake_created(tmp_vault, paths, days=10)
        monkeypatch.setenv("SABNYX_TRUSTED_SOURCES", "sabnyx-ingest")

        def fail_if_called(**kwargs):
            pytest.fail("--max-age-days 0 must disable the age fallback entirely")

        monkeypatch.setattr(svc_mod.llm, "parse_structured", fail_if_called)

        runner = CliRunner()
        result = runner.invoke(
            tend_mod.tend_cmd,
            [
                "--threshold",
                "5",
                "--max-age-days",
                "0",
                "--yes",
                "--no-reweave",
                "--no-health",
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert "oldest deferred item" not in result.output
        assert not [e for e in ledger.replay() if e.kind == "triage.decided"]

    def test_mixed_ages_sends_the_whole_deferred_batch(self, tmp_vault, monkeypatch):
        from sabnyx import services as svc_mod
        from sabnyx.commands import tend as tend_mod

        old_paths = [
            _make_intake(tmp_vault["INTAKE"], f"old-{i}", _good_meta("note")) for i in range(2)
        ]
        fresh_paths = [
            _make_intake(tmp_vault["INTAKE"], f"fresh-{i}", _good_meta("note")) for i in range(2)
        ]
        _backdate_intake_created(tmp_vault, old_paths, days=10)
        monkeypatch.setenv("SABNYX_TRUSTED_SOURCES", "sabnyx-ingest")

        calls: list = []

        def recording(**kwargs):
            calls.append(kwargs)
            return _keep_all_from_call(**kwargs)

        monkeypatch.setattr(svc_mod.llm, "parse_structured", recording)

        runner = CliRunner()
        result = runner.invoke(
            tend_mod.tend_cmd,
            ["--threshold", "5", "--yes", "--no-reweave", "--no-health"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert "oldest deferred item is 10.0 day(s) old" in result.output
        assert "→ triaging 4 below threshold" in " ".join(result.output.split())
        assert len(calls) == 1
        # age is a trigger over the whole to_triage queue, not a per-item
        # filter (plan's triage_due semantics) — one old item past
        # --max-age-days sends the entire deferred batch, fresh items
        # included, in one batched call.
        sent_slugs = sorted(re.findall(r"- slug: (\S+)", calls[0]["user"]))
        assert sent_slugs == ["fresh-0", "fresh-1", "old-0", "old-1"]

        decided_subjects = {e.subject for e in ledger.replay() if e.kind == "triage.decided"}
        assert decided_subjects == {
            str(p.relative_to(tmp_vault["DATA_ROOT"])) for p in old_paths + fresh_paths
        }

    def test_already_triaged_old_item_still_skipped(self, tmp_vault, monkeypatch):
        """The retriage-memory invariant holds regardless of age: an old item
        with a matching content_hash in its last triage.decided event is
        still filtered out before the age check ever sees it."""
        from sabnyx import services as svc_mod
        from sabnyx.commands import tend as tend_mod

        paths = [
            _make_intake(tmp_vault["INTAKE"], f"note-{i}", _good_meta("note")) for i in range(3)
        ]
        _backdate_intake_created(tmp_vault, paths, days=10)
        monkeypatch.setenv("SABNYX_TRUSTED_SOURCES", "sabnyx-ingest")
        monkeypatch.setattr(svc_mod.llm, "parse_structured", _keep_all_from_call)

        runner = CliRunner()
        result = runner.invoke(
            tend_mod.tend_cmd,
            ["--threshold", "5", "--yes", "--no-reweave", "--no-health"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert "oldest deferred item is 10.0 day(s) old" in result.output
        decided = [e for e in ledger.replay() if e.kind == "triage.decided"]
        assert len(decided) == 3

        def fail_if_called(**kwargs):
            pytest.fail(
                "an old item unchanged since its last triage.decided decision must "
                "still be filtered before the age check"
            )

        monkeypatch.setattr(svc_mod.llm, "parse_structured", fail_if_called)

        result2 = runner.invoke(
            tend_mod.tend_cmd,
            ["--threshold", "5", "--yes", "--no-reweave", "--no-health"],
            catch_exceptions=False,
        )
        assert result2.exit_code == 0, result2.output
        assert "skipped 3 already-triaged item(s)" in result2.output
        assert "oldest deferred item" not in result2.output


# ---------------------------------------------------------------------------
# PR #25 review: every LLM-triaged item must get a triage.decided record,
# or an unanswered/skipped item re-fires the whole-queue LLM call on every
# subsequent heartbeat once the age fallback has tripped once.
# ---------------------------------------------------------------------------


class TestTendUndecidedTriageMemory:
    def test_omitted_slug_recorded_as_unanswered_and_not_resent(self, tmp_vault, monkeypatch):
        from sabnyx import services as svc_mod
        from sabnyx.commands import tend as tend_mod

        paths = [
            _make_intake(tmp_vault["INTAKE"], f"note-{i}", _good_meta("note")) for i in range(3)
        ]
        _backdate_intake_created(tmp_vault, paths, days=10)
        monkeypatch.setenv("SABNYX_TRUSTED_SOURCES", "sabnyx-ingest")

        # the LLM batch omits note-2 entirely
        batch = TriageBatch(
            items=[
                TriageItem(slug="note-0", action="keep", atlas_type=None, reason="ok"),
                TriageItem(slug="note-1", action="keep", atlas_type=None, reason="ok"),
            ]
        )
        monkeypatch.setattr(svc_mod.llm, "parse_structured", lambda **kw: batch)

        runner = CliRunner()
        result = runner.invoke(
            tend_mod.tend_cmd,
            ["--threshold", "5", "--yes", "--no-reweave", "--no-health"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert "unanswered" in result.output

        decided = {e.payload["slug"]: e for e in ledger.replay() if e.kind == "triage.decided"}
        assert set(decided) == {"note-0", "note-1", "note-2"}
        assert decided["note-2"].payload["decision"] == "unanswered"
        assert decided["note-2"].payload["content_hash"].startswith("sha256:")
        assert decided["note-2"].subject == str(paths[2].relative_to(tmp_vault["DATA_ROOT"]))

        def fail_if_called(**kwargs):
            pytest.fail("the age fallback must not re-fire for an unanswered item")

        monkeypatch.setattr(svc_mod.llm, "parse_structured", fail_if_called)

        result2 = runner.invoke(
            tend_mod.tend_cmd,
            ["--threshold", "5", "--yes", "--no-reweave", "--no-health"],
            catch_exceptions=False,
        )
        assert result2.exit_code == 0, result2.output

    def test_atlas_dup_promote_recorded_and_not_resent(self, tmp_vault, monkeypatch):
        from sabnyx import services as svc_mod
        from sabnyx.commands import tend as tend_mod

        _make_atlas(tmp_vault["ATLAS_CONCEPTS"], "note-0")
        paths = [
            _make_intake(tmp_vault["INTAKE"], f"note-{i}", _good_meta("note")) for i in range(3)
        ]
        _backdate_intake_created(tmp_vault, paths, days=10)
        monkeypatch.setenv("SABNYX_TRUSTED_SOURCES", "sabnyx-ingest")

        batch = TriageBatch(
            items=[
                TriageItem(slug="note-0", action="promote", atlas_type="concept", reason="dup"),
                TriageItem(slug="note-1", action="keep", atlas_type=None, reason="ok"),
                TriageItem(slug="note-2", action="keep", atlas_type=None, reason="ok"),
            ]
        )
        monkeypatch.setattr(svc_mod.llm, "parse_structured", lambda **kw: batch)

        runner = CliRunner()
        result = runner.invoke(
            tend_mod.tend_cmd,
            ["--threshold", "5", "--yes", "--no-reweave", "--no-health"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output

        decided = {e.payload["slug"]: e for e in ledger.replay() if e.kind == "triage.decided"}
        assert decided["note-0"].payload["decision"] == "skipped_atlas_dup"
        assert (tmp_vault["INTAKE"] / "note-0.md").exists()  # kept in intake, not promoted

        def fail_if_called(**kwargs):
            pytest.fail("the age fallback must not re-fire for an already-recorded atlas-dup skip")

        monkeypatch.setattr(svc_mod.llm, "parse_structured", fail_if_called)

        result2 = runner.invoke(
            tend_mod.tend_cmd,
            ["--threshold", "5", "--yes", "--no-reweave", "--no-health"],
            catch_exceptions=False,
        )
        assert result2.exit_code == 0, result2.output

    def test_retriage_still_resends_unanswered_and_atlas_dup_items(self, tmp_vault, monkeypatch):
        from sabnyx import services as svc_mod
        from sabnyx.commands import tend as tend_mod

        _make_atlas(tmp_vault["ATLAS_CONCEPTS"], "note-0")
        paths = [
            _make_intake(tmp_vault["INTAKE"], f"note-{i}", _good_meta("note")) for i in range(3)
        ]
        _backdate_intake_created(tmp_vault, paths, days=10)
        monkeypatch.setenv("SABNYX_TRUSTED_SOURCES", "sabnyx-ingest")

        first_batch = TriageBatch(
            items=[
                TriageItem(slug="note-0", action="promote", atlas_type="concept", reason="dup"),
                TriageItem(slug="note-1", action="keep", atlas_type=None, reason="ok"),
                # note-2 omitted -> recorded "unanswered"
            ]
        )
        monkeypatch.setattr(svc_mod.llm, "parse_structured", lambda **kw: first_batch)

        runner = CliRunner()
        result = runner.invoke(
            tend_mod.tend_cmd,
            ["--threshold", "5", "--yes", "--no-reweave", "--no-health"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output

        calls: list = []

        def recording(**kwargs):
            calls.append(kwargs)
            return _keep_all_from_call(**kwargs)

        monkeypatch.setattr(svc_mod.llm, "parse_structured", recording)

        result2 = runner.invoke(
            tend_mod.tend_cmd,
            ["--threshold", "5", "--yes", "--no-reweave", "--no-health", "--retriage"],
            catch_exceptions=False,
        )
        assert result2.exit_code == 0, result2.output
        assert len(calls) == 1
        sent_slugs = sorted(re.findall(r"- slug: (\S+)", calls[0]["user"]))
        assert sent_slugs == ["note-0", "note-1", "note-2"]


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

        # A non-tty abort must NOT settle the item as "declined" — the
        # operator was never actually asked, so nothing is ledgered for
        # note-0, and it must be re-offered on the next run (PR #25
        # re-review), unlike the 4 settled "keep" decisions.
        decided = [e for e in ledger.replay() if e.kind == "triage.decided"]
        assert {e.payload["slug"] for e in decided} == {"note-1", "note-2", "note-3", "note-4"}

        calls: list = []

        def recording(**kwargs):
            calls.append(kwargs)
            return TriageBatch(
                items=[
                    TriageItem(slug="note-0", action="promote", atlas_type="concept", reason="r")
                ]
            )

        monkeypatch.setattr(svc_mod.llm, "parse_structured", recording)

        # note-1..4 are now filtered as already-triaged, so note-0 alone
        # clears a lowered threshold — proving it's still in the deferred
        # pool, not quietly settled.
        result2 = runner.invoke(
            tend_mod.tend_cmd,
            ["--threshold", "1", "--no-reweave", "--no-health"],
            catch_exceptions=False,
        )
        assert result2.exit_code == 0, result2.output
        assert len(calls) == 1
        assert re.findall(r"- slug: (\S+)", calls[0]["user"]) == ["note-0"]

    def test_declined_promote_is_recorded_and_not_resent(self, tmp_vault, monkeypatch):
        """Contrast with the non-tty-abort case above: an explicit
        interactive 'no' DOES settle the item as declined (PR #25
        re-review)."""
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
        # An explicit interactive "no" (not a non-tty EOF/Abort).
        monkeypatch.setattr(tend_mod.click, "confirm", lambda *a, **k: False)

        runner = CliRunner()
        result = runner.invoke(
            tend_mod.tend_cmd,
            ["--threshold", "5", "--no-reweave", "--no-health"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert (tmp_vault["INTAKE"] / "note-0.md").exists()  # declined, not promoted

        decided = {e.payload["slug"]: e for e in ledger.replay() if e.kind == "triage.decided"}
        assert decided["note-0"].payload["decision"] == "declined"

        def fail_if_called(**kwargs):
            pytest.fail("a declined item must not be resent until content changes or --retriage")

        monkeypatch.setattr(svc_mod.llm, "parse_structured", fail_if_called)

        result2 = runner.invoke(
            tend_mod.tend_cmd,
            ["--threshold", "1", "--no-reweave", "--no-health"],
            catch_exceptions=False,
        )
        assert result2.exit_code == 0, result2.output

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


# ---------------------------------------------------------------------------
# Abort record + anomaly trips (report-hygiene, B4)
#
# Why: a heartbeat that dies on its LLM budget or a failing reweave used to
# leave no trace but a non-zero exit. The abort record makes those runs
# countable; the trip record makes a persisting problem visible once, not
# once per tick. The guarantees below are what keep that record honest:
# the original exception always wins, the ledger's own failures are never
# answered with another append, and --dry-run writes nothing.
# ---------------------------------------------------------------------------


def _tend_event(kind: str, subject: str = "vault/", **payload) -> None:
    from sabnyx.services import ACTOR_TEND

    ledger.append_event(
        plane="projection", kind=kind, actor=ACTOR_TEND, subject=subject, payload=payload
    )


def _kinds(kind: str) -> list:
    return [e for e in ledger.replay() if e.kind == kind]


def _invoke_tend(args: list[str]):
    from sabnyx.commands import tend as tend_mod

    return CliRunner().invoke(tend_mod.tend_cmd, args)


def _raise_in(monkeypatch, name: str, exc: BaseException) -> None:
    """Make tend's helper `name` raise `exc` (pass 2: _ledger_memory,
    pass 3: _reweave_pending)."""
    from sabnyx.commands import tend as tend_mod

    def boom(*_a, **_k):
        raise exc

    monkeypatch.setattr(tend_mod, name, boom)


class TestTendAbort:
    def _budget_stub(self, monkeypatch, tmp_vault) -> None:
        """A real budget of 1 and a triage pass that makes two model calls
        through llm.parse_structured (provider stubbed): the second call is
        refused by the budget itself."""
        from sabnyx import llm
        from sabnyx.commands import tend as tend_mod
        from sabnyx.services import ACTOR_TEND

        monkeypatch.setenv("SABNYX_LLM_PROVIDER", "moonshot")
        monkeypatch.setenv("SABNYX_MAX_LLM_CALLS", "1")
        monkeypatch.setattr(llm, "_call_openai_compatible", lambda **k: (TriageBatch(), {}))

        def two_calls(paths, *, role):
            for _ in range(2):
                llm.parse_structured(
                    task_system="t",
                    user="u",
                    output_format=TriageBatch,
                    role=role,
                    actor=ACTOR_TEND,
                    purpose="triage:batch",
                )
            return TriageBatch()

        monkeypatch.setattr(tend_mod, "triage_batch", two_calls)
        _make_intake(tmp_vault["INTAKE"], "made-up-lantern", _good_meta("note"))

    def test_budget_exhaustion_records_abort_and_reraises(self, tmp_vault, monkeypatch):
        from sabnyx import llm

        self._budget_stub(monkeypatch, tmp_vault)
        result = _invoke_tend(["--yes", "--threshold", "1", "--no-reweave", "--no-health"])

        assert isinstance(result.exception, llm.LLMBudgetError)
        assert result.exit_code != 0
        aborted = _kinds("tend.aborted")
        assert [e.payload for e in aborted] == [{"error": "LLMBudgetError", "pass": 2}]
        assert aborted[0].subject == "vault/"
        assert _kinds("tend.completed") == []

    def test_two_budget_aborts_trip_ceiling_once(self, tmp_vault, monkeypatch):
        self._budget_stub(monkeypatch, tmp_vault)
        args = ["--yes", "--threshold", "1", "--retriage", "--no-reweave", "--no-health"]
        _invoke_tend(args)
        assert _kinds("tend.anomaly") == []  # one hit is not a pattern
        _invoke_tend(args)
        _invoke_tend(args)  # persists (3 of 3): no second event
        anomalies = _kinds("tend.anomaly")
        assert [e.payload for e in anomalies] == [
            {"detector": "ceiling_hit", "subject": "vault/", "count": 2, "window": 2}
        ]
        assert len(_kinds("tend.aborted")) == 3

    def test_pass3_failure_records_pass_3_type_only(self, tmp_vault, monkeypatch):
        original = ValueError("made-up-marker-7f3a provider said something")
        _raise_in(monkeypatch, "_reweave_pending", original)
        result = _invoke_tend(["--no-health"])

        assert result.exception is original
        assert [e.payload for e in _kinds("tend.aborted")] == [{"error": "ValueError", "pass": 3}]
        # Only the type name is ledgered: the message can carry model output.
        assert "made-up-marker-7f3a" not in tmp_vault["EVENTS_JSONL"].read_text()

    def test_pass2_failure_records_pass_2(self, tmp_vault, monkeypatch):
        original = OSError("disk")
        _raise_in(monkeypatch, "_ledger_memory", original)
        result = _invoke_tend(["--no-health"])

        assert result.exception is original
        assert [e.payload for e in _kinds("tend.aborted")] == [{"error": "OSError", "pass": 2}]

    @pytest.mark.parametrize(
        "original",
        [ledger.LedgerError("bad line 3"), ledger.LedgerWriteError("short write")],
        ids=["LedgerError", "LedgerWriteError"],
    )
    def test_ledger_error_is_not_answered_with_an_append(self, tmp_vault, monkeypatch, original):
        """The whole family bypasses the record, base class included: a guard
        narrowed to LedgerWriteError must fail the LedgerError case."""
        _raise_in(monkeypatch, "_reweave_pending", original)
        before = ledger.count()
        result = _invoke_tend(["--no-health"])

        assert result.exception is original
        assert _kinds("tend.aborted") == []
        assert ledger.count() == before

    @pytest.mark.parametrize(
        "interrupt", [KeyboardInterrupt(), ledger.LedgerWriteInterrupted()], ids=type
    )
    def test_interrupts_pass_through_untouched(self, tmp_vault, interrupt):
        """Direct on the guard: click turns a KeyboardInterrupt into Abort, so
        the CLI runner cannot show identity. Catch broadly, then assert."""
        from rich.console import Console

        from sabnyx.commands import tend as tend_mod

        before = ledger.count()
        caught: BaseException | None = None
        try:
            with tend_mod._AbortGuard(dry_run=False, console=Console()):
                raise interrupt
        except BaseException as e:  # noqa: BLE001 — asserting the type below
            caught = e
        assert caught is interrupt
        assert ledger.count() == before

    def test_interrupt_via_cli_writes_no_abort(self, tmp_vault, monkeypatch):
        _raise_in(monkeypatch, "_reweave_pending", KeyboardInterrupt())
        result = _invoke_tend(["--no-health"])
        assert result.exit_code != 0
        assert _kinds("tend.aborted") == []

    def test_detector_failure_does_not_replace_original(self, tmp_vault, monkeypatch):
        from sabnyx import tend_detectors

        original = ValueError("reweave broke")
        _raise_in(monkeypatch, "_reweave_pending", original)

        def broken(_events):
            raise RuntimeError("detector bug")

        monkeypatch.setattr(tend_detectors, "trips", broken)
        result = _invoke_tend(["--no-health"])

        assert result.exception is original
        assert len(_kinds("tend.aborted")) == 1
        assert "anomaly detection failed" in result.output

    def test_notice_print_failure_does_not_replace_original(self, tmp_vault, monkeypatch):
        from rich.console import Console

        from sabnyx.commands import tend as tend_mod

        class BrokenConsole(Console):
            def print(self, *_a, **_k):
                raise OSError("stdout gone")

        def broken(_console):
            raise RuntimeError("detector bug")

        monkeypatch.setattr(tend_mod, "_record_anomalies", broken)
        original = ValueError("reweave broke")
        caught: BaseException | None = None
        try:
            with tend_mod._AbortGuard(dry_run=False, console=BrokenConsole()):
                raise original
        except BaseException as e:  # noqa: BLE001 — asserting identity below
            caught = e
        assert caught is original
        assert len(_kinds("tend.aborted")) == 1

    def test_failed_abort_append_propagates_with_original_as_context(self, tmp_vault, monkeypatch):
        original = ValueError("reweave broke")
        _raise_in(monkeypatch, "_reweave_pending", original)
        append_failure = RuntimeError("append failed")
        attempts: list[str] = []
        real_append = ledger.append_event

        def flaky_append(**kwargs):
            if kwargs["kind"] == "tend.aborted":
                attempts.append(kwargs["kind"])
                raise append_failure
            return real_append(**kwargs)

        monkeypatch.setattr(ledger, "append_event", flaky_append)
        result = _invoke_tend(["--no-health"])

        assert result.exception is append_failure
        assert append_failure.__context__ is original
        assert attempts == ["tend.aborted"]  # never retried
        assert _kinds("tend.anomaly") == []

    def test_guard_tracks_pass(self):
        from rich.console import Console

        from sabnyx.commands import tend as tend_mod

        guard = tend_mod._AbortGuard(dry_run=True, console=Console())
        assert guard.pass_ == 2


class TestTendDryRunAnomalies:
    def test_exception_in_dry_run_writes_nothing(self, tmp_vault, monkeypatch):
        original = ValueError("prep broke")
        _raise_in(monkeypatch, "_ledger_memory", original)
        before = ledger.count()
        result = _invoke_tend(["--dry-run", "--no-health"])

        assert result.exception is original
        assert ledger.count() == before

    def test_dry_run_prints_anomalies_and_writes_nothing(self, tmp_vault):
        for _ in range(4):
            _tend_event("triage.decided", "vault/intake/made-up-heron.md", decision="keep")
            _tend_event("tend.completed")
        before = ledger.count()
        result = _invoke_tend(["--dry-run", "--no-reweave", "--no-health"])

        assert result.exit_code == 0, result.output
        assert ledger.count() == before
        assert "as of the last recorded tend run" in result.output
        assert "stuck_slug  vault/intake/made-up-heron.md  (4 consecutive runs)" in result.output


class TestTendAnomalyTrips:
    def test_completed_run_trips_once_then_clears(self, tmp_vault):
        slug = "vault/intake/made-up-heron.md"
        for _ in range(3):
            _tend_event("triage.decided", slug, decision="keep")
            _tend_event("tend.completed")
        # This run's own keep, recorded before its boundary.
        _tend_event("triage.decided", slug, decision="keep")
        result = _invoke_tend(["--no-reweave", "--no-health"])
        assert result.exit_code == 0, result.output
        assert [e.payload for e in _kinds("tend.anomaly")] == [
            {"detector": "stuck_slug", "subject": slug, "count": 4, "window": 4}
        ]
        assert _kinds("tend.anomaly")[0].subject == slug
        assert "stuck_slug" in result.output

        # Next tick has no keep for the slug: cleared, nothing new written.
        result = _invoke_tend(["--no-reweave", "--no-health"])
        assert result.exit_code == 0, result.output
        assert len(_kinds("tend.anomaly")) == 1
        assert "  none" in result.output

    def test_persisting_finding_is_printed_without_a_new_record(self, tmp_vault):
        # The report shows what is wrong now, not only what just tripped: a
        # finding that persists from an earlier run is printed again, and
        # this run writes no second record (the onset was an earlier run's).
        slug = "vault/intake/made-up-heron.md"
        for _ in range(4):
            _tend_event("triage.decided", slug, decision="keep")
            _tend_event("tend.completed")
        _tend_event("triage.decided", slug, decision="keep")
        result = _invoke_tend(["--no-reweave", "--no-health"])
        assert result.exit_code == 0, result.output
        assert _kinds("tend.anomaly") == []
        assert f"stuck_slug  {slug}  (5 consecutive runs)" in result.output
        assert "  none" not in result.output


def test_item_age_days_defaults_to_the_ledger_clock(tmp_vault, monkeypatch):
    from sabnyx.commands import tend as tend_mod

    p = _make_intake(tmp_vault["INTAKE"], "made-up-clock", _good_meta("note"))
    subject = str(p.relative_to(tmp_vault["DATA_ROOT"]))
    created = {subject: datetime(2030, 1, 1, tzinfo=UTC)}
    monkeypatch.setattr(ledger, "now", lambda: datetime(2030, 1, 4, tzinfo=UTC))
    assert tend_mod._item_age_days(p, created) == pytest.approx(3.0)
