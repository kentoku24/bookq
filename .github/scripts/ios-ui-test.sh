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
# http://localhost:8080 directly (simulator shares the host network —
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

SERVER_URL="${1:-http://localhost:8080}"
PROJECT="ios/Bookq.xcodeproj"
SCHEME="Bookq"
DD="${DERIVED_DATA_PATH:-$HOME/Library/Developer/Xcode/DerivedData/Bookq-ci}"
RESULTS="${IOS_TEST_RESULTS:-ios/build/ios-test-results}"
mkdir -p "$RESULTS"

# --- server gate -------------------------------------------------------
# Host-side check is sufficient: the simulator shares the host network
# namespace for localhost, so if the host can reach it the sim can too.
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
xcrun simctl bootstatus "$UDID" -b

[ -z "$DEVNAME" ] && DEVNAME="$DEVICE_NAME"
echo "simulator: $DEVNAME ($UDID)"
DEST="platform=iOS Simulator,id=$UDID"

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
  if ! grep -qE "Failed to clone device|CoreSimulator|remained in Creating|Failed to install|Lost connection|timed out waiting" "$log"; then
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
