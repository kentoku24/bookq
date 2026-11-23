#!/usr/bin/env bash
#
# test_oc_fail_classify.sh — regression tests for oc-fail-classify.sh (#635).
#
# Usage: bash .github/scripts/test_oc_fail_classify.sh
#
# `gh` is stubbed via PATH: `--json jobs` returns canned job/step JSON and
# `--log-failed` returns a canned log file, so both the step-name path and
# the log-refinement path are exercised offline.

set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
CLASSIFY="$DIR/oc-fail-classify.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

passed=0
failed=0
total=0

# --- stub gh ---------------------------------------------------------------
mkdir -p "$TMP/bin"
cat > "$TMP/bin/gh" <<'EOF'
#!/usr/bin/env bash
# stub gh: --json jobs -> $STUB_JOBS_JSON ; --log-failed -> $STUB_LOG_FILE
if [ "${STUB_GH_FAIL:-0}" = "1" ]; then exit 1; fi
for a in "$@"; do
  if [ "$a" = "--json" ]; then
    printf '%s' "${STUB_JOBS_JSON:-}"
    exit 0
  fi
done
cat "${STUB_LOG_FILE:-/dev/null}"
EOF
chmod +x "$TMP/bin/gh"
export PATH="$TMP/bin:$PATH"
export STUB_GH_FAIL=0

JOBS_PUSH='{"jobs":[{"name":"implement-direct","conclusion":"failure","steps":[{"name":"checkout","conclusion":"success"},{"name":"Run opencode CLI","conclusion":"success"},{"name":"Run bookq test gate (full suite, viewer-rs)","conclusion":"success"},{"name":"Branch, commit, push, open PR","conclusion":"failure"}]}]}'
JOBS_TEST='{"jobs":[{"name":"implement-direct","conclusion":"failure","steps":[{"name":"Run opencode CLI","conclusion":"success"},{"name":"Run bookq test gate (full suite, viewer-rs)","conclusion":"failure"},{"name":"Branch, commit, push, open PR","conclusion":"skipped"}]}]}'
JOBS_AGENT='{"jobs":[{"name":"implement-direct","conclusion":"failure","steps":[{"name":"Run opencode CLI","conclusion":"failure"},{"name":"Run bookq test gate (full suite, viewer-rs)","conclusion":"skipped"}]}]}'
JOBS_ENV='{"jobs":[{"name":"implement-direct","conclusion":"failure","steps":[{"name":"cargo build (test fixture binary)","conclusion":"failure"},{"name":"Run bookq test gate (full suite, viewer-rs)","conclusion":"skipped"}]}]}'
JOBS_NOSTEP='{"jobs":[{"name":"implement-direct","conclusion":"failure","steps":[{"name":"checkout","conclusion":"success"}]}]}'

run_classify() { # $1=jobs-json $2=logfile $3=job $4...=pairs
  export STUB_JOBS_JSON="$1" STUB_LOG_FILE="$2"
  shift 2
  bash "$CLASSIFY" "12345" "o/r" "$@"
}

assert_class() { # $1=label $2=expected-class $3=jobs-json $4=logfile $5=job $6...=pairs
  local label="$1" want="$2" jobs="$3" logf="$4" job="$5"; shift 5
  total=$((total + 1))
  local out
  out=$(run_classify "$jobs" "$logf" "$job" "$@")
  local got="${out%%|*}"
  if [ "$got" = "$want" ]; then
    echo "  ok  [$total] $label -> $out"
    passed=$((passed + 1))
  else
    echo "FAIL [$total] $label"
    echo "  expected class: $want"
    echo "  got:            $out"
    failed=$((failed + 1))
  fi
}

# --- fixtures ---------------------------------------------------------------
cat > "$TMP/log-workflows-perm" <<'EOF'
implement-direct	UNKNOWN STEP	2026-11-22T23:07:54Z  ! [remote rejected] ai/627-x -> ai/627-x (refusing to allow a GitHub App to create or update workflow `.github/workflows/ci-metrics.yml` without `workflows` permission)
implement-direct	UNKNOWN STEP	2026-11-22T23:07:54Z error: failed to push some refs to 'https://github.com/kentoku24/bookq'
EOF

cat > "$TMP/log-push-nonff" <<'EOF'
implement-direct	UNKNOWN STEP	2026-11-14T09:26:28Z  ! [rejected] ai/498-x -> ai/498-x (non-fast-forward)
implement-direct	UNKNOWN STEP	2026-11-14T09:26:28Z error: failed to push some refs to 'https://github.com/kentoku24/bookq'
EOF

cat > "$TMP/log-nocommit" <<'EOF'
implement-direct	UNKNOWN STEP	2026-11-14T09:33:37Z ##[error]no commits and no PR for #498 — agent produced nothing
EOF

cat > "$TMP/log-testgate" <<'EOF'
implement-direct	UNKNOWN STEP	2026-11-22T23:19:26Z FAILED tests/test_folder_visibility_navigation.py::test_loading_queues_browse_url_then_applies - Failed: visibility GET was never issued
implement-direct	UNKNOWN STEP	2026-11-22T23:19:30Z ====== 1 failed, 1268 passed in 812.34s ======
EOF

