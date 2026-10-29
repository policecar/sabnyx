"""Observation identity layer (IDD §5/§6, D8, D8b, D9, D14; threat row §10a).

Pure-function matrices build Resolver / ObservationFrontmatter / Observation
in memory; resolver() and the stem-vs-alias matrix use real pages under
tmp_vault. Sections are numbered after the operator's review requirements:

  (1) _component order and the four slugify traps
  (2) lookup stem-vs-alias matrix, agreeing with resolve_wikilink
  (3) recurrence_policy order
  (4) trust gate (IDENTITY_SOURCES)
  (5) chain head via supersedes, over the -2 sort trap
  (6) identity properties
  (7) no paths from tokens
"""

from __future__ import annotations

import builtins
import pathlib
import random
import re
from datetime import date
from pathlib import Path

import pytest

from sabnyx import context, fm, paths
from sabnyx import observations as obs
from sabnyx.observations import (
    AlreadyPending,
    Ambiguous,
    Found,
    Missing,
    New,
    Observation,
    ObsIndex,
    Reopen,
    Resolver,
    Suppressed,
)
from sabnyx.schemas import ObservationFrontmatter
from sabnyx.util import slugify

EMPTY = Resolver(stem={}, alias={})


def _resolver(stems: dict[str, set[str]] | None = None, aliases: dict[str, set[str]] | None = None):
    return Resolver(
        stem={k: frozenset(v) for k, v in (stems or {}).items()},
        alias={k: frozenset(v) for k, v in (aliases or {}).items()},
    )


def _rel(name: str) -> str:
    return f"{paths.OBSERVATIONS.relative_to(paths.DATA_ROOT).as_posix()}/{name}"


def _meta(category: str = "gap", **extra) -> ObservationFrontmatter:
    return ObservationFrontmatter.model_validate(
        {
            "type": "observation",
            "category": category,
            "observed": date(2026, 6, 12),
            "source_agent": "sabnyx-entities",
        }
        | extra
    )


def _closed(kind: str, resolved: date = date(2026, 6, 13)) -> dict:
    return {
        "status": "promoted" if kind == "done" else "archived",
        "resolved": resolved,
        "resolution": f"closed as {kind}",
        "resolution_kind": kind,
    }


def _rec(name: str, meta: ObservationFrontmatter) -> Observation:
    return Observation(path=paths.OBSERVATIONS / name, rel=_rel(name), fm=meta)


def _gap(name: str, target: str = "foo-page", **extra) -> Observation:
    return _rec(name, _meta("gap", target=target, **extra))


def _oid(meta: ObservationFrontmatter, resolver: Resolver = EMPTY) -> str:
    """obs_id of a meta the test expects to carry identity; fails on prose.

    Without this, a None id would reach recurrence_policy as an unknown key
    and come back New(), so a test asserting New() could pass on prose.
    """
    oid = obs.obs_id(meta, resolver)
    assert oid is not None, f"expected an identity-bearing observation: {meta!r}"
    return oid


def _page(dir_: Path, name: str, aliases: list[str] | None = None) -> None:
    meta: dict = {"type": "concept", "description": "a synthetic page for the identity layer tests"}
    if aliases is not None:
        meta["aliases"] = aliases
    fm.write(dir_ / f"{name}.md", meta, f"# {name}\n")


# -- (1) _component order and the four slugify traps --------------------------------


@pytest.mark.parametrize("value", ["", "   "])
def test_component_blank_is_none_not_untitled(value):
    assert slugify(value).startswith("untitled-")  # the trap
    assert obs._component(value, EMPTY) is None


@pytest.mark.parametrize(("value", "want"), [("Foo.md", "foo"), ("FOO.MD", "foo")])
def test_component_strips_md_before_slugify(value, want):
    assert slugify(value) == f"{want}-md"  # the trap
    assert obs._component(value, EMPTY) == want


@pytest.mark.parametrize("value", ["atlas/people/foo", "vault/atlas/people/foo.md"])
def test_component_keeps_last_path_segment(value):
    assert slugify("atlas/people/foo") == "atlas-people-foo"  # the trap
    assert obs._component(value, EMPTY) == "foo"


def test_component_long_names_stay_distinct():
    a, b = "x" * 80 + "a", "x" * 80 + "b"  # two distinct 81-char names
    assert slugify(a) == slugify(b)  # the trap at the default max_len=80
    assert obs._component(a, EMPTY) == a
    assert obs._component(b, EMPTY) == b


