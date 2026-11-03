#!/usr/bin/env bash
# perf-gate.sh — M10 §5 performance gates on CP08-J1 (issue #302).
#
# Usage:
#   ./scripts/perf-gate.sh <dev-url> [--generate] [--compilation <mode>]
#
#   <dev-url>     TEST_MODE dev environment (e.g. https://dev-01…ts.net)
#   --generate    run :app:generateReleaseBaselineProfile first
#                 (re-creates app/src/release/generated/baselineProfiles)
#   --compilation none|full   override the default Partial(Require)
#
# Runs every Macrobenchmark gate, prints the results table, and exits
# non-zero when any threshold is missed. Gates (issue #292 §5):
#   tap turn median < 60ms · fling janky < 1% & p99 < 16ms ·
#   cold start TTFD < 1.5s · reader open < 400ms
set -euo pipefail

URL="${1:?usage: perf-gate.sh <dev-url> [--generate] [--compilation mode]}"
GENERATE=0
COMPILATION="partial"
shift || true
while [[ $# -gt 0 ]]; do
    case "$1" in
        --generate) GENERATE=1 ;;
        --compilation) COMPILATION="${2:?--compilation needs a mode}"; shift ;;
        *) echo "unknown flag: $1" >&2; exit 2 ;;
    esac
    shift
done

cd "$(dirname "$0")/.."
export JAVA_HOME="${JAVA_HOME:-$(brew --prefix openjdk@21)}"
export ANDROID_HOME="${ANDROID_HOME:-$HOME/Library/Android/sdk}"

BENCH_PKG="dev.bookq.reader.baselineprofile"
BENCH_CLASSES="StartupBenchmark ReaderBenchmark BrowseBenchmark"

# The measured app: release-shaped `benchmark` variant (debug-signed).
ADB="${ANDROID_HOME}/platform-tools/adb"
"$ADB" devices | grep -q "device$" || {
    echo "no adb device — connect CP08-J1 first" >&2; exit 2; }

# With multiple devices attached the connected* task runs on ALL of them
# and the result-collection step would pick a device at random. Require
# the caller to pin ANDROID_SERIAL (or attach exactly one device).
DEV_COUNT=$("$ADB" devices | grep -c "device$")
if [[ -z "${ANDROID_SERIAL:-}" ]]; then
    if [[ "$DEV_COUNT" -gt 1 ]]; then
        echo "multiple devices attached — export ANDROID_SERIAL=<serial>" >&2
        "$ADB" devices >&2
        exit 2
    fi
else
    export ANDROID_SERIAL
fi
echo "gate device: ${ANDROID_SERIAL:-$("$ADB" devices | awk '/device$/{print $1}')}"

# Microphone-less, no dialog: clear any stale app state so the launch
# sequence is deterministic (TEST_MODE needs no login). The debug
# package is force-stopped too — a stale .debug reader window once held
# a11y/input focus while the benchmark app rendered underneath, making
# injected taps die silently on CP08-J1.
"$ADB" shell am force-stop dev.bookq.reader >/dev/null 2>&1 || true
"$ADB" shell am force-stop dev.bookq.reader.debug >/dev/null 2>&1 || true

if [[ "$GENERATE" == "1" ]]; then
    echo "== generating baseline profile on device =="
    # Restrict to the generator class — the benchmark classes fail fast
    # here because Partial(Require) has no installed profile yet.
    ./gradlew :app:generateReleaseBaselineProfile \
        "-Pandroid.testInstrumentationRunnerArguments.bookqServerUrl=$URL" \
        "-Pandroid.testInstrumentationRunnerArguments.class=dev.bookq.reader.baselineprofile.BaselineProfileGenerator" \
        --console=plain
    # The plugin only merges src/<variant>/baselineProfiles — generated
    # output lands under src/release/generated, so mirror it into the
    # measurement variants (committed so plain assemble* also carries
    # the profile).
    for v in benchmarkRelease nonMinifiedRelease; do
        mkdir -p "app/src/$v/baselineProfiles"
        for p in baseline-prof.txt startup-prof.txt; do
            cp "app/src/release/generated/baselineProfiles/$p" \
               "app/src/$v/baselineProfiles/" 2>/dev/null || true
        done
    done
fi

echo "== building + installing benchmark variants =="
# `benchmarkRelease` is the plugin-generated measurement variant —
# release shape (R8) + benchmark instrumented + debug-signed. It is
# what the connected*Release test runs measure.
./gradlew :app:installBenchmarkRelease \
    :baselineprofile:installBenchmarkRelease \
    --console=plain -q

