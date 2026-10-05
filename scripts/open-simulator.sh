#!/bin/bash
set -euo pipefail
# Usage: ./scripts/open-simulator.sh https://dev-xxx.<your-tailnet>.ts.net
#       ./scripts/open-simulator.sh http://127.0.0.1:8080   # ./dev.sh up --local の環境
#       SIMULATOR_DEVICE="iPad Pro 13-inch (M4) (16GB)" ./scripts/open-simulator.sh https://dev-xxx.<your-tailnet>.ts.net
#       ./scripts/open-simulator.sh check | list | screenshot [path] | shutdown | verify | tap <x> <y> [--udid <udid>] | swipe <x1> <y1> <x2> <y2> [--duration <s>] [--udid <udid>]
# iOS Simulator の Safari で URL を開くヘルパー
# Supports both bare https:// URL and subcommands for backward compatibility

DEVICE=${SIMULATOR_DEVICE:-"iPhone 17"}
[ -z "$DEVICE" ] && DEVICE="iPhone 17"

usage() {
  echo "Usage: $0 <dev-url> | $0 {check|list|screenshot [path]|shutdown|verify|tap <x> <y> [--udid <udid>]|swipe <x1> <y1> <x2> <y2> [--duration <s>] [--udid <udid>]}" >&2
  echo "  <dev-url>              https://... または http://127.0.0.1[:port] を Simulator の Safari で開く (?touch=1 を自動付与、recordVideoで赤丸表示)" >&2
  echo "  check                  Xcode / simctl / Runtime 前提チェック (idb 有無も表示)" >&2
  echo "  list                   利用可能な Simulator デバイス一覧" >&2
  echo "  screenshot [path]      booted Simulator のスクリーンショット (default: /tmp/sim.png)" >&2
  echo "  shutdown               booted Simulator を shutdown" >&2
  echo "  verify                 check → boot → open https://example.com → screenshot の一連検証" >&2
  echo "  tap <x> <y> [--udid <udid>]  座標タップ (idb ui tap, --udid省略時はbooted UDIDを自動取得)" >&2
  echo "  swipe <x1> <y1> <x2> <y2> [--duration <s>] [--udid <udid>]  スワイプ (idb ui swipe, ?touch=1 前提で赤丸が追従)" >&2
}

linux_skip() {
  echo "⚠️  xcrun not found — skipping (Linux CI)" >&2
  exit 0
}

cmd_check() {
  if ! command -v xcrun >/dev/null 2>&1; then
    linux_skip
  fi
  local xcrun_path
  xcrun_path=$(xcrun --find simctl 2>/dev/null || true)
  if [ -z "$xcrun_path" ]; then
    echo "Error: xcrun --find simctl failed. Install Xcode and run 'xcode-select -p'." >&2
    echo "  → Run: xcode-select --install" >&2
    echo "  → Or: Xcode > Settings > Platforms で iOS Runtime をインストール" >&2
    exit 2
  fi
  echo "$xcrun_path" >&2
  local xcode_path
  if ! xcode_path=$(xcode-select -p 2>/dev/null); then
    echo "Error: xcode-select -p failed. Run 'xcode-select --install'." >&2
    exit 2
  fi
  echo "Xcode path: $xcode_path" >&2
  if ! xcrun simctl list runtimes 2>/dev/null | grep -q "iOS"; then
    echo "Error: No iOS Runtime found. Install via Xcode > Settings > Platforms." >&2
    echo "  → Run: xcode-select --install" >&2
    echo "  → Or open Xcode > Settings > Platforms > iOS" >&2
    exit 2
  fi
  echo "iOS Runtime: available" >&2
  if command -v idb >/dev/null 2>&1; then
    echo "idb: available ($(command -v idb))" >&2
  else
    echo "idb: not found (tap requires: brew tap facebook/fb && brew install idb-companion && pipx install fb-idb)" >&2
  fi
}

cmd_list() {
  if ! command -v xcrun >/dev/null 2>&1; then
    linux_skip
  fi
  xcrun simctl list devices available
}