@pytest.mark.parametrize("value", ["[[foo|Alias]]", "[[foo#h]]", "[[foo]]", " [[ foo ]] "])
def test_component_unwraps_wikilinks(value):
    assert obs._component(value, EMPTY) == "foo"


@pytest.mark.parametrize("value", ["[[ ]]", "foo/", "atlas/ /", ".md", "foo/.md", "foo/ .MD"])
def test_component_empty_wikilink_or_segment_is_none(value):
    assert obs._component(value, EMPTY) is None


@pytest.mark.parametrize("value", ["[[]]", "!!!", "…", "---", "[[!!!]]", "  ¿?  "])
def test_component_junk_token_is_prose_not_untitled(value):
    # slugify invents 'untitled-<hash>' for text with no slug-able content:
    # the empty-string trap by another road. Such a token is no identity.
    assert slugify(value).startswith("untitled-")  # the trap
    assert obs._component(value, EMPTY) is None


def test_component_unclaimed_non_latin_name_is_prose():
    # Same rule, stated so it is a decision, not an accident: a fully
    # non-Latin name no page claims has no slug, so no identity. A page that
    # claims it by stem is still Found (lookup runs first).
    assert obs._component("知識の庭", EMPTY) is None


@pytest.mark.parametrize(
    ("value", "want"), [("untitled-notes", "untitled-notes"), ("Untitled", "untitled")]
)
def test_component_literal_untitled_name_is_still_a_slug(value, want):
    # The rule keys off slugify's fallback, not the 'untitled-' prefix.
    assert obs._component(value, EMPTY) == want


def test_contradiction_with_a_junk_involves_has_no_identity():
    meta = _meta("contradiction", involves=["[[]]", "foo"], source_agent="sabnyx-scan")
    assert obs.signal(meta, EMPTY) is None
    assert obs.obs_id(meta, EMPTY) is None
    assert obs.index([_rec("j.md", meta)], EMPTY) == _idx()


def test_component_dotdot_env_is_a_token():
    assert obs._component("../../.env", EMPTY) == "env"


def test_component_found_ambiguous_missing():
    r = _resolver(stems={"foo": {"foo"}}, aliases={"jd": {"jane-doe"}, "two": {"a", "b"}})
    assert obs._component("[[Foo]]", r) == "foo"  # Found -> the slug
    assert obs._component("JD", r) == "jane-doe"  # alias Found -> the page slug
    assert obs._component("Two", r) is None  # Ambiguous -> None, never guessed
    assert obs._component("Not A Page", r) == "not-a-page"  # Missing -> slugified


# -- (2) lookup stem-vs-alias matrix (real pages) -----------------------------------


@pytest.fixture
def matrix_vault(tmp_vault):
    concepts, people, ideas = (
        tmp_vault["ATLAS_CONCEPTS"],
        tmp_vault["ATLAS_PEOPLE"],
        tmp_vault["ATLAS_IDEAS"],
    )
    _page(concepts, "foo")
    _page(people, "jane-doe", aliases=["JD", "Janie"])
    _page(concepts, "bar")
    _page(concepts, "qux", aliases=["Bar"])  # qux's alias == bar's stem
    _page(concepts, "alpha", aliases=["Shared Name"])
    _page(concepts, "beta", aliases=["shared name"])
    _page(concepts, "Foo Bar")
    _page(ideas, "foo-bar")  # stem-stem: both slugify to 'foo-bar'
    _page(people, "Mary Major")  # slug.lower() != slugify(slug)
    _page(concepts, "shared")  # one stem, two folders: vault_slug_index keeps one
    _page(tmp_vault["ATLAS_PROJECTS"], "shared")
    _page(concepts, "Twin")  # case variants of one stem
    _page(people, "twin")
    return tmp_vault


