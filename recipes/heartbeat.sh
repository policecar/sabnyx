#!/usr/bin/env bash
# heartbeat.sh — run the sabnyx self-maintenance cycle.
#
# Suitable for cron or launchd. Runs `sabnyx tend --yes` from the repo root,
# directing output to a log file. Requires `uv` on PATH and the repo present
# at SABNYX_REPO (defaults to the directory containing this script's parent).
#
# Crontab line (runs at 06:00 and 18:00 daily):
#   0 6,18 * * * /path/to/sabnyx/recipes/heartbeat.sh >> /tmp/sabnyx-heartbeat.log 2>&1
#
# LaunchAgent plist: set ProgramArguments to this script path, and
# StartCalendarInterval to [{Hour: 6, Minute: 0}, {Hour: 18, Minute: 0}].

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${SABNYX_REPO:-$(dirname "$SCRIPT_DIR")}"

echo "=== sabnyx heartbeat $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

cd "$REPO"
uv run sabnyx tend --yes

echo "=== done ==="
