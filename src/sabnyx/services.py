"""Headless orchestration shared by the CLI commands and the wire adapters.

One write path for ingest and ask, so the terminal and the chat interface
can never drift. Input acquisition, gating, and presentation stay with the
caller; this module owns the model calls, the validated writes, the
provenance log, and the ledger trail. Console is injected so callers can
route progress to a terminal or a string buffer.

@decision DEC-01: promote_file is a service function, not a CLI concern
@rationale: `sabnyx tend` and `sabnyx promote` both need the same promote
  logic; extracting it here prevents duplication and keeps CLI code thin.
@decision DEC-02: auto_promote_decision is pure (no IO)
@rationale: Six boolean gates that depend only on passed-in data → fully
  unit-testable with no fixtures, no filesystem, no monkeypatching.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date as date_cls
from pathlib import Path

from pydantic import ValidationError
from rich.console import Console

from . import context, ledger, llm, paths, retrieval, sources, writers
from .schemas import AskAnswer, IngestChangeset, ProposedIntake, TriageBatch
from .util import extract_wikilinks, slugify

# Channel/provenance actors. The `source_agent` stamped on an intake file IS its
# provenance-trust level for unattended auto-promotion (see config.trusted_sources
# and auto_promote_decision's Gate 6). Only operator-vetted provenance belongs in
# the trusted set: ACTOR_INGEST is for content the operator hands in directly
# (local file / stdin). Content fetched from a remote/untrusted origin must use a
# DISTINCT, non-trusted actor so a prompt-injected `bookmark`/`paper` intake can't
# auto-promote into the authoritative atlas tier unattended.
ACTOR_INGEST = "sabnyx-ingest"  # trusted: operator-supplied local source
ACTOR_INGEST_WEB = "sabnyx-ingest-web"  # NOT trusted: remotely fetched URL content
ACTOR_ASK = "sabnyx-ask"
ACTOR_TEND = "sabnyx-tend"


# -- shared interface helpers ---------------------------------------------------
# Used by every channel (CLI, Telegram serve, R1 serve-http) so the title and
# pulse logic lives once. Channel commands format the results their own way.


def title_from(text: str, n: int = 8) -> str:
    """A short title from the first few words of a captured message."""
    words = text.split()
    return " ".join(words[:n]) if words else "untitled"


def vault_pulse() -> dict[str, int]:
    """A terse vault pulse: counts of intake, atlas, and pending observations.

    Computed without the full health command — cheap enough for a status poll.
    """
    return {
        "intake": len(list(paths.INTAKE.glob("*.md"))),
        "atlas": sum(1 for _ in paths.ATLAS.rglob("*.md")),
        "observations": len(list(paths.OBSERVATIONS.glob("*.md"))),
    }


# -- promote (reusable service) -------------------------------------------------


def promote_file(
    source: Path,
    atlas_type: str,
    *,
    last_verified: str | None = None,
    actor: str = "sabnyx-cli",
) -> Path:
    """Promote an intake file to atlas/<atlas_type>/<slug>.md.

    Contains the core logic extracted from commands/promote.py.
    Callers are responsible for the guards:
      - source must be under INTAKE
      - dest must not exist
      - kind must be "intake"
    This function writes, validates, rolls back on failure, unlinks the
    source, appends the knowledge log, and emits ledger events.

    Returns the destination Path.
    Raises RuntimeError on validation failure (destination is rolled back).
    """
    from . import fm
    from .paths import ATLAS_TYPE_DIRS, DATA_ROOT, KNOWLEDGE_LOG

    meta, body, _kind = fm.load(source)

    dest_dir = ATLAS_TYPE_DIRS[atlas_type]
    dest = dest_dir / source.name
    today = date_cls.today().isoformat()

    new_meta = {
        "type": atlas_type,
        "description": meta["description"],
        "tags": meta.get("tags", []),
        "status": "final",
        "promoted_from": str(source.relative_to(DATA_ROOT)),
        "promoted_date": today,
        "last_verified": last_verified or today,
        "proof": None,
    }

    # Preserve source provenance as HTML comments in the body (round-trip).
    provenance_lines = []
    if src_url := meta.get("source_url"):
        provenance_lines.append(f"<!-- source-url: {src_url} -->")
    if src_date := meta.get("source_date"):
        provenance_lines.append(f"<!-- source-date: {src_date} -->")
    if meta.get("source"):
        provenance_lines.append(f"<!-- source-kind: {meta['source']} -->")
    provenance = ("\n".join(provenance_lines) + "\n\n") if provenance_lines else ""

    # Add an empty Related marker block for future reweave passes.
    related_block = "\n\n## Related\n\n<!-- BEGIN sabnyx:related -->\n<!-- END sabnyx:related -->\n"
    new_body = provenance + body.rstrip() + related_block

    fm.write(dest, new_meta, new_body)

    # Validate what we wrote, before touching the source
    try:
        fm.load(dest)
    except fm.FrontmatterError as e:
        dest.unlink()
        raise RuntimeError(f"wrote invalid atlas file, rolled back: {e}") from e

    # Ledger BEFORE unlinking the source: the append is the commit point. If the
    # process dies after this, the promotion is recorded (and the reweave intent
    # preserved) and the next tend's duplicate-slug gate refuses a re-promote —
    # far better than the inverse (file moved, no ledger record, lost reweave).
    src_rel = str(source.relative_to(DATA_ROOT))
    dest_rel = str(dest.relative_to(DATA_ROOT))
    ledger.append_event(
        kind="atlas.promoted",
        actor=actor,
        subject=dest_rel,
        payload={
            "from": src_rel,
            "as": atlas_type,
            "description": meta["description"],
            "source_url": meta.get("source_url"),
        },
    )

    # Emit a reweave prompt as a separate event (not an action yet).
    ledger.append_event(
        plane="intent",
        kind="reweave.proposed",
        actor=actor,
        subject=dest_rel,
        payload={
            "description": meta["description"],
            "tags": meta.get("tags", []),
            "prompt": (
                "now that this file exists, what existing atlas files should "
                "link to it or be updated by it? look for contradictions and "
                "bridge links; propose additions to the Related marker block "
                "and write observations for conflicts."
            ),
        },
    )

    # Remove the intake source (the move is now committed on the ledger).
    source.unlink()

    # Provenance log (Gate 3 materia)
    KNOWLEDGE_LOG.parent.mkdir(parents=True, exist_ok=True)
    if not KNOWLEDGE_LOG.exists():
        KNOWLEDGE_LOG.write_text(
            "# Knowledge log\n\nAppend-only provenance trail.\n\n", encoding="utf-8"
        )
    with KNOWLEDGE_LOG.open("a", encoding="utf-8") as f:
        f.write(f"- {today} promote: `{src_rel}` -> `{dest_rel}` ({atlas_type})\n")

    return dest


# -- reconciliation (ledger vs filesystem) -------------------------------------


@dataclass
class Reconciliation:
    """Drift between the ledger's record of promotions and the actual atlas/.

    The ledger is an append-only audit of *agent* actions, not an event-sourced
    mirror of the vault — git is the source of truth, and a manual file edit
    produces no event. This surfaces where the two disagree.
    """

    unledgered: list[str]  # atlas files with no atlas.promoted event (manual/pre-ledger)
    orphaned: list[str]  # atlas.promoted subjects whose file is now missing

    @property
    def clean(self) -> bool:
        return not self.unledgered and not self.orphaned


def reconcile_atlas() -> Reconciliation:
    """Diff `atlas.promoted` ledger subjects against the files under atlas/.

    Read-only: replays the ledger and walks the filesystem, mutating nothing.
    Both sides use DATA_ROOT-relative paths so the comparison is apples-to-apples.
    """
    promoted: set[str] = {e.subject for e in ledger.replay() if e.kind == "atlas.promoted"}
    actual: set[str] = {
        str(p.relative_to(paths.DATA_ROOT))
        for p in paths.ATLAS.rglob("*.md")
        if p.is_file()
    }
    return Reconciliation(
        unledgered=sorted(actual - promoted),
        orphaned=sorted(promoted - actual),
    )


# -- auto-promote gate (pure, no IO) -------------------------------------------


# Map intake types to their atlas counterpart (deterministic tier).
# Types absent from this map are not auto-promotable.
_AUTO_PROMOTE_TYPE_MAP: dict[str, str] = {
    "bookmark": "reference",
    "paper": "reference",
}

# These types require LLM judgment or are temporal — not auto-promotable.
_LLM_TIER_TYPES = frozenset({"note", "thought"})
_TEMPORAL_TYPES = frozenset({"meeting-extract", "observation"})

_REQUIRED_FIELDS = frozenset({"type", "description", "source", "status"})


@dataclass
class AutoPromoteDecision:
    """Result of the deterministic auto-promote gate check.

    promotable=True means all six gates passed.
    atlas_type is set only when promotable=True.
    reasons records why any gate failed (or a success note).
    """

    promotable: bool
    atlas_type: str | None
    reasons: list[str] = field(default_factory=list)


def auto_promote_decision(
    meta: dict,
    kind: str,
    *,
    trusted: set[str],
    atlas_slugs: set[str],
    source_slug: str,
) -> AutoPromoteDecision:
    """Evaluate an intake file against all six auto-promote gates.

    Pure function — no IO, no filesystem, no ledger.

    Gates (all must pass for promotable=True):
      1. Promotable type: bookmark/paper → reference. note/thought deferred
         to LLM tier. meeting-extract/observation never auto-promoted.
      2. Not temporal (meeting-extract excluded explicitly).
      3. Required fields present: type, description, source, status.
      4. len(description) >= 30.
      5. source_slug not already in atlas_slugs (no duplicate).
      6. meta.get("source_agent") in trusted.
    """
    intake_type = meta.get("type", "")
    reasons: list[str] = []

    # Gate 1 + 2: promotable type / not temporal
    if kind == "observation":
        return AutoPromoteDecision(
            promotable=False, atlas_type=None, reasons=["observations are never auto-promoted"]
        )
    if intake_type in _TEMPORAL_TYPES:
        return AutoPromoteDecision(
            promotable=False, atlas_type=None, reasons=["temporal type → not auto-promotable"]
        )
    if intake_type in _LLM_TIER_TYPES:
        return AutoPromoteDecision(
            promotable=False, atlas_type=None, reasons=["needs judgment → LLM tier"]
        )
    atlas_type = _AUTO_PROMOTE_TYPE_MAP.get(intake_type)
    if atlas_type is None:
        return AutoPromoteDecision(
            promotable=False,
            atlas_type=None,
            reasons=[f"unknown type {intake_type!r} → not auto-promotable"],
        )

    # Gate 3: required fields present
    missing = [f for f in _REQUIRED_FIELDS if not meta.get(f)]
    if missing:
        reasons.append(f"missing required fields: {', '.join(sorted(missing))}")

    # Gate 4: description length
    desc = meta.get("description", "")
    if len(desc) < 30:
        reasons.append(f"description too short ({len(desc)} < 30 chars)")

    # Gate 5: no duplicate slug
    if source_slug in atlas_slugs:
        reasons.append(f"slug {source_slug!r} already exists in atlas")

    # Gate 6: trusted source_agent
    source_agent = meta.get("source_agent", "")
    if source_agent not in trusted:
        reasons.append(f"source_agent {source_agent!r} not in trusted set")

    if reasons:
        return AutoPromoteDecision(promotable=False, atlas_type=None, reasons=reasons)

    return AutoPromoteDecision(promotable=True, atlas_type=atlas_type, reasons=[])


# -- LLM triage tier -----------------------------------------------------------

TRIAGE_TASK_SYSTEM = """You are the triage agent for this vault. Given a list of intake files
with their metadata and descriptions, propose a routing decision for each.

