"""Pydantic schemas for frontmatter, ledger events, and normalized envelopes.

These are the *contract*. Breaking changes here are breaking changes
for the whole vault — bump the schema version and write a migration.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import paths
from .util import SLUG_RE

# -- frontmatter ---------------------------------------------------------------

IntakeType = Literal["bookmark", "paper", "note", "thought", "meeting-extract"]
# Knowledge types are the claim-bearing atlas tier; entity types (person/org/
# project) are lightweight reference scaffolding un-deferred in Phase 3.
AtlasKnowledgeType = Literal["concept", "reference", "idea"]
AtlasEntityType = Literal["person", "org", "project"]
AtlasType = Literal["concept", "reference", "idea", "person", "org", "project"]
ObservationCategory = Literal["gap", "contradiction", "connection", "friction", "structural"]
ObservationStatus = Literal["pending", "promoted", "archived"]
# The code path that built an observation -- not the command or channel that
# invoked it (reweave run by tend is "reweave"; telegram/R1/CLI ingest are all
# "ingest"). Never derived from source_agent.
Producer = Literal["entities", "scan", "reweave", "ingest", "manual"]
# done -> status promoted; every other kind -> status archived.
ResolutionKind = Literal["done", "wontfix", "duplicate", "obsolete"]
IntakeSource = Literal["manual", "web", "email", "import", "clipboard"]


class IntakeFrontmatter(BaseModel):
    """Schema for files under vault/intake/."""

    model_config = ConfigDict(extra="forbid")

    type: IntakeType
    description: str = Field(min_length=30, max_length=200)
    source: IntakeSource = "manual"
    source_url: str | None = None
    source_date: date | None = None
    tags: list[str] = Field(default_factory=list)
    status: Literal["draft"] = "draft"
    created: datetime
    source_agent: str = "manual"

    @field_validator("description")
    @classmethod
    def _not_placeholder(cls, v: str) -> str:
        bad = {"todo", "tbd", "...", "fix me", "placeholder"}
        if v.strip().lower() in bad:
            raise ValueError("description is a placeholder")
        return v

    @field_validator("created")
    @classmethod
    def _created_has_tz(cls, v: datetime) -> datetime:
        # SPEC §3: "ISO 8601 with TZ" — a naive timestamp is ambiguous provenance.
        if v.tzinfo is None:
            raise ValueError("created must be timezone-aware (SPEC §3: ISO 8601 with TZ)")
        return v

    @model_validator(mode="after")
    def _url_required_for_references(self) -> IntakeFrontmatter:
        # SPEC §3: source_url is required for bookmark/paper. This is Gate 2
        # (PRECISION) — the contract lives here, not in any one CLI command.
        if self.type in ("bookmark", "paper") and not self.source_url:
            raise ValueError(f"source_url is required for type={self.type!r}")
        return self


class AtlasFrontmatter(BaseModel):
    """Schema for files under vault/atlas/."""

    model_config = ConfigDict(extra="forbid")

    type: AtlasType
    description: str = Field(min_length=30, max_length=200)
    tags: list[str] = Field(default_factory=list)
    # Alternate names this page answers to (entity dedup/resolution, Phase 3).
    # Lightweight: a flat list of strings, NOT Senzing-scale identity matching.
    aliases: list[str] = Field(default_factory=list)
    status: Literal["final"] = "final"
    promoted_from: str | None = None
    promoted_date: date | None = None
    last_verified: date | None = None
    proof: str | None = None  # Lemma ZK proof, populated later


class ObservationFrontmatter(BaseModel):
    """Schema for files under vault/intake/.observations/."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["observation"]
    category: ObservationCategory
    observed: date
    source_agent: str = "manual"
    status: ObservationStatus = "pending"
    involves: list[str] = Field(default_factory=list)
    # A gap's missing page, as a slug. Optional: legacy and prose gaps lack it.
    target: str | None = None
    # Optional on read: legacy files are "unknown", never backfilled.
    producer: Producer | None = None
    resolved: date | None = None
    resolution: str | None = None
    resolution_kind: ResolutionKind | None = None
    # DATA_ROOT-relative path of the observation this one reopens (the ledger
    # `subject` form). Set only by the recurrence path in write_observation.
    supersedes: str | None = None

    @field_validator("target")
    @classmethod
    def _target_is_slug(cls, v: str | None) -> str | None:
        if v is not None and not SLUG_RE.fullmatch(v):
            raise ValueError(f"target must be a slug matching {SLUG_RE.pattern} (got {v!r})")
        return v

    @field_validator("resolution")
    @classmethod
    def _resolution_is_one_line(cls, v: str | None) -> str | None:
        # splitlines, not "\n" in v: of the 8 line-break characters, "\n"
        # catches only LF and CRLF; splitlines also catches CR, VT, FF, NEL,
        # LS and PS (NEL renders raw and breaks the YAML round-trip).
        # Demonstrate c5.
        if v is not None and v.strip().splitlines() != [v.strip()]:
            raise ValueError("resolution must be non-empty and a single line")
        return v

    @field_validator("supersedes")
    @classmethod
    def _supersedes_is_observation_rel(cls, v: str | None) -> str | None:
        if v is None:
            return v
        # Built at validation time, not import time: tests (and a split-repo
        # DATA_ROOT) redirect paths after this module loads.
        prefix = paths.OBSERVATIONS.relative_to(paths.DATA_ROOT).as_posix()
        if not re.fullmatch(rf"{re.escape(prefix)}/[^/]+\.md", v):
            raise ValueError(f"supersedes must be {prefix}/<file>.md (got {v!r})")
        return v

    @model_validator(mode="after")
    def _status_matches_resolution(self) -> ObservationFrontmatter:
        # Strict: status is derived from resolution_kind, never set alone.
        # pending  => resolved, resolution, resolution_kind all absent
        # promoted <=> all three present and kind == done
        # archived <=> all three present and kind in {wontfix, duplicate, obsolete}
        present = [f is not None for f in (self.resolved, self.resolution, self.resolution_kind)]
        if self.status == "pending":
            if any(present):
                raise ValueError(
                    "a pending observation must not carry resolved, resolution or resolution_kind"
                )
            return self
        if not all(present):
            raise ValueError(
                f"status {self.status!r} requires resolved, resolution and "
                "resolution_kind; revert status to pending and run "
                "`sabnyx observations resolve` or `sabnyx observations archive`"
            )
        expected = "promoted" if self.resolution_kind == "done" else "archived"
        if self.status != expected:
            raise ValueError(
                f"resolution_kind {self.resolution_kind!r} requires status "
                f"{expected!r} (got {self.status!r})"
            )
        return self


