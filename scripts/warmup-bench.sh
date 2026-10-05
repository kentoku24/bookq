#!/usr/bin/env bash
# warmup-bench.sh — real-data warmup/startup benchmark in ONE command.
#
# Replaces the ad-hoc manual procedure (dev.sh up + docker logs + cache dumps,
# twice: envs warmup-measure/warmup-cache-diag/warmup-final) with a reusable script.
#
# USAGE:
#   scripts/warmup-bench.sh [ENV_NAME] [RESTARTS]
#     ENV_NAME  dev env name (default: warmup-bench)
#     RESTARTS  number of `docker restart` cycles after the initial boot (default: 2)
#   Example (quick self-proof):  scripts/warmup-bench.sh warmup-bench 1
#
# WHAT IT DOES:
#   1. Validates repo root + branch (ai/warmup-startup-speedup) + clean tracked
#      tree; aborts otherwise. Aborts if ENV already exists (stale env from a
#      crashed run must be `down`ed manually, never reused blindly).
#   2. Picks a free loopback port by probing (never assumes 8080 is free).
#   3. `./dev.sh up <env> --local --port <port>` with the REAL data mount
#      (no --fixture). Aborts if the mount source looks synthetic (fixture/tmp).
#   4. Per boot (initial + N restarts): waits for the `起動完了` log line with a
#      bounded timeout (polls `docker logs --since <boot-epoch>` so restarts
#      never match stale lines from a previous boot), captures the FULL
#      `metadata index rebuilt ...` + `内部ウォームアップ完了 ...` line pair,
#      dumps cache stats via `docker exec` python (entry count + r:/a: prefix
#      histogram + kinds histogram; cache path resolved via BOOKQ_CACHE_DIR
#      or the /tmp default).
#   5. Prints a summary table (boot x elapsed/scan/rebuild/import/walk/archive/
#      pdf + cache entries) to stdout AND appends the full transcript to
#      .omo/evidence/warmup-startup-speedup/bench-<date>.log.
#   6. ALWAYS `./dev.sh down <env>` on exit (trap EXIT, success or failure),
#      then proves the slot is freed via `./dev.sh list`.
#
# EXPECTED REAL-DATA SIGNATURE (post-fix, /Volumes/San500g/books -> /app/downloads):
#   cold ~12.6s, warm ~5.0s, cache 85/85 all `r:` (84 archive + 1 pdf).
#
# FAILS LOUD (no silent fallbacks) on: dirty tree, wrong branch, fixture mount,
# missing log lines after timeout, pool exhaustion, no free port.
#
# MUST NOT: bare `docker compose up`, ./prd.sh, --lan-allow — dev.sh --local only.

set -euo pipefail

EXPECTED_BRANCH="ai/warmup-startup-speedup"
ENV_NAME="${1:-warmup-bench}"
RESTARTS="${2:-2}"
READY_TIMEOUT=300

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
LOGDIR="$REPO_ROOT/.omo/evidence/warmup-startup-speedup"
LOGFILE="$LOGDIR/bench-$(date +%F-%H%M).log"
mkdir -p "$LOGDIR"
: > "$LOGFILE"
exec > >(tee -a "$LOGFILE") 2>&1

PORT=""
CONTAINER=""
# Per-boot metric rows (newline-separated, portable — no bash4 arrays):
# "label elapsed scan rebuild import walk archive pdf cache_entries"
METRIC_ROWS=""

die() { printf '❌ warmup-bench: %s\n' "$*" >&2; exit 1; }

cleanup() {
    set +e
    echo "--- teardown (trap EXIT) ---"
    if [ -n "$ENV_NAME" ] && [ -n "$REPO_ROOT" ]; then
        (cd "$REPO_ROOT" && ./dev.sh down "$ENV_NAME") || true
    fi
    if [ -n "$REPO_ROOT" ]; then
        (cd "$REPO_ROOT" && ./dev.sh list) || true
    fi
    echo "--- transcript appended to: $LOGFILE ---"
}
trap cleanup EXIT

