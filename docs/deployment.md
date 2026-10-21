# Deploying sabnyx (always-on server + Telegram)

How to run sabnyx as a 24/7 service that ingests + answers over Telegram and tidies
the vault on a timer. Companion to `architecture.md` (what the system is) and
`README.md` (the CLI). Secrets never live in this repo — only in `~/sabnyx/.env`
on the server (chmod 600).

---

## Architecture: two repos + a bare hub

The **code** and the **personal vault/ledger** live in separate git repos so the
always-on `tend` loop (which runs every 15 min and commits when content
changed, else at most once a day) never pollutes code history.

```
github.com/policecar/sabnyx            ← CODE (this repo)
server:~/git/sabnyx-vault.git          ← DATA hub (bare repo, source of truth)
  ├─ server:~/sabnyx-vault             ← DATA working clone (serve + tend run here)
  ├─ laptop:~/src/_px/sabnyx-vault     ← DATA working clone (Obsidian + local CLI)
server:~/sabnyx                        ← CODE clone (the running app)
laptop:~/src/_px/sabnyx                ← CODE clone (development)
```

- **DATA repo** (`sabnyx-vault`) contains only `vault/**` + `ops/events.jsonl` +
  `ops/knowledge-log.md`. `ops/scripts/` is code and stays in the code repo.
- `SABNYX_DATA_DIR` points the runtime at the DATA working clone; without it,
  `vault/` and the ledger resolve under the code repo (single-repo dev mode).
- `ops/events.jsonl` has a **union** merge driver (`.gitattributes`) so concurrent
  ledger appends from laptop and server auto-merge instead of conflicting.

---

## Server prerequisites

- Linux with `git`, `systemd`, and [`uv`](https://docs.astral.sh/uv/) on PATH.
  No system Python needed — `uv` brings its own (3.12+).
- Network egress to `api.telegram.org` and your LLM provider
  (`api.moonshot.ai` for Kimi, or `api.anthropic.com`).
- A non-root user (examples use `p`, home `/home/p`); `sudo` only for one
  `loginctl enable-linger` call.
- Works on old distros (git ≥ 2.20, systemd ≥ 241) — see **Gotchas**.

Telegram long-poll is **outbound only**, so no inbound ports are opened; SSH stays
the only inbound surface.

---

## One-time bring-up

### 1. DATA hub (bare repo) + GitHub deploy key — on the server
```bash
mkdir -p ~/git
git init --bare ~/git/sabnyx-vault.git
git -C ~/git/sabnyx-vault.git symbolic-ref HEAD refs/heads/main   # git <2.28 has no --initial-branch

# read-only deploy key so the server can pull the private CODE repo
ssh-keygen -t ed25519 -f ~/.ssh/sabnyx_deploy -N "" -C "sabnyx-server"
printf 'Host github.com\n  IdentityFile ~/.ssh/sabnyx_deploy\n  IdentitiesOnly yes\n' >> ~/.ssh/config
cat ~/.ssh/sabnyx_deploy.pub      # → add to GitHub repo → Settings → Deploy keys (READ-ONLY)
ssh -T git@github.com             # expect: Hi policecar/sabnyx! ...authenticated
```

### 2. Publish the DATA repo to the hub — from the laptop
```bash
cd ~/src/_px/sabnyx-vault
git remote add origin ssh://USER@SERVER/home/USER/git/sabnyx-vault.git
git push -u origin main
```

### 3. Clone both repos — on the server
```bash
git clone ~/git/sabnyx-vault.git ~/sabnyx-vault           # DATA (local, no auth)
git clone git@github.com:policecar/sabnyx.git ~/sabnyx    # CODE (deploy key)
git -C ~/sabnyx-vault config user.name  "sabnyx-server"   # so tend can commit
git -C ~/sabnyx-vault config user.email "sabnyx@$(hostname)"
```

### 4. Install + configure — on the server
```bash
cd ~/sabnyx
uv sync                       # add --extra vector for the semantic retriever

# ~/sabnyx/.env — the single config file (sabnyx reads ROOT/.env; chmod 600).
# Use CLEAN KEY=VALUE lines (no inline # comments — systemd & the loader keep them literally).
cat >> ~/sabnyx/.env <<'EOF'
SABNYX_DATA_DIR=/home/USER/sabnyx-vault
SABNYX_LLM_PROVIDER=moonshot
SABNYX_TELEGRAM_ALLOW=YOUR_NUMERIC_TELEGRAM_ID
EOF
# add secrets without leaking them into shell history:
read -rsp 'MOONSHOT_API_KEY: ' K && printf 'MOONSHOT_API_KEY=%s\n' "$K" >> ~/sabnyx/.env && unset K; echo
read -rsp 'SABNYX_TELEGRAM_TOKEN: ' K && printf 'SABNYX_TELEGRAM_TOKEN=%s\n' "$K" >> ~/sabnyx/.env && unset K; echo
chmod 600 ~/sabnyx/.env

# verify the wiring
set -a; . ~/sabnyx/.env; set +a
curl -s "https://api.telegram.org/bot$SABNYX_TELEGRAM_TOKEN/getMe"   # → "username":"sabnyxbot"
uv run sabnyx verify          # LLM door answers
uv run sabnyx health          # vault resolves under ~/sabnyx-vault

# install the vault's pre-commit lint hook (gate: a crash-truncated atlas
# file must not be committable by `git add -A`). Reads SABNYX_DATA_DIR from
# ~/sabnyx/.env (just populated above) to find the DATA repo to hook into.
bash ~/sabnyx/ops/scripts/install-hooks.sh
```

### 5. Smoke-test the bot before daemonizing
Send `/status` to the bot from your phone, then:
```bash
cd ~/sabnyx && set -a; . ~/sabnyx/.env; set +a
uv run sabnyx serve --once    # processes one batch and exits; you should get a reply
```

### 6. Always-on via systemd (user units + linger)
```bash
sudo loginctl enable-linger "$USER"     # services survive logout + reboot
mkdir -p ~/.config/systemd/user
```

The heartbeat (pull → tend → commit → push) is tracked at
`ops/scripts/sabnyx-sync-tend.sh` in this repo — it is **not** pasted onto the
server. The unit below runs it straight from the CODE clone: edit it in the
repo and `git pull` on the server — never hand-edit it on the server. git
tracks only the executable bit (644/755) on that file; if the server's
deployment policy wants a tighter mode (e.g. 700), set it explicitly after
checkout — the repo itself doesn't (and can't) store that.

