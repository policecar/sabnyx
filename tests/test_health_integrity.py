"""Observation integrity warnings in health (IDD §6 health, D8b, D9).

Each warning kind has a fixture that triggers it, and one clean vault —
including a normal reopen chain — triggers none. The warnings are derived
from the ObsIndex by-products and the Resolver, never from a second listing
of the folder, and their text is sorted: Cycle 2 freezes it as a golden.
Warnings never change health's exit code.
"""

from __future__ import annotations

import random

import pytest
from click.testing import CliRunner

from sabnyx import fm, ledger, paths
from sabnyx import observations as obs

_DESC = "a synthetic page long enough to satisfy the atlas description schema"
_DIR = "vault/intake/.observations"


def _page(slug: str, *, folder: str = "concepts", aliases: list[str] | None = None) -> None:
    meta: dict = {"type": "concept", "description": f"{slug}: {_DESC}"}
    if aliases:
        meta["aliases"] = aliases
    fm.write(paths.ATLAS / folder / f"{slug}.md", meta, f"# {slug}\n")


def _obs(name: str, involves: list[str], *, kind: str | None = None, supersedes=None) -> None:
    meta: dict = {
        "type": "observation",
        "category": "contradiction",
        "observed": "2026-06-12",
        "source_agent": "sabnyx-scan",
        "status": "pending",
        "involves": involves,
    }
    if kind is not None:
        meta |= {
            "status": "promoted" if kind == "done" else "archived",
            "resolved": "2026-06-13",
            "resolution": f"closed as {kind}",
            "resolution_kind": kind,
        }
    if supersedes is not None:
        meta["supersedes"] = f"{_DIR}/{supersedes}"
    fm.write(paths.OBSERVATIONS / name, meta, f"# {name}\n")


def _warnings() -> list[obs.IntegrityWarning]:
    valid, _ = obs.load_all()
    r = obs.resolver()
    return obs.integrity_warnings(obs.index(valid, r), r)


def _kinds() -> list[str]:
    return [w.kind for w in _warnings()]


@pytest.fixture
def pages(tmp_vault):
    for slug in ("alpha", "beta", "gamma"):
        _page(slug)
    paths.OBSERVATIONS.mkdir(parents=True, exist_ok=True)
    return tmp_vault


# -- clean: nothing fires --------------------------------------------------------


def test_clean_vault_with_a_reopen_chain_has_no_warnings(pages):
    _obs("2026-06-12-contradiction-ab.md", ["alpha", "beta"], kind="done")
    _obs(
        "2026-06-20-contradiction-ab.md",
        ["alpha", "beta"],
        supersedes="2026-06-12-contradiction-ab.md",
    )
    _obs("2026-06-12-contradiction-bg.md", ["beta", "gamma"])
    _page("delta", aliases=["Only Delta"])
    assert _warnings() == []


# -- one fixture per kind ----------------------------------------------------------


def test_duplicate_pending_ids_warn(pages):
    _obs("2026-06-12-contradiction-ab.md", ["alpha", "beta"])
    _obs("2026-06-13-contradiction-ab.md", ["beta.md", "alpha"])  # same pair, other form
    (w,) = _warnings()
    assert w.kind == "duplicate_pending"
    assert w.detail == (
        f"pending: {_DIR}/2026-06-12-contradiction-ab.md, {_DIR}/2026-06-13-contradiction-ab.md"
    )
    assert len(w.subject) == 12


def test_several_chain_heads_warn(pages):
    _obs("2026-06-12-contradiction-ab.md", ["alpha", "beta"], kind="done")
    _obs("2026-06-13-contradiction-ab.md", ["alpha", "beta"], kind="wontfix")
    (w,) = _warnings()
    assert w.kind == "multi_head"
    assert w.detail == (
        f"head candidates: {_DIR}/2026-06-12-contradiction-ab.md, "
        f"{_DIR}/2026-06-13-contradiction-ab.md"
    )


def test_dangling_supersedes_warns(pages):
    _obs("2026-06-20-contradiction-ab.md", ["alpha", "beta"], supersedes="2026-01-01-gone.md")
    (w,) = _warnings()
    assert w.kind == "dangling_supersedes"
    assert w.subject == f"{_DIR}/2026-06-20-contradiction-ab.md"
    assert w.detail == f"supersedes {_DIR}/2026-01-01-gone.md, which does not exist"


