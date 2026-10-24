"""context.py: excerpt budget accounting and the slug index contract."""

from __future__ import annotations

from pathlib import Path

from sabnyx import context, fm


def _atlas_file(dir_: Path, name: str, *, description: str, body: str) -> Path:
    p = dir_ / f"{name}.md"
    fm.write(
        p,
        {
            "type": "concept",
            "description": description,
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


# -- excerpts budget (m7) --------------------------------------------------------

_TRIM_TOLERANCE = len(" […]") + 1  # the trim marker on the final excerpt


def test_excerpts_output_stays_within_total_chars(tmp_path):
    d = tmp_path / "atlas" / "concepts"
    d.mkdir(parents=True)
    desc = "a long enough description for the excerpt budget test here"
    for i in range(6):
        _atlas_file(d, f"file-{i}", description=desc, body=("word " * 400).strip())
    index = context.slug_index(d)

    total = 900
    out = context.excerpts(index, total_chars=total)
    # headers + excerpts both count; the final excerpt is trimmed, then we stop
    assert len(out) <= total + _TRIM_TOLERANCE
    # the budget genuinely bit: not every file rendered
    rendered = [s for s in index if f"### {s}" in out]
    assert 0 < len(rendered) < len(index)


def test_excerpts_trims_final_file_instead_of_including_in_full(tmp_path):
    d = tmp_path / "atlas" / "concepts"
    d.mkdir(parents=True)
    body = ("word " * 400).strip()  # ~2000 chars
    _atlas_file(d, "only", description="a long enough description for the trim test yes", body=body)
    index = context.slug_index(d)

    out = context.excerpts(index, total_chars=300)
    assert len(out) <= 300 + _TRIM_TOLERANCE
    assert "### only" in out
    assert "[…]" in out  # the overflowing excerpt was trimmed, not dumped whole


def test_excerpts_within_budget_unchanged_shape(tmp_path):
    d = tmp_path / "atlas" / "concepts"
    d.mkdir(parents=True)
    _atlas_file(
        d, "small", description="a long enough description for the shape test ok", body="tiny body"
    )
    out = context.excerpts(context.slug_index(d))
    assert "### small" in out
    assert "tiny body" in out
    assert "[…]" not in out


# -- slug_index (m8) ---------------------------------------------------------------


def test_slug_index_lowercases_keys(tmp_path):
    d = tmp_path / "atlas" / "concepts"
    d.mkdir(parents=True)
    p = _atlas_file(
        d, "Foo", description="a long enough description for the casing test ok", body="b"
    )
    idx = context.slug_index(d)
    assert "foo" in idx
    assert "Foo" not in idx
    assert idx["foo"] == p


def test_slug_index_collision_keeps_first_and_warns(tmp_path, capsys):
    d = tmp_path / "atlas"
    (d / "concepts").mkdir(parents=True)
    (d / "references").mkdir(parents=True)
    first = _atlas_file(
        d / "concepts",
        "dupe",
        description="a long enough description for the collision one",
        body="b",
    )
    second = _atlas_file(
        d / "references",
        "dupe",
        description="a long enough description for the collision two",
        body="b",
    )
    idx = context.slug_index(d)
    # deterministic walk order (sorted): concepts/ before references/ — first wins
    assert idx["dupe"] == first
    err = capsys.readouterr().err.replace("\n", "")  # rich soft-wraps long paths
    assert "duplicate slug 'dupe'" in err
    assert "shadowing" in err
    assert str(second) in err


def test_vault_and_scoped_indexes_share_collision_policy(tmp_vault, capsys):
    concepts = tmp_vault["ATLAS"] / "concepts"
    references = tmp_vault["ATLAS"] / "references"
    first = _atlas_file(
        concepts,
        "dupe",
        description="a long enough description for the first duplicate",
        body="first",
    )
    _atlas_file(
        references,
        "dupe",
        description="a long enough description for the second duplicate",
        body="second",
    )

    assert context.slug_index(tmp_vault["ATLAS"])["dupe"] == first
    assert context.vault_slug_index()["dupe"] == first
    assert capsys.readouterr().err.count("duplicate slug 'dupe'") == 2


# -- link_candidates: stem and alias claims kept apart, all claimants (IDD §6, D8b) --


def _page(dir_: Path, name: str, aliases: list | None = None) -> Path:
    meta: dict = {"type": "concept", "description": "a synthetic page for the link candidates test"}
    if aliases is not None:
        meta["aliases"] = aliases
    p = dir_ / f"{name}.md"
    fm.write(p, meta, f"# {name}\n")
    return p


def test_link_candidates_keeps_stem_and_alias_claims_apart(tmp_vault):
    d = tmp_vault["ATLAS_CONCEPTS"]
    _page(d, "foo", aliases=["Bar"])
    _page(d, "bar")
    stem, alias = context.link_candidates(context.vault_slug_index())
    assert stem["bar"] == {"bar"}  # bar's stem ...
    assert alias["bar"] == {"foo"}  # ... and foo's alias, never merged
    assert "foo" not in alias


def test_link_candidates_keeps_every_claimant(tmp_vault):
    """No first-wins: two pages claiming one token both appear."""
    _page(tmp_vault["ATLAS_CONCEPTS"], "alpha", aliases=["Shared Name"])
    _page(tmp_vault["ATLAS_CONCEPTS"], "beta", aliases=["shared name"])
    _page(tmp_vault["ATLAS_CONCEPTS"], "Foo Bar")
    _page(tmp_vault["ATLAS_IDEAS"], "foo-bar")
    stem, alias = context.link_candidates(context.vault_slug_index())
    assert alias["shared-name"] == {"alpha", "beta"}
    assert stem["foo-bar"] == {"foo bar", "foo-bar"}  # both stems slugify alike
    assert stem["foo bar"] == {"foo bar"}  # the lowercased stem form


def test_link_candidates_unloadable_page_claims_by_stem(tmp_vault):
    broken = tmp_vault["ATLAS_CONCEPTS"] / "broken.md"
    broken.write_text("---\naliases: [unclosed\n---\nbody\n", encoding="utf-8")
    stem, alias = context.link_candidates(context.vault_slug_index())
    assert stem["broken"] == {"broken"}
    assert alias == {}


def test_link_candidates_non_string_alias_does_not_crash(tmp_vault):
    """link_resolution_index would hand an int alias to slugify; here the page
    still claims by stem and the run survives."""
    _page(tmp_vault["ATLAS_CONCEPTS"], "foo", aliases=[42, "Real Alias"])
    _page(tmp_vault["ATLAS_CONCEPTS"], "bar", aliases=["Bar Alias"])
    stem, alias = context.link_candidates(context.vault_slug_index())
    assert stem["foo"] == {"foo"}
    assert alias == {"bar-alias": {"bar"}}