cat > "$TMP/log-pw-env" <<'EOF'
implement-direct	UNKNOWN STEP	2026-11-16T06:40:00Z E   playwright._impl._errors.Error: Executable doesn't exist at /home/runner/.cache/ms-playwright/chromium-*/chrome-linux/chrome
implement-direct	UNKNOWN STEP	2026-11-16T06:40:01Z FAILED tests/test_app.py::test_x - browser launch failed
EOF

cat > "$TMP/log-agent-plain" <<'EOF'
implement-direct	UNKNOWN STEP	2026-11-22T23:20:00Z opencode: model call failed after 3 retries
implement-direct	UNKNOWN STEP	2026-11-22T23:20:01Z ##[error]Process completed with exit code 1.
EOF

cat > "$TMP/log-infra" <<'EOF'
implement-direct	UNKNOWN STEP	2026-11-14T23:17:11Z curl: (56) Recv failure: Connection reset by peer
implement-direct	UNKNOWN STEP	2026-11-14T23:17:11Z ##[error]Process completed with exit code 56.
EOF

: > "$TMP/log-empty"

# --- step-name driven classification ----------------------------------------
assert_class "push step + workflows perm"   push     "$JOBS_PUSH"  "$TMP/log-workflows-perm" implement-direct
assert_class "push step + non-ff"           push     "$JOBS_PUSH"  "$TMP/log-push-nonff"     implement-direct
assert_class "push step + no commits"       no-output "$JOBS_PUSH" "$TMP/log-nocommit"       implement-direct
assert_class "test gate + FAILED"           test     "$JOBS_TEST"  "$TMP/log-testgate"      implement-direct
assert_class "test gate + pw noise"       test     "$JOBS_TEST"  "$TMP/log-pw-env"        implement-direct
assert_class "agent step + push reject"     push     "$JOBS_AGENT" "$TMP/log-workflows-perm" implement-direct
assert_class "agent step + plain failure"   agent    "$JOBS_AGENT" "$TMP/log-agent-plain"  implement-direct
assert_class "agent step + curl 56"         infra    "$JOBS_AGENT" "$TMP/log-infra"        implement-direct
assert_class "env step + clean log"         env      "$JOBS_ENV"   "$TMP/log-empty"        implement-direct
assert_class "env step + curl 56"           infra    "$JOBS_ENV"   "$TMP/log-infra"        implement-direct

# hint specificity: workflows rejection must mention workflows
total=$((total + 1))
out=$(run_classify "$JOBS_AGENT" "$TMP/log-workflows-perm" implement-direct)
case "$out" in
  *workflows*) echo "  ok  [$total] agent+workflows hint mentions workflows -> $out"; passed=$((passed + 1)) ;;
  *) echo "FAIL [$total] agent+workflows hint: $out"; failed=$((failed + 1)) ;;
esac

# --- fallback: step outcomes -------------------------------------------------
assert_class "fallback pushpr"    push     "$JOBS_NOSTEP" "$TMP/log-empty" implement-direct "pushpr=failure" "testgate=skipped"
assert_class "fallback testgate"  test     "$JOBS_NOSTEP" "$TMP/log-empty" implement-direct "testgate=failure" "agent=success"
assert_class "fallback preflight" env      "$JOBS_NOSTEP" "$TMP/log-empty" implement-direct "preflight=failure"
assert_class "fallback agent"     agent    "$JOBS_NOSTEP" "$TMP/log-empty" implement-direct "agent=failure"
assert_class "fallback untagged"  env      "$JOBS_NOSTEP" "$TMP/log-empty" implement-direct "agent=success" "testgate=skipped"
assert_class "fallback no-pairs"  unknown  "$JOBS_NOSTEP" "$TMP/log-empty" implement-direct

# gh itself failing must degrade to fallback, never break
total=$((total + 1))
out=$(STUB_GH_FAIL=1 STUB_JOBS_JSON= STUB_LOG_FILE=/dev/null \
      bash "$CLASSIFY" "12345" "o/r" implement-direct "agent=failure" 2>/dev/null)
case "$out" in
  agent\|*) echo "  ok  [$total] gh failure degrades to outcome fallback -> $out"; passed=$((passed + 1)) ;;
  *) echo "FAIL [$total] gh failure fallback: $out"; failed=$((failed + 1)) ;;
esac

# script must always exit 0 even with no args
total=$((total + 1))
bash "$CLASSIFY" >/dev/null 2>&1
rc=$?
if [ "$rc" -eq 0 ]; then
  echo "  ok  [$total] exits 0 with no args"
  passed=$((passed + 1))
else
  echo "FAIL [$total] exit code $rc with no args"
  failed=$((failed + 1))
fi

echo
echo "oc-fail-classify: $passed/$total passed"
[ "$failed" -eq 0 ]