# ---------- (a) preconditions ----------
[ -n "$REPO_ROOT" ] && [ -f "$REPO_ROOT/docker-compose.dev.yml" ] \
    || die "not at bookq repo root (run from the repo checkout)"
[ "$(git -C "$REPO_ROOT" branch --show-current)" = "$EXPECTED_BRANCH" ] \
    || die "wrong branch '$(git -C "$REPO_ROOT" branch --show-current)' — expected '$EXPECTED_BRANCH'"
git -C "$REPO_ROOT" diff --quiet && git -C "$REPO_ROOT" diff --cached --quiet \
    || die "dirty tracked tree — commit or stash first"
case "$RESTARTS" in ''|*[!0-9]*) die "RESTARTS must be a non-negative integer (got '$RESTARTS')" ;; esac
if docker ps -a --filter "label=com.bookq.name=$ENV_NAME" --format '{{.Names}}' 2>/dev/null | grep -q .; then
    die "stale env '$ENV_NAME' already exists (previous crashed run?) — run './dev.sh down $ENV_NAME' first, refusing to reuse blindly"
fi
echo "✅ preconditions: branch=$EXPECTED_BRANCH tree=clean env=$ENV_NAME restarts=$RESTARTS"

# ---------- (b) free loopback port ----------
pick_port() {
    local p published
    published="$(docker ps --format '{{.Ports}}' 2>/dev/null || true)"
    for p in $(seq 8090 8199); do
        case "$published" in *":$p->"*) continue ;; esac
        if (echo > /dev/tcp/127.0.0.1/"$p") 2>/dev/null; then
            continue  # something is listening
        fi
        printf '%s' "$p"
        return 0
    done
    return 1
}
PORT="$(pick_port)" || die "no free loopback port in 8090-8199"
echo "✅ port: $PORT"

# ---------- (c) boot with REAL data mount ----------
cd "$REPO_ROOT"
BOOT_EPOCH="$(date +%s)"
if ! ./dev.sh up "$ENV_NAME" --local --port "$PORT"; then
    die "dev.sh up failed — check output above for 'pool exhausted (10/10)' or build errors"
fi
resolve_container() {
    docker ps --filter "label=com.bookq.name=$ENV_NAME" --format '{{.Names}}' 2>/dev/null \
        | grep -i viewer | head -1
}
CONTAINER="$(resolve_container)"
[ -n "$CONTAINER" ] || die "viewer container for env '$ENV_NAME' not found after up"
echo "✅ container: $CONTAINER"

MOUNTS="$(docker inspect "$CONTAINER" --format '{{json .Mounts}}')"
echo "mounts: $MOUNTS"
case "$MOUNTS" in
    *"/app/downloads"*) : ;;
    *) die "no /app/downloads mount found: $MOUNTS" ;;
esac
case "$MOUNTS" in
    *fixture*|*/tmp/bookq-fixture-*)
        die "mount source looks SYNTHETIC (fixture path): $MOUNTS — real data required, refusing to benchmark fixtures" ;;
esac
echo "✅ real-data mount confirmed (not a fixture path)"

# ---------- helpers ----------
wait_ready() {
    local epoch="$1" i logs
    for i in $(seq 1 "$READY_TIMEOUT"); do
        logs="$(docker logs --since "$epoch" "$CONTAINER" 2>&1 || true)"
        case "$logs" in
            *"起動完了"*) return 0 ;;
        esac
        sleep 1
    done
    die "missing '起動完了' log line after ${READY_TIMEOUT}s (container=$CONTAINER) — startup hung or crashed; see 'docker logs $CONTAINER'"
}

field() { # field <line> <name> — extracts `name=<int>` value or 0
    printf '%s' "$1" | grep -o "$2=[0-9]*" | head -1 | cut -d= -f2 || true
}

