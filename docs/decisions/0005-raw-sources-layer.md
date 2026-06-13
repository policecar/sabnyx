# ADR 0005 — Raw sources layer at vault/sources/

Date: 2026-06-12
Status: accepted

## Context

Karpathy's LLM-wiki pattern keeps three layers: raw sources (immutable,
the single source of truth), the wiki (LLM-owned), and the schema.
Stage 0 sabnyx had only the latter two: intake/ is explicitly
*disposable* and provenance was folded into frontmatter fields
(`source_url`, `source_date`). For bookmarks that is fine; for papers
and long notes that the LLM re-summarizes, losing the original is a
real loss — there is nothing to re-extract against when extraction
quality improves, and nothing to verify claims against.

## Decision

Add `vault/sources/` for retained immutable artifacts:

  - Write-once. `sources.capture()` refuses to overwrite.
  - No frontmatter schema. Excluded from lint and the vault walk;
    provenance is carried by a leading HTML comment
    (`<!-- source-url: ... -->`) and the ledger `source.captured` event.
  - Knowledge files reference their source via the reserved syntax
    (docs/reserved-syntax.md): `<!-- source-id:sabnyx-sources:<file> -->`.
  - Capped at 600k chars per artifact (defensive; well under context).

`ingest` captures every source here before extraction. Nothing else
writes to the directory.

## Consequences

+ Re-extraction is possible forever: better prompts/models can be
  re-run over the original artifact.
+ Contradiction and currency checks (Gate 5) can diff against the
  source, not a summary of it.
+ The intake/atlas tiers stay clean: sources/ carries the bulk bytes,
  knowledge files stay small and typed.
- The vault grows faster (raw artifacts are big). Acceptable: text only,
  and git handles it; binaries stay out of scope until a largestore-style
  tier is warranted (jibot got years without one).
- One more containment boundary to enforce: sources/ MUST NOT be
  treated as knowledge. Lint exclusion + SPEC addendum cover it.