cmd_screenshot() {
  if ! command -v xcrun >/dev/null 2>&1; then
    linux_skip
  fi
  local dest="${1:-/tmp/sim.png}"
  xcrun simctl io booted screenshot "$dest"
  echo "Screenshot saved to $dest"
}

cmd_shutdown() {
  if ! command -v xcrun >/dev/null 2>&1; then
    linux_skip
  fi
  xcrun simctl shutdown booted
  echo "Simulator shutdown (booted)"
}

cmd_tap() {
  if ! command -v xcrun >/dev/null 2>&1; then
    linux_skip
  fi
  if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ] || [ "${1:-}" = "help" ] || [ $# -eq 0 ]; then
    echo "Usage: $0 tap <x> <y> [--udid <udid>]" >&2
    echo "  <x> <y>              tap coordinates (e.g. 200 400)" >&2
    echo "  --udid <udid>        target UDID (default: booted device auto-detected)" >&2
    echo "  Example: $0 tap 200 400" >&2
    echo "           $0 tap 200 400 --udid <UDID>" >&2
    exit 0
  fi
  if [ $# -lt 2 ]; then
    echo "Error: tap requires <x> <y> (got: $*)" >&2
    echo "Usage: $0 tap <x> <y> [--udid <udid>]" >&2
    exit 1
  fi
  local x="$1" y="$2"
  shift 2
  local udid=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --udid)
        if [ $# -lt 2 ] || [[ "$2" == --* ]]; then
          echo "Error: --udid requires a value" >&2
          exit 1
        fi
        udid="$2"
        shift 2
        ;;
      --help|-h|help)
        echo "Usage: $0 tap <x> <y> [--udid <udid>]" >&2
        exit 0
        ;;
      *)
        echo "Error: unknown tap option: $1" >&2
        echo "Usage: $0 tap <x> <y> [--udid <udid>]" >&2
        exit 1
        ;;
    esac
  done
  if ! [[ "$x" =~ ^[0-9]+$ ]] || ! [[ "$y" =~ ^[0-9]+$ ]]; then
    echo "Error: tap coordinates must be integers (got: $x $y)" >&2
    exit 1
  fi
  if ! command -v idb >/dev/null 2>&1; then
    echo "idb not found: brew tap facebook/fb && brew install idb-companion && pipx install fb-idb" >&2
    exit 2
  fi
  if [ -z "$udid" ]; then
    if command -v python3 >/dev/null 2>&1; then
      udid=$(xcrun simctl list devices booted -j 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(next((dev['udid'] for runtimes in d.get('devices',{}).values() for dev in runtimes if dev.get('state')=='Booted'), ''))" 2>/dev/null || true)
    fi
    if [ -z "$udid" ]; then
      udid=$(xcrun simctl list devices booted 2>/dev/null | grep -oE '[0-9A-F-]{36}' | head -n1 || true)
    fi
    if [ -z "$udid" ] && command -v idb >/dev/null 2>&1; then
      udid=$(idb list-targets --json 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); targets=d if isinstance(d,list) else d.get('targets',[]); print(next((t.get('udid','') for t in targets if t.get('state')=='Booted' or t.get('isAvailable',False)), ''))" 2>/dev/null || true)
    fi
    if [ -z "$udid" ]; then
      echo "Error: no booted Simulator found. Boot one via 'xcrun simctl boot \"$DEVICE\"' or specify --udid." >&2
      exit 1
    fi
  fi
  idb ui tap "$x" "$y" --udid "$udid"
  echo "Tapped ($x, $y) on $udid via idb (use ?touch=1 URL for red dot in recordVideo)"
}

