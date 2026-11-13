#!/usr/bin/env bash
# ios-ui-test.sh — Xcode test runner for .github/workflows/ios.yml
#
# One job, one build: `build-for-testing` runs once into a cached
# DerivedData dir, then `test-without-building` executes BookqTests
# (unit + API contract) and BookqUITests (XCUITest UX suite) back to
# back against the same .xctestrun bundle. macOS runner minutes are
# ~10x ubuntu, so the build cost is paid once per job rather than once
# per test bundle.
#
# The simulator reaches the host-side TEST_MODE bookq at
# http://127.0.0.1:8080 directly (simulator shares the host network —
# the Android 10.0.2.2 alias does NOT apply here). The app is launched
# by the test bundle with -serverURL/-testToken arguments; nothing in
# this script touches app credentials.
#
# Usage: ios-ui-test.sh [server_url]
# Env:   DERIVED_DATA_PATH (default ~/Library/.../Bookq-ci),
#        IOS_TEST_RESULTS (default ios/build/ios-test-results),
#        IOS_DEVICE_NAME (dedicated simulator name, default "bookq-ci-tests")
set -u
set -o pipefail
cd "$(dirname "$0")/../.."

SERVER_URL="${1:-http://127.0.0.1:8080}"
PROJECT="ios/Bookq.xcodeproj"
SCHEME="Bookq"
DD="${DERIVED_DATA_PATH:-$HOME/Library/Developer/Xcode/DerivedData/Bookq-ci}"
RESULTS="${IOS_TEST_RESULTS:-ios/build/ios-test-results}"
mkdir -p "$RESULTS"
# xcodebuild refuses to reuse an existing -resultBundlePath, and the
# results dir survives between local runs — clean last run's bundles.
rm -rf "$RESULTS/build.xcresult" "$RESULTS/tests.xcresult" \
  "$RESULTS"/tests.attempt*.log

# --- server gate -------------------------------------------------------
# Host-side check is sufficient: the simulator shares the host network
# namespace for loopback, so if the host can reach it the sim can too.
# 127.0.0.1 (not localhost) forces IPv4 — on hosts where another service
# listens on [::1]:8080 (e.g. AirPlay Receiver), localhost may resolve to ::1
# and the health gate would hit the wrong service.
echo "== server gate: $SERVER_URL/api/v1/health"
for i in $(seq 1 60); do
  if curl -sf "$SERVER_URL/api/v1/health" >/dev/null; then
    echo "server healthy after ${i}s"
    break
  fi
  if [ "$i" -eq 60 ]; then
    echo "ERROR: server did not become healthy"
    exit 1
  fi
  sleep 1
done

# --- dedicated test device ---------------------------------------------
# A script-owned simulator, erased every run, guarantees clean
# app/Keychain/UserDefaults state. Reusing a daily-driver sim lets
# leftovers fail stateful unit tests — ProgressSyncTests read
# UserDefaults.standard and break on progress entries the UI tests
# wrote on an earlier run (clones inherit device app data).
DEVICE_NAME="${IOS_DEVICE_NAME:-bookq-ci-tests}"
echo "== prepare simulator '$DEVICE_NAME' (erased each run)"

# Locate our device by name across all runtimes.
UDID=""
DEVNAME="$DEVICE_NAME"
UDID="$(xcrun simctl list devices -j | python3 - "$DEVICE_NAME" <<'PY'
import json, subprocess, sys
name = sys.argv[1]
devs = json.loads(subprocess.check_output(
    ["xcrun", "simctl", "list", "devices", "-j"]))["devices"]
for ds in devs.values():
    for d in ds:
        if d["name"] == name:
            print(d["udid"])
            sys.exit(0)
PY
)"

