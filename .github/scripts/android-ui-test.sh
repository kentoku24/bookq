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
#     The app module ships ABI splits (arm64-v8a / x86_64 / universal), so
#     the APK is picked by the emulator's `ro.product.cpu.abi` with the
#     universal APK as fallback.
#  3. One in-session retry — the second attempt reuses the already-booted
#     emulator, so it costs seconds rather than a fresh boot.
#
# The :data library androidTest (KeystoreTokenStoreTest) rides the same
# emulator: its APK is self-instrumenting (the library is compiled into the
# test APK and targetPackage is the test package itself) and needs no
# server, so it runs once before the connectivity-gated app suite. With
# two test APKs installed `pm list instrumentation` lists two `bookq`
# entries — components are resolved by target package, not `head -1`.
#
# Usage: android-ui-test.sh [server_url] [expected_folder]
set -u

SERVER_URL="${1:-http://10.0.2.2:8080}"
EXPECTED_FOLDER="${2:-Other}"
PROBE_URL="$SERVER_URL/api/v1/health"
PROBE_ATTEMPTS="${PROBE_ATTEMPTS:-60}"
PROBE_SLEEP="${PROBE_SLEEP:-2}"

APP_APK="${APP_APK:-}"
TEST_APK="${TEST_APK:-app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk}"
DATA_TEST_APK="${DATA_TEST_APK:-data/build/outputs/apk/androidTest/debug/data-debug-androidTest.apk}"
RESULTS_FILE="${RESULTS_FILE:-build/outputs/android-ui-test-results.txt}"
DATA_RESULTS_FILE="${DATA_RESULTS_FILE:-build/outputs/android-data-test-results.txt}"

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
  ./gradlew :app:assembleDebug :app:assembleDebugAndroidTest :data:assembleDebugAndroidTest
}

wait_for_adb() {
  # The emulator-runner action returns once the guest boots, but the adb
  # transport can still be mid-reconnect — and install_apks runs before
  # the server probe, so a dropped device here would exit before any
  # retry path exists.
  local i
  for i in $(seq 1 60); do
    if adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r' | grep -q '^1'; then
      return 0
    fi
    sleep 2
  done
  echo "ERROR: adb/device not ready after 120s"
  adb devices
  return 1
}

install_apks() {
  local app_apk="$APP_APK"
  if [[ -z "$app_apk" ]]; then
    local abi
    abi="$(adb shell getprop ro.product.cpu.abi | tr -d '\r')"
    app_apk="app/build/outputs/apk/debug/app-${abi}-debug.apk"
    [[ -f "$app_apk" ]] || app_apk="app/build/outputs/apk/debug/app-universal-debug.apk"
  fi
  local apk
  for apk in "$app_apk" "$TEST_APK" "$DATA_TEST_APK"; do
    if [[ ! -f "$apk" ]]; then
      echo "ERROR: APK not found: $apk"
      ls app/build/outputs/apk/debug/ app/build/outputs/apk/androidTest/debug/ \
        data/build/outputs/apk/androidTest/debug/ 2>/dev/null || true
      return 1
    fi
  done
  adb install -r "$app_apk" || return 1
  adb install -r "$TEST_APK" || return 1
  adb install -r "$DATA_TEST_APK" || return 1
}

instrumentation_component() {
  # `pm list instrumentation` → "instrumentation:<pkg>/<runner> (target=...)".
  # $1 is the target package — required now that two test APKs (app +
  # self-instrumenting :data) are installed and both match 'bookq'.
  # Discovered rather than hardcoded so a testApplicationId change can't
  # silently point at a stale component.
  adb shell pm list instrumentation | tr -d '\r' \
    | grep "target=$1)" \
    | grep -o 'instrumentation:[^ ]*' | head -1 \
    | sed 's/^instrumentation://'
}

instrument() {
  # $1 results file, $2 component; remaining args are extra `am instrument`
  # options (-e pairs). Pass/fail is read from the runner's summary below —
  # `am instrument`'s exit status alone is not a reliable test verdict.
  local results="$1" comp="$2"
  shift 2
  mkdir -p "$(dirname "$results")"
  adb shell am instrument -w "$@" "$comp" 2>&1 | tee "$results"
  # The runner prints "OK (N tests)" on success and "FAILURES!!!" on test
  # failures; am's own exit status does not distinguish the two. N must be
  # >0 — "OK (0 tests)" would be a silent pass if instrumentation ever
  # resolved to an empty suite.
  if grep -q 'FAILURES' "$results" \
    || ! grep -qE 'OK \([1-9][0-9]* tests?\)' "$results"; then
    return 1
  fi
}

run_app_tests() {
  local comp
  comp="$(instrumentation_component dev.bookq.reader.debug)"
  comp="${comp:-dev.bookq.reader.debug.test/androidx.test.runner.AndroidJUnitRunner}"
  instrument "$RESULTS_FILE" "$comp" \
    -e bookqServerUrl "$SERVER_URL" \
    -e bookqExpectedFolder "$EXPECTED_FOLDER"
}

run_data_tests() {
  # :data androidTest is self-instrumenting (targetPackage is the test
  # package itself) and needs no server — KeystoreTokenStoreTest is a pure
  # AndroidKeyStore round-trip.
  local comp
  comp="$(instrumentation_component dev.bookq.data.test)"
  comp="${comp:-dev.bookq.data.test/androidx.test.runner.AndroidJUnitRunner}"
  instrument "$DATA_RESULTS_FILE" "$comp"
}

echo "== build APKs"
build_apks || exit 1

echo "== install APKs"
wait_for_adb || exit 1
install_apks || exit 1

# :data androidTest is server-independent — run it once up front so a NAT
# bridge flake can't hide its result or burn a retry attempt on it.
echo "== instrumented tests: :data androidTest"
DATA_OK=0
run_data_tests || DATA_OK=1

echo "== emulator->server connectivity gate: $PROBE_URL"
echo "== instrumented tests (attempt 1)"
APP_OK=1
if wait_for_server && run_app_tests; then
  APP_OK=0
else
  # Retry only makes sense once connectivity is back — if the NAT bridge
  # dropped mid-run it may take tens of seconds to recover; a retry fired
  # while still unreachable would waste the attempt. APKs stay installed,
  # so the retry is probe + instrument only.
  echo "== attempt 1 failed; re-gating on $PROBE_URL before retry"
  if wait_for_server && run_app_tests; then
    APP_OK=0
  fi
fi

if [ "$DATA_OK" = 0 ] && [ "$APP_OK" = 0 ]; then
  exit 0
fi
exit 1
