# Deploying sabnyx (always-on server + Telegram)

How to run sabnyx as a 24/7 service that ingests + answers over Telegram and tidies
the vault on a timer. Companion to `ARCHITECTURE.md` (what the system is) and
`README.md` (the CLI). Secrets never live in this repo — only in `~/sabnyx/.env`
on the server (chmod 600).

---

## Architecture: two repos + a bare hub

The **code** and the **personal vault/ledger** live in separate git repos so the
always-on `tend` loop (which commits vault changes every 15 min) never pollutes
code history.

```
github.com/policecar/sabnyx            ← CODE (this repo)
server:~/git/sabnyx-vault.git          ← DATA hub (bare repo, source of truth)
  ├─ server:~/sabnyx-vault             ← DATA working clone (serve + tend run here)
  ├─ laptop:~/src/sabnyx-vault     ← DATA working clone (Obsidian + local CLI)
server:~/sabnyx                        ← CODE clone (the running app)
laptop:~/src/sabnyx                ← CODE clone (development)
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
cd ~/src/sabnyx-vault
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
curl -s "https://api.telegram.org/bot$SABNYX_TELEGRAM_TOKEN/getMe"   # → "username":"your-bot"
uv run sabnyx verify          # LLM door answers
uv run sabnyx health          # vault resolves under ~/sabnyx-vault
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

`~/sabnyx-sync-tend.sh` (the heartbeat: pull → tend → commit → push):
```bash
cat > ~/sabnyx-sync-tend.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
cd "$HOME/sabnyx-vault" && git pull --rebase --autostash origin main
cd "$HOME/sabnyx"       && uv run sabnyx tend --yes
cd "$HOME/sabnyx-vault" && git add -A
git diff --cached --quiet || { git commit -m "tend $(date -u +%FT%TZ)"; git push origin main; }
EOF
chmod 700 ~/sabnyx-sync-tend.sh
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
# hardening (systemd 241-safe; protects /usr,/etc while leaving $HOME writable for uv)
NoNewPrivileges=yes
ProtectSystem=full
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictRealtime=yes
RestrictNamespaces=yes
LockPersonality=yes
PrivateTmp=yes
PrivateDevices=yes
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
ExecStart=%h/sabnyx-sync-tend.sh
# hardening (same set as serve)
NoNewPrivileges=yes
ProtectSystem=full
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictRealtime=yes
RestrictNamespaces=yes
LockPersonality=yes
PrivateTmp=yes
PrivateDevices=yes
MemoryMax=2G
TasksMax=256
```

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

---

## Daily operation

- **Capture / ask** from the phone via `@your-bot` (trusted senders only).
- **`tend`** runs every 15 min: pulls the hub, promotes/triages/reweaves, commits +
  pushes any vault changes back to the hub.
- **Laptop sync**: `git -C ~/src/sabnyx-vault pull` to get the server's changes;
  Obsidian edits + `git push` flow back through the hub. Union-merge keeps the
  ledger conflict-free.

## Updating the code on the server
```bash
cd ~/sabnyx && git pull && uv sync
systemctl --user restart sabnyx-serve.service
```

## Logs / troubleshooting
```bash
journalctl --user -u sabnyx-serve.service -f         # live bot logs
journalctl --user -u sabnyx-tend.service --since today
systemctl --user list-timers | grep sabnyx           # next tend run
```

## `.env` reference

| Var | Purpose | Secret |
|-----|---------|--------|
| `SABNYX_DATA_DIR` | path to the DATA working clone (`~/sabnyx-vault`) | no |
| `SABNYX_LLM_PROVIDER` | `moonshot` (Kimi) \| `anthropic` \| … | no |
| `MOONSHOT_API_KEY` / `ANTHROPIC_API_KEY` | LLM provider key | **yes** |
| `SABNYX_TELEGRAM_TOKEN` | bot token from BotFather | **yes** |
| `SABNYX_TELEGRAM_ALLOW` | comma-separated trusted numeric user IDs | no |
| `SABNYX_RETRIEVER` | `grep` (default) \| `vector` | no |

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
- **EOL OS**: no security updates. Acceptable here because the
  attack surface is outbound-only (Telegram long-poll, git, LLM API). Reprovision
  to a supported release when convenient.
- **Backup**: the DATA hub (`~/git/sabnyx-vault.git`) lives on the same server — a
  single point of failure. Your laptop clone is a full-history backup, but only as
  fresh as your last `git -C ~/src/sabnyx-vault pull`. Pull regularly (or add a
  private off-site mirror remote) so a server loss doesn't take the vault with it.
- **Keep `git pull --rebase --autostash`** in the tend loop — NOT `--ff-only`. The
  sync is bidirectional (laptop edits + server tend both commit to the hub), so the
  branches legitimately diverge; `--ff-only` would wedge the loop. The ledger's
  union-merge driver resolves the only file that concurrently appends.
- **systemd hardening** is trimmed to what systemd 241 supports. Newer directives
  (`ProtectHostname` 242, `RestrictSUIDSGID` 242, `ProtectKernelLogs` 244,
  `ProtectClock` 245) are omitted — they'd be silently ignored on older systemd. Stricter
  opt-in (test that `uv run` still works): add `ProtectSystem=strict` +
  `ProtectHome=read-only` + `ReadWritePaths=%h/sabnyx %h/sabnyx-vault %h/git
  %h/.cache/uv %h/.local/share/uv`. (`MemoryMax` may be a no-op under an older release's
  cgroup v1 — harmless.)
