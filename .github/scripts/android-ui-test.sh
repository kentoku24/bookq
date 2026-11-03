#!/usr/bin/env bash
# android-ui-test.sh — emulator UI smoke test runner for .github/workflows/android.yml
#
# Invoked as the `script:` of reactivecircus/android-emulator-runner (the
# emulator is already booted; adb is on PATH). Two defences against the
# environment flake from #326, where all four connected tests failed
# identically when the emulator could not reach the host-side bookq:
#
#  1. Connectivity gate — probe the server FROM INSIDE the emulator
#     (10.0.2.2 is host loopback through the emulated NAT). adb being
#     mid-handshake counts as not-ready, same as a refused probe; either way
#     this is a bounded wait instead of an instant all-tests-fail run.
#  2. One in-session retry — the second attempt reuses the already-booted
#     emulator, so it costs seconds rather than a fresh boot, and covers the
#     case where the probe passed but adb/emulator wedged mid-run.
#
# Usage: android-ui-test.sh [server_url] [expected_folder]
set -u

SERVER_URL="${1:-http://10.0.2.2:8080}"
EXPECTED_FOLDER="${2:-Other}"
PROBE_URL="$SERVER_URL/api/v1/health"
PROBE_ATTEMPTS="${PROBE_ATTEMPTS:-60}"
PROBE_SLEEP="${PROBE_SLEEP:-2}"

HOSTPORT="${SERVER_URL#*://}"
HOSTPORT="${HOSTPORT%%/*}"
if [[ "$HOSTPORT" == *:* ]]; then
  HOST="${HOSTPORT%%:*}"
  PORT="${HOSTPORT##*:}"
else
  # `##*:` on a port-less host yields the host itself → nc host host.
  HOST="$HOSTPORT"
  case "$SERVER_URL" in https://*) PORT=443 ;; *) PORT=80 ;; esac
fi

# API-35 images ship /system/bin/curl; toybox nc is the fallback probe.
probe() {
  adb shell "curl -sf -m 3 '$PROBE_URL' >/dev/null 2>&1" \
    || adb shell "toybox nc -w 3 '$HOST' '$PORT' </dev/null >/dev/null 2>&1"
}

run_tests() {
  ./gradlew :app:connectedDebugAndroidTest \
    "-Pandroid.testInstrumentationRunnerArguments.bookqServerUrl=$SERVER_URL" \
    "-Pandroid.testInstrumentationRunnerArguments.bookqExpectedFolder=$EXPECTED_FOLDER"
}

wait_for_server() {
  for i in $(seq 1 "$PROBE_ATTEMPTS"); do
    if probe; then
      echo "emulator reached server after $i probe(s)"
      return 0
    fi
    sleep "$PROBE_SLEEP"
  done
  echo "ERROR: emulator cannot reach $PROBE_URL after $PROBE_ATTEMPTS probes"
  adb devices
  adb shell getprop sys.boot_completed || true
  return 1
}

echo "== emulator->server connectivity gate: $PROBE_URL"
wait_for_server || exit 1

echo "== connected tests (attempt 1)"
if run_tests; then
  exit 0
fi

# Retry only makes sense once connectivity is back — if the NAT bridge
# dropped mid-run it may take tens of seconds to recover; a retry fired
# while still unreachable would waste the attempt.
echo "== attempt 1 failed; re-gating on $PROBE_URL before retry"
wait_for_server || exit 1
echo "== connected tests (attempt 2)"
run_tests
