#!/usr/bin/env bash
# android-ui-test.sh — emulator UI smoke test runner for .github/workflows/android.yml
#
# Invoked as the `script:` of reactivecircus/android-emulator-runner (the
# emulator is already booted; adb is on PATH). Defences against the
# environment flake from #326, where all connected tests failed identically
# when the emulator could not reach the host-side bookq:
#
#  1. Build once, probe per attempt — `assemble*` runs before the first
#     probe so a NAT bridge that dies during the ~4min Gradle build is
#     caught by the gate instead of surfacing as a mid-suite network error.
#  2. `adb install` + `am instrument` instead of `connectedDebugAndroidTest`:
#     the retry reuses the already-installed APKs, so a second attempt costs
#     ~1min of probes+instrumentation instead of another Gradle invocation.
#     Pass/fail is read from the `OK (N tests)` / `FAILURES!!!` summary —
#     `am instrument`'s exit status alone is not a reliable test verdict.
#  3. One in-session retry — the second attempt reuses the already-booted
#     emulator, so it costs seconds rather than a fresh boot.
#
# Usage: android-ui-test.sh [server_url] [expected_folder]
set -u

SERVER_URL="${1:-http://10.0.2.2:8080}"
EXPECTED_FOLDER="${2:-Other}"
PROBE_URL="$SERVER_URL/api/v1/health"
PROBE_ATTEMPTS="${PROBE_ATTEMPTS:-60}"
PROBE_SLEEP="${PROBE_SLEEP:-2}"

APP_APK="${APP_APK:-app/build/outputs/apk/debug/app-debug.apk}"
TEST_APK="${TEST_APK:-app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk}"
RESULTS_FILE="${RESULTS_FILE:-build/outputs/android-ui-test-results.txt}"

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

build_apks() {
  ./gradlew :app:assembleDebug :app:assembleDebugAndroidTest
}

install_apks() {
  adb install -r "$APP_APK"
  adb install -r "$TEST_APK"
}

instrumentation_component() {
  # `pm list instrumentation` → "instrumentation:<pkg>/<runner> (target=...)".
  # Discovered rather than hardcoded so a testApplicationId change can't
  # silently point at a stale component.
  adb shell pm list instrumentation | tr -d '\r' \
    | grep -o 'instrumentation:[^ ]*' | grep 'bookq' | head -1 \
    | sed 's/^instrumentation://'
}

run_tests() {
  local comp
  comp="$(instrumentation_component)"
  comp="${comp:-dev.bookq.reader.debug.test/androidx.test.runner.AndroidJUnitRunner}"
  mkdir -p "$(dirname "$RESULTS_FILE")"
  adb shell am instrument -w \
    -e bookqServerUrl "$SERVER_URL" \
    -e bookqExpectedFolder "$EXPECTED_FOLDER" \
    "$comp" 2>&1 | tee "$RESULTS_FILE"
  # The runner prints "OK (N tests)" on success and "FAILURES!!!" on test
  # failures; am's own exit status does not distinguish the two.
  if grep -q 'FAILURES' "$RESULTS_FILE" || ! grep -q 'OK (' "$RESULTS_FILE"; then
    return 1
  fi
}

echo "== build APKs"
build_apks || exit 1

echo "== install APKs"
install_apks || exit 1

echo "== emulator->server connectivity gate: $PROBE_URL"
echo "== instrumented tests (attempt 1)"
if wait_for_server && run_tests; then
  exit 0
fi

# Retry only makes sense once connectivity is back — if the NAT bridge
# dropped mid-run it may take tens of seconds to recover; a retry fired
# while still unreachable would waste the attempt. APKs stay installed,
# so the retry is probe + instrument only.
echo "== attempt 1 failed; re-gating on $PROBE_URL before retry"
if wait_for_server && run_tests; then
  exit 0
fi
exit 1