# -- ledger --------------------------------------------------------------------

LedgerPlane = Literal["intent", "event", "projection", "action"]


class LedgerEvent(BaseModel):
    """One append to ops/events.jsonl."""

    model_config = ConfigDict(extra="forbid")

    ts: datetime
    plane: LedgerPlane
    kind: str  # dotted, e.g. "intake.created", "atlas.promoted"
    actor: str  # "sabnyx-cli", "operator", "triage-agent", ...
    subject: str  # file path or entity id
    payload: dict  # arbitrary, hashed canonically
    content_hash: str  # "sha256:<hex>" of canonical payload


# -- ingest changeset (LLM boundary) -------------------------------------------

ExtractionDepth = Literal["shallow", "standard", "standard-plus", "deep"]
ModelRole = Literal["fast", "standard", "deep"]


class ProposedIntake(BaseModel):
    """One intake file proposed by the ingest pass.

    The model proposes; deterministic code slugifies, writes, validates,
    and rolls back. Containment: ingest writes to intake/ only, never atlas/.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["bookmark", "paper", "note", "thought"]
    title: str
    description: str = Field(min_length=30, max_length=200)
    tags: list[str] = Field(default_factory=list)
    body: str  # markdown body, no frontmatter, no H1 (added by the writer)


class ProposedObservation(BaseModel):
    """One observation proposed by the ingest pass (gap, contradiction, ...)."""

    model_config = ConfigDict(extra="forbid")

    category: ObservationCategory
    title: str
    body: str
    involves: list[str] = Field(default_factory=list)


class IngestChangeset(BaseModel):
    """Everything one ingest pass wants to write. Applied atomically-ish
    by deterministic code behind a human approval gate."""

    model_config = ConfigDict(extra="forbid")

    summary: str  # one paragraph: what the source is, what was extracted, why
    intake_files: list[ProposedIntake] = Field(default_factory=list)
    observations: list[ProposedObservation] = Field(default_factory=list)


# -- contradiction scan (LLM boundary) ------------------------------------------


class HealthProbe(BaseModel):
    """Trivial structured output for `sabnyx verify` — confirms the whole
    structured-output path (call, schema, parse) works end to end live."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    note: str = Field(min_length=1, max_length=200)


