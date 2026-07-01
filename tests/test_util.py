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


# -- marker blocks (SPEC §8) --------------------------------------------------------


def test_set_marker_block_half_open_begin_raises():
    body = "# T\n\n<!-- BEGIN sabnyx:related -->\n- [[a]]\n"
    with pytest.raises(ValueError, match="half-open marker block sabnyx:related.*BEGIN"):
        util.set_marker_block(body, "related", ["- [[b]]"])


def test_set_marker_block_half_open_end_raises():
    body = "# T\n\n- [[a]]\n<!-- END sabnyx:related -->\n"
    with pytest.raises(ValueError, match="half-open marker block sabnyx:related.*END"):
        util.set_marker_block(body, "related", ["- [[b]]"])
