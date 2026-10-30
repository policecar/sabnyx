#!/usr/bin/env bash
# sabnyx pre-commit hook: lint staged vault files. Hard-fail on violations.
#
# Install with ops/scripts/install-hooks.sh. In split-root deployments the hook
# lives in the DATA repo while this script remains in the code repo.

set -euo pipefail

# Git runs a pre-commit hook with cwd at the repository being committed. That
# is the DATA repo in split-root deployments. Resolve the code root from this
# script's real location (the installer uses an absolute symlink).
DATA_ROOT="$(git rev-parse --show-toplevel)"
SCRIPT_PATH="${BASH_SOURCE[0]}"
while [[ -L "$SCRIPT_PATH" ]]; do
    SCRIPT_DIR="$(cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd)"
    SCRIPT_PATH="$(readlink "$SCRIPT_PATH")"
    if [[ "$SCRIPT_PATH" != /* ]]; then
        SCRIPT_PATH="$SCRIPT_DIR/$SCRIPT_PATH"
    fi
done
SCRIPT_PATH="$(cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd)/$(basename -- "$SCRIPT_PATH")"
CODE_ROOT="$(cd -- "$(dirname -- "$SCRIPT_PATH")/../.." && pwd)"

# Only run if anything under vault/ is staged; lint --staged picks the pages.
# Ask with a pathspec, never by parsing names: --name-only C-quotes a
# non-ASCII name ("vault/intake/caf\303\251.md"), which a '^vault/' match
# misses, and the gate would wave the commit through. --quiet exits 1 for
# "changes", 0 for "none"; anything else is a failed index read: fail closed.
if git -C "$DATA_ROOT" diff --cached --quiet --no-ext-diff -- vault; then
    exit 0
else
    status=$?
    if [[ $status -ne 1 ]]; then
        echo "sabnyx: could not inspect staged files (git exit $status); commit blocked."
        exit 1
    fi
fi

echo "sabnyx: linting staged vault files..."
if ! (cd "$CODE_ROOT" && SABNYX_DATA_DIR="$DATA_ROOT" uv run sabnyx lint --staged); then
    echo ""
    echo "sabnyx: commit blocked. Fix frontmatter or stage a correction."
    exit 1
fi
