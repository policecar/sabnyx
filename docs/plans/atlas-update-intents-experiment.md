# Plan: experimental atlas update intents

Status: proposed experiment
Depends on: observation lifecycle only for operator visibility, not execution
Scope: source-backed amendment preview; interactive application only

## Question

Does queuing source-backed amendments to existing atlas pages reduce duplicate
knowledge without creating an unmaintainable second prose layer?

ROUTING already says to update an existing page rather than re-intake the same
knowledge. Today that branch is manual. This plan tests a narrow mechanized arm
without declaring a permanent autonomous update subsystem.

## Safety position

- Ingest may detect overlap but never writes atlas.
- The update pass reads the full target and relevant raw-source excerpts.
- Every v0 application requires an interactive confirmation.
- `--yes`, tend auto-apply, and remote-origin auto-apply are intentionally
  unsupported during the experiment.
- Only a `sabnyx:updates` marker block is writable.
- Frontmatter and operator prose remain untouched.
- Empty/no-value proposals consume the intent without changing the page.

## Ingest-side intent

Add an optional model output:

```python
class ProposedUpdateCandidate(BaseModel):
    slug: str
    relation: Literal["covers", "extends", "contradicts"]
    reason: str = Field(min_length=10, max_length=240)
```

`IngestChangeset.update_candidates` defaults to an empty list.

Ingest sees descriptions only, so a candidate is a routing signal—not an
update judgment. Deterministic application:

1. Resolve the proposed slug with the shared alias-aware resolution index.
2. Drop unknown or ambiguous targets.
3. Emit `update.proposed`; do not write atlas.
4. Include immutable source path, canonical target, relation, reason, origin
   actor, and a stable intent ID derived from target + source.

The same target/source pair is idempotent. A different source creates a new
intent even when it targets the same page.

## Command surface

```text
sabnyx update list
sabnyx update review INTENT [--role standard] [--dry-run]
sabnyx update dismiss INTENT --reason TEXT
```

`list` is ledger-backed and read-only. `review` performs one LLM call, displays
the exact page diff, and requires a TTY confirmation before applying that same
in-memory proposal. `--dry-run` stops after the preview. There is no separate
"preview now, regenerate later" path: model output could change between calls.
`dismiss` consumes an intent without an LLM call and records the operator's
reason.

No update pass runs inside tend during v0. Tend may report the count of pending
intents after the experiment has operator value, but it does not call or apply
them.

## Context assembly

The update call receives:

- full target page;
- source provenance and relation/reason;
- source passages selected around target title, description terms, and shared
  keywords, within a fixed budget;
- head/tail only as fallback when passage selection finds nothing;
- atlas description index for valid wikilinks.

The command reports truncation explicitly. A blind fixed head/tail excerpt is
not sufficient for long papers because the relevant claim may be in the middle.

## Proposed amendment

```python
class ProposedAtlasAmendment(BaseModel):
    kind: Literal["extension", "correction"]
    content: str = Field(min_length=20, max_length=1200)
    reason: str = Field(min_length=10, max_length=240)
```

The model returns zero or one amendment for the canonical target. It cannot
redirect the update to another page.

The deterministic frame is:

```markdown
- **2026-07-11** (extension, from `source-file.md`): <content>
  <!-- source-id:sabnyx-sources:source-file.md -->
```

Unknown wikilinks become plain text. Headings and marker syntax in model content
are rejected. The applied entry is appended through `set_marker_block`; bytes
outside `sabnyx:updates` must remain identical.

## Ledger

Keep the experiment small:

| Kind | Plane | Meaning |
|------|-------|---------|
| `update.proposed` | intent | source may amend target page |
| `update.completed` | event | intent consumed by apply, no-op, or dismiss |
| `atlas.updated` | event | marker amendment applied |

`update.completed` records outcome `applied | no_change | dismissed` and the
preview hash when one exists. Declining or closing the review prompt leaves the
intent pending. LLM/application failures also leave it pending; existing
`llm.failed` covers model failures, while deterministic apply errors surface to
the operator and can be retried after correction.

## Evaluation gate

Do not integrate with tend or add consolidation automation until at least 20
intents have been reviewed. Record manually in the experiment report:

- how often the candidate targeted the correct page;
- how often preview was applied, dismissed, or no-op;
- whether amendments reduced a duplicate intake file;
- whether the updates block made the page easier or harder to reread;
- how often operator prose needed consolidation afterward.

Continue only if correct-target and useful-amendment rates are high enough for
the operator to prefer this over manual edits. Otherwise keep ROUTING's branch
manual and archive the experiment.

## Tests

- Old changeset fixtures validate with no candidates.
- Alias/spaced target resolves to one canonical page.
- Unknown and duplicate target/source intents are dropped/idempotent.
- Ingest never writes under atlas.
- Preview context includes target and relevant middle-source passage.
- Truncation is reported.
- No TTY/no confirmation means no atlas mutation.
- Applied diff changes only `sabnyx:updates`.
- Marker injection and unknown wikilinks are rejected/sanitized.
- Empty proposal consumes intent with no file write.
- Ledger outcomes are one-per-intent and replay deterministically.

## Explicitly deferred

- unattended or `--yes` application;
- observation-to-update automation;
- full prose rewriting;
- automatic consolidation after N entries;
- book ingest;
- update execution inside tend.
