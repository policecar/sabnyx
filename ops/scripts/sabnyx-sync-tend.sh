#!/usr/bin/env bash
# sabnyx heartbeat: pull the vault, run tend, commit + push any vault changes.
# Invoked every 15 min by the systemd user unit sabnyx-tend.service.
# Commit rule: content change (beyond the ledger) OR the last commit is >24h old.
# Edit here, not on the server — the server runs this file via `git pull`.
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"

SABNYX_CODE_DIR="${SABNYX_CODE_DIR:-$HOME/sabnyx}"
SABNYX_VAULT_DIR="${SABNYX_VAULT_DIR:-$HOME/sabnyx-vault}"

cd "$SABNYX_VAULT_DIR" && git pull --rebase --autostash origin main
cd "$SABNYX_CODE_DIR"  && uv run sabnyx tend --yes
cd "$SABNYX_VAULT_DIR" && git add -A

# tend.completed is ledgered every run (opsctl's liveness signal) — a
# ledger-only diff isn't "nothing changed", but committing it every 15 min
# is pure churn. Commit only if something else changed too, or the last
# commit is >24h old (bounds hub staleness); else leave it staged for next run.
if git diff --cached --quiet -- . ':(exclude)ops/events.jsonl'; then
  last_commit_ts=$(git log -1 --format=%ct 2>/dev/null || echo 0)
  now_ts=$(date -u +%s)
  if [ $(( now_ts - last_commit_ts )) -le 86400 ]; then
    exit 0
  fi
fi
git diff --cached --quiet || { git commit -m "tend $(date -u +%FT%TZ)"; git push origin main; }