echo "== running macrobenchmark gates (compilation=$COMPILATION) =="
# One class per invocation: a comma list silently runs only the first
# class on this AGP, and every run overwrites the same
# <pkg>-benchmarkData.json — stash each under its own name.
OUT_DIR="baselineprofile/build/outputs/connected_android_test_additional_output"
RESULTS_DIR="baselineprofile/build/perf-gate-results"
rm -rf "$RESULTS_DIR" && mkdir -p "$RESULTS_DIR"
for cls in $BENCH_CLASSES; do
    echo "--- $cls ---"
    STAMP=$(mktemp)
    ./gradlew :baselineprofile:connectedBenchmarkReleaseAndroidTest \
        "-Pandroid.testInstrumentationRunnerArguments.bookqServerUrl=$URL" \
        "-Pandroid.testInstrumentationRunnerArguments.class=$BENCH_PKG.$cls" \
        "-Pandroid.testInstrumentationRunnerArguments.compilation=$COMPILATION" \
        --console=plain -q || true   # threshold evaluation below decides
    find "$OUT_DIR" -name "*benchmarkData.json" -newer "$STAMP" \
        -exec cp {} "$RESULTS_DIR/$cls.json" \; 2>/dev/null || true
    rm -f "$STAMP"
done

echo "== results =="
mapfile -t JSON < <(find "$RESULTS_DIR" -name "*.json" | sort)
[[ ${#JSON[@]} -gt 0 ]] || { echo "no benchmarkData.json produced" >&2; exit 2; }

python3 - "${JSON[@]}" <<'PY'
import json, sys

# Gate thresholds from issue #292 §5.
GATES = {
    # metric key                      (stat, threshold_ms, label)
    "bookq.pageTurnMs":    ("p50",  60.0, "tap turn median"),
    "bookq.readerOpenMs":  ("p50", 400.0, "reader open median"),
    "timeToFullDisplayMs": ("p50", 1500.0, "cold start TTFD"),
    "frameDurationCpuMs":  ("p99",  16.0, "fling p99 frame"),
}

def stat(m, name):
    # `metrics` uses minimum/median/maximum, `sampledMetrics` uses
    # P50/P90/P95/P99 — normalise both spellings.
    for k in (name, name.upper()):
        if m.get(k) is not None:
            return m[k]
    if name == "p50":
        return m.get("median")
    return None

fails = []
seen = {}
for path in sys.argv[1:]:
    try:
        data = json.load(open(path))
    except Exception as e:
        print(f"unreadable {path}: {e}")
        continue
    for b in data.get("benchmarks", []):
        name = f"{b.get('className','?').split('.')[-1]}_{b.get('name','?')}"
        merged = dict(b.get("metrics") or {})
        merged.update(b.get("sampledMetrics") or {})
        seen[name] = merged

for test, metrics in sorted(seen.items()):
    print(f"\n{test}")
    for mkey, m in sorted(metrics.items()):
        if not isinstance(m, dict):
            continue
        parts = []
        for name in ("p50", "p90", "p95", "p99"):
            v = stat(m, name)
            if v is not None:
                parts.append(f"{name}={v:.1f}")
        for k in ("minimum", "maximum"):
            if m.get(k) is not None:
                parts.append(f"{k}={m[k]:.1f}")
        print("  %-24s %s" % (mkey, " ".join(parts)))
        gate = GATES.get(mkey)
        if gate:
            s, thr, label = gate
            val = stat(m, s)
            if val is not None and val > thr:
                fails.append(f"{test} {label}: {s}={val:.1f}ms > {thr}ms")
                print(f"    !! GATE MISS {s} {val:.1f} > {thr}")
        if mkey == "frameOverrunMs":
            # Jank% = frames past deadline (overrun > 0) / total —
            # derived because FrameTimingMetric emits no jankyFrames.
            runs = m.get("runs") or []
            flat = [f for it in runs for f in (it if isinstance(it, list) else [it])]
            if flat:
                pct = 100.0 * sum(1 for f in flat if f > 0) / len(flat)
                print(f"  {'derived janky%':<24} {pct:.2f}%")
                if pct > 1.0:
                    fails.append(f"{test} janky frames: {pct:.2f}% > 1%")

print("\n" + ("FAIL" if fails else "PASS"))
for f in fails:
    print("  - " + f)
sys.exit(1 if fails else 0)
PY
