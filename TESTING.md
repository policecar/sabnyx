# Testing & verification playbook — sabnyx + halbor

How to exercise both projects, from fast/free to live. Two layers of
confidence: the **automated suites** (no keys, safe anytime — this is the gate)
and the **live smoke tests** (real LLM calls, small cost, need a provider key).

Assumes the two repos are siblings:

```
_px/
  sabnyx/   ← you are here
  halbor/   ← the read-only face; reads sabnyx's vault on disk
```

---

## Layer 1 — Automated suites (free, no keys, the gate)

```sh
# sabnyx — Python
cd ../sabnyx
uv run pytest -q                                  # expect: 151 passed
uv run ruff check . && uv run ruff format --check .

# sabnyx — opsctl (Rust control plane) — NOT covered by pytest
cd opsctl && cargo test                           # ledger.rs + supervise.rs

# halbor — Python
cd ../halbor
uv run pytest -q                                  # expect: 41 passed
uv run ruff check . && uv run ruff format --check .
```

Green here means the code is sound; no network or keys involved.

---

## Layer 2 — Live smoke tests (real LLM calls = small cost)

### Provider prerequisites

- **sabnyx** — the gitignored `moonshot` file at the repo root holds
  `MOONSHOT_API_KEY`. The provider **defaults to `anthropic`**, so to exercise
  Kimi you must set the provider explicitly:

  ```sh
  export SABNYX_LLM_PROVIDER=moonshot     # key auto-loads from ./moonshot
  ```

  (Or set `ANTHROPIC_API_KEY` and leave the default.)

- **halbor** — no key file by default. Copy sabnyx's; halbor then
  **auto-selects** Kimi (it picks `moonshot` when a Moonshot key is present and
  no Anthropic key):

  ```sh
  cp ../sabnyx/moonshot ../halbor/moonshot
  ```

### sabnyx — cheapest → read-only → dry mutations

```sh
cd ../sabnyx
uv run sabnyx verify           # cheapest live: one structured call per role (connectivity)
uv run sabnyx ask "..."        # read-only: answers from the vault, cites [[links]]
uv run sabnyx lint             # no LLM: frontmatter + containment gates
uv run sabnyx health           # no LLM: orphans, broken links, currency, cost
uv run sabnyx tend --dry-run   # the heartbeat — previews everything, writes NOTHING
```

> ⚠️ **Mutating** (run only when you mean it — these write to `vault/` and
> append to `ops/events.jsonl`): `sabnyx ingest`, `sabnyx promote`,
> `sabnyx reweave`, `sabnyx scan`, `sabnyx tend --yes`.

### halbor — project first (`ask` reads the projected face, not the vault)

```sh
cd ../halbor
uv run halbor project --dry-run                                  # decision set only, no writes
uv run halbor project --vault ../sabnyx/vault --out build/site   # writes build/site/** + llms.txt
uv run halbor ask "what surface should agents get?"              # grounded answer over build/site, cites [[slug]]
```

---

## Layer 3 — Vector retrieval (optional opt-in; default is `grep`)

```sh
# sabnyx
cd ../sabnyx && uv sync --extra vector
SABNYX_RETRIEVER=vector uv run sabnyx ask "..."

# halbor
cd ../halbor && uv sync --extra vector
HALBOR_RETRIEVER=vector uv run halbor ask "..."
```

First run downloads the static model (`minishlab/potion-retrieval-32M`, ~30 MB,
torch-free, CPU). Override with `SABNYX_EMBED_MODEL` / `HALBOR_EMBED_MODEL`.

---

## Suggested order

1. **Layer 1** — prove green, free.
2. Copy the `moonshot` key into halbor.
3. **Layer 2** — start with `sabnyx verify` (cheapest live call), then the
   read-only/dry commands, then halbor `project` → `ask`.
4. **Layer 3** — only if you want to compare retrieval quality.

## Notes

- **Reasoning-model budget.** Kimi `kimi-k2.5`/`k2.6` are reasoning models —
  their thinking tokens are billed against `max_tokens`. Too small a budget is
  exhausted mid-reasoning and returns empty content. `verify` was fixed to use a
  budget that fits this (every other command already uses the 16k default). If
  you see *"model returned empty output … hit max_tokens=N"*, raise the budget.
- Every command that mutates the vault appends one event to `ops/events.jsonl`.
- See `README.md` for the full command list and provider matrix.
