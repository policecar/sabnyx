"""Smoke test: can we create a valid file and round-trip it through lint?"""

from __future__ import annotations

from datetime import UTC, datetime
from datetime import date as date_cls

import pytest
from pydantic import ValidationError

from sabnyx import fm, ledger
from sabnyx.paths import ATLAS_CONCEPTS, INTAKE, OBSERVATIONS, ensure_tree
from sabnyx.schemas import (
    AtlasFrontmatter,
    IntakeFrontmatter,
    NormalizedEnvelope,
    ObservationFrontmatter,
)


def test_intake_schema_requires_long_description():
    with pytest.raises(ValidationError):
        IntakeFrontmatter(
            type="thought",
            description="too short",
            created=datetime.now(UTC),
        )


def test_intake_schema_roundtrip():
    m = IntakeFrontmatter(
        type="thought",
        description="a description that is definitely at least 30 characters long, yes indeed",
        created=datetime.now(UTC),
    )
    assert m.status == "draft"
    assert m.source == "manual"


def test_intake_schema_requires_source_url_for_bookmark_and_paper():
    # SPEC §3: source_url required for bookmark/paper — enforced by the schema
    # itself (Gate 2), not just by one CLI command.
    for t in ("bookmark", "paper"):
        with pytest.raises(ValidationError, match="source_url is required"):
            IntakeFrontmatter(
                type=t,
                description="a description that is definitely at least 30 characters long",
                created=datetime.now(UTC),
            )
    m = IntakeFrontmatter(
        type="bookmark",
        description="a description that is definitely at least 30 characters long",
        source_url="https://example.com/x",
        created=datetime.now(UTC),
    )
    assert m.source_url == "https://example.com/x"


def test_intake_schema_rejects_naive_created():
    # SPEC §3: "ISO 8601 with TZ"
    with pytest.raises(ValidationError, match="timezone-aware"):
        IntakeFrontmatter(
            type="thought",
            description="a description that is definitely at least 30 characters long",
            created=datetime(2026, 7, 1, 12, 0, 0),  # naive
        )


def test_fm_write_survives_reserved_metadata_keys(tmp_path):
    # frontmatter.Post(body, **meta) would raise TypeError on keys like
    # "content"/"handler"; fm.write assigns .metadata instead. (Reading such a
    # file back via frontmatter.loads has the same upstream collision, so the
    # written text is verified directly.)
    import yaml

    p = tmp_path / "x.md"
    fm.write(p, {"content": "meta-value", "handler": "h", "type": "note"}, "the body\n")
    text = p.read_text(encoding="utf-8")
    _, meta_block, body = text.split("---\n", 2)
    assert yaml.safe_load(meta_block) == {"content": "meta-value", "handler": "h", "type": "note"}
    assert body.strip() == "the body"


def test_atlas_schema_roundtrip():
    m = AtlasFrontmatter(
        type="concept",
        description="a description that is definitely at least 30 characters long, yes indeed",
        promoted_from="intake/foo.md",
        promoted_date=date_cls.today(),
    )
    assert m.status == "final"
    assert m.proof is None


def test_observation_schema_roundtrip():
    m = ObservationFrontmatter(
        type="observation",
        category="gap",
        observed=date_cls.today(),
    )
    assert m.status == "pending"


def test_envelope_id_stable_shape():
    e = NormalizedEnvelope(
        envelope_id="sha256:abc",
        source="cli",
        source_id="sabnyx lint",
        ts=datetime.now(UTC),
        actor_raw="operator",
        kind="command.invoke",
        payload={"argv": ["sabnyx", "lint"]},
    )
    assert e.source == "cli"


def test_ledger_append_and_replay(tmp_path, monkeypatch):
    from sabnyx import paths as paths_mod

    monkeypatch.setattr(paths_mod, "OPS", tmp_path)
    monkeypatch.setattr(paths_mod, "EVENTS_JSONL", tmp_path / "events.jsonl")
    monkeypatch.setattr(ledger, "EVENTS_JSONL", tmp_path / "events.jsonl")
    monkeypatch.setattr(ledger, "OPS", tmp_path)

    ledger.append_event(
        kind="test.happened",
        actor="pytest",
        subject="test",
        payload={"k": 1},
    )
    events = list(ledger.replay())
    assert len(events) == 1
    assert events[0].kind == "test.happened"
    assert events[0].content_hash.startswith("sha256:")


def test_ensure_tree_is_idempotent():
    ensure_tree()
    ensure_tree()
    assert INTAKE.exists()
    assert OBSERVATIONS.exists()
    assert ATLAS_CONCEPTS.exists()


def test_classify():
    from sabnyx.paths import ROOT

    assert fm.classify(ROOT / "vault" / "intake" / "x.md") == "intake"
    assert fm.classify(ROOT / "vault" / "atlas" / "concepts" / "x.md") == "atlas"
    assert fm.classify(ROOT / "vault" / "intake" / ".observations" / "x.md") == "observation"


def test_classify_is_vault_relative(tmp_path, monkeypatch):
    """A data root containing an 'intake' path component must not misclassify
    the whole vault — classification keys off the VAULT-relative path."""
    from sabnyx import paths as paths_mod

    vault = tmp_path / "intake" / "vault"
    monkeypatch.setattr(paths_mod, "VAULT", vault)
    assert fm.classify(vault / "atlas" / "concepts" / "x.md") == "atlas"
    assert fm.classify(vault / "intake" / "x.md") == "intake"


def test_classify_domains_raises_value_error(tmp_vault):
    p = tmp_vault["VAULT"] / "domains" / "chanoyu" / "wabi.md"
    with pytest.raises(ValueError, match="cannot classify"):
        fm.classify(p)


def test_load_wraps_malformed_yaml_as_frontmatter_error(tmp_vault):
    """Malformed YAML must surface as the recoverable FrontmatterError, not a
    raw yaml parser exception (which would abort a whole tend run)."""
    p = tmp_vault["INTAKE"] / "bad-yaml.md"
    p.write_text("---\ntype: note\ntags: [unclosed\n---\nbody\n", encoding="utf-8")
    with pytest.raises(fm.FrontmatterError, match="malformed YAML"):
        fm.load(p)
    # a literal tab in the YAML block is the other classic
    p.write_text("---\ntype: note\n\tdescription: x\n---\nbody\n", encoding="utf-8")
    with pytest.raises(fm.FrontmatterError, match="malformed YAML"):
        fm.load(p)
