# Plan: observation lifecycle and age-based triage

Status: proposed
Depends on: PR #15
Scope: one small Python feature set; no new LLM disposition pass

## Outcome

Make observations actionable without letting another model pass judge
model-written signals, and prevent small intake queues from starving forever.

Success means:

- the operator can list, resolve, and archive observations;
- every reader treats pending status consistently;
- transitions preserve observation prose and are ledgered;
- new gap observations can name a deterministic missing target;
- tend triage fires on count or age, never on an empty queue.

## Non-goals

- No LLM observation-triage pass.
- No direct creation of atlas pages from observations.
- No category-specific `promote` automation in v0.
- No automatic change to SPEC or ROUTING from friction observations.
- No auto-resolution of legacy gaps whose target is ambiguous.

## Schema additions

Add optional fields to `ObservationFrontmatter`:

```yaml
resolved: 2026-07-11       # absent while pending
resolution: "page now exists: jane-doe"
target: jane-doe           # new gap observations only; optional for compatibility
```

Rules:

- `pending` requires `resolved` and `resolution` to be absent.
- `promoted` or `archived` requires both fields.
- `target` is accepted on every existing file for compatibility, but new gap
  producers must set it when a single missing slug is known.
- Existing files remain valid without migration.

`target` is necessary before deterministic gap closure. `involves` names all
related files and does not reliably distinguish the missing page from its
referrers.

## Transition helper

Add `writers.transition_observation`:

```python
def transition_observation(
    path: Path,
    *,
    status: Literal["promoted", "archived"],
    resolution: str,
    actor: str,
) -> None:
    ...
```

Invariants:

1. Load and require kind `observation` and current status `pending`.
2. Change only `status`, `resolved`, and `resolution`.
3. Preserve the body byte-for-byte.
4. Validate the replacement before committing it.
5. Restore the original bytes if validation or ledger append fails.
6. Append one `observation.resolved` event with category, old/new status,
   resolution, and target when present.
7. Refuse a second transition; reopening creates a new observation.

## Command surface

Add a command group:

```text
sabnyx observations list [--category C] [--status pending|promoted|archived|all]
                         [--involves SLUG]
sabnyx observations resolve FILE --reason TEXT
sabnyx observations archive FILE --reason TEXT
```

`list` defaults to pending and prints file, category, observed age, target, and
involves. `resolve` transitions to `promoted`; the name means "acted on or
otherwise resolved," not "promoted directly to atlas." `archive` records a
false positive, obsolete signal, or deliberate no-action decision.

Both mutating commands require a non-empty reason and operate only under
`vault/intake/.observations/`.

## Pending-only reader contract

Create one helper, rather than repeating glob/filter logic:

```python
def observations(status: ObservationStatus | None = "pending") -> list[Path]:
    ...
```

Use it in:

- health and tend counts;
- scan contradiction dedupe;
- reweave context;
- triage report generation;
- entities gap dedupe.

Malformed observations are reported by health and excluded from semantic
inputs; they must not silently count as pending.

## Deterministic gap closure

Only new gap observations with explicit `target` participate.

During `entities gaps` and tend health:

1. Build the shared alias-aware resolution index.
2. For each pending gap with `target`, resolve the target.
3. If it now resolves, archive with `resolved by existing page: <canonical>`.
4. Leave legacy or multi-target observations untouched.

This transition is deterministic and safe to run unattended. It must emit the
same `observation.resolved` event as a manual transition.

## Age-based triage

Add to tend:

```text
--max-age-days D   default 7; 0 disables the age trigger
```

Pure helper:

```python
def triage_due(
    deferred: list[tuple[Path, dict]],
    *,
    threshold: int,
    max_age_days: int,
) -> tuple[bool, Literal["threshold", "age"] | None]:
    ...
```

Rules:

- empty queue: never call the model;
- count at or above threshold: `threshold`;
- otherwise, oldest valid `created` age at or above D: `age`;
- invalid frontmatter uses file mtime only for scheduling, not knowledge data;
- D <= 0 disables age triggering;
- dry-run reports the trigger but makes no LLM call.

Extend `tend.completed` with `triage_trigger`. Existing consumers tolerate the
additive payload field.

## Ledger kinds

Only one new kind:

| Kind | Plane | Meaning |
|------|-------|---------|
| `observation.resolved` | event | observation left pending state |

Age triggering extends existing `tend.completed` and `triage.decided` payloads;
it does not need a new event kind.

## Tests

- Existing observation frontmatter remains valid.
- Pending/resolved field consistency is validated.
- Transition changes only allowed frontmatter and preserves body bytes.
- Transition rollback on validation and ledger failure.
- Second transition refused.
- All readers exclude archived/promoted fixtures by default.
- New gap with target auto-archives when target resolves through slug or alias.
- Legacy gap without target never auto-archives.
- `triage_due` count/age/disabled/empty/invalid-file matrix.
- Tend dry-run and live trigger payloads.

## Delivery

One PR is appropriate: schema, helper, command, reader convergence, and age
trigger form one coherent contract. Do not include notifications, update
intents, or analytics in the same change.
