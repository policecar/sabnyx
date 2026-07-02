# Codebase review — sabnyx against the jibot/Switchboard spec

Date: 2026-07-01
Reviewed at: `03ae1ed` (main)
Reference: *Switchboard / jibrain / Jibot: A Production Knowledge Architecture
for AI Agents* (mirrored at `docs/spec/jibot.md`, 1,346 lines), plus the
repo's own SPEC.md, ROUTING.md, AGENTS.md, and ADRs 0001–0007.

Method: full read of the reference spec and all contract docs; line-level
review of the Python package (`src/sabnyx/`), the Rust control plane
(`opsctl/`), channels/adapters and the R1 web client, tests, and ops scripts.
The highest-impact findings were reproduced live in a sandboxed vault or
verified by execution; those are marked **[reproduced]**. Baseline health at
review time: `pytest` 258/258 passing, `cargo test` 23/23 passing, `ruff`
clean.

---

## Executive summary

sabnyx is an unusually honest and disciplined adaptation of the jibot
architecture. Nearly everything the spec treats as core is either implemented
or excluded with a written, ADR-cited rationale — the `DEVIATION:` annotations
in SPEC.md are accurate, and several sabnyx choices (provenance-based trust,
the ledgered LLM boundary, the cross-language hash contract, the raw
`sources/` layer) genuinely *improve* on the spec. The test suite is strong
where it matters most (the unattended auto-promote path, the HTTP ingress,
the data-root split).

That said, the review found **one critical data-loss bug** and a cluster of
majors, most sharing a single theme: **the guarantees hold on the happy path
the tests exercise, but break on inputs the system will eventually see** — a
duplicate slug reaching LLM triage, a malformed YAML file in intake, a
graduated `domains/` file, a DEL character in a payload, a `.env` with a
duplicated key, a cron working directory. In a system whose own doctrine is
"more effort should go into maintaining boundaries than into adding new
content," these edge paths are exactly where the boundaries live.

Top findings, in fix order:

| # | Sev | Finding | Where |
|---|-----|---------|-------|
| 1 | Critical | Heartbeat LLM-triage promote overwrites (or deletes) existing atlas files | `services.py:96–131`, `tend.py:217–223` |
| 2 | Major | Any file under `vault/domains/` crashes `health`/`tend` and hard-fails `lint` | `fm.py:38`, `health.py:34` |
| 3 | Major | `lint --staged` resolves against the code repo — pre-commit gate is a no-op in split-root setups | `lint.py:64–75` |
| 4 | Major | One malformed-YAML intake file aborts the whole scheduled `tend` run | `fm.py:41–63`, `tend.py:146–151, 220` |
| 5 | Major | Non-ASCII bearer token crashes the pre-auth path of `serve-http` | `adapters/r1.py:56` |
| 6 | Major | Telegram bot token leaks into logs via httpx error strings | `adapters/telegram.py:45,53`, `serve.py:210` |
| 7 | Major | Rust ledger append is not atomic (two `write(2)` syscalls) | `opsctl/src/ledger.rs:161` |
| 8 | Major | DEL (U+007F) breaks the Python⇆Rust `content_hash` contract | `opsctl/src/ledger.rs:114` |
| 9 | Major | Root/.env resolution diverges between Python and opsctl (duplicate keys; cwd-based discovery panics under cron) | `opsctl/src/root.rs:8–28, 98–125` |
| 10 | Major | Entity `aliases` are never consulted → false gap observations and false broken links | `entities.py:74–89`, `health.py:40–46` |
| 11 | Major | Capture-first ingest permanently poisons a slug when the LLM call fails | `services.py:543–578`, `sources.py:127–146` |
| 12 | Major | `source_url` required-for-bookmark/paper lives only in one CLI command, not the schema (Gate 2 hole) | `schemas.py:27–48` |
| 13 | Major | One bad ledger line bricks every ledger consumer, with no repair path | `ledger.py:102–116`, `opsctl/src/ledger.rs:176–184` |

---

## 1. Critical

### C1. `tend` LLM-triage promotion silently overwrites — or deletes — existing atlas files **[reproduced]**

