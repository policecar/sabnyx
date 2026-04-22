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