cmd_swipe() {
  if ! command -v xcrun >/dev/null 2>&1; then
    linux_skip
  fi
  if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ] || [ "${1:-}" = "help" ] || [ $# -lt 4 ]; then
    echo "Usage: $0 swipe <x1> <y1> <x2> <y2> [--duration <seconds>] [--udid <udid>]" >&2
    echo "  <x1> <y1> <x2> <y2>   swipe from/to coordinates" >&2
    echo "  --duration <s>       duration in seconds (default: 0.5)" >&2
    echo "  --udid <udid>        target UDID (default: booted device auto-detected)" >&2
    echo "  Example: $0 swipe 200 400 200 100 --duration 0.3" >&2
    [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ] || [ "${1:-}" = "help" ] && exit 0
    echo "Error: swipe requires <x1> <y1> <x2> <y2>" >&2
    exit 1
  fi
  local x1="$1" y1="$2" x2="$3" y2="$4"
  shift 4
  local duration="0.5"
  local udid=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --duration)
        if [ $# -lt 2 ] || [[ "$2" == --* ]]; then echo "Error: --duration requires a value" >&2; exit 1; fi
        duration="$2"; shift 2 ;;
      --udid)
        if [ $# -lt 2 ] || [[ "$2" == --* ]]; then echo "Error: --udid requires a value" >&2; exit 1; fi
        udid="$2"; shift 2 ;;
      --help|-h|help) echo "Usage: $0 swipe <x1> <y1> <x2> <y2> [--duration <s>] [--udid <udid>]" >&2; exit 0 ;;
      *) echo "Error: unknown swipe option: $1" >&2; echo "Usage: $0 swipe <x1> <y1> <x2> <y2> [--duration <s>] [--udid <udid>]" >&2; exit 1 ;;
    esac
  done
  for v in "$x1" "$y1" "$x2" "$y2"; do
    if ! [[ "$v" =~ ^[0-9]+$ ]]; then echo "Error: swipe coordinates must be integers (got: $x1 $y1 $x2 $y2)" >&2; exit 1; fi
  done
  if ! [[ "$duration" =~ ^[0-9]*\.?[0-9]+$ ]]; then echo "Error: --duration must be numeric (got: $duration)" >&2; exit 1; fi
  if ! command -v idb >/dev/null 2>&1; then
    echo "idb not found: brew tap facebook/fb && brew install idb-companion && pipx install fb-idb" >&2
    exit 2
  fi
  if [ -z "$udid" ]; then
    if command -v python3 >/dev/null 2>&1; then
      udid=$(xcrun simctl list devices booted -j 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(next((dev['udid'] for runtimes in d.get('devices',{}).values() for dev in runtimes if dev.get('state')=='Booted'), ''))" 2>/dev/null || true)
    fi
    if [ -z "$udid" ]; then
      udid=$(xcrun simctl list devices booted 2>/dev/null | grep -oE '[0-9A-F-]{36}' | head -n1 || true)
    fi
    if [ -z "$udid" ] && command -v idb >/dev/null 2>&1; then
      udid=$(idb list-targets --json 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); targets=d if isinstance(d,list) else d.get('targets',[]); print(next((t.get('udid','') for t in targets if t.get('state')=='Booted' or t.get('isAvailable',False)), ''))" 2>/dev/null || true)
    fi
    if [ -z "$udid" ]; then echo "Error: no booted Simulator found. Boot one via 'xcrun simctl boot \"$DEVICE\"' or specify --udid." >&2; exit 1; fi
  fi
  idb ui swipe "$x1" "$y1" "$x2" "$y2" --duration "$duration" --udid "$udid"
  echo "Swiped ($x1,$y1)->($x2,$y2) duration $duration on $udid via idb (use ?touch=1 URL for red dot in recordVideo)"
}

with_touch_url() {
  local u="$1"
  if [[ "$u" == *"touch=1"* ]]; then echo "$u"; return; fi
  if [[ "$u" == *"#"* ]]; then
    local base="${u%%#*}"
    local hash="#${u#*#}"
    if [[ "$base" == *"?"* ]]; then echo "${base}&touch=1${hash}"
    else echo "${base}?touch=1${hash}"
    fi
  else
    if [[ "$u" == *"?"* ]]; then echo "${u}&touch=1"
    else echo "${u}?touch=1"
    fi
  fi
}

