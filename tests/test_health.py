"""util.age_days must accept every shape frontmatter dates arrive in.

YAML parses an unquoted `last_verified: 2026-06-13` into a `datetime.date`,
a quoted one into a `str`, and a full timestamp into a `datetime`. A bare
date used to crash the helper (`date` has no `.tzinfo`).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sabnyx.util import age_days


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
