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
BENCH_CLASSES="dev.bookq.reader.baselineprofile.StartupBenchmark,dev.bookq.reader.baselineprofile.ReaderBenchmark,dev.bookq.reader.baselineprofile.BrowseBenchmark"

# The measured app: release-shaped `benchmark` variant (debug-signed).
ADB="${ANDROID_HOME}/platform-tools/adb"
"$ADB" devices | grep -q "device$" || {
    echo "no adb device — connect CP08-J1 first" >&2; exit 2; }

# Microphone-less, no dialog: clear any stale app state so the launch
# sequence is deterministic (TEST_MODE needs no login).
"$ADB" shell am force-stop dev.bookq.reader >/dev/null 2>&1 || true

if [[ "$GENERATE" == "1" ]]; then
    echo "== generating baseline profile on device =="
    # Restrict to the generator class — the benchmark classes fail fast
    # here because Partial(Require) has no installed profile yet.
    ./gradlew :app:generateReleaseBaselineProfile \
        "-Pandroid.testInstrumentationRunnerArguments.bookqServerUrl=$URL" \
        "-Pandroid.testInstrumentationRunnerArguments.class=dev.bookq.reader.baselineprofile.BaselineProfileGenerator" \
        --console=plain
fi

echo "== building + installing benchmark variants =="
./gradlew :app:installBenchmark :baselineprofile:installBenchmarkBenchmark \
    --console=plain -q

echo "== running macrobenchmark gates (compilation=$COMPILATION) =="
./gradlew :baselineprofile:connectedBenchmarkBenchmarkAndroidTest \
    "-Pandroid.testInstrumentationRunnerArguments.bookqServerUrl=$URL" \
    "-Pandroid.testInstrumentationRunnerArguments.class=$BENCH_CLASSES" \
    "-Pandroid.testInstrumentationRunnerArguments.compilation=$COMPILATION" \
    --console=plain -q || true   # threshold evaluation below decides

echo "== results =="
OUT_DIR="baselineprofile/build/outputs/connected_android_test_additional_output"
JSON=$(find "$OUT_DIR" -name "benchmarkData.json" -newermt "-2 hours" 2>/dev/null | sort)
if [[ -z "$JSON" ]]; then
    JSON=$(find "$OUT_DIR" -name "benchmarkData.json" 2>/dev/null | sort -r | head -6)
fi
[[ -n "$JSON" ]] || { echo "no benchmarkData.json produced" >&2; exit 2; }

python3 - "$JSON" <<'PY'
import json, sys

# Gate thresholds from issue #292 §5.
GATES = {
    # metric key                      (gate_ms, column)
    "bookq.pageTurnMs":    ("p50",  60.0, "tap turn median"),
    "bookq.readerOpenMs":  ("p50", 400.0, "reader open median"),
    "timeToFullDisplayMs": ("p50", 1500.0, "cold start TTFD"),
    "frameDurationCpuMs":  ("p99",  16.0, "fling p99 frame"),
}

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
        # keep the newest file's entry per test name (sorted by path ts)
        seen[name] = b.get("metrics", {})

worst = 0
for test, metrics in sorted(seen.items()):
    print(f"\n{test}")
    for mkey, m in sorted(metrics.items()):
        runs = m.get("runs") or []
        stats = {k: m.get(k) for k in
                 ("minimum","median","maximum","p50","p90","p95","p99")}
        line = "  %-24s %s" % (mkey, " ".join(
            f"{k}={v:.1f}" for k, v in stats.items() if v is not None))
        print(line)
        gate = GATES.get(mkey)
        if gate:
            stat, thr, label = gate
            val = m.get(stat)
            if val is not None and val > thr:
                fails.append(f"{test} {label}: {stat}={val:.1f}ms > {thr}ms")
                print(f"    !! GATE MISS {stat} {val:.1f} > {thr}")
    janky = metrics.get("jankyFrames") or {}
    if janky and janky.get("percent", 0) > 1.0:
        fails.append(f"{test} janky {janky['percent']:.2f}% > 1%")

print("\n" + ("FAIL" if fails else "PASS"))
for f in fails:
    print("  - " + f)
sys.exit(1 if fails else 0)
PY
