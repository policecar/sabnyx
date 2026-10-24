"""One reader for observations (IDD §5/§6 observations.py, D7, D13; M1).

Every reader of vault/intake/.observations/ must agree on what is pending.
The 4-file fixture below is the M1 acceptance case: one pending, one
promoted, one archived and one malformed file. Every reader reports
pending = 1, and the malformed file is reported (health, triage) but never
counted. Before the shared reader, vault_pulse, triage and reweave's LLM
context counted archived and malformed files as pending.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from sabnyx import context, fm, ledger, paths

PENDING = "2026-06-12-gap-open-one.md"
PROMOTED = "2026-06-12-gap-done-one.md"
ARCHIVED = "2026-06-12-contradiction-dismissed-one.md"
MALFORMED = "2026-06-12-friction-hand-archived.md"


def _obs(name: str, status: str, **extra) -> None:
    meta = {
        "type": "observation",
        "category": name.split("-")[3],
        "observed": "2026-06-12",
        "source_agent": "sabnyx-entities",
        "status": status,
        "involves": [],
    } | extra
    fm.write(paths.OBSERVATIONS / name, meta, f"# {name}\n\nnoted\n")


@pytest.fixture
def four_files(tmp_vault):
    _obs(PENDING, "pending")
    _obs(
        PROMOTED,
        "promoted",
        resolved="2026-06-13",
        resolution="page exists",
        resolution_kind="done",
    )
    _obs(
        ARCHIVED,
        "archived",
        resolved="2026-06-13",
        resolution="not a conflict",
        resolution_kind="wontfix",
    )
    _obs(MALFORMED, "archived")  # hand-set: no resolution fields → schema-invalid
    return tmp_vault


def _write_atlas_page(slug: str) -> None:
    fm.write(
        paths.ATLAS / "concepts" / f"{slug}.md",
        {"type": "concept", "description": "a concept page used as the reweave target here"},
        f"# {slug}\n\nbody\n",
    )


# -- M1: every reader reports pending = 1 ------------------------------------------


def test_vault_pulse_counts_pending_only(four_files):
    from sabnyx import services

    assert services.vault_pulse()["observations"] == 1


def test_health_counts_pending_only_and_reports_malformed(four_files):
    from sabnyx.commands import health as health_mod

    assert health_mod.pending_observation_count() == 1
    result = CliRunner().invoke(health_mod.health_cmd, [], catch_exceptions=False)
    assert MALFORMED in result.output  # reported, by name
    audit = [e for e in ledger.replay() if e.kind == "health.audited"][-1]
    assert audit.payload["observations"] == 1
    assert audit.payload["malformed_observations"] == 1


def test_triage_counts_pending_only_and_lists_malformed_as_invalid(four_files):
    from sabnyx.commands import triage as triage_mod

    CliRunner().invoke(triage_mod.triage_cmd, [], catch_exceptions=False)
    event = [e for e in ledger.replay() if e.kind == "triage.proposed"][-1]
    assert event.payload["observation_count"] == 1
    pending_md = (paths.REVIEW / "pending.md").read_text(encoding="utf-8")
    obs_section = pending_md.split("## Observations (pending)", 1)[1]
    assert PENDING in obs_section
    assert PROMOTED not in obs_section
    assert ARCHIVED not in obs_section
    assert f"[INVALID] vault/intake/.observations/{MALFORMED}" in obs_section


def test_reweave_context_shows_pending_only(four_files):
    from sabnyx.commands import reweave as reweave_mod

    _write_atlas_page("alpha-concept")
    _write_atlas_page("beta-concept")
    index = context.slug_index(paths.ATLAS)
    ctx = reweave_mod._context_for(paths.ATLAS / "concepts" / "alpha-concept.md", index)
    section = ctx.split("## Pending observations", 1)[1]
    assert PENDING.removesuffix(".md") in section
    for name in (PROMOTED, ARCHIVED, MALFORMED):
        assert name.removesuffix(".md") not in section  # dismissed signals never reach the LLM


# -- the reader itself -------------------------------------------------------------


def test_load_all_separates_valid_from_malformed(four_files):
    from sabnyx import observations as obs

    valid, malformed = obs.load_all()
    assert [o.path.name for o in valid] == sorted([PENDING, PROMOTED, ARCHIVED])
    assert [p.name for p, _err in malformed] == [MALFORMED]
    assert "requires resolved" in malformed[0][1]  # the schema's own message, not swallowed


def test_observations_filters_by_status(four_files):
    from sabnyx import observations as obs

    assert [o.path.name for o in obs.observations()] == [PENDING]
    assert [o.path.name for o in obs.observations("promoted")] == [PROMOTED]
    assert [o.path.name for o in obs.observations("archived")] == [ARCHIVED]
    assert len(obs.observations("all")) == 3  # malformed is never in any status


def test_observation_record_carries_rel_and_typed_frontmatter(four_files):
    from sabnyx import observations as obs

    (o,) = obs.observations()
    assert o.path == paths.OBSERVATIONS / PENDING
    assert o.path.is_absolute()
    assert o.rel == f"vault/intake/.observations/{PENDING}"  # the ledger subject form
    assert o.fm.status == "pending"
    assert o.fm.category == "gap"


def test_load_all_sorts_by_filename_and_ignores_non_md(tmp_vault):
    from sabnyx import observations as obs

    for name in ("2026-06-14-gap-c.md", "2026-06-12-gap-a.md", "2026-06-13-gap-b.md"):
        _obs(name, "pending")
    (paths.OBSERVATIONS / "notes.txt").write_text("not an observation", encoding="utf-8")
    (paths.OBSERVATIONS / "sub.md").mkdir()  # a directory named *.md is not a file
    valid, malformed = obs.load_all()
    assert [o.path.name for o in valid] == [
        "2026-06-12-gap-a.md",
        "2026-06-13-gap-b.md",
        "2026-06-14-gap-c.md",
    ]
    assert malformed == []


def test_load_all_on_missing_folder_is_empty(tmp_vault):
    from sabnyx import observations as obs

    assert not paths.OBSERVATIONS.exists() or not any(paths.OBSERVATIONS.iterdir())
    assert obs.load_all() == ([], [])


def test_unparseable_yaml_is_malformed_not_raised(tmp_vault):
    from sabnyx import observations as obs

    paths.OBSERVATIONS.mkdir(parents=True, exist_ok=True)
    (paths.OBSERVATIONS / "2026-06-12-gap-broken.md").write_text(
        "---\ntype: [unclosed\n---\n", encoding="utf-8"
    )
    valid, malformed = obs.load_all()
    assert valid == []
    assert len(malformed) == 1
    assert "malformed YAML" in malformed[0][1]


def test_observations_module_never_imports_writers():
    # IDD §3: imports point one way; the reader never writes.
    import ast

    src = (paths.ROOT / "src" / "sabnyx" / "observations.py").read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom):
            imported |= {a.name for a in node.names} | {node.module or ""}
        elif isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
    assert not {n for n in imported if n.endswith("writers")}, imported
