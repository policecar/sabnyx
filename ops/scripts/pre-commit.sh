#!/usr/bin/env bash
# sabnyx pre-commit hook: lint staged vault files. Hard-fail on violations.
#
# Install:
#   ln -sf ../../ops/scripts/pre-commit.sh .git/hooks/pre-commit
# or use `sabnyx install-hooks` (see ops/scripts/install-hooks.sh).

set -euo pipefail

# Resolve repo root
REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

# Only run if there are staged .md files under vault/
staged=$(git diff --cached --name-only --diff-filter=ACMR | grep '^vault/.*\.md$' || true)
if [[ -z "$staged" ]]; then
    exit 0
fi

echo "sabnyx: linting staged vault files..."
if ! uv run sabnyx lint --staged; then
    echo ""
    echo "sabnyx: commit blocked. Fix frontmatter or stage a correction."
    exit 1
fi
