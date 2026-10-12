#!/usr/bin/env bash
# Live-library regression for PR review (#31).
#
# Runs tests/live/ against a REAL library without recording anything about
# it: folders are discovered at runtime and addressed by hash ID only, so no
# real title/folder name can leak into code, logs, or reports. Screenshots of
# live content are forbidden (see tests/live/README.md).
#
#   BOOKQ_LIVE_ROOT=/path/to/library ./scripts/live-regress.sh
#
# Knobs: BOOKQ_LIVE_TOP (default 6), BOOKQ_LIVE_RANDOM (default 6),
#        BOOKQ_LIVE_SEED (default 7). Extra args pass through to pytest.
set -euo pipefail

: "${BOOKQ_LIVE_ROOT:?Set BOOKQ_LIVE_ROOT to the live library root. The suite refuses missing dirs and asserts the tree is byte-identical before/after; caches are sandboxed to tmp.}"
export BOOKQ_TEST_MODE=true

exec uv run --with pytest --with playwright --with-requirements requirements-dev.txt \
  pytest tests/live/ -q --tb=line "$@"
