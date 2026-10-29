"""Deterministic vault + ledger for the report goldens (tests/golden/).

Every slug is made up. The builder writes fixed content in a fixed order;
run it under the `fixed_clock` fixture so every ledger timestamp and every
age is the same on every machine. It exercises each report line the
goldens freeze:

  - a broken wikilink, a stale unverified reference, untracked provenance
  - stale intake (created long before the clock), one trusted bookmark
    (auto-promote preview) and one deferred note
  - `ambiguous_alias` (two pages claim one alias) and `ambiguous_stem`
    (one stem in two folders), plus a pending gap targeting that stem,
    so tend's gap closure prints its ambiguous line too
  - a pending reweave intent
  - five tend runs that trip all three anomaly detectors

Validity is asserted as it is written: a builder that silently produced a
malformed file would freeze a schema-drift report instead.
"""

from __future__ import annotations

from sabnyx import fm, ledger, paths
from sabnyx.services import ACTOR_TEND

_DESC = "a made-up page long enough to satisfy the atlas description schema"
_CREATED = "2026-03-01T09:00:00+00:00"  # ~122 days before FIXED_CLOCK: stale intake


def _atlas(folder: str, slug: str, body: str, **extra: object) -> None:
    meta: dict = {
        "type": "concept",
        "description": f"{slug}: {_DESC}",
        "tags": [],
        "status": "final",
        "promoted_from": f"vault/intake/{slug}.md",
        "promoted_date": "2026-05-01",
        "last_verified": None,
        "proof": None,
    }
    meta.update(extra)
    p = paths.ATLAS / folder / f"{slug}.md"
    fm.write(p, meta, f"# {slug}\n\n{body}\n")
    fm.load(p)


def _intake(slug: str, intake_type: str, **extra: object) -> None:
    meta: dict = {
        "type": intake_type,
        "description": f"{slug}: a made-up intake item with a long enough description",
        "source": "web",
        "status": "draft",
        "created": _CREATED,
        "source_agent": "sabnyx-ingest",
    }
    meta.update(extra)
    p = paths.INTAKE / f"{slug}.md"
    fm.write(p, meta, f"# {slug}\n\ncontent\n")
    fm.load(p)
    ledger.append_event(
        kind="intake.created",
        actor="sabnyx-ingest",
        subject=str(p.relative_to(paths.DATA_ROOT)),
        payload={"type": intake_type},
    )


def _gap(name: str, target: str) -> None:
    meta: dict = {
        "type": "observation",
        "category": "gap",
        "observed": "2026-06-12",
        "source_agent": "sabnyx-entities",
        "status": "pending",
        "involves": ["vault/atlas/concepts/made-up-heron.md"],
        "producer": "entities",
        "target": target,
    }
    p = paths.OBSERVATIONS / name
    fm.write(p, meta, f"# {name}\n\nbody line\n")
    fm.load(p)
    ledger.append_event(
        kind="observation.created",
        actor="sabnyx-entities",
        subject=p.relative_to(paths.DATA_ROOT).as_posix(),
        payload={"category": "gap", "target": target},
    )


def _tend(kind: str, payload: dict, subject: str = "vault/") -> None:
    ledger.append_event(
        plane="projection", kind=kind, actor=ACTOR_TEND, subject=subject, payload=payload
    )


def build() -> None:
    """Populate the (fixture-redirected) DATA_ROOT. Call under fixed_clock."""
    # -- atlas --------------------------------------------------------------
    _atlas("concepts", "made-up-heron", "links to [[made-up-kettle]] and [[made-up-nowhere]].")
    _atlas("concepts", "made-up-kettle", "back to [[made-up-heron]].")
    _atlas("concepts", "made-up-delta", "see [[made-up-heron]].", aliases=["Shared Lantern"])
    _atlas("concepts", "made-up-epsilon", "see [[made-up-heron]].", aliases=["shared lantern"])
    _atlas("concepts", "made-up-twin", "see [[made-up-kettle]].")
    _atlas("projects", "made-up-twin", "see [[made-up-kettle]].")
    _atlas(
        "references",
        "made-up-almanac",
        "cites [[made-up-heron]].",
        type="reference",
        promoted_from=None,  # untracked provenance; never verified: stale
    )

    # -- intake -------------------------------------------------------------
    _intake("made-up-lantern", "note")
    _intake("made-up-bookmark", "bookmark", source_url="https://example.com/made-up")

    # -- observations -------------------------------------------------------
    _gap("2026-06-12-gap-made-up-twin.md", "made-up-twin")

    # -- reweave queue ------------------------------------------------------
    ledger.append_event(
        plane="intent",
        kind="reweave.proposed",
        actor="sabnyx-cli",
        subject="vault/atlas/concepts/made-up-heron.md",
        payload={"prompt": "made-up"},
    )

    # -- five tend runs: stuck_slug (5), ceiling_hit (2 of 5),
    #    repeat_failure on reweave:made-up-heron (3) -------------------------
    lantern = "vault/intake/made-up-lantern.md"
    for _ in range(2):
        _tend("triage.decided", {"decision": "keep"}, lantern)
        _tend("tend.aborted", {"error": "LLMBudgetError", "pass": 2})
    for _ in range(3):
        _tend("triage.decided", {"decision": "keep"}, lantern)
        ledger.append_event(
            plane="action",
            kind="llm.failed",
            actor=ACTOR_TEND,
            subject="reweave:made-up-heron",
            payload={"error": "RuntimeError"},
        )
        _tend("tend.aborted", {"error": "RuntimeError", "pass": 3})