`services.promote_file` never checks `dest.exists()` before `fm.write(dest, …)`
(`services.py:96,124`), and its validation rollback does `dest.unlink()`
(`services.py:130`). The docstring pushes the guard to callers ("dest must not
exist"), but only the `promote` CLI honors it (`promote.py:54`). The heartbeat's
pass 2 does not (`tend.py:217–223`).

The failure is not hypothetical — the deferred queue *systematically contains*
duplicates: auto-promote Gate 5 defers a file precisely because its slug
already exists in atlas (`tend.py:180–182`, `services.py:306–307`), and the LLM
triage prompt is shown only intake metadata, never the atlas index
(`services.py:323–336`), so it cannot know it is proposing a duplicate.

Reproduced: with `atlas/concepts/dupe.md` present, `promote_file(intake/dupe.md,
"concept")` replaced the durable atlas file with draft intake content and
unlinked the intake source, emitting a clean `atlas.promoted` event that masks
the loss. If post-write validation fails instead, the rollback deletes the
pre-existing atlas file. Under a scheduled `tend --yes` this is unattended data
loss on the authoritative tier — precisely the "temporal pollutes durable"
failure SPEC §5 and the spec's Containment section exist to prevent, executed
by the maintenance loop itself.

**Fix:** refuse in `promote_file` itself when `dest.exists()` (two of its three
call sites don't guard), and/or check `path.stem.lower() in atlas_slugs` before
the pass-2 call — the set is already maintained at `tend.py:221`. Only unlink
`dest` on rollback if this call created it.

---

## 2. Major

### M1. Domain graduation breaks the entire lint/health layer **[reproduced]**

`fm.classify` raises `ValueError` for any vault path not under
intake/atlas/.observations (`fm.py:38`). `health.scan_wikilinks` catches only
`FrontmatterError` (`health.py:34–39`), so with a single file at
`vault/domains/chanoyu/wabi.md`:

- `sabnyx health` dies with a traceback, and `sabnyx tend` dies with it via
  `_health_summary` (`tend.py:91`);
- `entities gaps` has the same hole (`entities.py:78–82`);
- `lint` catches broad `Exception` and reports the file as a gate failure
  (`lint.py:94–96`), so the pre-commit hook blocks every commit.

Domain graduation is a first-class feature of both SPEC §1 and the jibot spec;
the moment the operator uses it, the maintenance layer stops working. A related
foot-gun: `classify` keys off *any* path component (`"intake" in parts`,
`fm.py:29–38`) against the absolute path — a data root like
`/home/user/intake/...` misclassifies every file vault-wide. Classification
should be computed relative to `paths.VAULT`, and unclassifiable files should
be reported, not fatal.

### M2. `lint --staged` checks the wrong repository in split-root setups

`lint.py:64–75` runs `git diff --cached` with `cwd=ROOT` (the code repo) and
joins results against `ROOT`, then filters on `VAULT in p.parents` where
`VAULT = DATA_ROOT / "vault"`. With `SABNYX_DATA_DIR` pointing at a separate
vault repo — the documented deployment (DEPLOYMENT.md, `paths.py` DEC-001) — a
pre-commit hook in the data repo queries the *code* repo's index and never
matches a vault file: "no vault .md files to lint", exit 0. SPEC §3's claim
that "the git pre-commit hook blocks violations" is silently false exactly
where the vault actually lives. Use `DATA_ROOT` for both the subprocess `cwd`
and the path join.

### M3. One malformed intake file halts the heartbeat

Two compounding gaps:

- `fm.load` converts only `pydantic.ValidationError` into `FrontmatterError`
  (`fm.py:41–63`). Malformed YAML raises raw `yaml.parser.ParserError` /
  `yaml.scanner.ScannerError` (verified by execution: an unclosed bracket or a
  tab in the block). `tend`'s intake loop catches only `FrontmatterError`
  (`tend.py:146–151`), so one bad file aborts the entire scheduled run.
- Even for well-formed-but-invalid files that get deferred to pass 2: if the
  LLM says `promote`, `promote_file` → `fm.load(source)` raises
  `FrontmatterError`, which does **not** subclass `RuntimeError`, so the
  `except RuntimeError` at `tend.py:220–223` misses it and the run dies
  mid-pass, with no `tend.completed` event.

This is the "queue stagnation via gradual abandonment" mode the spec warns
about: the heartbeat silently stops doing its job until a human notices. Wrap
YAML errors into `FrontmatterError` in `fm.load`, and make tend's per-item
handlers catch `Exception`-per-file, report, and continue.

### M4. Non-ASCII bearer token crashes `serve-http`'s pre-auth path **[reproduced]**

`hmac.compare_digest(token, k)` raises `TypeError` on non-ASCII `str` input.
`check_bearer` (`adapters/r1.py:56`) is called from `route()`
(`serve_http.py:97`) *before* the try/except that wraps actions, and `route()`
is called from `do_GET`/`do_POST` unwrapped. Reproduced over a socket: any
unauthenticated client sending `Authorization: Bearer café` gets a stdlib
traceback on stderr and a reset connection instead of a clean 401 —
contradicting the module's own docstring ("one bad request can't take the
server down"). The constant-time fold over all keys is otherwise correct.
Compare on bytes (`token.encode()` vs `k.encode()`) or guard `token.isascii()`.

Related unauthenticated crash: `resolve_static` doesn't catch the
`ValueError: embedded null byte` from `.resolve()` (`serve_http.py:159–168`),
reachable via any GET containing `%00` when `--static` is set.

### M5. Telegram bot token leaks into logs on poll/send errors

`adapters/telegram.py:45,53` build URLs as `…/bot{token()}/…` and call
`raise_for_status()`; httpx embeds the full URL — token included — in the
exception string. `serve.py:210` prints that exception on poll errors, and
`send_message` failures (outside the per-message try at `serve.py:172–182`)
reach the same handler. A routine `409` (two pollers) or `401` (revoked token)
writes the secret to stdout — which the documented deployment tees to
`/tmp/sabnyx-heartbeat.log`. Log exception type/status only, and redact the
URL.

### M6. Rust ledger append is not atomic, despite claiming to be

`writeln!(f, "{}", line)` on an unbuffered `File` issues two `write(2)`
syscalls — the JSON, then the newline — defeating the O_APPEND single-write
atomicity the function's doc comment claims (`opsctl/src/ledger.rs:161`). The
documented deployment runs exactly the racing pair: a long-lived
`opsctl supervise --every 900` daemon plus interactive Python CLI appends. An
interleave produces a line containing two concatenated JSON objects — which
then hard-fails **every** reader in both languages (see M9). Single
`write_all` of `line + "\n"`. The Python side's atomicity comment is also
wrong (`ledger.py:95–96`): PIPE_BUF governs pipes, not regular files, and the
buffered text writer can split large lines; a single-write append helper is
warranted there too.

### M7. DEL (U+007F) breaks the cross-language hash contract **[verified by execution]**

Rust's `write_json_string` passes through every char `< 0x80` above the
control range; CPython's `ensure_ascii=True` escapes everything outside
`0x20..=0x7E`, so `\x7f` becomes the six-byte escape `\u007f` in Python but stays raw in Rust —
different `content_hash` for the same payload (`opsctl/src/ledger.rs:114`).
This directly violates the README's "same `content_hash`" claim and ADR-0002's
hash contract, and would poison future Lemma attestation. One-character fix:
passthrough range `0x20..=0x7E`. (The surrogate-pair emulation and the
shell-out parity tests are otherwise excellent — the DEL case sits exactly
between the tested ranges. Add it as a test vector.)

Adjacent asymmetries worth closing while there: Rust doesn't enforce the
no-floats rule Python enforces (`ledger.rs:138–144` — the first future float
field silently breaks parity with no error on either side), and Rust's `Event`
accepts unknown fields where Python's `LedgerEvent` is `extra="forbid"`.

### M8. Python and opsctl can silently resolve different data roots

Two divergences in what is supposed to be a parity module
(`opsctl/src/root.rs` vs `paths.py`/`_dotenv.py`):

1. **Duplicate `.env` keys:** Rust returns the *first* `SABNYX_DATA_DIR`
   match (`root.rs:98–125`); Python's dict-assignment parser returns the
   *last*. A user who appends a new value without deleting the old line gets a
   silently split ledger — the exact failure the module's own comment says it
   exists to prevent.
2. **Root discovery walks up from cwd**, not from the installed module
   (`root.rs:8–28`). The README-endorsed cron form (`opsctl supervise --once`)
   runs with cwd `$HOME`; if `SABNYX_DATA_DIR` lives in `<repo>/.env` (the
   README-endorsed method), `data_root()` reaches an `expect`-panic ("no
   SPEC.md found") every 15 minutes, and no `task.*` events are ever emitted.
   Running from inside any *other* checkout containing a `SPEC.md` silently
   adopts that repo's root instead.

Fix: last-match-wins in the dotenv scan; resolve the repo root from the binary
or an explicit `--root`/env override rather than cwd; return a clean error
instead of panicking.

### M9. One bad ledger line bricks every consumer, with no legal repair

Both `ledger.replay()` (`ledger.py:102–116`) and Rust `read_events`
(`ledger.rs:176–184`) hard-fail on the first corrupt, truncated, or
foreign-field line — killing `count()`, `reconcile`, `reweave`'s work-queue
build, `opsctl query`, and `opsctl project` in one stroke. The append-only
doctrine ("never edit a past line") forbids the only repair. Compounding it,
the spec's integrity features have no analogue: `content_hash` is written but
**never verified** on any read path, there is no gap detection, no idempotent
ingest. Hash-without-verification is inert. Minimum: report the offending line
number and skip-with-warning (loudly); better: an `opsctl verify` pass that
recomputes hashes — ~10 lines that would also have caught M7.

### M10. Entity aliases are declared but never resolved **[reproduced]**

SPEC §2 defines `aliases` as "alternate names the page answers to," but
nothing answers to them: `entities gaps` builds its existence set from file
stems only (`entities.py:74–89`) and `health.scan_wikilinks` matches targets
against stems (`health.py:40–46`). Reproduced: a page `jane-doe.md` with
`aliases: ["JD"]` plus three `[[JD]]` links yields a false `gap` observation
("no page exists") and three false broken-link reports — and alias-only pages
inflate the orphan count. Health also lowercases but does not slugify targets,
so `[[Spaced Name]]` doesn't credit `spaced-name.md`, while `entities` does
slugify — the two subsystems disagree about what a link resolves to. Build one
shared alias-and-slug-aware resolution index and use it from health, entities,
and `context.slug_index`.

### M11. Capture-first ingest permanently poisons a slug on LLM failure

`ingest_text` writes the write-once source artifact *before* the LLM call
(`services.py:543–578`). If extraction fails (retries exhausted, budget,
malformed output), no intake file is written — and every retry of the same
content dies in `sources.capture` with `FileExistsError` ("already captured").
The content can never be ingested without manually deleting the artifact.
Compounding it, `title_from` titles free text by its **first 8 words**
(`services.py:49–52`), so two *different* messages sharing an opening phrase
collide the same way. No ledger event distinguishes "captured and extracted"
from "captured, extraction failed." Fix: on extraction failure either remove
the just-captured artifact or record a `ingest.failed` event and let a
re-ingest of an already-captured source proceed to extraction against the
stored artifact; add a content-hash suffix to collision-prone slugs.

### M12. `source_url` requirement lives outside the contract

SPEC §3 marks `source_url` as *required for bookmark, paper*. The pydantic
`IntakeFrontmatter` — explicitly the implementation of Gate 2 PRECISION —
makes it optional for all types with no validator (`schemas.py:27–48`).
`new.py:71` re-implements the rule manually (contract drift), but the LLM path
doesn't: ingesting pasted text the model classifies as `bookmark` writes a
bookmark with no `source_url` that lint passes, and the auto-promote gates
don't check it either (`services.py:296–313`) — so unverifiable references can
reach atlas unattended. Add a model validator; delete the hand-rolled check.

---

## 3. Minor findings

### Pipeline semantics

- **Reweave queue can never re-fire for a completed path.** `_pending_targets`
  (`reweave.py:60–77`) uses set difference: any historical `reweave.completed`
  permanently masks *later* `reweave.proposed` events for the same subject
  (delete → re-ingest → re-promote drops the second intent). `opsctl project`
  implements last-event-wins (`project.rs:37–43`) — the two disagree about the
  queue's contents; Rust's semantics are the right ones, fix both to match.
- **`--dry-run` is not write-free on the ledger** for `ingest` (appends
  `ingest.proposed` with a fabricated subject plus `llm.*` events), `scan`
  (appends `scan.completed` before the gate), and `reweave` (llm events).
  `tend --dry-run` and `entities gaps --dry-run` are genuinely write-free —
  make the others match, or at least stamp `dry_run: true` in payloads.
- **"Pending observations" never shrinks.** `health.py:105` and `tend.py:94`
  count every `.observations/*.md` regardless of `status`; `scan` filters
  `status == "pending"` (`scan.py:58`) — inconsistent, and no command ever
  transitions an observation's status, so the "unaddressed friction" pulse
  only ever grows.
- **Unattended discards vanish.** DEC-02 says "note it, do not delete," but
  the note is console-only (`tend.py:226–230`) — no ledger event, no `_review/`
  line. Under cron the operator never learns what the LLM judged worthless
  (jibot raises a review notification for exactly this).
- **`reconcile` has no compensating-event path** (`reconcile.py:64`): any
  legitimate atlas deletion makes it exit 1 forever; SPEC §10 prescribes
  compensating events but no kind is recognized and none is ever emitted.
- **tend edge cases:** without `--yes` on a non-tty (cron), `click.confirm`
  raises `Abort` and kills the run; `--threshold 0` triggers an LLM triage
  call with zero items.
- **Gate 5 currency over-fires:** `health.py:93–95` applies the 180-day check
  to every atlas type, contradicting the file's own comment ("references are
  the time-sensitive type at stage 0") and the spec — every never-re-verified
  concept becomes a permanent warning (alarm fatigue).
- **Orphan definition deviates; dead-end detection missing.** Health flags
  atlas files with no *incoming* links (`health.py:62–66`); jibot defines
  orphan as no links either direction and separately requires dead-end
  detection (receives links, links nowhere) — absent and unmentioned.

### Core library

- **Non-ASCII titles all slugify to `"untitled"`** (`util.py:31–44`): the
  second Japanese-titled capture collides in write-once `sources.capture`
  (dead-ends per M11). A hash-suffix fallback keeps SPEC §6's ASCII rule
  without the collision.
- **Two knowledge-log formats, neither jibot's.** `writers.py:84–95` writes
  `# [ts] op | title`; `services.py:173–179` (promote) writes `- {date}
  promote: …` bullets into the same file. jibot's Gate 3 grep contract is
  `grep "^## \["` — a single audit misses promotions entirely. Unify on one
  format via `append_knowledge_log`.
- **Call budget under-counts:** `_charge_budget` fires once per
  `parse_structured` (`llm.py:219–229`), but retries and the repair re-ask are
  full-price provider calls — worst case ~8× the configured ceiling in HTTP
  requests. Charge repair re-asks at minimum.
- **Failed LLM calls leave dangling `llm.requested` intents** with no
  compensating `llm.failed` action (`llm.py:298–346`) — an auditor cannot
  distinguish in-flight from failed, and it's the failed runs that most need
  auditing.
- **Salvage is dead code on the default provider:** the Anthropic path raises
  plain `RuntimeError` without `raw_content` (`llm.py:420–424`), so
  `_salvage_ask_answer` can never recover an answer there — only on
  OpenAI-compatible providers.
- **Slug collisions silently shadow files in retrieval:** `context.slug_index`
  maps stem → path last-wins (`context.py:21–35`), so same-stem files across
  atlas subdirs (which C1 can create) become invisible to `ask`/`reweave`, no
  warning; it is also case-sensitive while `vault_slug_index` lowercases.
- **Captured artifact ≠ what the model read:** `sources.capture` truncates at
  600k chars but `build_ingest_proposal` receives the untruncated text
  (`services.py:559–568`) — the immutable provenance artifact doesn't match
  the extraction input, undermining Gate 3's audit purpose.
- **`set_marker_block` on a half-corrupted block appends a duplicate**
  (`util.py:88–104`): if only one of BEGIN/END survives a manual edit, a
  second block is appended — violating SPEC §8's "never nest markers".
- **`_clean_citations` fallback launders hallucinated citations**
  (`services.py:612–628`): extracts wikilinks from prose without checking they
  exist in the vault index (in scope at the call site).
- Nits: CGNAT `100.64.0.0/10` passes the SSRF guard; `_dotenv` keeps inline
  comments in values; `created` accepts naive datetimes (SPEC says "ISO 8601
  with TZ"); `new.py` validates after writing (a 29-char description writes
  then deletes) and never appends to the knowledge log.

### Channels

- **`serve.py` echoes raw exception text to the chat client**
  (`serve.py:113–114,174–175`) — the same leak class the hardening pass closed
  on `serve_http` (which now returns a generic error, detail to ledger only).
- **Module-level shared `StringIO` console in `serve.py:41`** — the exact slow
  memory leak `serve_http` fixed with a per-request factory; `serve` is the
  long-running process.
- **Allowlist check fails open on an empty set** in `_dispatch_batch`
  (`serve.py:163`, `if trusted and …`) — covered by the startup refusal, but
  the primitive itself should fail closed like `r1.check_bearer` does.
- **A `send_message` failure re-processes the whole batch**
  (`serve.py:184`): the call sits outside the per-message try, so the offset
  never advances and `ask` calls re-run the LLM on the next poll.
- **Envelope dedup is aspirational:** `envelope_id` (the SHA-256 fingerprint)
  is computed but never stored or checked; `actor_canonical` has no
  handle→canonical mapping. Acceptable stage-0 gaps, correctly labeled
  "dedup-ready" in ADR-0007 — noting so the README's envelope story isn't
  over-read.

### opsctl

- **`query --since` lexicographic comparison is wrong across mixed-precision
  timestamps** (`query.rs:32–36`): pydantic emits `…T12:00:00Z` for
  zero-microsecond times, Rust always emits 6-digit micros, and
  `'.' < 'Z'` — so `--since 2026-04-22T12:00:00Z` *excludes* an event at
  12:00:00.5. Parse with chrono or normalize.
- **`--limit N` returns the oldest N matches** (`query.rs:46–48`) — the
  README's own example (`--limit 10`) will show the first ten promotions ever,
  forever. Take the tail.
- **`supervise --every N` drifts** (`supervise.rs:112–122`): sleeps N *after*
  each cycle, so real period = cycle + backoffs + N; a 5-minute tend under
  `--every 900` runs every ~20 minutes. Anchor to cycle start.
- **A single failed ledger append exits the daemon** (`supervise.rs:113`):
  one transient ENOSPC kills the supervisor; it should outlive its
  bookkeeping.
- **No signal handling:** SIGTERM between `task.started` and completion leaves
  a dangling started event, and `code().unwrap_or(-1)` conflates
  signal-killed children with exit −1.
- **Backoff exponent overflows** with `--retries ≥ 64`
  (`supervise.rs:80` — cap the exponent, not the result); `atlas.created` in
  `project.rs:44` is a dead kind no producer emits; byte-slicing `ts` in the
  query table can panic on multibyte corruption (`query.rs:102–104`).

---

## 4. Spec conformance map

Statuses: **IMPL** implemented · **PARTIAL** · **DOC-ONLY** documented, no
code · **OUT** deliberately excluded with written rationale ·
**MISSING-SILENT** neither built nor acknowledged.

### The spec's 17-item implementation checklist

| # | jibot checklist item | Status | Notes |
|---|---|---|---|
| 1 | Three-tier directory structure | **IMPL** | `vault/intake\|atlas\|domains` + `_review/`; domains is an empty tier awaiting graduation, as designed — but see M1: existing tooling crashes on it |
| 2 | Frontmatter schema + `description` | **IMPL** | Pydantic `extra="forbid"`; description hard-bounded 30–200 (tightens jibot's "~150 chars" into an enforced range); Gate 2 hole for `source_url` (M12) |
| 3 | Routing decision tree | **IMPL** | ROUTING.md mirrors the spec; channel routing is deterministic-by-shape, not LLM (ADR-0007) — a stronger reading than jibot's |
| 4 | Triage process | **IMPL** | Two-speed heartbeat exactly per spec: all 6 auto-promote gates present and pure, LLM tier at ≥5 threshold, human-gated unless `--yes`; no scheduled-hours trigger; C1 undermines the gate in pass 2 |
| 5 | Health monitoring | **IMPL** (partial) | Orphans, broken links, schema drift, stale intake, gates 3/5/6 metrics; dead-end detection and jibot's orphan definition missing-silent (minor) |
| 6 | Reweave pass | **IMPL** (partial) | Ledger-driven intent queue, marker-block-only edits, contradiction observations; the 4-tier link-priority ranking is prompt-level, not mechanized; steps 4/5/7 of the algorithm partial |
| 7 | Observations mechanism | **IMPL** | 5 categories field-for-field with jibot; written by ingest/scan/entities; no status transitions ever happen (minor) |
| 8 | Pipeline health checks (7 gates) | **IMPL** | SPEC §11 maps all seven; enforcement tiering (1/2/4 hard, 3/5/7 warn, 6 dashboard) follows the spec's own "start with 1, 2, 4" |
| 9 | Currency tracking | **IMPL** | `last_verified`, promote default, Gate 5 flagging — over-broad type coverage (minor) |
| 10 | Agent workspace boundaries | **DOC-ONLY** (acknowledged) | AGENTS.md §2 role table; "stage 0 runs everything under operator… stage 3 introduces hard sandboxing" — honest, but cites a recon doc that doesn't exist, and has no row for the `sabnyx-r1` actor |
| 11 | Multi-machine sync w/ containment | **ADAPTED** (ADR-0001) | Syncthing → git; DEPLOYMENT's two-repo code/data split is a real containment boundary; per-machine subsets irrelevant for one user and said so |
| 12 | GTD/task integration | **MISSING** (rationale buried) | Nothing anywhere; only trace is a SKIP note in `docs/recon/obs-dailynotes-reuse.md` — deserves a line in the build-out non-goals |
| 13 | Domain graduation | **DOC-ONLY** (deliberate) | Criteria in ROUTING.md (10+ files vs jibot's ~15+, required SCHEMA.md, deliberate act); no tooling — and M1 means the rest of the tooling breaks when used |
| 14 | Search infrastructure | **PARTIAL** (deliberate) | Retrieval seam: keyword `GrepRetriever` default + opt-in `VectorRetriever` (static embeddings, torch-free); no "deep" mode, no collection segmentation (atlas-scoping achieves the containment effect); YAGNI rationale written twice. Unresolved tension: the repo's own architecture review argues reweave needs semantic search first, yet reweave runs on grep |
| 15 | Contact intelligence | **OUT** → lite layer | Senzing/Nyne/CRM explicitly non-goals; replaced by entity types + `aliases` + `entities list/gaps/resolve` — a defensible distillation, currently undercut by M10 (aliases unresolved) |
| 16 | Operational control plane | **IMPL-LITE** (ADR-0002) | `query`/`project`/`supervise` over one JSONL ledger, cross-language hash contract, `task.*` events with retry/backoff; the best-documented reduction in the repo. Two unacknowledged debts: concurrency safety the zones used to provide (M6), and integrity verification (M9) |
| 17 | Continuous improvement review | **MISSING-SILENT** | No telemetry/session-scanning analogue and — uniquely among the 17 — no written acknowledgment anywhere |

### Notable extras

| jibot feature | Status | Notes |
|---|---|---|
| Knowledge provenance log (Gate 3) | **IMPL** | Grep contract preserved; format drift: `# [` vs jibot's `## [`, and promote writes a second, different format (minor above) |
| Trusted-source auto-promote gate | **IMPL, improved** | Sharpened from jibot's "demonstrated reliable output quality" into provenance-trust: only operator-local `sabnyx-ingest` is trusted; `-web`/`-telegram`/`-r1` can never auto-promote — closing the prompt-injection→atlas path. The strongest single design decision in the repo |
| Heartbeat | **IMPL** | `tend` + `opsctl supervise --every 900`/`--once` + `recipes/heartbeat.sh`; cadence and gate set mirror jibot's, including the supervise-wraps-the-scheduler pattern |
| LLM routing / roles | **IMPL** | ADR-0006 role→model→provider; iblai-style message scorer with `[fast]`/`[deep]` overrides; depth classifier per jibot's extraction tiers; additions jibot lacks: per-run budget, retries, and ledgering every call (intent + action, prompt hash not text) |
| NormalizedEnvelope | **IMPL** | Three normalizers (cli/telegram/r1), one `services.py` write path; ADR-0007 frames Telegram as "the second implementation that earns the abstraction," proven by the R1 adapter; dedup/canonical-actor aspirational |
| Async extraction service | **ADAPTED** | `serve` + `serve-http` ≈ the knowledge-intake sprite's `POST /intake`; no `/intake/structured` bridge endpoint (unneeded) |
| Session-to-knowledge capture | **IMPL** | `ask`'s capture proposal with salvage logic (salvage dead on Anthropic — minor above) |
| Contradiction detection | **IMPL** | Split into `scan` (atlas-wide) + reweave-time observations, dedup against pending |
| Raw sources layer | **sabnyx-original** | ADR-0005, from Karpathy — closes a gap jibot has (jibot keeps extractions, not originals) |
| `reconcile` drift check | **sabnyx-original** | With an unusually honest ARCHITECTURE.md admission that the ledger is *not* event-sourcing; git is truth |
| Onyx / enterprise / Drive / RBAC | **OUT** | Named non-goals; halbor (separate repo) is the single-user analogue of "vault is truth, read-only projection" |
| Amplifier runtime | **OUT** (ADR-0003) | "Steal patterns, don't adopt" — backed by recon that falsified the spec's implied recipe portability |
| NanoClaw/Hermes fleet, kata, news-scan, GEPA, book-ingest, meeting pipeline | **OUT** (mostly implicit) | Covered by "single-user, no legacy bits"; meeting-extract type reserved in SPEC |
| Payments (x402) / ZK provenance (Lemma) | **DOC-ONLY, parked** | Reserved syntax + `proof:` field; correctly labeled as sabnyx-original (not in jibot) |

---

## 5. Documentation coherence

1. **README "Status" is stale and self-contradictory:** it names the entity
   layer, the retrieval seam, and halbor as "Next" — while its own command
   list documents `sabnyx entities`, ARCHITECTURE.md documents the shipped
   retrieval seam and vector retriever, and the build-out plan marks Phases 3–4
   done and halbor v0 shipped. Three docs, three states of the world.
2. **README references files a fresh clone cannot have:** the jibot mirror at
   `vault/atlas/references/…` and `ops/events.jsonl`/`knowledge-log.md` are
   `.gitignore`d (they live in the private data repo). DEPLOYMENT.md explains
   the split; README never cross-references it.
3. **AGENTS.md cites `docs/recon/nanoclaw-mirror.md`, which does not exist**
   (ADR-0004 admits "to be written"); hardcodes the operator's laptop path in
   the session checklist; the checklist's `git status` is subtly wrong
   post-data-split (vault mutations don't show in the code repo); the
   workspace table has no row for the `sabnyx-r1` actor.
4. **roadmap.md has an ADR-numbering collision:** it reserves 0005/0006 for
   Lemma and x402, but those numbers have since been assigned to raw-sources
   and the LLM boundary. Semi-historical ("the original stage 0–3 arc"), but
   the dangling numbers will bite whoever writes the Lemma ADR.
5. **halbor's state is described three inconsistent ways** across
   build-out-plan (deferred / "currently empty"), ARCHITECTURE.md, and
   TESTING.md (working, "expect: 41 passed") — and none is verifiable from
   this checkout since halbor is a separate, absent repo; the docs should say
   so.
6. **TESTING.md's "expect: 151 passed" is stale** (258 pass today).
7. What is *not* overstated, for the record: the command list, heartbeat
   description, provider matrix, trust model, and opsctl semantics all check
   out against the code; SPEC's `DEVIATION:` annotations are accurate against
   the jibot source. The exceptions are SPEC §3's pre-commit claim (M2) and
   the README's "both write to one file" / "same content_hash" (M6–M8).

---

## 6. Test coverage

258 Python tests + 23 Rust tests; CI runs pytest/ruff/ty and cargo
test/clippy/fmt, including cross-language hash-parity tests that shell from
Rust into Python.

**Strong:** the heartbeat/auto-promote path (`test_tend.py`, 626 lines — every
gate individually, dry-run purity, trust config, ledger events); the HTTP
ingress (`test_serve_http.py` — auth, refuse-open, traversal,
Transfer-Encoding, body caps, socket-level); Telegram serve; the provider
layer (roles, JSON modes, empty-output diagnostics, repair-retry, budget,
backoff); the `SABNYX_DATA_DIR` resolution matrix (clearly hardened after the
systemd incident in `.solutions/`); entities, retrieval, ingest provenance,
reweave marker blocks, scan dedup, ledger hash vectors, SSRF guard, reconcile.

**Weak:**

- `health` — 36 lines testing one helper; orphan/broken-link/drift/cost logic
  essentially untested (M1 and M10 live exactly here).
- `lint` — no dedicated test file; the pre-commit hook, the "hard enforcement"
  SPEC leans on, is untested entirely (M2 lives exactly here).
- `triage` and `new` — path-display tests only.
- opsctl: `query.rs` and `project.rs` have **zero** tests — `project` is the
  most logic-dense untested Rust surface (the pending-reweave divergence lives
  there).
- The hash-parity vectors miss the DEL character (M7 sits exactly between the
  tested ranges).

The pattern is consistent: the well-tested subsystems are the ones where this
review found only edge-case bugs; the untested ones (health, lint, project)
are where the reproduced majors cluster.

---

## 7. Strengths

Worth stating plainly, because the fix list above shouldn't obscure it:

- **The provenance-trust model is better than the spec's.** jibot gates
  auto-promotion on sources that "demonstrated reliable output quality";
  sabnyx gates on *provenance* — remotely-fetched and third-party content
  carries a distinct untrusted `source_agent` and can never reach atlas
  unattended. This closes a prompt-injection path the spec leaves open, and
  it's wired correctly end-to-end (verified from channel actor stamps through
  `auto_promote_decision`).
- **The hash contract is exemplary for its size:** payload-only hashing,
  opaque `ts`, the no-floats rule — documented identically in ADR-0002,
  `ledger.py`, and `ledger.rs`, with live cross-language shell-out tests. The
  DEL bug is a one-character gap in an otherwise unusually careful piece of
  engineering (the surrogate-pair `ensure_ascii` emulation is correct — a
  detail most reimplementations get wrong).
- **`promote_file`'s crash-ordering is carefully reasoned** (write → validate
  → ledger-as-commit-point → unlink), and the reasoning is written down where
  the code is.
- **SSRF hardening in `sources.fetch_url` is well above typical:** scheme
  allowlist, all-resolved-address checks, per-redirect re-validation, byte
  caps, and an honest TOCTOU disclosure.
- **The LLM boundary delivers everything the README claims** — role
  indirection, per-call double-ledgering with prompt hashes, fail-closed
  budget, backoff — and the budget contextvar and retrieval seam are clean
  designs.
- **The documentation discipline is the best part of the repo:** ADRs with
  real trade-offs, `DEVIATION:` annotations that are accurate, recon that
  falsified a spec claim before adopting it (ADR-0003), and an architecture
  doc that *under*-claims ("the ledger is not event-sourcing") to stay honest.

---

## 8. Notes on the reference spec itself

- The jibot document is a narrative accretion, not a specification: three
  "Recent Additions" sections partially supersede the body (beads→kata,
  NanoClaw→Hermes, Onyx connector swap) with corrections in asides. sabnyx's
  ADR + `DEVIATION:` discipline is a straightforwardly better format for the
  same material.
- The spec's 17-item checklist has no item for the LLM loop itself — the
  engine appears only in prose. sabnyx's architecture review documents the
  consequence empirically: building items 1–5 deterministically produced "a
  typed markdown linter, not a knowledge system."
- Ambiguities sabnyx resolved well: description length (hard 30–200), trusted
  sources (provenance-based), graduation threshold (10+ plus required
  SCHEMA.md), and — most substantially — ledger hashing: jibot never
  specifies canonical JSON; ADR-0002's cross-language hash contract is a real
  specification the source lacks.
- Where jibot is genuinely ahead: deep search + collection segmentation,
  contradiction detection over semantic search (sabnyx's runs on grep), and
  the continuous-improvement review loop (the one silently missing checklist
  item). Both systems lack adversarial verification and confidence decay;
  jibot says so in "What We Don't Have Yet" — sabnyx's own review recommended
  adopting that honesty list into its non-goals and never did.

---

## 9. Recommended fix order

1. **C1** — destination guard in `promote_file` + duplicate check in tend
   pass 2 (one-line guard; closes unattended data loss).
2. **M3** — wrap YAML errors into `FrontmatterError`; per-file exception
   handling in tend (keeps the heartbeat alive).
3. **M1** — make `classify` relative to `paths.VAULT` and unclassifiable
   files non-fatal (unblocks domain graduation).
4. **M2** — `DATA_ROOT` in `lint --staged` (restores the pre-commit gate
   where the vault actually lives).
5. **M4/M5** — bytes-compare bearer tokens; redact Telegram URLs in error
   logs (both are small, both are exposed surfaces).
6. **M6–M8** — single-write append in Rust (and Python), DEL passthrough fix
   + test vector, last-match dotenv + non-cwd root discovery (restores the
   cross-language ledger contract the README promises).
7. **M9** — line-numbered, skip-with-warning replay + an `opsctl verify`
   hash pass (makes the append-only doctrine survivable).
8. **M10–M12** — shared alias-aware link resolution; ingest failure recovery
   path; `source_url` model validator.
9. Backfill tests for `health`, `lint`/pre-commit, and `opsctl
   query`/`project` — the three surfaces where the reproduced majors
   clustered.
10. Doc sweep: README Status, AGENTS.md dangling references and r1 row,
    roadmap ADR numbers, TESTING.md counts.

---

## 10. Resolution log (2026-07-01)

Every finding was re-verified under scrutiny (reproduced live or traced
end-to-end) before being fixed; findings that did not survive are marked
refuted. Fixes landed in four commits on this branch:

- `cd7d5eb` — channels (serve, serve-http, adapters)
- `99f6898` — opsctl (Rust control plane)
- `c04fac4` — pipeline/heartbeat (fm, tend, lint, health, entities, reweave, scan, reconcile, promote)
- `52e59b2` — core library (ledger, llm, schemas, sources, context, util, writers, ingest, new, ask)

Gates after the last commit: `pytest` 328 passed (was 258), `cargo test`
50 passed (was 23), `ruff`/`ty`/`clippy`/`fmt` clean.

### Critical and major findings

| Finding | Verdict | Fixed in |
|---|---|---|
| C1 — heartbeat LLM-triage overwrites/deletes atlas files | CONFIRMED (reproduced) — fixed: `promote_file` refuses an existing destination; tend pass 2 skips duplicate slugs | `c04fac4` |
| M1 — `domains/` files crash health/tend, hard-fail lint | CONFIRMED (reproduced) — fixed: VAULT-relative `classify`; unclassifiable files skipped non-fatally, still resolve as link targets | `c04fac4` |
| M2 — `lint --staged` checks the code repo, not the data repo | CONFIRMED — fixed: `DATA_ROOT` for both cwd and join; first dedicated lint tests added | `c04fac4` |
| M3 — one malformed-YAML intake file aborts `tend` | CONFIRMED (reproduced) — fixed: YAML errors wrapped as `FrontmatterError`; per-file handling in both tend passes | `c04fac4` |
| M4 — non-ASCII bearer token crashes pre-auth; NUL byte crashes static path | CONFIRMED (socket-level repro) — fixed: bytes compare keeping the constant-time fold; `ValueError`/`OSError` caught in `resolve_static` | `cd7d5eb` |
| M5 — Telegram bot token leaks into logs via httpx errors | CONFIRMED (reproduced) — fixed: errors re-raised redacted (`status + method` only, `from None`) | `cd7d5eb` |
| M6 — Rust ledger append is two syscalls, not atomic | CONFIRMED (write_fmt trace) — fixed: single `write_all` of line+newline | `99f6898` |
| M7 — DEL (U+007F) breaks the cross-language hash | CONFIRMED (executed both sides) — fixed: passthrough `0x20..=0x7E`; DEL added to hardcoded and shell-out parity vectors | `99f6898` |
| M8 — root/.env resolution divergence (duplicate keys; cwd discovery panics under cron) | CONFIRMED (repro incl. cwd=/ panic) — fixed: last-match dotenv; exe-relative root discovery with clean error, no panic | `99f6898` |
| M9 — one bad ledger line bricks all consumers; hash never verified | CONFIRMED — fixed both sides: line-numbered errors with snippet (Rust + Python), Python single-write append, new `opsctl verify` recomputing every `content_hash` | `99f6898`, `52e59b2` |
| M10 — aliases never consulted; health/entities disagree on resolution | CONFIRMED (reproduced) — fixed: shared `link_resolution_index` (stems + slugified aliases) used by both | `c04fac4` |
| M11 — capture-first ingest permanently poisons a slug | CONFIRMED — fixed: rollback + `ingest.failed` event on extraction failure or decline; content-hash suffix on different-content collisions | `52e59b2` |
| M12 — `source_url` rule lives outside the schema | CONFIRMED — fixed: model validator (bookmark/paper); LLM path downgrades urlless bookmark to note; manual check removed | `52e59b2` |

### Minor findings

All pipeline, channel, and core minors were confirmed and fixed in the
commits above (reweave requeue semantics now last-event-wins matching
`opsctl project`; scan dry-run ledger purity; pending-observation status
filter; ledgered `triage.decided` for keep/discard; `atlas.removed`
compensating events in reconcile; non-tty and `--threshold 0` guards;
Gate 5 restricted to references; orphan definition per spec plus
dead-end detection; validated `--last-verified`; within-batch link
dedup; generic error replies + per-call console + fail-closed dispatch +
per-message send in serve; `--since`/`--limit`/backoff/drift/signal
fixes in opsctl; slugify hash fallback; unified knowledge-log format;
repair re-ask charged; `llm.failed` events; salvage on Anthropic;
excerpt budget; slug-index collisions; captured-text parity; half-open
marker refusal; citation filtering; CGNAT block; tz-aware `created`;
`fm.write` kwargs; validate-before-write + Gate 3 in `new`; forced
`--capture` note).

Refuted or resolved as not-a-bug on scrutiny:

- **reweave `--dry-run` ledger writes** — the `llm.requested`/`llm.completed`
  events record calls that really happened; only scan's premature
  `scan.completed` was wrong (fixed). REFUTED in part.
- **`thinking: {"type": "adaptive"}`** — correct for the pinned anthropic
  SDK 0.109.1 (`ThinkingConfigAdaptiveParam` is in the non-beta union).
  VERIFIED-NO-FIX.
- **dotenv inline-comment handling** — intentional byte parity between
  `_dotenv.py` and `root.rs`; both keep `# comment` in values.
  VERIFIED-NO-FIX.

Deferred with reasons:

- **opsctl signal trapping** (F18, partial) — recording the killing signal
  in `task.failed` is done (`ExitStatusExt::signal`); trapping SIGTERM in
  opsctl itself to close the dangling `task.started` window needs a new
  dependency (`signal-hook`) — out of minimal-fix scope.
- **serde_json lossy large ints** (F20) — a real fix requires the
  `arbitrary_precision` feature with ripple effects; mitigated: the new
  float guard makes such payloads error loudly in `content_hash`, and
  `opsctl verify` flags them.
- **Section 5 documentation-coherence items** (stale README Status,
  dangling AGENTS.md references, roadmap ADR numbering, TESTING.md
  counts) — fixed in the follow-up docs sweep on this branch: README
  Status/layout/credits updated, AGENTS.md gained the r1 row and lost
  the dangling reference and hardcoded paths, roadmap and the 2026-06
  architecture review are marked historical with shipped items
  annotated, build-out-plan is marked complete with a current
  environment note, TESTING.md counts refreshed, and ARCHITECTURE.md
  notes that halbor is a separate repo.