if [ -z "$UDID" ]; then
  # Create on the newest iOS runtime with the first available modern
  # iPhone type (iPhone 17 preferred — closest to the iPhone 13/18
  # class the suite was validated on; layout tests use identifiers and
  # proportional tap zones, so the exact model is not load-bearing).
  read -r UDID DEVNAME < <(python3 - "$DEVICE_NAME" <<'PY'
import json, re, subprocess, sys
name = sys.argv[1]
runtimes = json.loads(subprocess.check_output(
    ["xcrun", "simctl", "list", "runtimes", "-j"]))["runtimes"]
ios = [r for r in runtimes if "iOS" in r.get("name", "")]
def rt_key(r):
    m = re.search(r"(\d+)\.(\d+)", r.get("version", ""))
    return (int(m.group(1)), int(m.group(2))) if m else (-1, -1)
runtime = sorted(ios, key=rt_key, reverse=True)[0]["identifier"]
types = json.loads(subprocess.check_output(
    ["xcrun", "simctl", "list", "devicetypes", "-j"]))["devicetypes"]
iphones = [t for t in types if t["name"].startswith("iPhone")]
iphones.sort(key=lambda t: t["name"], reverse=True)
# Prefer a 15+ generation device for a modern screen; fall back to any.
pick = next((t for t in iphones
             if re.match(r"iPhone (1[5-9]|2\d)", t["name"])), iphones[0])
udid = subprocess.check_output(
    ["xcrun", "simctl", "create", name, pick["identifier"], runtime],
    text=True).strip()
print(udid, pick["name"])
PY
) || { echo "ERROR: could not create simulator"; exit 1; }
  DEVNAME="$DEVICE_NAME ($DEVNAME)"
fi

xcrun simctl shutdown "$UDID" 2>/dev/null || true
xcrun simctl erase "$UDID" || { echo "ERROR: simctl erase failed"; exit 1; }
xcrun simctl boot "$UDID" 2>/dev/null || true
# bootstatus -b can wedge even after the device reports Booted (seen
# locally on CoreSimulator after repeated erase/boot cycles) — bound it.
xcrun simctl bootstatus "$UDID" -b >/dev/null 2>&1 &
BSPID=$!
for _ in $(seq 1 48); do
  kill -0 "$BSPID" 2>/dev/null || break
  sleep 5
done
if kill -0 "$BSPID" 2>/dev/null; then
  echo "WARN: bootstatus timed out after 240s — device reports Booted, continuing"
  kill "$BSPID" 2>/dev/null || true
fi
wait "$BSPID" 2>/dev/null || true

[ -z "$DEVNAME" ] && DEVNAME="$DEVICE_NAME"
echo "simulator: $DEVNAME ($UDID)"
DEST="platform=iOS Simulator,id=$UDID"

# Leave no running simulator behind (AGENTS.md iOS Simulator rule).
# Scoped part always runs: shutdown our device + clones (delete fails
# on a Booted clone), prune our XCTestDevices data. Then the repo
# teardown handles the rest with --skip-process-kills — its global
# idb/diagnose kills would otherwise interrupt a parallel session's
# verification, while its CPU audit (AGENTS.md-mandated) still runs.
cleanup() {
  xcrun simctl shutdown "$UDID" 2>/dev/null || true
  clone_udids="$(xcrun simctl list devices -j 2>/dev/null | python3 -c '
import json,sys
suffix = " of " + sys.argv[1]
for devs in json.load(sys.stdin).get("devices", {}).values():
    for d in devs:
        n = d.get("name", "")
        if n.startswith("Clone") and n.endswith(suffix):
            print(d["udid"])
' "$DEVICE_NAME")"
  for cu in $clone_udids; do
    xcrun simctl shutdown "$cu" 2>/dev/null || true
    xcrun simctl delete "$cu" 2>/dev/null || true
  done
  # XCTestDevices dirs are keyed by the (clone) device uuid.
  for xctd in "$HOME"/Library/Developer*/XCTestDevices \
              "$HOME"/Library/Developer*/Xcode/XCTestDevices; do
    [ -d "$xctd" ] || continue
    for cu in $UDID $clone_udids; do
      rm -rf "$xctd/$cu" 2>/dev/null || true
    done
  done
  if [ -z "${CI:-}" ] && [ -x scripts/sim-teardown.sh ]; then
    scripts/sim-teardown.sh --udid "$UDID" --skip-process-kills || true
  fi
}
trap cleanup EXIT