LOOKUP_MATRIX = [
    ("foo", Found("foo")),  # stem hit
    ("FOO", Found("foo")),
    ("JD", Found("jane-doe")),  # alias only, one claimant
    ("janie", Found("jane-doe")),
    ("bar", Found("bar")),  # stem beats qux's alias
    ("Bar", Found("bar")),
    # Claimants are pages (vault-relative paths): two pages can share a slug.
    ("Shared Name", Ambiguous(("atlas/concepts/alpha.md", "atlas/concepts/beta.md"))),
    ("shared-name", Ambiguous(("atlas/concepts/alpha.md", "atlas/concepts/beta.md"))),
    # two stems slugify alike
    ("foo-bar", Ambiguous(("atlas/concepts/Foo Bar.md", "atlas/ideas/foo-bar.md"))),
    # one stem in two folders, and case variants: never first-in-walk-order
    ("shared", Ambiguous(("atlas/concepts/shared.md", "atlas/projects/shared.md"))),
    ("TWIN", Ambiguous(("atlas/concepts/Twin.md", "atlas/people/twin.md"))),
    ("Mary Major", Found("mary major")),  # by slug.lower()
    ("mary-major", Found("mary major")),  # by slugify(slug)
    ("nobody", Missing()),
]


@pytest.mark.parametrize(("token", "want"), LOOKUP_MATRIX)
def test_lookup_stem_vs_alias_matrix(matrix_vault, token, want):
    assert obs.lookup(token, obs.resolver()) == want


def test_lookup_found_agrees_with_resolve_wikilink(matrix_vault):
    """Identity and health/entities agree on every name lookup calls Found."""
    r = obs.resolver()
    resolution = context.link_resolution_index(context.vault_slug_index())
    found = [(t, obs.lookup(t, r)) for t, _ in LOOKUP_MATRIX]
    found = [(t, res) for t, res in found if isinstance(res, Found)]
    assert len(found) == 8
    for token, res in found:
        assert res.slug == context.resolve_wikilink(token, resolution), token


def test_a_stem_shared_by_two_pages_gives_no_identity(matrix_vault):
    """The first-wins slug index would name one of the two pages; identity
    must not: no id for a gap on it or a contradiction involving it."""
    r = obs.resolver()
    assert obs.obs_id(_meta(target="shared"), r) is None
    assert obs.obs_id(_meta("contradiction", involves=["shared", "foo"]), r) is None
    assert obs.obs_id(_meta(target="foo"), r) is not None  # control: a unique stem


def test_resolver_excludes_observations_and_is_rebuilt_per_call(tmp_vault):
    _page(tmp_vault["ATLAS_CONCEPTS"], "foo")
    (tmp_vault["OBSERVATIONS"] / "noted.md").write_text("---\n---\n", encoding="utf-8")
    first = obs.resolver()
    assert "noted" not in first.stem
    _page(tmp_vault["ATLAS_CONCEPTS"], "bar")
    assert obs.lookup("bar", first) == Missing()
    assert obs.lookup("bar", obs.resolver()) == Found("bar")


# -- (3) recurrence_policy order ----------------------------------------------------


def _idx(pending=None, resolved=None) -> ObsIndex:
    return ObsIndex(
        pending=pending or {}, resolved=resolved or {}, multi_head={}, dangling=(), mismatched=()
    )


PENDING = _gap("g.md")
DONE_HEAD = _gap("g-done.md", **_closed("done"))
WONTFIX_HEAD = _gap("g-wontfix.md", **_closed("wontfix"))


@pytest.mark.parametrize("head", [DONE_HEAD, WONTFIX_HEAD], ids=["done", "wontfix"])
def test_policy_pending_wins_over_any_head(head):
    idx = _idx(pending={"x": [PENDING]}, resolved={"x": head})
    assert obs.recurrence_policy(idx, "x") == AlreadyPending(PENDING)


def test_policy_done_head_reopens():
    assert obs.recurrence_policy(_idx(resolved={"x": DONE_HEAD}), "x") == Reopen(DONE_HEAD)


@pytest.mark.parametrize("kind", ["wontfix", "duplicate", "obsolete"])
def test_policy_dismissed_head_is_suppressed(kind):
    head = _gap(f"g-{kind}.md", **_closed(kind))
    assert obs.recurrence_policy(_idx(resolved={"x": head}), "x") == Suppressed(head)


def test_policy_nothing_is_new():
    assert obs.recurrence_policy(_idx(), "x") == New()
    assert obs.recurrence_policy(_idx(resolved={"y": DONE_HEAD}), "x") == New()


def test_policy_already_pending_is_first_in_filename_order(tmp_vault):
    a, b = _gap("2026-06-12-gap-foo.md"), _gap("2026-06-12-gap-foo-2.md")
    idx = obs.index([a, b], EMPTY)
    oid = _oid(a.fm)
    assert idx.pending[oid] == [b, a]  # '-' sorts before '.': foo-2 first by name
    assert obs.recurrence_policy(idx, oid) == AlreadyPending(b)


