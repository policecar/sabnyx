"""Headless orchestration shared by the CLI commands and the wire adapters.

One write path for ingest and ask, so the terminal and the chat interface
can never drift. Input acquisition, gating, and presentation stay with the
caller; this module owns the model calls, the validated writes, the
provenance log, and the ledger trail. Console is injected so callers can
route progress to a terminal or a string buffer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_cls
from pathlib import Path

from rich.console import Console

from . import context, ledger, llm, paths, sources, writers
from .schemas import AskAnswer, IngestChangeset, ProposedIntake
from .util import extract_wikilinks, slugify

ACTOR_INGEST = "sabnyx-ingest"
ACTOR_ASK = "sabnyx-ask"

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
  decision) or `note`; description 30-200 chars, distinct from the title,
  dense; body terse, specific, with [[wikilinks]] to the files it builds on.
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


# -- ask --------------------------------------------------------------------------------


@dataclass
class AskResult:
    answer: AskAnswer
    role: str


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
        f"{context.excerpts(index)}\n\n"
        f"{context.description_index(paths.INTAKE, 'Intake queue (drafts, not yet authoritative)')}\n\n"
        f"## Question\n{question}"
    )
    purpose = f"ask:{slugify(question)[:60]}"
    answer = llm.parse_structured(
        task_system=ASK_TASK_SYSTEM,
        user=user,
        output_format=AskAnswer,
        role=role,  # type: ignore[arg-type]
        actor=actor,
        purpose=purpose,
    )
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
    return AskResult(answer=answer, role=role)


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