Syntax-check it before wiring it into systemd, so a typo doesn't run half a
script once the timer is live:
```bash
bash -n ~/sabnyx/ops/scripts/sabnyx-sync-tend.sh
```

`~/.config/systemd/user/sabnyx-serve.service`:
```ini
[Unit]
Description=sabnyx Telegram serve (long-poll)

[Service]
Type=simple
WorkingDirectory=%h/sabnyx
EnvironmentFile=%h/sabnyx/.env
ExecStart=%h/.local/bin/uv run sabnyx serve
Restart=always
RestartSec=5
# hardening — namespace-free set (works even where unprivileged user namespaces
# are disabled, the Debian default). Seccomp/cgroup based; needs no mount ns.
NoNewPrivileges=yes
RestrictRealtime=yes
RestrictNamespaces=yes
LockPersonality=yes
MemoryMax=2G
TasksMax=256

[Install]
WantedBy=default.target
```

`~/.config/systemd/user/sabnyx-tend.service`:
```ini
[Unit]
Description=sabnyx tend (sync + heartbeat)

[Service]
Type=oneshot
EnvironmentFile=%h/sabnyx/.env
ExecStart=%h/sabnyx/ops/scripts/sabnyx-sync-tend.sh
# hardening (same namespace-free set as serve)
NoNewPrivileges=yes
RestrictRealtime=yes
RestrictNamespaces=yes
LockPersonality=yes
MemoryMax=2G
TasksMax=256
```

