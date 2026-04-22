#!/usr/bin/env bash
# Install the sabnyx pre-commit hook into .git/hooks/.
set -euo pipefail
REPO_ROOT="$(git rev-parse --show-toplevel)"
HOOK_SRC="$REPO_ROOT/ops/scripts/pre-commit.sh"
HOOK_DST="$REPO_ROOT/.git/hooks/pre-commit"

chmod +x "$HOOK_SRC"
ln -sf ../../ops/scripts/pre-commit.sh "$HOOK_DST"
echo "installed pre-commit hook -> $HOOK_DST"