Rules:
- action "promote": the file has durable value and should be promoted to atlas/.
  Set atlas_type to "concept" (analytical, structural knowledge), "reference"
  (a source, link, or dataset), or "idea" (hypothetical, speculative thought).
- action "keep": the file may have future value but is not ready to promote.
  Leave it in intake/ for manual review.
- action "discard": the file has no lasting value (noise, duplicate, superseded).
  We never delete automatically — this is a note for the operator.
- Be conservative: when uncertain, prefer "keep" over "discard".
- reason: one concise sentence explaining the decision.
Return a TriageBatch with one TriageItem per input file."""


def triage_batch(files: list[Path], *, role: str = "standard") -> TriageBatch:
    """Run a batched LLM triage pass over `files`.

    Assembles a description of each file's metadata, calls parse_structured
    to get per-item routing decisions, and ledgers the intent. Does not write
    or move any files — the caller applies decisions.
    """
    from . import fm

    lines: list[str] = []
    for p in files:
        try:
            meta, _body, _kind = fm.load(p)
        except Exception as e:
            lines.append(f"- slug: {p.stem}  [INVALID: {e}]")
            continue
        lines.append(
            f"- slug: {p.stem}\n"
            f"  type: {meta.get('type', '?')}\n"
            f"  description: {meta.get('description', '')}\n"
            f"  source_agent: {meta.get('source_agent', '')}\n"
            f"  tags: {', '.join(meta.get('tags', []))}"
        )

    user = "## Intake files to triage\n\n" + "\n".join(lines)

    ledger.append_event(
        plane="intent",
        kind="triage.llm",
        actor=ACTOR_TEND,
        subject="intake/",
        payload={"file_count": len(files), "role": role},
    )

    return llm.parse_structured(
        task_system=TRIAGE_TASK_SYSTEM,
        user=user,
        output_format=TriageBatch,
        role=role,  # type: ignore[arg-type]
        actor=ACTOR_TEND,
        purpose="triage:batch",
    )


DEPTH_GUIDANCE = {
    "shallow": "This is a short source. Extract concisely: usually one intake file "
    "with a tight summary. Do not pad.",
    "standard": "Standard-length source. Extract the core claims and anything the "
    "operator would want to find again.",
    "standard-plus": "Dense source. Extract thoroughly: distinct concepts may warrant "
    "separate intake files.",
    "deep": "Long-form source (paper/book-length). Extract section by section: key "
    "points, argument structure, and distinct concepts as separate intake files.",
}

INGEST_TASK_SYSTEM = """You are the intake agent for this vault. Given one raw source and an
index of what the vault already contains, produce an IngestChangeset.

