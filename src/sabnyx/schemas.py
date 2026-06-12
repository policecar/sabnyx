"""Pydantic schemas for frontmatter, ledger events, and normalized envelopes.

These are the *contract*. Breaking changes here are breaking changes
for the whole vault — bump the schema version and write a migration.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# -- frontmatter ---------------------------------------------------------------

IntakeType = Literal["bookmark", "paper", "note", "thought", "meeting-extract"]
AtlasType = Literal["concept", "reference", "idea"]
ObservationCategory = Literal["gap", "contradiction", "connection", "friction", "structural"]
ObservationStatus = Literal["pending", "promoted", "archived"]
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


class AtlasFrontmatter(BaseModel):
    """Schema for files under vault/atlas/."""

    model_config = ConfigDict(extra="forbid")

    type: AtlasType
    description: str = Field(min_length=30, max_length=200)
    tags: list[str] = Field(default_factory=list)
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
