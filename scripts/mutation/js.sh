#!/usr/bin/env bash
# JS mutation-testing wrapper — thin pass-through so docs/AGENTS can point at
# one entry point. Run `npm ci` (or `npm install`) once first.
#
# Usage:
#   scripts/mutation/js.sh test      # vitest unit suite (fast, ~seconds)
#   scripts/mutation/js.sh mutate    # stryker run (minutes; writes .reports/)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

case "${1:-mutate}" in
  test)   exec npm run test:js ;;
  mutate) exec npm run mutation:js ;;
  *) echo "usage: $0 [test|mutate]" >&2; exit 2 ;;
esac