Rules, in addition to the SPEC and ROUTING contract above:

- Propose intake files only. You never write to atlas/; promotion is a separate
  human-gated step. This is a containment boundary, not a suggestion.
- The `description` field is the most load-bearing field in the vault: 30-200
  chars, distinct from the title, answering "what is this and why would I
  search for it?". Dense, specific, no marketing language.
- Check the atlas index before proposing anything. If the source's content is
  already covered by an existing atlas entry, do NOT duplicate it — propose an
  observation (category: connection) naming the existing file instead.
- If the source contradicts a claim suggested by an existing description,
  propose an observation (category: contradiction) naming the file.
- If the source references an entity the vault clearly lacks, you may note a
  gap observation. Be conservative: observations are cheap but not free.
- Bodies are plain markdown, no frontmatter, no leading H1 (the writer adds
  the title). Terse but not curt; specific over abstract.
- Titles become kebab-case slugs; keep them phrase-like, atemporal, no dates."""

ASK_TASK_SYSTEM = """You are the surfacing agent for this vault: you answer the operator's
questions from the compiled knowledge provided. Produce an AskAnswer.

Rules, in addition to the SPEC and ROUTING contract above:

- Ground the answer in the vault. Cite every claim drawn from a vault file as a
  [[wikilink]] to its slug, inline where the claim is made, and list those
  slugs in `cited`. Only cite slugs that appear in the material provided.
