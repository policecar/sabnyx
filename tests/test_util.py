"""Slug and marker-block invariants in util.py."""

from __future__ import annotations

import pytest

from sabnyx import util

# -- slugify ---------------------------------------------------------------------


def test_slugify_basic_kebab():
    assert util.slugify("My Source: Notes!") == "my-source-notes"


def test_slugify_non_ascii_titles_do_not_collide():
    # Two distinct fully non-Latin titles must not both collapse to "untitled"
    # (write-once sources.capture would dead-end the second one).
    a = util.slugify("知識の庭")
    b = util.slugify("茶の湯の心")
    assert a != b
    assert a.startswith("untitled-")
    assert b.startswith("untitled-")
    # still pure ASCII kebab (SPEC §6)
    assert a.isascii()
    assert b.isascii()


def test_slugify_non_ascii_is_deterministic():
    assert util.slugify("知識の庭") == util.slugify("知識の庭")


_SLUG_EDGE_INPUTS = [
    "",
    "-",
    "---a---",
    "a--b",
    "Foo.md",
    "atlas/people/foo",
    "[[Jane Doe|alias]]",
    "  spaced  out  ",
    "知識の庭",
    "Ünïcödé Náme",
    "x" * 79 + "-y",  # truncation lands on the hyphen: rstrip must remove it
    "a " * 60,
]


def _seeded_inputs(n: int = 500) -> list[str]:
    import random

    rng = random.Random(0)  # deterministic: same 500 strings every run
    alphabet = "aZ09 -_./:#|[]\t\nÄé知\u0085 "
    return ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 120))) for _ in range(n)]


@pytest.mark.parametrize("max_len", [80, 10_000])
def test_slugify_output_always_fullmatches_slug_re(max_len):
    # SLUG_RE validates fields that must already be slugs (an observation's
    # `target`, produced by slugify). If slugify could emit something
    # SLUG_RE rejects, a producer would write a target its own schema refuses.
    for text in _SLUG_EDGE_INPUTS + _seeded_inputs():
        out = util.slugify(text, max_len=max_len)
        assert util.SLUG_RE.fullmatch(out), (text, out)


@pytest.mark.parametrize(
    "bad", ["", "Foo", "foo_bar", "foo--bar", "-foo", "foo-", "foo bar", "foo.md", "a/b"]
)
def test_slug_re_rejects_non_slugs(bad):
    assert util.SLUG_RE.fullmatch(bad) is None


def test_slug_fold_is_slugify_without_the_untitled_fallback():
    for text in _SLUG_EDGE_INPUTS + _seeded_inputs():
        for max_len in (80, 10_000):
            folded = util.slug_fold(text, max_len=max_len)
            plain = util.slugify(text, max_len=max_len)
            if folded is None:
                assert plain.startswith("untitled-"), text  # the fallback fired
            else:
                assert folded == plain, text


@pytest.mark.parametrize("text", ["", "!!!", "[[]]", "知識の庭", "  -  "])
def test_slug_fold_is_none_when_nothing_slug_able_remains(text):
    assert util.slug_fold(text) is None


def test_slug_fold_keeps_a_literal_untitled_name():
    assert util.slug_fold("untitled-notes") == "untitled-notes"


@pytest.mark.parametrize("method", ["match", "search", "fullmatch"])
def test_slug_re_rejects_trailing_newline_under_every_call(method):
    # `$` matches before a trailing "\n"; the \Z anchor must not. A caller
    # using .match must not get a slug with a newline in it.
    assert getattr(util.SLUG_RE, method)("abc\n") is None
    assert getattr(util.SLUG_RE, method)("abc") is not None


# -- marker blocks (SPEC §8) --------------------------------------------------------


def test_set_marker_block_half_open_begin_raises():
    body = "# T\n\n<!-- BEGIN sabnyx:related -->\n- [[a]]\n"
    with pytest.raises(ValueError, match="half-open marker block sabnyx:related.*BEGIN"):
        util.set_marker_block(body, "related", ["- [[b]]"])


def test_set_marker_block_half_open_end_raises():
    body = "# T\n\n- [[a]]\n<!-- END sabnyx:related -->\n"
    with pytest.raises(ValueError, match="half-open marker block sabnyx:related.*END"):
        util.set_marker_block(body, "related", ["- [[b]]"])


def test_set_marker_block_renders_the_given_newline_only_inside_its_region():
    body = "a  \r\n\r\n<!-- BEGIN sabnyx:related -->\r\n<!-- END sabnyx:related -->\r\ntail\r\n"
    out = util.set_marker_block(body, "related", ["- [[a]]", "- [[b]]"], newline="\r\n")
    assert out == body.replace("-->\r\n<!-- END", "-->\r\n- [[a]]\r\n- [[b]]\r\n<!-- END")


@pytest.mark.parametrize(
    "body",
    [
        "<!-- END sabnyx:related -->\nx\n<!-- BEGIN sabnyx:related -->\n",
        "<!-- BEGIN sabnyx:related -->\n<!-- END sabnyx:related -->\n" * 2,
        "<!-- BEGIN sabnyx:related -->\n<!-- BEGIN sabnyx:related -->\n<!-- END sabnyx:related -->\n",
    ],
    ids=["end-first", "two-blocks", "nested"],
)
def test_set_marker_block_refuses_ambiguous_layouts(body):
    # the old split(begin)[0] + split(end)[1] duplicated "x" for end-first
    with pytest.raises(ValueError, match="sabnyx:related"):
        util.set_marker_block(body, "related", ["- [[b]]"])


def test_age_days_reads_the_ledger_clock(monkeypatch):
    """age_days goes through ledger.now(), the single clock seam, so a fixed
    clock fixes every age health prints (goldens depend on it)."""
    from datetime import UTC, date, datetime

    from sabnyx import ledger

    monkeypatch.setattr(ledger, "now", lambda: datetime(2030, 1, 11, tzinfo=UTC))
    assert util.age_days(date(2030, 1, 1)) == 10
    assert util.age_days("2030-01-10T00:00:00Z") == 1
