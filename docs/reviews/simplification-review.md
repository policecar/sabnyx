# Sabnyx Simplification Review

**Date:** 2026-06-19
**Scope:** Full codebase — Python (`src/sabnyx`), Rust (`opsctl`), JS (`creations/js`), shell (`recipes`, `ops/scripts`).
**Goal:** Find code that is over-engineered or could be simplified **without** losing efficiency, robustness, speed, or increasing vulnerability.

## Headline

Sabnyx is a lean, disciplined codebase. The large majority of what could look like "extra" code is load-bearing and covered by tests — retry/repair logic, the provider seam, the dotenv import-time handling, the HTTP-server hardening, and the Rust JSON-hash compatibility serializer are all justified. The genuine over-engineering surface is small and concentrated in a few duplications and two pieces of dead code.

Findings are graded **HIGH** (clear win, low risk), **MEDIUM** (real dedup, slightly more care), **LOW** (nitpick).

---

## HIGH

### H1 — Dead `--once` flag in `opsctl/src/main.rs`
`--once` is parsed, threaded through the `Supervise` arm, then explicitly discarded (`let _ = once;`). Single-shot is already the default when `--every` is absent, so the flag does nothing. The adjacent runtime `command.is_empty()` guard is also dead: the arg is declared `#[arg(last = true, required = true)]`, so clap rejects an empty command before `main` runs.
- **Fix:** remove the `--once` flag and its discard line; remove the dead `is_empty()` guard.
- **Risk:** none — behavior identical; clap still enforces a non-empty command.

### H2 — Dead "discoverability" code in `commands/lint.py`
`INTAKE`/`ATLAS` are imported but unused, kept alive only by `_ = (INTAKE, ATLAS)  # keep paths imported for discoverability`. Dead code dressed as intent.
- **Fix:** delete the `_ = (...)` line and the unused imports.
- **Risk:** none — removing unused imports cannot change behavior.

### H3 — `_age_days` implemented twice
`commands/triage.py` and `commands/health.py` both convert a timestamp to age-in-days. The `health` version is a strict superset (handles `None`/`str`/`datetime`/bare `date`, with the correct "test `datetime` before `date`" ordering); `triage`'s is the weaker copy.
- **Fix:** promote the `health` version to `util.age_days(ts)`; call from both. `triage` never passes `None`, so adopting the `| None` return is safe.
- **Risk:** none material — identical date math, shared version strictly more capable.

### H4 — Broken-wikilink scan implemented three times
The "load every vault `.md` → extract wikilinks → compare against a slug set" loop appears in `health.py`, in `tend.py` (`_health_summary`, which even re-imports `extract_wikilinks` inside the loop), and in `entities.py`. `tend._health_summary` is essentially a hand-rolled re-derivation of `health.py`'s counts.
- **Fix:** expose `health.compute_summary()` returning the counts; have `tend._health_summary` call it and just render the table. Keep `entities`' `slugify`-canonicalized variant separate — it is deliberately different (DEC-02).
- **Risk:** low — `tend` and `health` already use the same raw-lowercase matching, so consolidating changes nothing.

---

## MEDIUM

### M1 — Shared `reweave_one(...)` helper
`tend._reweave_pending` re-implements ~90% of `reweave_cmd`'s apply sequence (LLM call → confirm → `_apply_links` → write observations → ledger) and re-imports `llm`/`writers`/`ReweaveChangeset` inside the per-target loop. The only real differences are `actor` (`sabnyx-tend` vs `sabnyx-reweave`) and a fixed `standard` role.
- **Fix:** factor `reweave_one(tgt, index, *, role, yes, dry_run, actor, console)` in `reweave.py`; both call sites use it. Hoist the in-loop imports to module top.
- **Risk:** low — both paths already emit the same `reweave.completed` event shape; preserve tend's role/actor via params.

### M2 — Consolidate slug-index builders
`health._slug_index` and `entities._existing_slugs` scan the identical set (excluding observations); one returns the dict, the other its keys.
- **Fix:** one `context.vault_slug_index(*, lower=True, exclude_observations=True)`; `entities._existing_slugs` becomes `set(vault_slug_index())`. Leave `tend._atlas_slugs` (ATLAS-only) alone.
- **Risk:** none — pure consolidation of identical traversals.