- If the vault does not cover (part of) the question, say so plainly and keep
  any general-knowledge additions clearly marked as such. Never imply vault
  support for an unsupported claim.
- Set `capture` ONLY when answering produced durable, novel insight: a
  synthesis across files, a connection not yet recorded, a decision. A capture
  must add something no existing file's description already covers. Simple
  lookups, restatements of one file, and negative results get capture = null.
- A capture follows intake conventions: type is usually `thought` (synthesis,
  decision) or `note`; description is one dense sentence of STRICTLY 30-200
  characters (never exceed 200), distinct from the title; body terse, specific,
  with [[wikilinks]] to the files it builds on.
- Answer voice: terse but not curt; specific over abstract; no filler."""


# -- ingest -----------------------------------------------------------------------------


@dataclass
class IngestProposal:
    changeset: IngestChangeset
    depth: str
    role: str


def build_ingest_proposal(
    *,
    text: str,
    title: str,
    source_kind: str,
    source_url: str | None,
    src_rel: str,
    role_override: str | None = None,
    actor: str = ACTOR_INGEST,
) -> IngestProposal:
    """Classify depth, assemble vault context, run the extraction call,
    ledger the proposal. Writes nothing."""
    depth = sources.classify_depth(text, source_url=source_url)
    role = role_override or llm.role_for_depth(depth)

    user = (
        f"{context.description_index(paths.ATLAS, 'Atlas index (durable knowledge already in the vault)')}\n\n"
        f"{context.description_index(paths.INTAKE, 'Intake queue (already captured, awaiting triage)')}\n\n"
        f"## Extraction depth\n{DEPTH_GUIDANCE[depth]}\n\n"
        f"## Source ({source_kind}"
        + (f", {source_url}" if source_url else "")
        + f": {title})\n\n{text}"
    )
    changeset = llm.parse_structured(
        task_system=INGEST_TASK_SYSTEM,
        user=user,
        output_format=IngestChangeset,
        role=role,  # type: ignore[arg-type]
        actor=actor,
        purpose=f"ingest:{slugify(title)}",
    )
    ledger.append_event(
        plane="intent",
        kind="ingest.proposed",
        actor=actor,
        subject=src_rel,
        payload={
            "summary": changeset.summary,
            "intake_count": len(changeset.intake_files),
            "observation_count": len(changeset.observations),
            "depth": depth,
            "role": role,
        },
    )
    return IngestProposal(changeset=changeset, depth=depth, role=role)


def apply_ingest(
    proposal: IngestProposal,
    *,
    title: str,
    source_kind: str,
    source_url: str | None,
    src_rel: str,
    console: Console,
    actor: str = ACTOR_INGEST,
) -> list[str]:
    """Write the proposed intake files + observations, log provenance,
    ledger ingest.applied. Returns relative paths written."""
    written: list[str] = []
    trailer = f"<!-- source-id:sabnyx-sources:{Path(src_rel).name} -->"
    for pi in proposal.changeset.intake_files:
        rel = writers.write_intake(
            pi,
            actor=actor,
            source=source_kind,
            source_url=source_url,
            trailer=trailer,
            console=console,
        )
        if rel:
            written.append(rel)
    for ob in proposal.changeset.observations:
        rel = writers.write_observation(ob, actor=actor, console=console)
        if rel:
            written.append(rel)

    writers.append_knowledge_log(
        "ingest",
        title,
        {
            "source": src_rel,
            "url": source_url,
            "depth": proposal.depth,
            "files": ", ".join(written) or "none",
        },
    )
    ledger.append_event(
        kind="ingest.applied",
        actor=actor,
        subject=src_rel,
        payload={"written": written},
    )
    return written


def ingest_text(
    *,
    text: str,
    title: str,
    source_kind: str,
    source_url: str | None,
    console: Console,
    actor: str = ACTOR_INGEST,
) -> tuple[str, list[str]]:
    """Capture raw text, then build and apply an ingest proposal.

    The whole capture -> propose -> apply pipeline every channel shares. Returns
    ``(summary, written_paths)``. Raises ``FileExistsError`` if the source was
    already captured (sources are write-once); callers shape that into their own
    "already captured" reply.
    """
    src_path = sources.capture(text=text, slug=title, actor=actor, source_url=source_url)
    src_rel = str(src_path.relative_to(paths.DATA_ROOT))
    proposal = build_ingest_proposal(
        text=text,
        title=title,
        source_kind=source_kind,
        source_url=source_url,
        src_rel=src_rel,
        actor=actor,
    )
    written = apply_ingest(
        proposal,
        title=title,
        source_kind=source_kind,
        source_url=source_url,
        src_rel=src_rel,
        console=console,
        actor=actor,
    )
    return proposal.changeset.summary, written


# -- ask --------------------------------------------------------------------------------


@dataclass
class AskResult:
    answer: AskAnswer
    role: str
    capture_dropped: bool = False  # a malformed optional capture was discarded


def _salvage_ask_answer(error: llm.StructuredOutputError) -> AskAnswer:
    """Best-effort floor: the answer is the deliverable, so never lose it to a
    malformed *optional* capture. Re-parse the raw output with `capture` nulled;
    re-raise if the answer itself can't be recovered."""
    raw = error.raw_content
    if raw is None or error.output_format is not AskAnswer:
        raise error
    try:
        data = json.loads(llm._extract_json(raw))
    except (json.JSONDecodeError, ValueError) as exc:
        raise error from exc
    if not isinstance(data, dict):
        raise error
    data["capture"] = None  # drop the only part that failed; keep the answer
    try:
        return AskAnswer.model_validate(data)
    except ValidationError:
        # the answer too is unsalvageable — surface the original, not this retry
        raise error from None


