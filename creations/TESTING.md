# Testing the R1 ingress with a real Rabbit R1

End-to-end test of `sabnyx serve-http --static creations` + the Creation, staged
so each step de-risks the next. Companion to this directory's `README.md`
(contract + setup) and the repo's `DEPLOYMENT.md` (production systemd/nginx).

## Heads-up

- The R1 needs a **public HTTPS** endpoint — it `fetch()`es *in*. The tunnel path
  (Stage 2A) opens **no ports**; the nginx path opens 443.
- `--static` serves the Creation **and** the API from one origin, so CORS can't
  arise and `BASE_URL` in `js/api.js` stays `""` (no edit).

## ⚠️ Credentials must be in the *server process'* environment

The single most common trip-up:

- `serve-http` is a long-running process. **Env vars you export *after* it starts
  do not reach it** — restart it inside a shell that already has them.
- It needs, at launch: the LLM provider key (`MOONSHOT_API_KEY` /
  `SABNYX_LLM_API_KEY`) for `/ingest` + `/ask`, and `SABNYX_R1_KEYS` for auth.
  (`/status` and static serving need neither — which is why they can pass while
  `/ingest` fails with `no API key for provider …`.)
- **A git worktree has no `.env` of its own** (`ROOT` resolves to the worktree
  dir), so nothing auto-loads there. Source your repo's `.env` before launching.
- On the server this is a non-issue: the `sabnyx-serve-http.service`
  `EnvironmentFile=%h/sabnyx/.env` injects creds at start — *provided the key
  actually lives in that `.env`* (not in the legacy loose `moonshot` file, which
  the loader no longer reads).

---

## Stage 1 — prove the server with `curl` (no device)

From the worktree, with creds loaded into the launching shell:

```bash
cd <repo>/.worktrees/r1
uv sync
set -a
. <repo>/.env            # provider, SABNYX_DATA_DIR, …
. <repo>/moonshot        # MOONSHOT_API_KEY, until you migrate it into .env (see Cleanup)
set +a
export SABNYX_DATA_DIR="$(mktemp -d)/vault"    # throwaway vault for the test
export SABNYX_R1_KEYS="r1-test-key-123"        # fixed, so the curl shell can match
uv run sabnyx serve-http --static creations --port 8787
# sanity: `printenv MOONSHOT_API_KEY | head -c 8` should be non-empty before launch
```

In a second shell (same key):

```bash
K=r1-test-key-123
curl -s localhost:8787/            | head -c 40   # Creation HTML  (static works)
curl -s localhost:8787/status                      # 401            (auth gates)
curl -s localhost:8787/status -H "Authorization: Bearer $K"   # {"intake":..,"atlas":..}
curl -s -X POST localhost:8787/ingest -H "Authorization: Bearer $K" \
     -H 'Content-Type: application/json' -d '{"text":"oat milk and a dentist appointment"}'
#   -> {"text":"…","filed":["vault/intake/…md"]}      (real LLM + vault write)
curl -s -X POST localhost:8787/ask -H "Authorization: Bearer $K" \
     -H 'Content-Type: application/json' -d '{"text":"what should I buy?"}'
```

You can also open `http://localhost:8787/` in a desktop browser and use the typed
box — same path, no device. **Don't proceed until `/ingest` and `/ask` return real
results.**

> Note: `_ingest` writes the source file *before* calling the LLM. So if a call
> fails at the LLM step, the capture is already on disk; a retry with the same
> text returns `already captured`. Use fresh text (or a fresh `SABNYX_DATA_DIR`,
> which means restarting the server) when re-testing.

---

## Stage 2 — expose over HTTPS

### Path A — quickest, no open ports (Cloudflare Tunnel)

```bash
cloudflared tunnel --url http://localhost:8787    # prints https://<random>.trycloudflare.com
```

One origin serves both static + API; `BASE_URL=""` already points there.

### Path B — production on the server

See `DEPLOYMENT.md` → **"Rabbit R1 ingress (optional)"**: the
`sabnyx-serve-http.service` unit (`serve-http --static creations`), an nginx TLS
terminator with `limit_req`, and `SABNYX_R1_KEYS` in `~/sabnyx/.env`.

---

## Stage 3 — install on the R1 and test voice

1. Open `https://<your-origin>/install.html` → scan the QR with the R1.
2. First run prompts for a key → paste one of your `SABNYX_R1_KEYS` (stored in
   `creationStorage.secure`).
3. Header shows `CAPTURE` + a live `intake N · atlas N` — if that loads, `/status`
   round-trips from the device. ✅
4. **CAPTURE:** hold PTT, say *"remember to buy oat milk"*, release → transcript
   shows, the R1 **speaks** the summary, the count ticks up.
5. **ASK:** single-click the side button → header flips to `ASK`. Hold PTT, ask a
   question → spoken, vault-grounded answer.
6. Voice stuck? The on-screen text box does the same without the mic — isolates
   "network vs. mic".

---

## Stage 4 — verify server-side

```bash
ls "$SABNYX_DATA_DIR"/vault/intake/                                  # new captures
grep -o '"kind":"[^"]*"' "$SABNYX_DATA_DIR"/ops/events.jsonl | sort | uniq -c
#   expect wire.received, source.captured, intake.created, ask.answered
# (Path B) journalctl --user -u sabnyx-serve-http -f
```

---

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `/ingest`/`/ask`: `no API key for provider …` | LLM key not in the **server** process — restart it after sourcing creds (env changes don't reach a running server) |
| `/ingest` says `already captured` | server still on an earlier vault; the prior attempt wrote the source before failing — use fresh text or restart with a fresh `SABNYX_DATA_DIR` |
| Device status shows "offline?" | tunnel/nginx not pointing at `:8787`, or a non-empty `BASE_URL` was set |
| 401 everywhere | the key isn't in `SABNYX_R1_KEYS`, or the curl `$K` ≠ the server's key |
| PTT does nothing, typing works | `CreationVoiceHandler` absent/renamed on your firmware — the one unverified API |
| `GET /` is 404 | launched without `--static`, or the static path is wrong relative to cwd |

Smartest order: **Stage 1 curl → Stage 2A tunnel → desktop-browser the tunnel URL
→ then the device.**

---

## Cleanup (do once, unrelated to R1)

Move `MOONSHOT_API_KEY` into `~/sabnyx/.env`, delete the loose `moonshot` file,
and **rotate the key** (it has sat in plaintext). After that, `uv run sabnyx …`
from `~/sabnyx` loads it automatically — no manual sourcing.
