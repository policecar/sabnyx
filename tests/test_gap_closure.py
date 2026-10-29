"""Deterministic gap closure (IDD §6, D3, D8b; M5).

A pending gap whose `target` now names a page is resolved (kind done)
through transition_observation, so the file change and the
observation.resolved event are the manual `observations resolve` ones by
construction. Ambiguous targets stay pending and are named; gaps without a
target (legacy) and targets that are still missing are not touched.
"""

from __future__ import annotations

from datetime import date

import pytest
from click.testing import CliRunner
from rich.console import Console

from sabnyx import fm, ledger, paths
from sabnyx import observations as obs
from sabnyx.writers import close_resolved_gaps

_DESC = "a synthetic page long enough to satisfy the atlas description schema"


class _Lines(Console):
    """A quiet Console that records each print's markup string verbatim."""

    def __init__(self) -> None:
        super().__init__(quiet=True)
        self.lines: list[str] = []

    def print(self, *objects: object, **_kw: object) -> None:
        self.lines.append(" ".join(str(o) for o in objects))


def _page(slug: str, aliases: list[str] | None = None) -> None:
    meta: dict = {"type": "concept", "description": f"{slug}: {_DESC}"}
    if aliases:
        meta["aliases"] = aliases
    fm.write(paths.ATLAS / "concepts" / f"{slug}.md", meta, f"# {slug}\n")


def _gap(name: str, *, target: str | None, source_agent: str = "sabnyx-entities") -> object:
    meta: dict = {
        "type": "observation",
        "category": "gap",
        "observed": "2026-06-12",
        "source_agent": source_agent,
        "status": "pending",
        "involves": ["vault/atlas/concepts/ref.md"],
        "producer": "entities",
    }
    if target is not None:
        meta["target"] = target
    p = paths.OBSERVATIONS / name
    fm.write(p, meta, f"# {name}\n\nbody line\n")
    return p


def _events(kind: str) -> list:
    return [e for e in ledger.replay() if e.kind == kind]


def _snapshot() -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(paths.OBSERVATIONS.glob("*.md"))}


SLUG_HIT = "2026-06-12-gap-jane-doe.md"
ALIAS_HIT = "2026-06-12-gap-acme-corp.md"
AMBIGUOUS = "2026-06-12-gap-shared-name.md"
MISSING = "2026-06-12-gap-nobody.md"
LEGACY = "2026-01-01-gap-jane-doe.md"  # the old *-gap-<slug>.md shape, no target


@pytest.fixture
def vault(tmp_vault, monkeypatch):
    monkeypatch.setattr(ledger, "today", lambda: date(2026, 6, 20))
    _page("jane-doe")
    _page("acme", aliases=["Acme Corp"])
    _page("alpha", aliases=["Shared Name"])
    _page("beta", aliases=["shared name"])
    _gap(SLUG_HIT, target="jane-doe")
    _gap(ALIAS_HIT, target="acme-corp")
    _gap(AMBIGUOUS, target="shared-name")
    _gap(MISSING, target="nobody")
    _gap(LEGACY, target=None)
    return tmp_vault


# -- closable_gaps: pure ------------------------------------------------------------


def test_closable_gaps_splits_found_from_ambiguous(vault):
    closable, ambiguous = obs.closable_gaps(obs.observations(), obs.resolver())
    assert [(g.path.name, slug) for g, slug in closable] == [
        (ALIAS_HIT, "acme"),  # by alias
        (SLUG_HIT, "jane-doe"),  # by stem
    ]
    assert [(g.path.name, t, c) for g, t, c in ambiguous] == [
        (AMBIGUOUS, "shared-name", ("atlas/concepts/alpha.md", "atlas/concepts/beta.md"))
    ]


def test_a_gap_on_a_stem_two_pages_share_is_ambiguous_not_closed(vault):
    """concepts/twin.md and projects/twin.md: the first-wins slug index keeps
    one, but closure must not pick it; the gap stays pending."""
    _page("twin")
    fm.write(
        paths.ATLAS / "projects" / "twin.md",
        {"type": "project", "description": f"twin: {_DESC}"},
        "# twin\n",
    )
    _gap("2026-06-12-gap-twin.md", target="twin")
    closable, ambiguous = obs.closable_gaps(obs.observations(), obs.resolver())
    assert "2026-06-12-gap-twin.md" not in [g.path.name for g, _ in closable]
    assert ("2026-06-12-gap-twin.md", ("atlas/concepts/twin.md", "atlas/projects/twin.md")) in [
        (g.path.name, c) for g, _t, c in ambiguous
    ]