# -- (4) trust gate ----------------------------------------------------------------

UNTRUSTED = ["sabnyx-telegram", "sabnyx-r1", "sabnyx-ingest-web"]


def _signals(agent: str, **status) -> tuple[ObservationFrontmatter, ObservationFrontmatter]:
    return (
        _meta("contradiction", involves=["foo", "bar"], source_agent=agent, **status),
        _meta("gap", target="qux", source_agent=agent, **status),
    )


@pytest.mark.parametrize("agent", UNTRUSTED)
def test_trust_gate_untrusted_source_has_no_identity(agent):
    for meta in _signals(agent):
        assert obs.signal(meta, EMPTY) is None
        assert obs.obs_id(meta, EMPTY) is None


@pytest.mark.parametrize("agent", sorted(obs.IDENTITY_SOURCES))
def test_trust_gate_trusted_source_has_identity(agent):
    """The same records with a trusted source do get an id (not vacuous)."""
    contradiction, gap = _signals(agent)
    assert obs.signal(contradiction, EMPTY) == ["bar", "foo"]
    assert obs.signal(gap, EMPTY) == "qux"
    assert obs.obs_id(contradiction, EMPTY) is not None


@pytest.mark.parametrize("agent", UNTRUSTED)
@pytest.mark.parametrize("kind", ["wontfix", "done"])
def test_trust_gate_untrusted_cannot_suppress_or_reopen(tmp_vault, agent, kind):
    untrusted = [_rec(f"u-{i}.md", m) for i, m in enumerate(_signals(agent, **_closed(kind)))] + [
        _rec(f"p-{i}.md", m) for i, m in enumerate(_signals(agent))
    ]
    idx = obs.index(untrusted, EMPTY)
    assert idx == _idx()  # absent from every map
    for trusted in _signals("sabnyx-entities"):
        assert obs.recurrence_policy(idx, _oid(trusted)) == New()

    # Control: the same dismissed records from a trusted source DO suppress.
    trusted_recs = [
        _rec(f"t-{i}.md", m) for i, m in enumerate(_signals("sabnyx-scan", **_closed("wontfix")))
    ]
    tidx = obs.index(trusted_recs, EMPTY)
    for meta, rec in zip(_signals("sabnyx-entities"), trusted_recs, strict=True):
        assert obs.recurrence_policy(tidx, _oid(meta)) == Suppressed(rec)


# -- (5) chain head via supersedes, over the -2 sort trap ---------------------------

FOO, FOO2, FOO3 = (f"2026-06-12-gap-foo{s}.md" for s in ("", "-2", "-3"))


def test_chain_head_filename_sort_trap_is_real():
    assert sorted([FOO, FOO2, FOO3]) == [FOO2, FOO3, FOO]  # max filename = the OLDEST


def test_chain_head_follows_supersedes_past_sort_trap(tmp_vault):
    foo = _gap(FOO, **_closed("done", date(2026, 6, 13)))
    foo2 = _gap(FOO2, supersedes=_rel(FOO), **_closed("wontfix", date(2026, 6, 14)))
    foo3 = _gap(FOO3, supersedes=_rel(FOO2), **_closed("done", date(2026, 6, 15)))
    idx = obs.index([foo, foo2, foo3], EMPTY)
    oid = _oid(foo.fm)
    assert idx.resolved == {oid: foo3}
    assert idx.multi_head == {}
    assert obs.recurrence_policy(idx, oid) == Reopen(foo3)


def test_chain_head_middle_link_is_not_max_filename_nor_latest_date(tmp_vault):
    """foo-2 supersedes foo, so foo-2 is the head, although foo.md is the max
    filename and carries the later `resolved` date (hand-edited)."""
    foo = _gap(FOO, **_closed("done", date(2026, 6, 20)))
    foo2 = _gap(FOO2, supersedes=_rel(FOO), **_closed("wontfix", date(2026, 6, 14)))
    idx = obs.index([foo, foo2], EMPTY)
    oid = _oid(foo.fm)
    assert idx.resolved == {oid: foo2}
    assert obs.recurrence_policy(idx, oid) == Suppressed(foo2)


