# How sabnyx + halbor work (ELI5)

A plain-English tour of the whole system. For the formal contract see `SPEC.md`;
for commands to run see `TESTING.md` and `README.md`. This doc is the gentle
version.

---

## The one-sentence version

**sabnyx** is a second brain made of plain Markdown files that an AI helps you
keep tidy. **halbor** is a read-only window that shows the safe parts of that
brain to the outside world.

---

## The big analogy: a filing cabinet with a smart assistant

Imagine a filing cabinet.

- The **files** are plain Markdown (`.md`) — readable by you, by Obsidian, by
  any text editor, forever. No database. **Git** is the cabinet's lock, history,
  and the way it syncs between your machines.
- A **smart assistant** (an LLM — Kimi or Claude) does the boring filing work:
  reads new material, writes tidy summaries, files things in the right drawer,
  notices contradictions, and links related cards together.
- A **logbook** records every single change the assistant makes, so you can
  always see *what happened and why*.

You own the cabinet. The assistant just keeps it neat. If the assistant
disappeared tomorrow, your files are still plain Markdown you can read.

---

## sabnyx: the brain

### Three drawers (the tiers)

Everything flows in one direction, from messy to durable:

```
intake/   →   atlas/   →   domains/
(inbox)       (the keep)   (deep topics, later)
```

- **`intake/`** — the inbox. New stuff lands here as rough drafts. Disposable.
- **`atlas/`** — the keep. Durable, trustworthy, curated knowledge. This is the
  "real" library: `concepts/`, `references/`, `ideas/`.
- **`domains/`** — graduated topics big enough to deserve their own rules (a
  later-stage feature).

There are also `sources/` (the original raw clippings, kept untouched as proof
of where things came from) and `_review/` (scratch space — never knowledge).

### Every card has a label (frontmatter)

Each file starts with a little structured header (YAML "frontmatter"): its
`type`, a `description`, `tags`, when it was created, etc. This is a strict
**contract** — files that don't follow it get flagged.

### The most important line: `description`

The single most load-bearing field. It's a one-sentence answer to *"what is this
and why would I ever search for it?"* (30–200 characters).

Why it matters: a vault of 1000+ files is too big to read end-to-end. So the
assistant **reads only the descriptions** to decide what's relevant —
"filter-before-read." Descriptions are the **index**. (This is exactly the field
whose 200-char limit caused the recent `ask` bug — see `TESTING.md` notes.)

### The logbook (the ledger)

`ops/events.jsonl` is an **append-only** log. Every action that changes the vault
writes one line: "promoted this file," "the LLM was asked X," "captured an
answer." You never edit it; you only add to it. It's the brain's shadow — a
complete, replayable history. `ops/knowledge-log.md` is the human-readable
version.

### The rules (gates)

Before anything is trusted, it must pass **gates** — deterministic checks like
"has a valid type and description," "lives in the right drawer," "isn't a
duplicate," "came from a trusted source." Think of them as the bouncer at each
drawer. They run without the AI, so they're fast and predictable.

### The heartbeat (`sabnyx tend`)

One command runs the whole self-maintenance cycle, so the brain tidies *itself*:

1. **Auto-promote** — move obviously-good inbox items to the keep (no AI needed,
   just the gates).
2. **Triage** — for the rest, *once enough have piled up*, the AI proposes
   keep / promote / discard. You approve (unless you pass `--yes`).
3. **Reweave** — link new cards back to related old ones; flag contradictions.
4. **Health** — a quick "how's the vault doing" report.

`sabnyx tend --dry-run` previews everything and writes nothing.

### The clock (`opsctl`, in Rust)

`opsctl` is a small, fast control plane that owns the logbook and **schedules**
the heartbeat (run `tend` every 15 minutes, or once per cron tick). It speaks the
exact same logbook format as the Python side, so they interoperate.

### Talking to the AI (the LLM boundary)

All AI calls go through **one door** with three layers of indirection:

```
role        →     model            →    provider
(fast/          (kimi-k2.5,           (moonshot/Kimi,
 standard/       claude-opus-4-8,      anthropic,
 deep)           …)                    openrouter, …)
```

Your code only ever asks for a *role* ("give me a fast model"), never a specific
model. Swapping Kimi for Claude is one environment variable
(`SABNYX_LLM_PROVIDER`). Every call is logged (with token counts, never the
prompt text).

Two safety nets live at this door (added recently):
- **Empty-output guard** — reasoning models "think" using up their token budget;
  if the budget's too small they return nothing. We now say so clearly instead
  of crashing cryptically.