open_url() {
  local url="$1"
  url="$(with_touch_url "$url")"
  # https:// は tailnet の dev-XX、http://127.0.0.1|localhost は ./dev.sh up --local。
  # Simulator は host network を共有するので loopback にそのまま到達でき、
  # http://127.0.0.1 は Secure Context なので Service Worker も動く。
  # 素の http://<lan-ip> は Secure Context でないため拒否する（README: Secure Context 注意）。
  if [[ ! "$url" =~ ^https://[^[:space:]]+$ && ! "$url" =~ ^http://(127\.0\.0\.1|localhost)(:[0-9]+)?([/?#][^[:space:]]*)?$ ]]; then
    echo "Error: URL must be https://... or http://127.0.0.1[:port] (got: $url)" >&2
    exit 1
  fi
  if ! command -v xcrun >/dev/null 2>&1; then
    linux_skip
  fi
  # 既boot時は boot をskip（失敗は無視）
  xcrun simctl boot "$DEVICE" 2>/dev/null || true
  open -a Simulator 2>/dev/null || true
  # Simulator の起動待ち — sleep 2 固定ではなく bootstatus でポーリング（最大30秒）
  local ready=0
  for _ in $(seq 1 30); do
    if xcrun simctl bootstatus "$DEVICE" -b >/dev/null 2>&1; then ready=1; break; fi
    # booted エイリアスでも確認（DEVICE名解決が不安定な環境向けフォールバック）
    if xcrun simctl bootstatus booted -b >/dev/null 2>&1; then ready=1; break; fi
    sleep 1
  done
  if [ "$ready" -eq 0 ]; then
    echo "Error: timed out waiting for Simulator ($DEVICE) to boot (30s). Check 'xcrun simctl list | grep Booted'." >&2
    exit 1
  fi
  if ! xcrun simctl openurl booted "$url"; then
    echo "Error: failed to open $url in Simulator ($DEVICE). Check 'xcrun simctl list | grep Booted' if multiple simulators are booted." >&2
    exit 1
  fi
  # 複数台 boot 時の警告（booted はいずれか1台に解決される）
  local booted_count
  booted_count=$(xcrun simctl list devices booted 2>/dev/null | grep -c "Booted" || true)
  if [ "${booted_count:-0}" -gt 1 ]; then
    echo "Note: multiple simulators are booted ($booted_count). 'booted' opened on one of them; check 'xcrun simctl list | grep Booted'." >&2
  fi
  echo "Opened $url in Simulator ($DEVICE)"
}

cmd_verify() {
  if ! command -v xcrun >/dev/null 2>&1; then
    linux_skip
  fi
  echo "== check ==" >&2
  cmd_check >&2
  echo "== boot & open ==" >&2
  # verify は example.com で到達性を証明（tailnet不要）
  open_url "https://example.com"
  echo "== screenshot ==" >&2
  mkdir -p .omo/evidence 2>/dev/null || true
  local dest=".omo/evidence/sim-verify.png"
  if xcrun simctl io booted screenshot "$dest" 2>/dev/null; then
    echo "Screenshot saved to $dest" >&2
  else
    echo "Warning: screenshot failed (Simulator may not be fully ready)" >&2
  fi
  echo "✅ Simulator ready"
}

# ---- dispatch ----
if [ $# -eq 0 ]; then
  usage
  exit 1
fi

case "$1" in
  check)
    cmd_check
    ;;
  list)
    cmd_list
    ;;
  screenshot)
    cmd_screenshot "${2:-}"
    ;;
  shutdown)
    cmd_shutdown
    ;;
  verify)
    cmd_verify
    ;;
  tap)
    shift
    cmd_tap "$@"
    ;;
  swipe)
    shift
    cmd_swipe "$@"
    ;;
  -h|--help|help)
    usage
    exit 0
    ;;
  http://*|https://*)
    # loopback http か https のみ通す（判定は open_url 側）
    open_url "$1"
    ;;
  *)
    # 後方互換: 引数がURLっぽくなければUsageを出す（check以外はデフォルト動作）
    echo "Error: unknown subcommand or invalid URL: $1" >&2
    usage
    exit 1
    ;;
esac