# NOTE: no CODE_SIGNING_ALLOWED=NO — an unsigned app cannot write the
# Keychain, which silently breaks AuthManagerTests' refresh-persist path
# (SecItemAdd fails → refresh returns nil). Simulator builds ad-hoc sign
# ("Sign to Run Locally") without any certificate, so plain signing works
# on CI runners too.
echo "== build-for-testing"
xcodebuild build-for-testing \
  -project "$PROJECT" -scheme "$SCHEME" \
  -destination "$DEST" \
  -derivedDataPath "$DD" \
  -resultBundlePath "$RESULTS/build.xcresult" || exit 1

XCTESTRUN="$(ls "$DD"/Build/Products/*.xctestrun 2>/dev/null | head -1)"
if [ -z "$XCTESTRUN" ]; then
  echo "ERROR: no .xctestrun under $DD/Build/Products"
  exit 1
fi
echo "xctestrun: $XCTESTRUN"

# UITestSupport reads the TEST_MODE server URL from the test runner's
# environment (BOOKQ_UI_TEST_SERVER_URL) — inject the URL this script
# health-gated so the gate and the UI tests always agree. xctestrun
# files carry per-target EnvironmentVariables; patch BookqUITests'.
PB=/usr/libexec/PlistBuddy
INJECTED=0
for cfg in 0 1 2 3; do
  $PB -c "Print :TestConfigurations:$cfg" "$XCTESTRUN" >/dev/null 2>&1 || break
  for t in 0 1 2 3 4; do
    bn=$($PB -c "Print :TestConfigurations:$cfg:TestTargets:$t:BlueprintName" "$XCTESTRUN" 2>/dev/null) || break
    [ "$bn" = "BookqUITests" ] || continue
    envp=":TestConfigurations:$cfg:TestTargets:$t:EnvironmentVariables"
    # The dict itself is absent when the scheme defines no env vars —
    # create it first (the Add errors harmlessly when it exists).
    $PB -c "Add $envp dict" "$XCTESTRUN" 2>/dev/null || true
    if $PB -c "Add $envp:BOOKQ_UI_TEST_SERVER_URL string $SERVER_URL" "$XCTESTRUN" 2>/dev/null \
       || $PB -c "Set $envp:BOOKQ_UI_TEST_SERVER_URL $SERVER_URL" "$XCTESTRUN" 2>/dev/null; then
      INJECTED=1
      echo "injected BOOKQ_UI_TEST_SERVER_URL=$SERVER_URL -> $bn"
    fi
  done
done
if [ "$INJECTED" != 1 ]; then
  if [ "$SERVER_URL" != "http://127.0.0.1:8080" ]; then
    echo "ERROR: BOOKQ_UI_TEST_SERVER_URL injection failed — UI tests"
    echo "  would hit the default, not $SERVER_URL"
    exit 1
  fi
  echo "WARN: env injection failed; UI tests use default $SERVER_URL"
fi

# One test-without-building invocation runs BOTH bundles from the
# xctestrun (BookqTests + BookqUITests) in a single clone session —
# halves the clone-creation surface where the "device remained in
# Creating state" flake lives, and skips a second device setup.
# Parallel testing stays ON (the default): the per-run device clone is
# the test-isolation boundary — running directly on the booted device
# lets app/UserDefaults state leak between suites (ProgressSyncTests
# fail on leftovers from UI-test runs).
#
# Retry ONCE, and only when the log shows an infrastructure signature —
# real assertion failures must never get a second chance to pass.
BUNDLE="$RESULTS/tests.xcresult"
for attempt in 1 2; do
  log="$RESULTS/tests.attempt${attempt}.log"
  if xcodebuild test-without-building \
      -xctestrun "$XCTESTRUN" \
      -destination "$DEST" \
      -resultBundlePath "$BUNDLE" 2>&1 | tee "$log"; then
    exit 0
  fi
  if ! grep -qE "Failed to clone device|CoreSimulator|remained in Creating|Failed to install|Lost connection|timed out waiting|test runner crashed|encountered an error|died unexpectedly" "$log"; then
    echo "== test failures (not infra) — no retry"
    exit 1
  fi
  [ "$attempt" -eq 1 ] || break
  echo "== infra error on attempt $attempt — retrying once"
  # xcodebuild refuses to reuse an existing -resultBundlePath; the
  # attempt-1 bundle holds only the infra failure, so drop it.
  rm -rf "$BUNDLE"
  sleep 10
done
exit 1
