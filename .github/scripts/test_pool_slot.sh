#!/usr/bin/env bash
#
# test_pool_slot.sh — TDD RED test for dev.sh pool slot normalization (issue #165).
#
# Usage: bash .github/scripts/test_pool_slot.sh
#
# RED phase (unmodified dev.sh — this todo): asserts CORRECT normalized output
# on EXISTING functions, so it exits non-zero. Observed live (rc=0, not 127):
#   pool_hostname_of 000008 → dev-00000008 garbage
#   pool_hostname_of 0008   → dev-000008 garbage
#   pool_hostname_of 08     → dev-0008 garbage
#   pool_project_of 000008 x → bookq-dev-0000000008-x garbage
# Root cause (dev.sh:134,146 `|| printf '%s'` fallback): bash reads 8-containing
# slots as invalid octal, so `printf '%02d'` prints a partial `00` (value 0) AND
# exits non-zero — then the `||` fallback appends the raw slot. Every pass
# prepends another `00` (hence 0008→000008→00000008… growth), and nested calls
# (project→hostname) compound it. Valid octal inputs (01, 10) are unaffected.
# Deliberately does NOT call normalize_pool_slot in RED (nonexistent → exit 127
# would mask the bug; the failure here must be the octal/fallback bug, not 127).
#
# GREEN phase (after Todos 2-4, NO edits to this file needed): the RED-section
# assertions pass once call sites route through the helper, and the helper
# matrix below activates automatically via the `command -v` guard:
#   repair: 08→08, 0008→08, 000008→08, 8→08, 01→01, 10→10
#   die (non-zero exit): 00, 11, abc, empty
#
# NOTE for Todo 2 (do NOT fix dev.sh in this todo): two sourcing traps, both
# verified live on unmodified dev.sh —
#   (a) plain `bash -c 'source ./dev.sh …'` (i.e. $0=bash) ABORTS the subshell
#       before any function runs — dev.sh:34 `set -euo pipefail` + tail dispatch
#       (dev.sh:837) hits `usage` → `awk … "$0"` fails ("can't open file bash",
#       rc=2) and errexit kills the sourcing shell (empty output, rc=2).
#       Workaround: `bash -c '…' ./dev.sh` sets $0 to a real file so usage()
#       succeeds (its output is suppressed by the redirect).
#   (b) the tail dispatch reads the SOURCING shell's $1, so function args must
#       NOT be passed as positional params — `$1=pool_hostname_of` hits the
#       `*) die "不明なサブコマンド…"` branch and the function never runs
#       (empty output, rc=1). Workaround: stash args, `set --` before sourcing,
#       then invoke from the array.
# If Todo 2 adds a source-guard (`[[ "${BASH_SOURCE[0]}" != "${0}" ]] &&
# return 0` style), plain sourcing starts working too — this harness keeps
# working either way.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DEVSH="$REPO_ROOT/dev.sh"

passed=0
failed=0
skipped=0
total=0

# --- harness --------------------------------------------------------------

# dev_call <func> [args...] — run a dev.sh function in a disposable subshell.
# Exit code is the function's; stdout is the function's stdout. Args are
# stashed in an array and positional params cleared before sourcing so the
# tail dispatch (dev.sh:837) sees empty $1 and takes the usage path instead
# of `die "不明なサブコマンド…"`.
dev_call() {
  bash -c 'args=("$@"); set --; source ./dev.sh >/dev/null 2>&1; "${args[@]}"' ./dev.sh "$@"
}

# helper_present — rc 0 iff normalize_pool_slot exists (GREEN phase gate).
helper_present() {
  dev_call command -v normalize_pool_slot >/dev/null 2>&1
}

assert_hostname_eq() {
  local slot="$1" expect="$2"
  total=$((total + 1))
  local got rc
  set +e
  got=$(dev_call pool_hostname_of "$slot" 2>/dev/null)
  rc=$?
  set -e
  if [ "$rc" -ne 0 ]; then
    echo "FAIL [$total] pool_hostname_of '$slot' — exited rc=$rc (expected '$expect')"
    failed=$((failed + 1))
    return
  fi
  if [ "$got" = "$expect" ]; then
    echo "  ok  [$total] pool_hostname_of '$slot' → '$got'"
    passed=$((passed + 1))
  else
    echo "FAIL [$total] pool_hostname_of '$slot' → '$got' (expected '$expect')"
    failed=$((failed + 1))
  fi
}