@pytest.mark.parametrize(
    ("a_dates", "b_dates", "winner"),
    [
        ((date(2026, 6, 13), date(2026, 6, 12)), (date(2026, 6, 15), date(2026, 6, 1)), "b"),
        ((date(2026, 6, 13), date(2026, 6, 12)), (date(2026, 6, 13), date(2026, 6, 1)), "a"),
        ((date(2026, 6, 13), date(2026, 6, 12)), (date(2026, 6, 13), date(2026, 6, 12)), "b"),
    ],
    ids=["by-resolved", "by-observed", "by-rel"],
)
def test_chain_head_multi_head_tie_break_and_report(tmp_vault, a_dates, b_dates, winner):
    a = _rec("a.md", _meta("gap", target="foo", observed=a_dates[1], **_closed("done", a_dates[0])))
    b = _rec(
        "b.md", _meta("gap", target="foo", observed=b_dates[1], **_closed("wontfix", b_dates[0]))
    )
    idx = obs.index([b, a], EMPTY)
    oid = _oid(a.fm)
    assert idx.resolved[oid] == {"a": a, "b": b}[winner]
    assert idx.multi_head == {oid: (a, b)}


def test_chain_head_dangling_and_mismatched_reported(tmp_vault):
    head = _gap("head.md", **_closed("done"))
    dangling = _gap("dangling.md", supersedes=_rel("gone.md"), **_closed("wontfix"))
    other = _gap("other.md", target="bar-page", supersedes=_rel("head.md"), **_closed("wontfix"))
    prose = _rec("prose.md", _meta("friction", supersedes=_rel("head.md")))
    idx = obs.index([head, dangling, other, prose], EMPTY)
    oid = obs.obs_id(head.fm, EMPTY)
    assert idx.dangling == (dangling,)
    assert idx.mismatched == (other, prose)  # another id, and no id at all
    # Neither edge removes a head: head and dangling are both candidates.
    assert idx.multi_head == {oid: (dangling, head)}
    assert idx.resolved[_oid(other.fm)] == other


@pytest.mark.parametrize("kind", ["wontfix", "done"])
def test_chain_head_forged_cycle_never_new(tmp_vault, kind):
    a = _gap("a.md", supersedes=_rel("b.md"), **_closed(kind, date(2026, 6, 13)))
    b = _gap("b.md", supersedes=_rel("a.md"), **_closed(kind, date(2026, 6, 14)))
    idx = obs.index([a, b], EMPTY)
    oid = _oid(a.fm)
    assert idx.multi_head == {oid: (a, b)}
    assert idx.resolved[oid] == b
    assert obs.recurrence_policy(idx, oid) != New()


def test_chain_head_self_supersede_never_new(tmp_vault):
    a = _gap("a.md", supersedes=_rel("a.md"), **_closed("wontfix"))
    idx = obs.index([a], EMPTY)
    oid = _oid(a.fm)
    assert idx.multi_head == {oid: (a,)}
    assert obs.recurrence_policy(idx, oid) == Suppressed(a)


def test_chain_head_reopen_in_progress_is_pending_not_multi_head(tmp_vault):
    """A pending reopen supersedes the done head: AlreadyPending, no health warning."""
    foo = _gap(FOO, **_closed("done"))
    foo2 = _gap(FOO2, supersedes=_rel(FOO))
    idx = obs.index([foo, foo2], EMPTY)
    oid = _oid(foo.fm)
    assert idx.pending == {oid: [foo2]}
    assert idx.resolved == {}
    assert idx.multi_head == {}
    assert obs.recurrence_policy(idx, oid) == AlreadyPending(foo2)


def test_index_is_independent_of_input_order(tmp_vault):
    recs = [
        _gap(FOO, **_closed("done", date(2026, 6, 13))),
        _gap(FOO2, supersedes=_rel(FOO), **_closed("wontfix", date(2026, 6, 14))),
        _gap(FOO3, supersedes=_rel(FOO2)),
        _gap("x.md", target="bar-page", **_closed("done")),
        _gap("y.md", target="bar-page", **_closed("obsolete")),
        _gap("z.md", target="baz-page"),
        _gap("z-2.md", target="baz-page"),
        _gap("d.md", target="qux-page", supersedes=_rel("gone.md"), **_closed("wontfix")),
        _rec("m.md", _meta("friction", supersedes=_rel("x.md"))),
    ]
    want = obs.index(recs, EMPTY)
    rng = random.Random(1234)
    for _ in range(25):
        shuffled = recs[:]
        rng.shuffle(shuffled)
        assert obs.index(shuffled, EMPTY) == want


