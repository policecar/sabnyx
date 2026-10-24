"""ObservationFrontmatter: lifecycle fields and the strict status<->kind rule.

Status is derived from resolution_kind (IDD §5, D7b): a file whose status
disagrees with its resolution fields is malformed, never silently counted.
Every row of the derived rule gets an accepting and a rejecting case, so a
validator that drops one direction fails here.
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from sabnyx import fm
from sabnyx.schemas import ObservationFrontmatter

BASE = {
    "type": "observation",
    "category": "gap",
    "observed": "2026-06-12",
    "source_agent": "sabnyx-entities",
    "involves": [],
}
RESOLVED = {"resolved": "2026-06-13", "resolution": "page now exists: foo"}


def _validate(**fields) -> ObservationFrontmatter:
    return ObservationFrontmatter.model_validate(BASE | fields)


# -- the derived rule -------------------------------------------------------------


@pytest.mark.parametrize(
    "fields",
    [
        {},  # legacy: no status key at all → pending
        {"status": "pending"},
        {"status": "promoted", **RESOLVED, "resolution_kind": "done"},
        {"status": "archived", **RESOLVED, "resolution_kind": "wontfix"},
        {"status": "archived", **RESOLVED, "resolution_kind": "duplicate"},
        {"status": "archived", **RESOLVED, "resolution_kind": "obsolete"},
    ],
)
def test_valid_status_kind_combinations(fields):
    ob = _validate(**fields)
    assert ob.status == fields.get("status", "pending")


@pytest.mark.parametrize(
    "fields",
    [
        # pending ⇒ all three absent: any one present is a contradiction
        {"status": "pending", "resolved": "2026-06-13"},
        {"status": "pending", "resolution": "x"},
        {"status": "pending", "resolution_kind": "done"},
        {"status": "pending", **RESOLVED, "resolution_kind": "done"},
        # promoted/archived ⇒ all three present
        {"status": "archived"},  # the hand-set archive (old test_health fixture shape)
        {"status": "promoted"},
        {"status": "archived", "resolved": "2026-06-13", "resolution_kind": "wontfix"},
        {"status": "archived", "resolution": "x", "resolution_kind": "wontfix"},
        {"status": "archived", **RESOLVED},
        # kind decides status: done ⇔ promoted
        {"status": "archived", **RESOLVED, "resolution_kind": "done"},
        {"status": "promoted", **RESOLVED, "resolution_kind": "wontfix"},
        {"status": "promoted", **RESOLVED, "resolution_kind": "duplicate"},
        {"status": "promoted", **RESOLVED, "resolution_kind": "obsolete"},
        # unknown kind
        {"status": "archived", **RESOLVED, "resolution_kind": "rejected"},
    ],
)
def test_status_kind_mismatches_are_rejected(fields):
    with pytest.raises(ValidationError):
        _validate(**fields)


def test_hand_set_archive_message_says_how_to_fix():
    with pytest.raises(ValidationError, match="revert status to pending.*observations archive"):
        _validate(status="archived")


# -- resolution: non-empty, single line (P2, Demonstrate c5) ----------------------


@pytest.mark.parametrize(
    "reason",
    [
        "",
        "   ",
        "line one\nline two",  # LF
        "a\r\nb",  # CRLF
        "a\rb",  # CR
        "a\vb",  # VT
        "a\fb",  # FF
        "a\u0085b",  # NEL — renders as one line in YAML, fails the round-trip
        "a b",  # LS
        "a b",  # PS
    ],
)
def test_resolution_must_be_one_nonempty_line(reason):
    with pytest.raises(ValidationError, match="single line"):
        _validate(
            status="archived", resolved="2026-06-13", resolution=reason, resolution_kind="wontfix"
        )


@pytest.mark.parametrize("reason", ["fixed", "status: archived", "a: b # c", "---", "  padded  "])
def test_resolution_single_line_variants_are_accepted(reason):
    ob = _validate(
        status="archived", resolved="2026-06-13", resolution=reason, resolution_kind="wontfix"
    )
    assert ob.resolution == reason
    assert ob.resolved == date(2026, 6, 13)


# -- target: a slug ---------------------------------------------------------------


@pytest.mark.parametrize("target", ["foo", "jane-doe", "a1-b2-c3"])
def test_target_accepts_slugs(target):
    assert _validate(target=target).target == target


@pytest.mark.parametrize(
    "target", ["", "Jane Doe", "jane_doe", "foo.md", "atlas/people/foo", "../../.env", "foo-"]
)
def test_target_rejects_non_slugs(target):
    with pytest.raises(ValidationError, match="target must be a slug"):
        _validate(target=target)


# -- supersedes: a DATA_ROOT-relative observation path ----------------------------


@pytest.mark.parametrize("status", ["pending", "archived"])  # allowed in any status
def test_supersedes_accepts_observation_rel(status):
    extra = {**RESOLVED, "resolution_kind": "wontfix"} if status == "archived" else {}
    ob = _validate(
        status=status, supersedes="vault/intake/.observations/2026-06-12-gap-foo.md", **extra
    )
    assert ob.supersedes == "vault/intake/.observations/2026-06-12-gap-foo.md"


@pytest.mark.parametrize(
    "value",
    [
        "2026-06-12-gap-foo.md",  # bare filename, not the ledger subject form
        "vault/intake/.observations/2026-06-12-gap-foo",  # no .md
        "vault/intake/.observations/sub/x.md",  # nested
        "vault/intake/.observations/../secret.md",  # escapes via ..
        "vault/intake/x.md",  # wrong folder
        "/abs/vault/intake/.observations/x.md",  # absolute
        "sha256:" + "0" * 64,  # an id, not a path
        "vaultXintake/.observations/x.md",  # the prefix is escaped, not a regex
    ],
)
def test_supersedes_rejects_anything_else(value):
    with pytest.raises(ValidationError, match="supersedes must be"):
        _validate(supersedes=value)


# -- producer, and extra keys stay forbidden --------------------------------------


@pytest.mark.parametrize("producer", ["entities", "scan", "reweave", "ingest", "manual"])
def test_producer_values(producer):
    assert _validate(producer=producer).producer == producer


def test_producer_rejects_channel_names():
    # producer is the code path, never the channel (telegram/R1 are "ingest").
    with pytest.raises(ValidationError):
        _validate(producer="telegram")


def test_obs_id_is_never_a_frontmatter_key():
    # obs_id is computed on every read, never stored (extra="forbid" stays).
    with pytest.raises(ValidationError):
        _validate(obs_id="sha256:" + "0" * 64)


# -- through fm.load: the same rule reaches every reader -------------------------


def test_fm_load_rejects_hand_set_archive(tmp_vault):
    p = tmp_vault["OBSERVATIONS"] / "2026-06-12-gap-foo.md"
    fm.write(p, BASE | {"status": "archived"}, "# foo\n")
    with pytest.raises(fm.FrontmatterError, match="requires resolved, resolution"):
        fm.load(p)


def test_fm_load_accepts_resolved_file(tmp_vault):
    p = tmp_vault["OBSERVATIONS"] / "2026-06-12-gap-foo.md"
    fm.write(
        p,
        BASE
        | {
            "status": "promoted",
            "target": "foo",
            "producer": "entities",
            **RESOLVED,
            "resolution_kind": "done",
        },
        "# foo\n",
    )
    meta, _body, kind = fm.load(p)
    assert kind == "observation"
    assert meta["resolution_kind"] == "done"
