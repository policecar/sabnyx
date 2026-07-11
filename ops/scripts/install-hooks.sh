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
HOOK_DST="$(git -C "$DATA_ROOT" rev-parse --path-format=absolute --git-path hooks/pre-commit)"

chmod +x "$HOOK_SRC"
mkdir -p "$(dirname -- "$HOOK_DST")"
ln -sfn "$HOOK_SRC" "$HOOK_DST"
echo "installed pre-commit hook for $DATA_ROOT -> $HOOK_DST"