def test_closable_gaps_ignores_non_gaps_and_closed_records(vault):
    _gap("2026-06-12-gap-done-already.md", target="jane-doe")
    p = paths.OBSERVATIONS / "2026-06-12-gap-done-already.md"
    meta, body, _ = fm.load(p)
    fm.write(
        p,
        meta
        | {
            "status": "promoted",
            "resolved": "2026-06-13",
            "resolution": "x",
            "resolution_kind": "done",
        },
        body,
    )
    closable, _ = obs.closable_gaps(obs.observations("all"), obs.resolver())
    assert "2026-06-12-gap-done-already.md" not in [g.path.name for g, _ in closable]


# -- close_resolved_gaps -------------------------------------------------------------


def test_slug_and_alias_hits_close_the_rest_are_untouched(vault):
    before = _snapshot()
    lines = _Lines()
    report = close_resolved_gaps(actor="sabnyx-entities", console=lines)

    assert sorted(t.path.name for t in report.closed) == sorted([SLUG_HIT, ALIAS_HIT])
    for name, slug in ((SLUG_HIT, "jane-doe"), (ALIAS_HIT, "acme")):
        meta, _, _ = fm.load(paths.OBSERVATIONS / name)
        assert meta["status"] == "promoted"
        assert meta["resolution_kind"] == "done"
        assert meta["resolution"] == f"page now exists: {slug}"
    after = _snapshot()
    for name in (AMBIGUOUS, MISSING, LEGACY):
        assert after[name] == before[name]  # byte-identical
    assert report.ambiguous == (
        (
            f"vault/intake/.observations/{AMBIGUOUS}",
            "shared-name",
            ("atlas/concepts/alpha.md", "atlas/concepts/beta.md"),
        ),
    )
    assert report.refused == ()
    assert "  [green]closed 2 gap(s)[/green]" in lines.lines
    assert "  [dim]1 gap(s) ambiguous, left pending[/dim]" in lines.lines
    assert any(
        "shared-name" in ln and "atlas/concepts/alpha.md, atlas/concepts/beta.md" in ln
        for ln in lines.lines
    )
    resolved = _events("observation.resolved")
    assert [e.actor for e in resolved] == ["sabnyx-entities"] * 2


def test_closure_event_and_bytes_equal_a_manual_resolve(vault):
    # M5 "same event shape as manual resolve", by comparison not by reading:
    # close the gap, then put its original bytes back and resolve it by hand
    # through the CLI with the same reason. Payload and file must be equal.
    from sabnyx.commands.observations import observations_cmd

    p = paths.OBSERVATIONS / SLUG_HIT
    original = p.read_bytes()
    close_resolved_gaps(actor="sabnyx-entities", console=Console(quiet=True))
    auto = next(e for e in _events("observation.resolved") if e.subject.endswith(SLUG_HIT))
    auto_bytes = p.read_bytes()

    p.write_bytes(original)
    result = CliRunner().invoke(
        observations_cmd,
        [
            "resolve",
            f"vault/intake/.observations/{SLUG_HIT}",
            "--reason",
            "page now exists: jane-doe",
        ],
    )
    assert result.exit_code == 0, result.output
    manual = _events("observation.resolved")[-1]

    assert manual.actor == "sabnyx-cli"
    assert (manual.kind, manual.plane, manual.subject) == (auto.kind, auto.plane, auto.subject)
    assert manual.payload == auto.payload
    assert auto.payload["obs_id"].startswith("sha256:")
    assert auto.payload["target"] == "jane-doe"
    assert p.read_bytes() == auto_bytes


def test_second_run_emits_nothing(vault):
    close_resolved_gaps(actor="sabnyx-entities", console=Console(quiet=True))
    events, files = ledger.count(), _snapshot()
    report = close_resolved_gaps(actor="sabnyx-entities", console=Console(quiet=True))
    assert report.closed == ()
    assert ledger.count() == events
    assert _snapshot() == files