**Migrating an existing install** (the script used to be pasted at
`~/sabnyx-sync-tend.sh`; now it's tracked in the repo): edit `ExecStart` in
`~/.config/systemd/user/sabnyx-tend.service` to the path above, then:
```bash
systemctl --user daemon-reload
rm ~/sabnyx-sync-tend.sh
```
Don't just trust that you edited it right — the "Is it alive?" checklist
below has a one-line command to *detect* whether `ExecStart` still points
at the old pasted path or the tracked one.

`~/.config/systemd/user/sabnyx-tend.timer`:
```ini
[Unit]
Description=run sabnyx tend every 15 min

[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
Persistent=true

[Install]
WantedBy=timers.target
```

Enable:
```bash
systemctl --user daemon-reload
systemctl --user enable --now sabnyx-serve.service sabnyx-tend.timer
systemctl --user status sabnyx-serve.service --no-pager
systemctl --user list-timers --no-pager | grep sabnyx
```

Manual smoke run (once the unit is installed): trigger it through systemd
rather than invoking the script directly — `start` runs the oneshot unit
exactly once and systemd serialises it against the timer, so this run
cannot race a timer-fired one.
```bash
systemctl --user start sabnyx-tend.service
systemctl --user status sabnyx-tend.service --no-pager | tail -20
```
Running the script directly (`~/sabnyx/ops/scripts/sabnyx-sync-tend.sh`) is
fine too — e.g. on a box where the timer is disabled — since the script's
own lock (`.sync-tend.lock` in the DATA clone) protects against overlap
either way.

---

## Rabbit R1 ingress (optional)

Drives the vault by voice from a Rabbit R1 Creation over HTTP — additive to the
Telegram setup above, sharing the same `.env` and vault. The device/client side
(the Creation, install QR, voice flow) is in `creations/README.md`. Three
differences from the Telegram service:

- **It needs an *inbound* TLS endpoint.** Telegram is outbound-only; the R1
  `fetch()`es *in*, so nginx terminates TLS on 443 and proxies to
  `127.0.0.1:8787`. Auth is a bearer-key allowlist (`SABNYX_R1_KEYS`); the server
  refuses to run open.
- **One process serves the Creation *and* the API** via `--static creations`, so
  the page and API share an origin (no CORS) and one unit covers both.
- **Low-latency LLM (Anthropic/Haiku).** Voice can't wait, and Moonshot
  round-trips measured ~35s. The unit pins *this service* to
  `SABNYX_LLM_PROVIDER=anthropic` (the `fast` role → `claude-haiku-4-5`, or
  whatever `SABNYX_MODEL_FAST` points at); Telegram/tend keep their `.env`
  provider. Requires `ANTHROPIC_API_KEY` in `.env`.

### 1. Keys — on the server
```bash
printf 'SABNYX_R1_KEYS=%s\n' "$(openssl rand -hex 24)" >> ~/sabnyx/.env   # one per device
# Anthropic key for the low-latency voice path (see the LLM note above):
read -rsp 'ANTHROPIC_API_KEY: ' K && printf 'ANTHROPIC_API_KEY=%s\n' "$K" >> ~/sabnyx/.env && unset K; echo
```

### 2. `~/.config/systemd/user/sabnyx-serve-http.service`
```ini
[Unit]
Description=sabnyx Rabbit R1 HTTP ingress

[Service]
Type=simple
WorkingDirectory=%h/sabnyx
EnvironmentFile=%h/sabnyx/.env
# Haiku for low-latency voice — overrides the .env provider for THIS service only.
# NOTE: do NOT use `Environment=SABNYX_LLM_PROVIDER=anthropic` here — systemd's
# EnvironmentFile= overrides Environment= regardless of order, so the .env's
# `moonshot` would win and the voice path would silently run on Kimi (~35s →
# nginx 504s). Set it in ExecStart via env(1), which is applied to the actual
# exec'd process and is unambiguous.
ExecStart=/usr/bin/env SABNYX_LLM_PROVIDER=anthropic %h/.local/bin/uv run sabnyx serve-http --static creations --host 127.0.0.1 --port 8787
Restart=always
RestartSec=5
# same namespace-free hardening as serve
NoNewPrivileges=yes
RestrictRealtime=yes
RestrictNamespaces=yes
LockPersonality=yes
MemoryMax=2G
TasksMax=256

[Install]
WantedBy=default.target
```

### 3. nginx TLS terminator (opens 443)
```nginx
limit_req_zone $binary_remote_addr zone=r1:10m rate=10r/s;   # in the http{} block
server {
    listen 443 ssl;                       # + your certbot ssl_certificate lines
    server_name sabnyx.example.net;
    client_max_body_size 64k;             # match serve-http's _MAX_BODY; reject big bodies at the edge
    location / {
        limit_req zone=r1 burst=20 nodelay;   # blunt brute-force / floods
        proxy_pass http://127.0.0.1:8787;
        proxy_read_timeout 120s;              # headroom over the LLM round-trip (Haiku ~seconds);
        proxy_send_timeout 120s;              # without this, nginx's 60s default 504s a slow reply
    }
}
```

### 4. Enable + smoke
```bash
systemctl --user daemon-reload
systemctl --user enable --now sabnyx-serve-http.service
K=$(grep '^SABNYX_R1_KEYS=' ~/sabnyx/.env | cut -d= -f2 | cut -d, -f1)
curl -s https://sabnyx.example.net/status -H "Authorization: Bearer $K"   # {"intake":..,"atlas":..}
curl -s https://sabnyx.example.net/status                                  # 401 (no key)
```
Then open `https://sabnyx.example.net/install.html` and scan the QR on the R1.

---

## Daily operation

- **Capture / ask** from the phone via `@sabnyxbot` (trusted senders only).
- **`tend`** runs every 15 min: pulls the hub, promotes/triages/reweaves, commits +
  pushes any vault changes back to the hub. It ledgers a `tend.completed` event
  every run regardless (the liveness signal `opsctl project` reads) — the sync
  script commits that line only when something else also changed, or the last
  commit has gone stale (>24h), so the ledger doesn't turn into one commit per run.
- **Laptop sync**: `git -C ~/src/_px/sabnyx-vault pull` to get the server's changes;
  Obsidian edits + `git push` flow back through the hub. Union-merge keeps the
  ledger conflict-free.

## Updating the code on the server
```bash
cd ~/sabnyx && git pull && uv sync
systemctl --user restart sabnyx-serve.service
```
The timer invokes `ops/scripts/sabnyx-sync-tend.sh` straight from the CODE
clone, so a `git pull` alone picks up a fixed heartbeat script on its next
15-min run — no restart needed. Don't hand-edit `sabnyx-tend.service` or the
script on the server; it's a tracked file, so a hand-edit is just local drift
that `git pull` will silently overwrite (or conflict with) later.

## Logs / troubleshooting
```bash
journalctl --user -u sabnyx-serve.service -f         # live bot logs
journalctl --user -u sabnyx-tend.service --since today
systemctl --user list-timers | grep sabnyx           # next tend run
```
`journalctl --user` needs the invoking user in the `systemd-journal` group
(`sudo usermod -aG systemd-journal "$USER"`, then re-login); without it, use
`systemctl --user status <unit> --no-pager`, which works regardless.

### Is it alive? (after a restart)
Seven checks, cheapest first:
```bash
loginctl show-user "$USER" -p Linger                            # Linger=yes
systemctl --user list-timers --no-pager | grep sabnyx           # NEXT within 15 min
systemctl --user status sabnyx-serve.service --no-pager | head -3   # active (running)
systemctl --user cat sabnyx-tend.service | grep ExecStart       # expected below
git -C ~/sabnyx-vault log -1 --format='%cI %s'                  # recent tend commit or content commit
git -C ~/sabnyx-vault status --short
git -C ~/sabnyx-vault status -sb | head -1                      # expected below
```
- `Linger=no` means services stop at logout — rerun `sudo loginctl enable-linger "$USER"`.
- No `NEXT` line or one far in the past means the timer isn't enabled — `systemctl --user enable --now sabnyx-tend.timer`.
- Anything but `active (running)` for `sabnyx-serve.service` means the bot is down.
- `ExecStart=%h/sabnyx/ops/scripts/sabnyx-sync-tend.sh` is the tracked path — any other value (e.g. the old pasted `~/sabnyx-sync-tend.sh`) means the migration above didn't take; this detects stale `ExecStart` drift, not just the migration step itself.
- A `tend …` or content commit from more than ~20 min ago (one run + margin) means the loop has stalled.
- `M  ops/events.jsonl` in `git status --short` is the **normal idle state**, not a problem: the ledger line is staged but deliberately left uncommitted until real content changes or 24h pass — see the gating block in the script. Anything else staged or untracked there is worth investigating.
- `git status -sb | head -1` showing `## main...origin/main` (no suffix) is good; a `[ahead N]` suffix means there are commits the hub never received — the last push failed. Check `journalctl --user -u sabnyx-tend.service` for the "push failed after rebase retry" message and whether the repo is still mid-rebase (see Gotchas).

## `.env` reference

| Var | Purpose | Secret |
|-----|---------|--------|
| `SABNYX_DATA_DIR` | path to the DATA working clone (`~/sabnyx-vault`) | no |
| `SABNYX_LLM_PROVIDER` | `moonshot` (Kimi) \| `anthropic` \| … | no |
| `MOONSHOT_API_KEY` / `ANTHROPIC_API_KEY` | LLM provider key | **yes** |
| `SABNYX_TELEGRAM_TOKEN` | bot token from BotFather | **yes** |
| `SABNYX_TELEGRAM_ALLOW` | comma-separated trusted numeric user IDs | no |
| `SABNYX_R1_KEYS` | comma-separated bearer keys for the R1 ingress (`serve-http`) | **yes** |
| `SABNYX_RETRIEVER` | `grep` (default) \| `vector` | no |
| `SABNYX_TRUSTED_SOURCES` | `source_agent`s trusted for auto-promote (default `sabnyx-ingest`) | no |
| `SABNYX_MAX_LLM_CALLS` | per-run LLM call ceiling for `tend` (default `50`; `0` = unlimited) | no |
| `SABNYX_LLM_MAX_RETRIES` | retries on transient provider 429/5xx (default `3`) | no |

`sabnyx-sync-tend.sh` reads `SABNYX_DATA_DIR` too, with the same precedence
(real env var over `.env`) — relocating the vault via this variable moves
the heartbeat script's target along with everything else.

---

## Gotchas (learned the hard way)

- **Old git (<2.28)**: no `--initial-branch`; use
  `git -C <bare> symbolic-ref HEAD refs/heads/main`. git 2.20 is otherwise fine
  (`clone`, `pull --rebase --autostash`, the tend sync all work).
- **`SABNYX_DATA_DIR` must be a real env var OR in `~/sabnyx/.env`.** `paths.py`
  reads the dotenv file at import; a real env var (e.g. systemd `EnvironmentFile`)
  always wins. (Earlier versions froze `DATA_ROOT` before `.env` loaded — fixed.)
- **`.env` is the only config file.** The legacy `moonshot` file convention was
  removed; put `MOONSHOT_API_KEY` (exact name) in `~/sabnyx/.env`.
- **Set `git config user.name/email`** in the DATA clone on the server, or the tend
  commit fails with "Please tell me who you are."
- **No inline `# comments`** after a value in `.env` — both systemd and the loader
  keep the rest of the line literally.
- **EOL OS**: no security updates. Acceptable for the *Telegram*
  setup because its attack surface is outbound-only (long-poll, git, LLM API).
  Reprovision to a supported release when convenient.
- **The R1 ingress is the one *inbound* surface** — enabling `serve-http` opens
  nginx:443 → `127.0.0.1:8787`, unlike everything else here. The bearer
  allowlist + refuse-open + nginx `limit_req` are the guard: use
  `openssl rand`-grade keys, one per device, and rotate on leak. On an EOL OS,
  weigh this added exposure before enabling it.
- **Backup**: the DATA hub (`~/git/sabnyx-vault.git`) lives on the same server — a
  single point of failure. Your laptop clone is a full-history backup, but only as
  fresh as your last `git -C ~/src/_px/sabnyx-vault pull`. Pull regularly (or add a
  private off-site mirror remote) so a server loss doesn't take the vault with it.
- **Keep `git pull --rebase --autostash`** in the tend loop — NOT `--ff-only`. The
  sync is bidirectional (laptop edits + server tend both commit to the hub), so the
  branches legitimately diverge; `--ff-only` would wedge the loop. The ledger's
  union-merge driver resolves the only file that concurrently appends.
- **The script never leaves the clone mid-rebase.** Both the initial
  `git pull --rebase --autostash` and the push-retry's own `git pull --rebase`
  run `git rebase --abort` before exiting non-zero on failure (a real content
  conflict, not the ledger — that auto-merges). If you ever find
  `ls ~/sabnyx-vault/.git/rebase-merge` or `.../rebase-apply` existing, the
  script crashed before this guard ran (or was hand-interrupted); run
  `git -C ~/sabnyx-vault rebase --abort` yourself before the next tick.
- **systemd hardening is namespace-free by necessity.** Mount-namespace directives
  (`ProtectSystem`, `ProtectHome`, `PrivateTmp`, `PrivateDevices`, `ProtectKernel*`,
  `ProtectControlGroups`) require **unprivileged user namespaces**, which Debian
  disables by default (`kernel.unprivileged_userns_clone=0`). On a *user* service
  they make it die at sandbox setup (`status=226/NAMESPACE`). So we ship only the
  seccomp/cgroup directives, which need no namespaces. Fuller mount-ns set is opt-in
  ONLY if you `sudo sysctl -w kernel.unprivileged_userns_clone=1` (has its own
  privesc tradeoff) or run these as system units. (`MemoryMax` may also be a no-op
  under cgroup v1 — harmless.) Also note several newer directives
  (`ProtectHostname` 242, `RestrictSUIDSGID` 242, `ProtectClock` 245) don't exist on
  systemd 241 and would be ignored.
- **Don't paste multi-line heredocs into a shell.** An unterminated `<<'EOF'`
  (one missed keystroke) silently swallows every following command into the
  target file instead of erroring — cost ten minutes re-pasting
  `sabnyx-sync-tend.sh` once. Prefer a tracked script (this file now ships
  one) and, when you must paste a heredoc, run `bash -n` on the result before
  trusting it.
