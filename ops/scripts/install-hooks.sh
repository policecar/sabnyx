#!/usr/bin/env bash
# Install the sabnyx pre-commit hook into the DATA repo's git hooks.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CODE_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
HOOK_SRC="$SCRIPT_DIR/pre-commit.sh"

# Ask the Python path resolver so env/.env precedence stays identical to the
# application. Running from CODE_ROOT makes this work regardless of caller cwd.
DATA_ROOT="$(cd "$CODE_ROOT" && uv run python -c 'from sabnyx.paths import DATA_ROOT; print(DATA_ROOT)')"
git -C "$DATA_ROOT" rev-parse --git-dir >/dev/null
# --git-path is relative to the repo when run via -C; make it absolute ourselves
# instead of --path-format=absolute, which needs git >= 2.31 (older git echoes
# the unknown flag through and the mkdir below gets garbage).
HOOK_DST="$(git -C "$DATA_ROOT" rev-parse --git-path hooks/pre-commit)"
case "$HOOK_DST" in /*) ;; *) HOOK_DST="$DATA_ROOT/$HOOK_DST" ;; esac

chmod +x "$HOOK_SRC"
mkdir -p "$(dirname -- "$HOOK_DST")"
ln -sfn "$HOOK_SRC" "$HOOK_DST"
echo "installed pre-commit hook for $DATA_ROOT -> $HOOK_DST"