def test_mismatched_supersedes_warns(pages):
    # points at a real record, but one with a different id (another pair)
    _obs("2026-06-12-contradiction-bg.md", ["beta", "gamma"], kind="done")
    _obs(
        "2026-06-20-contradiction-ab.md",
        ["alpha", "beta"],
        supersedes="2026-06-12-contradiction-bg.md",
    )
    (w,) = _warnings()
    assert w.kind == "mismatched_supersedes"
    assert w.subject == f"{_DIR}/2026-06-20-contradiction-ab.md"


def test_alias_claimed_by_two_pages_warns(pages):
    _page("delta", aliases=["Shared Name"])
    _page("epsilon", aliases=["shared name"])
    (w,) = _warnings()
    assert w == obs.IntegrityWarning("ambiguous_alias", "shared-name", "pages: delta, epsilon")


def test_alias_shadowed_by_a_stem_does_not_warn(pages):
    # stem wins (D8b): 'beta' is a page, so an alias 'Beta' elsewhere is not ambiguous
    _page("delta", aliases=["Beta"])
    _page("epsilon", aliases=["beta"])
    assert _warnings() == []


def test_two_stems_that_slugify_alike_warn(pages):
    _page("Foo Bar", folder="concepts")
    _page("foo-bar", folder="ideas")
    (w,) = _warnings()
    assert w == obs.IntegrityWarning("ambiguous_stem", "foo-bar", "pages: foo bar, foo-bar")


# -- sorted text, every kind at once ---------------------------------------------------


def _all_kinds_fixture() -> None:
    _obs("2026-06-12-contradiction-ab.md", ["alpha", "beta"])
    _obs("2026-06-13-contradiction-ab.md", ["alpha", "beta"])
    _obs("2026-06-12-contradiction-ag.md", ["alpha", "gamma"], kind="done")
    _obs("2026-06-13-contradiction-ag.md", ["alpha", "gamma"], kind="obsolete")
    _obs("2026-06-20-contradiction-bg.md", ["beta", "gamma"], supersedes="2026-01-01-gone.md")
    _obs(
        "2026-06-21-contradiction-xx.md",
        ["gamma", "beta"],
        supersedes="2026-06-12-contradiction-ab.md",
    )
    _page("delta", aliases=["Shared Name"])
    _page("epsilon", aliases=["Shared Name"])
    _page("Foo Bar")
    _page("foo-bar", folder="ideas")


def test_every_kind_together_is_sorted_and_stable(pages):
    _all_kinds_fixture()
    ws = _warnings()
    assert [w.kind for w in ws] == sorted(w.kind for w in ws)
    assert set(_kinds()) == {
        "duplicate_pending",
        "multi_head",
        "dangling_supersedes",
        "mismatched_supersedes",
        "ambiguous_alias",
        "ambiguous_stem",
    }
    assert ws == sorted(ws)
    # The same records in another order give the same warnings (no walk order).
    valid, _ = obs.load_all()
    r = obs.resolver()
    shuffled = list(valid)
    random.Random(7).shuffle(shuffled)
    assert obs.integrity_warnings(obs.index(shuffled, r), r) == ws


# -- health wiring ----------------------------------------------------------------


def test_health_prints_the_sorted_block_counts_it_and_keeps_exit_0(pages):
    from sabnyx.commands import health as health_mod

    _all_kinds_fixture()
    expected = [w.text() for w in _warnings()]
    result = CliRunner().invoke(health_mod.health_cmd, [], catch_exceptions=False)
    assert result.exit_code == 0, result.output  # warnings never fail health
    lines = result.output.splitlines()
    start = lines.index("observation integrity:")
    assert [ln.strip() for ln in lines[start + 1 : start + 1 + len(expected)]] == expected
    audit = [e for e in ledger.replay() if e.kind == "health.audited"][-1]
    assert audit.payload["integrity_warnings"] == len(expected)


def test_health_clean_vault_prints_no_block(pages):
    from sabnyx.commands import health as health_mod

    _obs("2026-06-12-contradiction-ab.md", ["alpha", "beta"])
    result = CliRunner().invoke(health_mod.health_cmd, [], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert "observation integrity:" not in result.output
    audit = [e for e in ledger.replay() if e.kind == "health.audited"][-1]
    assert audit.payload["integrity_warnings"] == 0