def _clean_citations(cited: list[str], answer_text: str) -> list[str]:
    """Make `cited` trustworthy regardless of how the model filled it.

    Models (especially smaller ones) sometimes return blank citation slots or
    cite inline but leave `cited` empty. Drop blanks, dedupe order-preserving,
    and fall back to the [[wikilinks]] actually present in the answer prose."""
    candidates = [c.strip() for c in cited if c and c.strip()]
    if not candidates:
        candidates = extract_wikilinks(answer_text)
    seen: set[str] = set()
    cleaned: list[str] = []
    for c in candidates:
        key = c.lower()
        if key not in seen:
            seen.add(key)
            cleaned.append(c)
    return cleaned


def answer_question(
    *, question: str, role_override: str | None = None, actor: str = ACTOR_ASK
) -> AskResult:
    """Assemble context, answer the question grounded in the vault, ledger
    the projection. Writes nothing (capture is a separate step)."""
    role = role_override or llm.role_for_message(question)
    index = context.slug_index(paths.ATLAS)
    user = (
        "## Atlas (durable knowledge; excerpts)\n"
        f"{retrieval.get_retriever().retrieve(question, index)}\n\n"
        f"{context.description_index(paths.INTAKE, 'Intake queue (drafts, not yet authoritative)')}\n\n"
        f"## Question\n{question}"
    )
    purpose = f"ask:{slugify(question)[:60]}"
    capture_dropped = False
    try:
        answer = llm.parse_structured(
            task_system=ASK_TASK_SYSTEM,
            user=user,
            output_format=AskAnswer,
            role=role,  # type: ignore[arg-type]
            actor=actor,
            purpose=purpose,
        )
    except llm.StructuredOutputError as e:
        # The repair re-ask at the LLM boundary already failed; fall back to
        # salvaging the answer without its optional capture.
        answer = _salvage_ask_answer(e)
        capture_dropped = True
    answer.cited = _clean_citations(answer.cited, answer.answer)
    ledger.append_event(
        plane="projection",
        kind="ask.answered",
        actor=actor,
        subject=purpose,
        payload={
            "question_hash": ledger.content_hash({"q": question}),
            "cited": answer.cited,
            "capture_proposed": answer.capture is not None,
        },
    )
    return AskResult(answer=answer, role=role, capture_dropped=capture_dropped)


def capture_answer(
    pi: ProposedIntake, *, question: str, console: Console, actor: str = ACTOR_ASK
) -> str | None:
    """File a durable answer back into intake/ — Karpathy's 'good answers
    become pages'. Returns the relative path, or None if skipped."""
    rel = writers.write_intake(
        pi,
        actor=actor,
        source="manual",
        trailer=f"<!-- source-id:sabnyx-ask:{date_cls.today().isoformat()} -->",
        console=console,
    )
    if rel:
        writers.append_knowledge_log(
            "ask-capture", pi.title, {"question": question[:200], "file": rel}
        )
    return rel
