"""Tests for Phase 3: the lightweight entity layer.

Covers the schema/path additions (person/org/project + aliases) and the
three `entities` subcommands: list, gaps, resolve.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from sabnyx import fm, ledger
from sabnyx.commands.entities import entities_cmd

DESC = "a deliberately long-enough description satisfying the 30-char minimum"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _atlas_meta(atlas_type: str, *, aliases: list[str] | None = None) -> dict:
    meta: dict = {
        "type": atlas_type,
        "description": DESC,
        "tags": [],
        "status": "final",
        "promoted_from": None,
        "promoted_date": None,
        "last_verified": None,
        "proof": None,
    }
    if aliases is not None:
        meta["aliases"] = aliases
    return meta


def _make_entity(
    dir_: Path, slug: str, atlas_type: str, *, aliases: list[str] | None = None
) -> Path:
    p = dir_ / f"{slug}.md"
    fm.write(p, _atlas_meta(atlas_type, aliases=aliases), f"# {slug}\n\n{DESC}\n")
    return p


def _make_referrer(dir_: Path, slug: str, target: str) -> Path:
    """A valid concept atlas file whose body references [[target]]."""
    p = dir_ / f"{slug}.md"
    fm.write(p, _atlas_meta("concept"), f"# {slug}\n\nSee [[{target}]] for context.\n")
    return p


def _run(args, **kwargs):
    runner = CliRunner()
    return runner.invoke(entities_cmd, args, catch_exceptions=False, **kwargs)


# ---------------------------------------------------------------------------
# Schema + paths
# ---------------------------------------------------------------------------


class TestEntitySchema:
    def test_atlas_accepts_entity_types(self):
        from sabnyx.schemas import AtlasFrontmatter

        for t in ("person", "org", "project"):
            m = AtlasFrontmatter.model_validate(_atlas_meta(t))
            assert m.type == t
            assert m.aliases == []

    def test_aliases_round_trip(self):
        from sabnyx.schemas import AtlasFrontmatter

        m = AtlasFrontmatter.model_validate(_atlas_meta("person", aliases=["JD", "Jane D."]))
        assert m.aliases == ["JD", "Jane D."]

    def test_knowledge_types_still_valid(self):
        from sabnyx.schemas import AtlasFrontmatter

        for t in ("concept", "reference", "idea"):
            assert AtlasFrontmatter.model_validate(_atlas_meta(t)).type == t

    def test_entity_page_classifies_as_atlas(self, tmp_vault):
        p = _make_entity(tmp_vault["ATLAS_PEOPLE"], "jane-doe", "person")
        meta, _body, kind = fm.load(p)
        assert kind == "atlas"
        assert meta["type"] == "person"


class TestEntityPaths:
    def test_entity_types_constant(self):
        from sabnyx.paths import ENTITY_TYPES

        assert set(ENTITY_TYPES) == {"person", "org", "project"}

    def test_atlas_type_dirs_include_entities(self):
        from sabnyx.paths import ATLAS_TYPE_DIRS

        for t in ("concept", "reference", "idea", "person", "org", "project"):
            assert t in ATLAS_TYPE_DIRS

    def test_ensure_tree_creates_entity_dirs(self, tmp_path, monkeypatch):
        from sabnyx import paths as paths_mod

        atlas = tmp_path / "vault" / "atlas"
        people, orgs, projects = atlas / "people", atlas / "orgs", atlas / "projects"
        patches = {
            "INTAKE": tmp_path / "vault" / "intake",
            "OBSERVATIONS": tmp_path / "vault" / "intake" / ".observations",
            "ATLAS_CONCEPTS": atlas / "concepts",
            "ATLAS_REFERENCES": atlas / "references",
            "ATLAS_IDEAS": atlas / "ideas",
            "ATLAS_PEOPLE": people,
            "ATLAS_ORGS": orgs,
            "ATLAS_PROJECTS": projects,
            "DOMAINS": tmp_path / "vault" / "domains",
            "REVIEW": tmp_path / "vault" / "_review",
            "SOURCES": tmp_path / "vault" / "sources",
            "OPS": tmp_path / "ops",
        }
        for name, sub in patches.items():
            monkeypatch.setattr(paths_mod, name, sub)

        paths_mod.ensure_tree()

        assert people.is_dir()
        assert orgs.is_dir()
        assert projects.is_dir()
        assert (people / ".gitkeep").exists()


# ---------------------------------------------------------------------------
# promote --as <entity type>
# ---------------------------------------------------------------------------


class TestPromoteEntity:
    def test_promote_as_person_lands_in_people_dir(self, tmp_vault):
        from sabnyx.services import promote_file

        intake = tmp_vault["INTAKE"]
        meta = {
            "type": "note",
            "description": DESC,
            "source": "manual",
            "tags": [],
            "status": "draft",
            "created": ledger.now().isoformat(),
            "source_agent": "sabnyx-cli",
        }
        src = intake / "jane-doe.md"
        fm.write(src, meta, "# Jane Doe\n\nnotes\n")

        result = promote_file(src, "person", actor="test")

        assert result == tmp_vault["ATLAS_PEOPLE"] / "jane-doe.md"
        assert result.exists()
        assert not src.exists()
        atlas_meta, _body, kind = fm.load(result)
        assert kind == "atlas"
        assert atlas_meta["type"] == "person"


# ---------------------------------------------------------------------------
# entities list
# ---------------------------------------------------------------------------


class TestEntitiesList:
    def test_empty_reports_no_pages(self, tmp_vault):
        result = _run(["list"])
        assert result.exit_code == 0, result.output
        assert "no entity pages yet" in result.output

    def test_lists_pages_with_type(self, tmp_vault):
        _make_entity(tmp_vault["ATLAS_PEOPLE"], "jane-doe", "person")
        _make_entity(tmp_vault["ATLAS_ORGS"], "acme-corp", "org")

        result = _run(["list"])
        assert result.exit_code == 0, result.output
        assert "jane-doe" in result.output
        assert "acme-corp" in result.output
        assert "person" in result.output
        assert "org" in result.output

    def test_filter_by_type(self, tmp_vault):
        _make_entity(tmp_vault["ATLAS_PEOPLE"], "jane-doe", "person")
        _make_entity(tmp_vault["ATLAS_ORGS"], "acme-corp", "org")

        result = _run(["list", "--type", "person"])
        assert result.exit_code == 0, result.output
        assert "jane-doe" in result.output
        assert "acme-corp" not in result.output


# ---------------------------------------------------------------------------
# entities gaps
# ---------------------------------------------------------------------------


class TestEntitiesGaps:
    def _seed_referrers(self, tmp_vault, target: str, n: int) -> None:
        for i in range(n):
            _make_referrer(tmp_vault["ATLAS_CONCEPTS"], f"ref-{i}", target)

    def test_recurring_broken_link_becomes_gap(self, tmp_vault):
        self._seed_referrers(tmp_vault, "missing-person", 3)

        result = _run(["gaps"])  # default threshold 3
        assert result.exit_code == 0, result.output
        assert "missing-person" in result.output

        gaps = list(tmp_vault["OBSERVATIONS"].glob("*-gap-missing-person.md"))
        assert len(gaps) == 1
        meta, _body, kind = fm.load(gaps[0])
        assert kind == "observation"
        assert meta["category"] == "gap"
        assert len(meta["involves"]) == 3

    def test_gap_records_producer_entities(self, tmp_vault):
        from sabnyx import observations

        self._seed_referrers(tmp_vault, "missing-person", 3)

        result = _run(["gaps"])
        assert result.exit_code == 0, result.output
        (o,) = observations.observations("all")
        assert o.fm.producer == "entities"
        assert o.fm.target == "missing-person"  # identity-bearing since 6a
        (ev,) = [e for e in ledger.replay() if e.kind == "observation.created"]
        assert ev.subject == o.rel
        assert ev.payload["producer"] == "entities"
        assert ev.payload["target"] == "missing-person"
        assert ev.payload["obs_id"].startswith("sha256:")

    def test_below_threshold_is_no_gap(self, tmp_vault):
        self._seed_referrers(tmp_vault, "rarely-named", 2)

        result = _run(["gaps"])  # default threshold 3, only 2 referrers
        assert result.exit_code == 0, result.output
        assert not list(tmp_vault["OBSERVATIONS"].glob("*-gap-*.md"))

    def test_resolved_target_is_no_gap(self, tmp_vault):
        # The target HAS a page, so its references resolve — not a gap.
        _make_entity(tmp_vault["ATLAS_PEOPLE"], "real-person", "person")
        self._seed_referrers(tmp_vault, "real-person", 4)

        result = _run(["gaps", "--threshold", "2"])
        assert result.exit_code == 0, result.output
        assert not list(tmp_vault["OBSERVATIONS"].glob("*-gap-real-person.md"))

    def test_alias_reference_is_not_a_gap(self, tmp_vault):
        # [[JD]] resolves via jane-doe's `aliases` — no false gap observation.
        _make_entity(tmp_vault["ATLAS_PEOPLE"], "jane-doe", "person", aliases=["JD"])
        self._seed_referrers(tmp_vault, "JD", 4)

        result = _run(["gaps", "--threshold", "2"])
        assert result.exit_code == 0, result.output
        assert not list(tmp_vault["OBSERVATIONS"].glob("*-gap-*.md"))

    def test_dedup_across_runs(self, tmp_vault):
        self._seed_referrers(tmp_vault, "missing-person", 3)

        _run(["gaps"])
        _run(["gaps"])  # second run must not duplicate

        gaps = list(tmp_vault["OBSERVATIONS"].glob("*-gap-missing-person.md"))
        assert len(gaps) == 1

    def test_dry_run_writes_nothing(self, tmp_vault):
        self._seed_referrers(tmp_vault, "missing-person", 3)
        events_before = ledger.count()

        result = _run(["gaps", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "dry run" in result.output

        assert not list(tmp_vault["OBSERVATIONS"].glob("*-gap-*.md"))
        # no observation event and no gaps_scanned audit event
        assert ledger.count() == events_before

    def test_emits_gaps_scanned_event(self, tmp_vault):
        self._seed_referrers(tmp_vault, "missing-person", 3)

        result = _run(["gaps"])
        assert result.exit_code == 0, result.output

        scans = [e for e in ledger.replay() if e.kind == "entities.gaps_scanned"]
        assert len(scans) == 1
        assert scans[0].payload["filed"] == 1
        assert scans[0].payload["threshold"] == 3

    def test_threshold_option(self, tmp_vault):
        self._seed_referrers(tmp_vault, "missing-person", 2)

        result = _run(["gaps", "--threshold", "2"])
        assert result.exit_code == 0, result.output
        assert len(list(tmp_vault["OBSERVATIONS"].glob("*-gap-missing-person.md"))) == 1


class TestEntitiesGapsRecurrence:
    """6a: entities gaps through write_observation with a target (M3, M4)."""

    def _seed_referrers(self, tmp_vault, target: str, n: int = 3) -> None:
        # Referrer names are per target, so seeding two targets keeps both.
        for i in range(n):
            _make_referrer(tmp_vault["ATLAS_CONCEPTS"], f"ref-{target}-{i}", target)

    def _seed_gap(self, tmp_vault, name: str, *, target: str | None, kind: str | None):
        meta = {
            "type": "observation",
            "category": "gap",
            "observed": "2026-06-12",
            "source_agent": "sabnyx-entities",
            "status": "pending",
            "involves": [],
        }
        if target is not None:
            meta["target"] = target
        if kind is not None:
            meta |= {
                "status": "promoted" if kind == "done" else "archived",
                "resolved": "2026-06-13",
                "resolution": f"closed as {kind}",
                "resolution_kind": kind,
            }
        p = tmp_vault["OBSERVATIONS"] / name
        fm.write(p, meta, f"# {name}\n")
        return p

    def _files(self, tmp_vault) -> list[str]:
        return sorted(p.name for p in tmp_vault["OBSERVATIONS"].glob("*.md"))

    def _kinds(self, kind: str) -> list:
        return [e for e in ledger.replay() if e.kind == kind]

    def test_gaps_scanned_counts_partition_the_candidates(self, tmp_vault):
        self._seed_referrers(tmp_vault, "fresh-one")
        self._seed_referrers(tmp_vault, "pending-one")
        self._seed_referrers(tmp_vault, "gone-one")
        self._seed_gap(tmp_vault, "2026-06-12-gap-pending-one.md", target="pending-one", kind=None)
        self._seed_gap(tmp_vault, "2026-06-12-gap-gone-one.md", target="gone-one", kind="duplicate")
        assert _run(["gaps"]).exit_code == 0
        (scan,) = self._kinds("entities.gaps_scanned")
        assert scan.payload == {
            "threshold": 3,
            "candidates": 3,
            "filed": 1,
            "already_tracked": 1,
            "suppressed": 1,
            "invalid": 0,
        }

    def test_second_run_files_nothing(self, tmp_vault):
        # M3: the filename glob is gone; the policy stops the re-file.
        self._seed_referrers(tmp_vault, "missing-person")
        assert _run(["gaps"]).exit_code == 0
        files, created = self._files(tmp_vault), len(self._kinds("observation.created"))
        assert created == 1

        second = _run(["gaps"])
        assert second.exit_code == 0, second.output
        assert self._files(tmp_vault) == files  # 0 files
        assert len(self._kinds("observation.created")) == created  # 0 observation.created
        assert "0 new gap observation(s) filed" in second.output
        assert "exists" in second.output

    def test_second_run_on_a_later_day_files_nothing(self, tmp_vault, monkeypatch):
        # The case the old "*-gap-<slug>.md" glob existed for: a re-run on
        # another day has a new date prefix, so no filename collides and only
        # identity (target) stops the re-file.
        from datetime import date

        from sabnyx import ledger as ledger_mod

        self._seed_referrers(tmp_vault, "missing-person")
        monkeypatch.setattr(ledger_mod, "today", lambda: date(2026, 6, 12))
        assert _run(["gaps"]).exit_code == 0
        files, created = self._files(tmp_vault), len(self._kinds("observation.created"))
        monkeypatch.setattr(ledger_mod, "today", lambda: date(2026, 6, 13))
        second = _run(["gaps"])
        assert second.exit_code == 0, second.output
        assert self._files(tmp_vault) == files
        assert len(self._kinds("observation.created")) == created

    def test_dismissed_gap_is_suppressed_and_counted_once(self, tmp_vault):
        # M4 (ledger-asserting): one observation.suppressed with count 1.
        head = self._seed_gap(
            tmp_vault, "2026-06-12-gap-missing-person.md", target="missing-person", kind="wontfix"
        )
        self._seed_referrers(tmp_vault, "missing-person")
        result = _run(["gaps"])
        assert result.exit_code == 0, result.output
        assert self._files(tmp_vault) == [head.name]
        assert self._kinds("observation.created") == []
        (ev,) = self._kinds("observation.suppressed")
        assert ev.actor == "sabnyx-entities"
        assert ev.plane == "projection"
        assert ev.subject == f"vault/intake/.observations/{head.name}"
        assert ev.payload["count"] == 1
        assert ev.payload["kind"] == "wontfix"
        (scan,) = self._kinds("entities.gaps_scanned")
        assert scan.payload["filed"] == 0
        assert scan.payload["suppressed"] == 1
        p = scan.payload  # the counts partition the candidates
        assert p["filed"] + p["already_tracked"] + p["suppressed"] + p["invalid"] == p["candidates"]
        assert "suppressed (wontfix)" in result.output

    def test_done_gap_that_recurs_is_reopened(self, tmp_vault):
        head = self._seed_gap(
            tmp_vault, "2026-06-12-gap-missing-person.md", target="missing-person", kind="done"
        )
        self._seed_referrers(tmp_vault, "missing-person")
        result = _run(["gaps"])
        assert result.exit_code == 0, result.output
        (ev,) = self._kinds("observation.created")
        assert ev.payload["supersedes"] == f"vault/intake/.observations/{head.name}"
        assert "reopened gap" in result.output
        assert "1 new gap observation(s) filed" in result.output

    def test_legacy_targetless_gap_no_longer_suppresses(self, tmp_vault):
        # Accepted (A12, Demonstrate c4b): a legacy *-gap-<slug>.md without a
        # `target` has no identity, so it neither blocks nor suppresses; the
        # operator archives the legacy twin with --kind duplicate.
        legacy = self._seed_gap(
            tmp_vault, "2026-01-01-gap-missing-person.md", target=None, kind=None
        )
        self._seed_referrers(tmp_vault, "missing-person")
        assert _run(["gaps"]).exit_code == 0
        assert len(self._files(tmp_vault)) == 2
        assert legacy.name in self._files(tmp_vault)

    def test_dry_run_previews_through_the_policy(self, tmp_vault):
        self._seed_referrers(tmp_vault, "missing-person")
        self._seed_referrers(tmp_vault, "other-gap")
        self._seed_gap(
            tmp_vault, "2026-06-12-gap-other-gap.md", target="other-gap", kind="obsolete"
        )
        files, events = self._files(tmp_vault), ledger.count()
        result = _run(["gaps", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "would file gap" in result.output
        assert "suppressed (obsolete)" in result.output
        assert self._files(tmp_vault) == files
        assert ledger.count() == events  # dry runs ledger nothing, suppressions included


# ---------------------------------------------------------------------------
# entities resolve
# ---------------------------------------------------------------------------


class TestEntitiesResolve:
    def test_no_collisions_reports_clean(self, tmp_vault):
        _make_entity(tmp_vault["ATLAS_PEOPLE"], "jane-doe", "person", aliases=["founder"])

        result = _run(["resolve"])
        assert result.exit_code == 0, result.output
        assert "no collisions" in result.output

        events = [e for e in ledger.replay() if e.kind == "entities.resolved"]
        assert len(events) == 1
        assert events[0].payload["collisions"] == 0

    def test_shared_alias_is_collision(self, tmp_vault):
        # Two distinct pages both claim the alias "Janet" -> token "janet".
        _make_entity(tmp_vault["ATLAS_PEOPLE"], "jane-doe", "person", aliases=["Janet"])
        _make_entity(tmp_vault["ATLAS_PEOPLE"], "janet-smith", "person", aliases=["Janet"])

        result = _run(["resolve"])
        assert result.exit_code == 0, result.output
        assert "janet" in result.output
        assert "collision" in result.output

        events = [e for e in ledger.replay() if e.kind == "entities.resolved"]
        assert events[-1].payload["collisions"] == 1

    def test_alias_clashing_with_other_slug_is_collision(self, tmp_vault):
        # Page B's alias slugifies to Page A's slug.
        _make_entity(tmp_vault["ATLAS_PEOPLE"], "jane-doe", "person")
        _make_entity(tmp_vault["ATLAS_ORGS"], "acme", "org", aliases=["Jane Doe"])

        result = _run(["resolve"])
        assert result.exit_code == 0, result.output
        assert "jane-doe" in result.output

        events = [e for e in ledger.replay() if e.kind == "entities.resolved"]
        assert events[-1].payload["collisions"] >= 1

    def test_self_alias_not_a_collision(self, tmp_vault):
        # An alias equal to the page's own slug must not register as a clash.
        _make_entity(tmp_vault["ATLAS_PEOPLE"], "jane-doe", "person", aliases=["Jane Doe"])

        result = _run(["resolve"])
        assert result.exit_code == 0, result.output
        assert "no collisions" in result.output