class ContradictionScan(BaseModel):
    """Cross-atlas contradiction findings (Gate 5, semantic half).

    Each finding is an observation with category contradiction: two atlas
    files making conflicting claims, both named in `involves`, with a
    suggested resolution in the body. jibot's stale-claim detector.
    """

    model_config = ConfigDict(extra="forbid")

    summary: str
    findings: list[ProposedObservation] = Field(default_factory=list)


# -- ask answer (LLM boundary) ----------------------------------------------------


class AskAnswer(BaseModel):
    """One grounded answer plus an optional capture proposal.

    Karpathy: "good answers become new pages rather than disappearing
    into chat history" — jibot's capture-to-jibrain. `capture` is set
    only when the answer contains durable, novel insight.
    """

    model_config = ConfigDict(extra="forbid")

    answer: str  # markdown, claims drawn from the vault cited as [[wikilinks]]
    cited: list[str] = Field(default_factory=list)  # slugs actually relied on
    capture: ProposedIntake | None = None


# -- reweave changeset (LLM boundary) -------------------------------------------

LinkPriority = Literal["bridge", "enrichment", "correction", "low"]


class RelatedAddition(BaseModel):
    """Links to add to one atlas file's `sabnyx:related` marker block.

    `file` may be the reweave target itself (outbound links) or any other
    atlas file (backward links). Marker blocks only; prose is never touched.
    """

    model_config = ConfigDict(extra="forbid")

    file: str  # slug of the atlas file whose Related block gains links
    add_links: list[str]  # slugs to add as [[wikilinks]]
    reason: str
    priority: LinkPriority = "enrichment"


class ReweaveChangeset(BaseModel):
    """Everything one reweave pass wants to do: marker-block link
    additions plus observations (contradictions, gaps, connections)."""

    model_config = ConfigDict(extra="forbid")

    summary: str
    related_additions: list[RelatedAddition] = Field(default_factory=list)
    observations: list[ProposedObservation] = Field(default_factory=list)


# -- triage batch (LLM boundary) -----------------------------------------------


class TriageItem(BaseModel):
    """One triage decision proposed by the LLM triage pass.

    action: promote → the LLM wants this file promoted as atlas_type.
            keep    → stays in intake/ for manual review.
            discard → note only; we never auto-delete.
    """

    model_config = ConfigDict(extra="forbid")

    slug: str
    action: Literal["promote", "keep", "discard"]
    atlas_type: Literal["concept", "reference", "idea"] | None = None
    reason: str


class TriageBatch(BaseModel):
    """Batch triage decisions from one LLM call over the deferred intake queue."""

    model_config = ConfigDict(extra="forbid")

    items: list[TriageItem] = Field(default_factory=list)


# -- normalized envelope (adapters) --------------------------------------------

EnvelopeSource = Literal["cli", "telegram", "email", "web", "slack", "other"]


class NormalizedEnvelope(BaseModel):
    """Interface-agnostic event shape.

    Every adapter maps its raw events to this before they hit the
    policy/action layer. Lets us add new interfaces without touching
    downstream code. Stage 0 only has `cli`.
    """

    model_config = ConfigDict(extra="forbid")

    envelope_id: str  # sha256 of (source, source_id, ts) for dedup
    source: EnvelopeSource
    source_id: str  # the adapter's native id for this event
    ts: datetime
    actor_raw: str  # whatever id the source gave us
    actor_canonical: str | None = None  # resolved, if adapter knows
    kind: str  # dotted, adapter-agnostic: "content.submit", "command.invoke"
    payload: dict
