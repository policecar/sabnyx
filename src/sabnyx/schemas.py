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