measure_boot() { # measure_boot <label> <boot-epoch>
    local label="$1" epoch="$2" logs rebuild warmup cache_dump
    local elapsed scan rebuild_ms import walk archive pdf entries
    wait_ready "$epoch"
    logs="$(docker logs --since "$epoch" "$CONTAINER" 2>&1)"
    rebuild="$(printf '%s\n' "$logs" | grep "metadata index rebuilt" | tail -1 || true)"
    warmup="$(printf '%s\n' "$logs" | grep "内部ウォームアップ完了" | tail -1 || true)"
    [ -n "$rebuild" ] || die "boot $label: missing 'metadata index rebuilt' line after 起動完了"
    [ -n "$warmup" ] || die "boot $label: missing '内部ウォームアップ完了' line after 起動完了"
    echo "--- boot $label: FULL line pair ---"
    printf '%s\n' "$rebuild"
    printf '%s\n' "$warmup"
    elapsed="$(field "$rebuild" elapsed_ms)"
    scan="$(field "$rebuild" scan_ms)"
    rebuild_ms="$(field "$rebuild" rebuild_ms)"
    import="$(field "$warmup" import_ms)"
    walk="$(field "$rebuild" walk_ms)"
    archive="$(field "$rebuild" archive_ms)"
    pdf="$(field "$rebuild" pdf_ms)"
    echo "--- boot $label: cache dump ---"
    cache_dump="$(docker exec "$CONTAINER" python3 -c '
import json, os
from collections import Counter
base = os.environ.get("BOOKQ_CACHE_DIR") or "/tmp/bookq-cache"
p = os.path.join(base, "warmup-cache-v1.json")
print("cache_file: " + p)
with open(p) as f:
    data = json.load(f)
ents = data.get("entries", data) if isinstance(data, dict) else data
keys = list(ents.keys()) if isinstance(ents, dict) else list(ents)
pref = Counter(("r:" if k.startswith("r:") else ("a:" if k.startswith("a:") else "?")) for k in keys)
kinds = Counter((v.get("kind", "?") if isinstance(v, dict) else "?") for v in (ents.values() if isinstance(ents, dict) else []))
print("entries: %d" % len(keys))
print("prefix: %s" % dict(pref))
print("kinds: %s" % dict(kinds))
' 2>&1)" || die "boot $label: cache dump failed — $cache_dump"
    printf '%s\n' "$cache_dump"
    entries="$(printf '%s' "$cache_dump" | grep -o 'entries: [0-9]*' | head -1 | grep -o '[0-9]*')"
    [ -n "$entries" ] || die "boot $label: could not parse cache entry count"
    METRIC_ROWS="${METRIC_ROWS}${label} ${elapsed:-?} ${scan:-?} ${rebuild_ms:-?} ${import:-?} ${walk:-?} ${archive:-?} ${pdf:-?} ${entries}
"
}

# ---------- (d)-(g) initial boot + N restarts ----------
measure_boot "0-cold" "$BOOT_EPOCH"
i=1
while [ "$i" -le "$RESTARTS" ]; do
    echo "--- docker restart $i/$RESTARTS ---"
    docker restart "$CONTAINER" > /dev/null
    measure_boot "$i-warm" "$(date +%s)"
    i=$((i + 1))
done

# ---------- (h) summary table ----------
echo ""
echo "================ warmup-bench SUMMARY (env=$ENV_NAME port=$PORT) ================"
printf '%-8s %8s %8s %8s %8s %8s %8s %8s %7s\n' boot elapsed scan rebuild import walk archive pdf cache
printf '%s' "$METRIC_ROWS" | while IFS= read -r row; do
    [ -n "$row" ] || continue
    # shellcheck disable=SC2086
    set -- $row
    printf '%-8s %8s %8s %8s %8s %8s %8s %8s %7s\n' "$1" "${2:-?}" "${3:-?}" "${4:-?}" "${5:-?}" "${6:-?}" "${7:-?}" "${8:-?}" "${9:-?}"
done
echo "(times in ms; cache = warmup-cache-v1.json entry count)"
echo "=================================================================="

# (i) teardown runs via trap EXIT (down + list receipts above/below this line).