### M3 — Single-pass pending-reweave projection in `opsctl/src/project.rs`
`reweave_proposed_idx` and `reweave_completed_idx` track last-seen index per subject, then a post-loop filter compares them. Because events replay in file order, this collapses to a single `HashMap<String, bool>` (pending true/false), last-write-wins.
- **Fix:** single map, set `true` on `reweave.proposed`, `false` on `reweave.completed`, filter `true` after the loop.
- **Risk:** low — equivalent under in-order replay; covered by projection's deterministic tests.

### M4 — `serve.py classify` re-splits the message
The `ask`/`ingest_text` branches call `t.split(None, 1)` two or three times and re-check its length to peel off the argument.
- **Fix:** split once with `head, _, rest = t.partition(" ")`; use `rest.strip()` as the arg.
- **Risk:** none — `t` is already stripped; routing only, not a security path.

### M5 — `paths.effective_data_dir` duplicates `resolve_data_root`
`effective_data_dir` re-implements its own dotenv-file walk and its own `expanduser().resolve()`, duplicating both `resolve_data_root` and the early-return then calls `resolve_data_root` anyway — two resolution paths for one value.
- **Fix:** route both branches through `resolve_data_root` so expansion/resolution lives in one place.
- **Risk:** negligible — behavior identical (real env excluded before the dotenv loop, so no shadowing); covered by `test_data_root.py`.

### M6 — Wrong provider in `llm.openai_base_url` error
The "base URL required" error hardcodes `provider=openai-compatible` even when the provider is `moonshot`/`openrouter`.
- **Fix:** interpolate the actual provider: `f"SABNYX_LLM_BASE_URL is required for provider={prov!r}"`.
- **Risk:** none — diagnostic-quality only.

---

## LOW (candidates)

- **L1 — `lint.py` `--staged` vs `files` predicate inconsistency.** The two input paths apply the "is this a lintable `.md`?" filter two different ways; the `staged` branch additionally requires `VAULT in p.parents` while the `files` branch does not — a subtle divergence. Unify behind one `_lintable(path)` predicate.
- **L2 — `writers.py` write→validate→rollback helper.** `write_intake` and `write_observation` share an identical write/reload/unlink-on-error skeleton; factor the mechanical part into `_write_validated(...)`, keep per-type meta/ledger in callers.
- **L3 — `retrieval.py` haystack dedup.** `_doc_terms` and `_page_text` build the identical "slug + description + tags + body[:cap]" string; extract `_page_haystack(path, cap)`.
- **L4 — `# type: ignore` cleanup.** Several `model_for(role)` call sites carry `# type: ignore[arg-type]`; a shared `Role = Literal[...]` would drop them.
- **L5 — `tend.py` `DATA_ROOT` relative-path.** Use `tgt.relative_to(paths.DATA_ROOT)` (as elsewhere) instead of `ATLAS.parent.parent` + explanatory comment (folds away if M1 is done).

---

## Explicitly DO NOT "simplify" (verified load-bearing — flagged so a future pass doesn't strip them)

- **`adapters/r1.check_bearer`** — the non-short-circuiting compare loop must stay; collapsing to `any(...)` reintroduces a **timing side-channel** leaking which key matched.
- **`serve_http.py`** — `_reject_te` (request-smuggling), Content-Length validation (guards `rfile.read(-1)`), `_MAX_BODY` cap (DoS), generic-500 body with ledgered detail (prevents leaking exception text), single-threaded server (vault writes aren't concurrency-safe). All security/robustness-load-bearing.
- **`opsctl/src/ledger.rs write_json_string`** — hand-rolled JSON escaper required for byte-for-byte parity with Python's `json.dumps(ensure_ascii=True, sort_keys=True)`; `serde_json` would break the cross-language `content_hash` (guarded by compat tests).
- **`creations/js/api.js`** — the layered `localStorage` + secure-bridge key storage (localStorage-first ordering) fixes a documented R1 key-loss bug.
- **`commands/new.py`** — write-then-reload validation is cheap defense-in-depth against persisting an invalid intake file.
- **`ledger.now()` wrapper, `_dotenv.py` minimal parser, `retrieval` Retriever Protocol + VectorRetriever** — all deliberate seams, used and tested; not speculative scaffolding.