# -- (6) identity properties -------------------------------------------------------


def _contradiction(*involves: str) -> ObservationFrontmatter:
    return _meta("contradiction", involves=list(involves))


def test_identity_contradiction_is_order_independent():
    ab, ba = _contradiction("foo", "bar"), _contradiction("bar", "foo")
    assert obs.obs_id(ab, EMPTY) == obs.obs_id(ba, EMPTY) is not None


@pytest.mark.parametrize(("a", "b"), [("foo", "foo.md"), ("foo", "[[foo]]"), ("atlas/foo", "FOO")])
def test_identity_same_component_twice_is_prose(a, b):
    assert obs.signal(_contradiction(a, b), EMPTY) is None


@pytest.mark.parametrize("involves", [["foo"], ["foo", "bar", "baz"], []])
def test_identity_contradiction_needs_exactly_two(involves):
    assert obs.signal(_contradiction(*involves), EMPTY) is None


def test_identity_id_format():
    oid = _oid(_contradiction("foo", "bar"))
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", oid)


def test_identity_category_is_part_of_the_id():
    gap = _oid(_meta("gap", target="foo"))
    assert gap != obs.obs_id(_contradiction("foo", "bar"), EMPTY)
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", gap)


def test_identity_gap_target_alias_normalises_to_page(tmp_vault):
    _page(tmp_vault["ATLAS_PEOPLE"], "jane-doe", aliases=["JD"])
    r = obs.resolver()
    via_alias, via_slug = _meta("gap", target="jd"), _meta("gap", target="jane-doe")
    assert obs.signal(via_alias, r) == "jane-doe"
    assert obs.obs_id(via_alias, r) == obs.obs_id(via_slug, r)


def test_identity_ambiguous_component_is_prose():
    r = _resolver(aliases={"shared-name": {"alpha", "beta"}})
    assert obs.signal(_contradiction("Shared Name", "foo"), r) is None
    assert obs.signal(_meta("gap", target="shared-name"), r) is None
    assert obs.obs_id(_meta("gap", target="shared-name"), r) is None


@pytest.mark.parametrize("category", ["connection", "friction", "structural"])
def test_identity_prose_categories_have_none(category):
    meta = _meta(category, involves=["foo", "bar"], target="foo")
    assert obs.signal(meta, EMPTY) is None


# -- (7) no paths from tokens (threat row, IDD §10a) --------------------------------

PATHLIKE = ["../../.env", "/etc/hosts", "/abs/vault/atlas/people/foo.md", "~/.ssh/id_rsa"]


def test_tokens_never_reach_the_filesystem(tmp_vault, monkeypatch):
    _page(tmp_vault["ATLAS_CONCEPTS"], "foo", aliases=["Env"])
    r = obs.resolver()  # built BEFORE tracking: reading pages is expected here
    recs = [
        _rec("c.md", _contradiction(PATHLIKE[0], PATHLIKE[1])),
        _rec("c2.md", _contradiction(PATHLIKE[2], PATHLIKE[3])),
        _rec("c3.md", _contradiction(PATHLIKE[0], "bar")),
        # target is slug-validated on load; model_copy skips validation, as a
        # hand-built record might, to prove the token still never opens.
        _rec("g.md", _meta("gap").model_copy(update={"target": PATHLIKE[0]})),
    ]

    calls: list[tuple[str, tuple]] = []

    def tracking(name, real):
        def wrapper(*args, **kwargs):
            calls.append((name, args))
            return real(*args, **kwargs)

        return wrapper

    with monkeypatch.context() as mp:  # undoes only these, not tmp_vault's
        mp.setattr(builtins, "open", tracking("open", builtins.open))
        mp.setattr(pathlib.Path, "open", tracking("Path.open", pathlib.Path.open))
        mp.setattr(pathlib.Path, "read_text", tracking("Path.read_text", pathlib.Path.read_text))
        mp.setattr(fm, "load", tracking("fm.load", fm.load))
        for tok in PATHLIKE:
            obs._component(tok, r)
        for o in recs:
            obs.signal(o.fm, r)
            obs.obs_id(o.fm, r)
        idx = obs.index(recs, r)

    assert calls == []
    assert obs._component("../../.env", r) == "foo"  # resolved by alias 'Env', as a name
    assert len(idx.pending) == 4  # identity-bearing, so the run was not vacuous
