#!/usr/bin/env bash
# heartbeat.sh — run the sabnyx self-maintenance cycle, then project the face.
#
# Suitable for cron or launchd. Runs `sabnyx tend --yes` from the repo root,
# then (if halbor is present) projects the read-only face — closing the
# vault → face loop. Requires `uv` on PATH and the repo present at SABNYX_REPO
# (defaults to the directory containing this script's parent).
#
# The face is strictly downstream: a projection failure logs a warning but
# never fails the heartbeat — the vault is the source of truth and must keep
# maintaining itself regardless of the face. halbor is discovered at
# HALBOR_REPO (default: sibling `../halbor`); the step is skipped if absent,
# and warns (without failing) until halbor v0 is available on that checkout.
#
# Crontab line (runs at 06:00 and 18:00 daily):
#   0 6,18 * * * /path/to/sabnyx/recipes/heartbeat.sh >> /tmp/sabnyx-heartbeat.log 2>&1
#
# LaunchAgent plist: set ProgramArguments to this script path, and
# StartCalendarInterval to [{Hour: 6, Minute: 0}, {Hour: 18, Minute: 0}].

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${SABNYX_REPO:-$(dirname "$SCRIPT_DIR")}"
HALBOR_REPO="${HALBOR_REPO:-$(dirname "$REPO")/halbor}"

echo "=== sabnyx heartbeat $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

cd "$REPO"
uv run sabnyx tend --yes

# --- project the read-only face (halbor), if present -------------------------
# Downstream only: never let a face failure break self-maintenance.
if [ -f "$HALBOR_REPO/pyproject.toml" ]; then
  echo "=== project face (halbor) ==="
  if ( cd "$HALBOR_REPO" && uv run halbor project --vault "$REPO/vault" ); then
    echo "face projected"
  else
    echo "WARN: halbor projection failed — vault unaffected (face is downstream)" >&2
  fi
else
  echo "halbor not found at $HALBOR_REPO; skipping face projection"
fi

echo "=== done ==="
