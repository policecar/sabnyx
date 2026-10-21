#!/usr/bin/env bash
# sabnyx heartbeat: pull the vault, run tend, commit + push any vault changes.
# Invoked every 15 min by the systemd user unit sabnyx-tend.service.
# Commit rule: content change (beyond the ledger) OR the last commit is >24h old.
# Edit here, not on the server — the server runs this file via `git pull`.
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"

SABNYX_CODE_DIR="${SABNYX_CODE_DIR:-$HOME/sabnyx}"
SABNYX_CODE_DIR="${SABNYX_CODE_DIR/#\~/$HOME}"   # ~ expansion, same as paths.py (systemd EnvironmentFile does none)
# Same variable src/sabnyx/paths.py reads (SABNYX_DATA_DIR); the systemd unit's
# EnvironmentFile=%h/sabnyx/.env supplies it for timer runs. Unlike paths.py this
# script does NOT fall back to the code repo when it is unset: it assumes the
# split layout from docs/deployment.md and fails loudly otherwise (a cron job
# must never `git add -A && commit && push` the code checkout).
SABNYX_DATA_DIR="${SABNYX_DATA_DIR:-$HOME/sabnyx-vault}"
# paths.py applies expanduser(); mirror it so `~/...` in the env file works here too.
SABNYX_DATA_DIR="${SABNYX_DATA_DIR/#\~/$HOME}"

[ -d "$SABNYX_CODE_DIR" ] || { echo "sabnyx code dir not found: $SABNYX_CODE_DIR" >&2; exit 1; }
[ -d "$SABNYX_DATA_DIR" ] || { echo "sabnyx data dir not found: $SABNYX_DATA_DIR" >&2; exit 1; }

# Guard against an overlapping timer tick (or a manual run colliding with the
# timer): exit 0 on contention, not 1 — an overlap is expected, not an error.
LOCK="$SABNYX_DATA_DIR/.sync-tend.lock"
if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK"
  flock -n 9 || { echo "sabnyx-sync-tend: another run holds $LOCK; exiting" >&2; exit 0; }
else
  echo "sabnyx-sync-tend: flock(1) not available; running unlocked" >&2
fi

# Try a push; if the hub moved underneath us, rebase once and retry. A second
# failure means a real conflict or outage — surface it loudly so the systemd
# unit fails and is visible, rather than silently dropping the push.
# Abort an in-progress rebase and say whether the clone is actually clean
# afterwards — never report "aborted" on faith.
abort_rebase() {
  git rebase --abort 2>/dev/null || true
  if [ -d .git/rebase-merge ] || [ -d .git/rebase-apply ]; then
    echo "sabnyx-sync-tend: rebase --abort did not clean up; clone is STILL mid-rebase — fix by hand in $SABNYX_DATA_DIR" >&2
    return 1
  fi
}

push_with_retry() {
  if git push origin "$BRANCH"; then
    return 0
  fi
  echo "sabnyx-sync-tend: push rejected; pulling --rebase and retrying once" >&2
  if git pull --rebase origin "$BRANCH" && git push origin "$BRANCH"; then
    return 0
  fi
  # The retry's own rebase can conflict too (another writer's push collided
  # with ours) — never leave that mid-rebase either.
  abort_rebase || true
  echo "sabnyx-sync-tend: push rejected and the rebase-retry failed (conflict or hub unreachable) — inspect $SABNYX_DATA_DIR" >&2
  return 1
}

cd "$SABNYX_DATA_DIR"
# Sync whatever branch the clone is on (normally main); refuse a detached HEAD
# rather than silently pushing a ref that is not what was just committed.
BRANCH=$(git symbolic-ref --short -q HEAD) || { echo "sabnyx-sync-tend: $SABNYX_DATA_DIR is on a detached HEAD; refusing to sync" >&2; exit 1; }
if ! git pull --rebase --autostash origin "$BRANCH"; then
  abort_rebase || true
  echo "sabnyx-sync-tend: pull/rebase failed; rebase aborted (see above if not), local changes kept in stash or index — inspect $SABNYX_DATA_DIR" >&2
  exit 1
fi

# 9>&- : don't let tend (or anything it spawns) inherit the lock fd.
cd "$SABNYX_CODE_DIR" && uv run sabnyx tend --yes 9>&-
cd "$SABNYX_DATA_DIR" && git add -A -- . ':(exclude).sync-tend.lock'

# tend.completed is ledgered every run (opsctl's liveness signal) — a
# ledger-only diff isn't "nothing changed", but committing it every 15 min
# is pure churn. Commit only if something else changed too, or the last
# commit is >24h old (bounds hub staleness); else leave it staged for next run.
if git diff --cached --quiet -- . ':(exclude)ops/events.jsonl' ':(exclude).sync-tend.lock'; then
  last_commit_ts=$(git log -1 --format=%ct) || { echo "sabnyx-sync-tend: git log failed; not committing" >&2; exit 1; }
  now_ts=$(date -u +%s)
  if [ $(( now_ts - last_commit_ts )) -le 86400 ]; then
    exit 0
  fi
fi
git diff --cached --quiet && exit 0
git commit -m "tend $(date -u +%FT%TZ)"
push_with_retry || exit 1