- **Repair-retry** — if the AI's structured answer breaks a rule (e.g. a field
  too long), we hand the error back and ask once more before giving up.

### Finding things (the retrieval seam)

When you ask a question, sabnyx first **finds the relevant cards** to feed the
AI. Two interchangeable ways (`SABNYX_RETRIEVER`):

- **`grep`** (default) — keyword overlap. Zero dependencies, instant.
- **`vector`** (opt-in) — semantic similarity using small static embeddings
  (~30 MB, runs on CPU, no GPU/torch). Install with `uv sync --extra vector`.

It's a "seam" because you can swap the finder without touching anything else.

### Asking questions (`sabnyx ask`)

You ask; sabnyx finds relevant cards, the AI answers **grounded in your vault**
and cites the cards it used as `[[wikilinks]]`. If the answer contains genuinely
*new* insight, the AI optionally proposes a **capture** — turning the answer into
a new inbox card. ("Good answers become new pages, not lost chat history.")
The answer is always the priority; a malformed capture never sinks it.

---

## halbor: the face

### A one-way mirror

halbor is the **read-only public face** of a sabnyx vault. The vault is the
single source of truth; halbor only ever *reads* it and *projects* a filtered,
public-safe view. Nothing in the vault depends on halbor.

```
sabnyx vault/atlas/**   ──project──▶   build/site/**  +  llms.txt
   (source of truth)                   (the public face)
```

### What it produces (`halbor project`)

It walks the durable `atlas/` drawer and emits:
- **staged Markdown** — each public page with a *rebuilt, redacted* header (only
  `description` + `tags`; internal fields are dropped).
- **`llms.txt`** — an index of the published pages (the
  [llmstxt.org](https://llmstxt.org) convention) so other AIs can find them.

### The sensitivity filter (fail-closed)

Two gates, both must pass to publish:
1. **Structural** — only `atlas/` (durable, final) is even *read*. The inbox,
   raw sources, and scratch are never touched, so they can't leak.
2. **Per-file** — an optional `visibility:` field. Absent or `public` → publish;
   anything else (or anything unrecognized) → **withheld**. When in doubt, it
   hides. Every withheld page is logged.

### Asking the face (`halbor ask`)

Same idea as `sabnyx ask`, but it answers over the **projected face** (never the
raw vault), with a tunable persona. Grep by default, opt-in vector. Free-form
text answers (no strict schema), so it's immune to the capture-overflow issue.

---

## How they fit together (the daily loop)

```
you capture / ingest  ──▶  intake/  ──(tend: gates + AI)──▶  atlas/
                                                               │
                                              opsctl schedules tend
                                                               │
                                                  halbor project (the face)
                                                               ▼
                                                   build/site/ + llms.txt
```

1. Stuff comes in (you jot a thought, or `ingest` a URL/file).
2. The **heartbeat** (`tend`, scheduled by `opsctl`) tidies, promotes, links.
3. **halbor** projects the safe, durable parts into a public face.
4. You (or an agent) **ask** questions of either the vault or the face.

---

## How to use it, day to day

| Want to… | Do |
|----------|-----|
| Check it's healthy (free) | `uv run pytest -q`; `sabnyx lint`; `sabnyx health` |
| Prove the AI is wired up | `sabnyx verify` |
| Add knowledge | `sabnyx new` / `sabnyx ingest <url\|file>` |
| Ask a question | `sabnyx ask "..."` |
| Tidy the vault | `sabnyx tend --dry-run`, then `--yes` |
| Publish the face | `halbor project --vault ../sabnyx/vault --out build/site` |
| Ask the face | `halbor ask "..."` |

Full command list + provider setup: `README.md`. Step-by-step testing recipe:
`TESTING.md`.

---

## Where to look in the code (a small map)

```
sabnyx/src/sabnyx/
  llm.py         the single AI door — providers, roles, repair-retry, ledgering
  schemas.py     the frontmatter / structured-output contracts (incl. description)
  services.py    shared orchestration: ingest, ask, promote, triage
  retrieval.py   the finder seam: GrepRetriever / VectorRetriever
  commands/      one file per CLI verb (ask, ingest, tend, verify, …)
  config.py      .env / moonshot autoload, trusted sources
opsctl/          the Rust clock: ledger, query, project, supervise

halbor/src/halbor/
  project.py     vault → staged markdown + llms.txt
  sensitivity.py the two publish gates (fail-closed)
  retrieval.py   grep / vector finder over the face
  persona.py     the answer voice for `halbor ask`
```

---

*Mental model in one line: **plain Markdown is the truth; the AI is the
librarian; the ledger is the logbook; halbor is the front window.***
