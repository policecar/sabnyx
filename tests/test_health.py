"""Health checks: age_days shapes, link resolution, orphans/dead-ends,
domains/ tolerance, and pending-observation counting.

YAML parses an unquoted `last_verified: 2026-06-13` into a `datetime.date`,
a quoted one into a `str`, and a full timestamp into a `datetime`. A bare
date used to crash the helper (`date` has no `.tzinfo`).
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from sabnyx import context, fm, ledger
from sabnyx.util import age_days

DESC = "a deliberately long-enough description satisfying the 30-char minimum"


def _atlas(dir_: Path, slug: str, body: str = "", *, aliases: list[str] | None = None) -> Path:
    meta: dict = {
        "type": "concept",
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
    p = dir_ / f"{slug}.md"
    fm.write(p, meta, f"# {slug}\n\n{body}")
    return p


def _domains_file(tmp_vault, slug: str = "wabi") -> Path:
    d = tmp_vault["VAULT"] / "domains" / "chanoyu"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{slug}.md"
    p.write_text("---\nkind: aesthetic\n---\n# wabi\n", encoding="utf-8")
    return p


def test_age_days_none():
    assert age_days(None) is None


def test_age_days_bare_date():
    d = date.today() - timedelta(days=10)
    assert age_days(d) in (9, 10)  # tz/midnight rounding tolerance


def test_age_days_iso_string():
    s = (datetime.now(UTC) - timedelta(days=30)).date().isoformat()
    assert age_days(s) in (29, 30)


def test_age_days_naive_datetime():
    dt = datetime.now() - timedelta(days=5)  # noqa: DTZ005 — testing naive path
    assert age_days(dt) in (4, 5)


def test_age_days_aware_datetime():
    dt = datetime.now(UTC) - timedelta(days=3)
    assert age_days(dt) == 3


# -- link resolution (aliases + slugified spaced names, M10) --------------------


class TestScanWikilinks:
    def test_alias_and_spaced_links_resolve(self, tmp_vault):
        from sabnyx.commands import health as health_mod

        c = tmp_vault["ATLAS_CONCEPTS"]
        _atlas(c, "jane-doe", aliases=["JD"])
        _atlas(c, "linker", body="See [[JD]] and [[Jane Doe]] for context.\n")

        idx = context.vault_slug_index()
        broken, linked, drift, outgoing = health_mod.scan_wikilinks(idx)

        assert broken == []  # alias and spaced-name links are not broken
        assert "jane-doe" in linked  # incoming credit lands on the canonical page
        assert "linker" in outgoing
        assert drift == []

    def test_domains_file_skipped_but_resolves_as_target(self, tmp_vault):
        from sabnyx.commands import health as health_mod

        _domains_file(tmp_vault)
        _atlas(tmp_vault["ATLAS_CONCEPTS"], "tea", body="rooted in [[wabi]].\n")

        idx = context.vault_slug_index()
        broken, linked, drift, _outgoing = health_mod.scan_wikilinks(idx)

        assert broken == []  # the domains slug resolves as a target
        assert "wabi" in linked
        assert drift == []  # its own schema, not schema drift


# -- domains/ must not crash health or tend's health pass (M1) ------------------


def test_health_cmd_survives_domains_file(tmp_vault):
    from sabnyx.commands import health as health_mod

    _domains_file(tmp_vault)
    _atlas(tmp_vault["ATLAS_CONCEPTS"], "tea")

    result = CliRunner().invoke(health_mod.health_cmd, [], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    audit = [e for e in ledger.replay() if e.kind == "health.audited"][-1]
    assert audit.payload["schema_drift"] == 0


def test_tend_health_pass_survives_domains_file(tmp_vault):
    from sabnyx.commands import tend as tend_mod

    _domains_file(tmp_vault)

    result = CliRunner().invoke(tend_mod.tend_cmd, ["--no-reweave"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert "health summary" in result.output


# -- orphans and dead ends (jibot definitions) -----------------------------------


def test_orphan_and_dead_end_classification(tmp_vault):
    from sabnyx.commands import health as health_mod

    c = tmp_vault["ATLAS_CONCEPTS"]
    _atlas(c, "hub", body="links to [[sink]].\n")  # outgoing only → neither
    _atlas(c, "sink")  # incoming only → dead end
    _atlas(c, "island")  # no links either way → orphan

    result = CliRunner().invoke(health_mod.health_cmd, [], catch_exceptions=False)
    assert result.exit_code == 0, result.output

    audit = [e for e in ledger.replay() if e.kind == "health.audited"][-1]
    assert audit.payload["orphans"] == 1
    assert audit.payload["dead_ends"] == 1


def test_alias_only_incoming_is_not_an_orphan(tmp_vault):
    from sabnyx.commands import health as health_mod

    c = tmp_vault["ATLAS_CONCEPTS"]
    _atlas(c, "jane-doe", aliases=["JD"])
    _atlas(c, "linker", body="ping [[JD]] about it.\n")

    CliRunner().invoke(health_mod.health_cmd, [], catch_exceptions=False)
    audit = [e for e in ledger.replay() if e.kind == "health.audited"][-1]
    # jane-doe has alias-incoming and no outgoing → dead end, not orphan;
    # linker has outgoing and no incoming → neither.
    assert audit.payload["orphans"] == 0
    assert audit.payload["dead_ends"] == 1


# -- pending observations count only status: pending ------------------------------


def _observation(tmp_vault, slug: str, status: str) -> Path:
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
        # The schema derives status from resolution_kind (strict): a resolved
        # observation carries all three resolution fields.
        meta |= {
            "resolved": "2026-06-13",
            "resolution": "noted elsewhere",
            "resolution_kind": "done" if status == "promoted" else "wontfix",
        }
    fm.write(p, meta, f"# {slug}\n\nnoted\n")
    # Valid on disk, so a count of 1 means "filtered by status", not
    # "skipped as malformed".
    fm.load(p)
    return p


def test_pending_observation_count_filters_status(tmp_vault):
    from sabnyx.commands import health as health_mod

    _observation(tmp_vault, "open-one", "pending")
    _observation(tmp_vault, "done-one", "archived")

    assert health_mod.pending_observation_count() == 1

    result = CliRunner().invoke(health_mod.health_cmd, [], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    audit = [e for e in ledger.replay() if e.kind == "health.audited"][-1]
    assert audit.payload["observations"] == 1


# -- tend anomalies (report-hygiene, B4) -----------------------------------------
# Why: health is where the operator looks; a heartbeat that misbehaves must
# show up there in both modes, and ledger text must print as text (a subject
# carrying Rich markup would otherwise restyle or hide the line).


def _stuck_runs(subject: str, n: int = 4) -> None:
    from sabnyx.services import ACTOR_TEND

    for _ in range(n):
        ledger.append_event(
            plane="projection",
            kind="triage.decided",
            actor=ACTOR_TEND,
            subject=subject,
            payload={"decision": "keep"},
        )
        ledger.append_event(
            plane="projection",
            kind="tend.completed",
            actor=ACTOR_TEND,
            subject="vault/",
            payload={},
        )


@pytest.mark.parametrize("args", [[], ["--quick"]], ids=["full", "quick"])
def test_health_lists_anomalies(tmp_vault, args):
    from sabnyx.commands import health as health_mod

    _stuck_runs("vault/intake/made-up-heron.md")
    result = CliRunner().invoke(health_mod.health_cmd, args, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert re.search(r"tend anomalies\s.*\b1\b", result.output)
    assert "as of the last recorded tend run" in result.output
    assert "stuck_slug  vault/intake/made-up-heron.md  (4 consecutive runs)" in result.output


@pytest.mark.parametrize("args", [[], ["--quick"]], ids=["full", "quick"])
def test_health_no_anomalies_row_zero_no_section(tmp_vault, args):
    from sabnyx.commands import health as health_mod

    result = CliRunner().invoke(health_mod.health_cmd, args, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert re.search(r"tend anomalies\s.*\b0\b", result.output)
    assert "as of the last recorded tend run" not in result.output


def test_health_anomaly_subject_markup_prints_literally(tmp_vault):
    from sabnyx.commands import health as health_mod

    _stuck_runs("[red]x[/red]")
    result = CliRunner().invoke(health_mod.health_cmd, [], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert "stuck_slug  [red]x[/red]  (4 consecutive runs)" in result.output


def test_health_atlas_listings_ignore_filesystem_order(tmp_vault, monkeypatch):
    """The printed untracked-provenance list follows path order, not the
    order the filesystem happens to return (APFS sorts, ext4 does not):
    the goldens freeze this text."""
    from sabnyx.commands import health as health_mod

    for slug in ("made-up-alder", "made-up-birch", "made-up-cedar"):
        _atlas(tmp_vault["ATLAS_CONCEPTS"], slug)
    real_rglob = Path.rglob
    monkeypatch.setattr(Path, "rglob", lambda self, pat: reversed(list(real_rglob(self, pat))))

    result = CliRunner().invoke(health_mod.health_cmd, [], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    section = result.output.split("untracked provenance (Gate 3):")[1]
    found = re.findall(r"made-up-(alder|birch|cedar)", section)
    assert found == ["alder", "birch", "cedar"]