assert_project_eq() {
  local slot="$1" env_name="$2" expect="$3"
  total=$((total + 1))
  local got rc
  set +e
  got=$(dev_call pool_project_of "$slot" "$env_name" 2>/dev/null)
  rc=$?
  set -e
  if [ "$rc" -ne 0 ]; then
    echo "FAIL [$total] pool_project_of '$slot' '$env_name' — exited rc=$rc (expected '$expect')"
    failed=$((failed + 1))
    return
  fi
  if [ "$got" = "$expect" ]; then
    echo "  ok  [$total] pool_project_of '$slot' '$env_name' → '$got'"
    passed=$((passed + 1))
  else
    echo "FAIL [$total] pool_project_of '$slot' '$env_name' → '$got' (expected '$expect')"
    failed=$((failed + 1))
  fi
}

# GREEN-phase assertions (run only when the helper exists; skipped in RED).
assert_normalize_eq() {
  local slot="$1" expect="$2"
  total=$((total + 1))
  local got rc
  set +e
  got=$(dev_call normalize_pool_slot "$slot" 2>/dev/null)
  rc=$?
  set -e
  if [ "$rc" -ne 0 ]; then
    echo "FAIL [$total] normalize_pool_slot '$slot' — exited rc=$rc (expected '$expect')"
    failed=$((failed + 1))
    return
  fi
  if [ "$got" = "$expect" ]; then
    echo "  ok  [$total] normalize_pool_slot '$slot' → '$got'"
    passed=$((passed + 1))
  else
    echo "FAIL [$total] normalize_pool_slot '$slot' → '$got' (expected '$expect')"
    failed=$((failed + 1))
  fi
}

assert_normalize_dies() {
  local label="$1"
  shift
  total=$((total + 1))
  set +e
  dev_call normalize_pool_slot "$@" >/dev/null 2>&1
  local rc=$?
  set -e
  if [ "$rc" -ne 0 ]; then
    echo "  ok  [$total] normalize_pool_slot $label dies (rc=$rc)"
    passed=$((passed + 1))
  else
    echo "FAIL [$total] normalize_pool_slot $label — exited 0, expected die"
    failed=$((failed + 1))
  fi
}

skip_green() {
  total=$((total + 1))
  skipped=$((skipped + 1))
  echo "  skip [$total] $1 (normalize_pool_slot absent — GREEN phase)"
}

# --- run tests ------------------------------------------------------------

echo "=== pool slot normalization tests (issue #165) ==="
echo ""

echo "--- RED: existing functions must produce normalized output ---"
assert_hostname_eq "000008" "dev-08"
assert_hostname_eq "0008" "dev-08"
assert_project_eq "000008" "x" "bookq-dev-08-x"
assert_hostname_eq "08" "dev-08"
assert_hostname_eq "01" "dev-01"
assert_hostname_eq "10" "dev-10"

echo ""
echo "--- GREEN: normalize_pool_slot repair/die matrix (guarded) ---"
if helper_present; then
  assert_normalize_eq "08" "08"
  assert_normalize_eq "0008" "08"
  assert_normalize_eq "000008" "08"
  assert_normalize_eq "8" "08"
  assert_normalize_eq "01" "01"
  assert_normalize_eq "10" "10"
  assert_normalize_dies "'00'" "00"
  assert_normalize_dies "'11'" "11"
  assert_normalize_dies "'abc'" "abc"
  assert_normalize_dies "empty" ""
else
  skip_green "repair 08→08"
  skip_green "repair 0008→08"
  skip_green "repair 000008→08"
  skip_green "repair 8→08"
  skip_green "repair 01→01"
  skip_green "repair 10→10"
  skip_green "die 00"
  skip_green "die 11"
  skip_green "die abc"
  skip_green "die empty"
fi

echo ""
echo "=== results: $passed/$total passed, $failed failed, $skipped skipped ==="

if [ "$failed" -gt 0 ]; then
  exit 1
fi