def test_dry_run_previews_and_writes_nothing(vault):
    events, files = ledger.count(), _snapshot()
    lines = _Lines()
    report = close_resolved_gaps(actor="sabnyx-entities", dry_run=True, console=lines)
    assert sorted(t.path.name for t in report.closed) == sorted([SLUG_HIT, ALIAS_HIT])
    assert all(t.dry_run for t in report.closed)
    assert ledger.count() == events
    assert _snapshot() == files
    assert (
        f"  [dim]would close:[/dim] vault/intake/.observations/{SLUG_HIT} → jane-doe" in lines.lines
    )
    assert not any("closed 2" in ln for ln in lines.lines)


def test_a_refused_gap_is_recorded_and_the_run_continues(vault):
    # Two column-0 status lines: valid YAML (last wins), so the reader counts
    # it pending, but the patcher cannot touch it safely -> refused malformed.
    bad = paths.OBSERVATIONS / "2026-06-12-gap-alpha.md"
    bad.write_text(
        "---\ntype: observation\ncategory: gap\nobserved: 2026-06-12\n"
        "source_agent: sabnyx-entities\nstatus: pending\nstatus: pending\n"
        "involves: []\ntarget: alpha\n---\n\n# alpha\n",
        encoding="utf-8",
    )
    before = bad.read_bytes()
    report = close_resolved_gaps(actor="sabnyx-entities", console=Console(quiet=True))
    assert report.refused == ((f"vault/intake/.observations/{bad.name}", "malformed"),)
    assert bad.read_bytes() == before
    assert sorted(t.path.name for t in report.closed) == sorted([SLUG_HIT, ALIAS_HIT])


# -- wiring: entities gaps and tend ---------------------------------------------------


def test_entities_gaps_closes_first_and_reports_it(vault):
    from sabnyx.commands.entities import entities_cmd

    result = CliRunner().invoke(entities_cmd, ["gaps"])
    assert result.exit_code == 0, result.output
    resolved = _events("observation.resolved")
    assert len(resolved) == 2
    assert {e.actor for e in resolved} == {"sabnyx-entities"}
    (scan,) = _events("entities.gaps_scanned")
    assert scan.payload["closed"] == 2
    assert scan.payload["ambiguous"] == 1
    assert "closed 2 gap(s)" in result.output
    # ordering: closure happens before the scan's summary event
    kinds = [e.kind for e in ledger.replay()]
    assert kinds.index("observation.resolved") < kinds.index("entities.gaps_scanned")


def test_entities_gaps_dry_run_previews_closure_and_writes_nothing(vault):
    from sabnyx.commands.entities import entities_cmd

    events, files = ledger.count(), _snapshot()
    result = CliRunner().invoke(entities_cmd, ["gaps", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "would close:" in result.output
    assert ledger.count() == events
    assert _snapshot() == files


def _tend(args):
    from sabnyx.commands.tend import tend_cmd

    return CliRunner().invoke(tend_cmd, args, catch_exceptions=False)


def test_tend_closes_before_the_health_summary_and_reports_it(vault):
    result = _tend(["--no-reweave"])
    assert result.exit_code == 0, result.output
    resolved = _events("observation.resolved")
    assert len(resolved) == 2
    assert {e.actor for e in resolved} == {"sabnyx-tend"}
    (done,) = _events("tend.completed")
    assert done.payload["gaps_closed"] == 2
    out = result.output
    assert out.index("closing resolved gaps") < out.index("health summary")


def test_tend_closes_even_with_no_health(vault):
    result = _tend(["--no-reweave", "--no-health"])
    assert result.exit_code == 0, result.output
    (done,) = _events("tend.completed")
    assert done.payload["gaps_closed"] == 2


def test_tend_dry_run_lists_would_close_and_writes_nothing(vault):
    events, files = ledger.count(), _snapshot()
    result = _tend(["--dry-run", "--no-reweave", "--no-health"])
    assert result.exit_code == 0, result.output
    assert "would close:" in result.output
    assert ledger.count() == events
    assert _snapshot() == files
