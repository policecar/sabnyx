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
