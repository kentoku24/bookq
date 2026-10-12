#!/usr/bin/env bash
# sim-gesture-battery — iOS Simulator 上でビューアのページめくり系ジェスチャを
# 一通りの分類 (タップ / ゆるドリフト / スワイプ) で叩き、最後にスクショを残す。
#
# Usage:
#   ./scripts/sim-gesture-battery.sh <viewer-url> [--screenshot PATH] [--udid UDID]
#     [--udid 省略時は booted から自動取得]
#   例:
#     ./scripts/sim-gesture-battery.sh "http://127.0.0.1:8094/?perf=1#/view/rawmanga/…/ch-1.1?p=0"
#
# 何を検証するか (app.js touchend の3経路):
#   1. 静止タップ ×2        → ターン数 +2
#   2. 12px ドリフト x2     → ターン数 +2 (F2: 即時タップ経路)
#   3. 22px fast /慢性 x1  → ターン数 +1
#   4. 22px slow ×1        → ターン数 +1
#   5. 40px slow スワイプ ×1 → ターン数 +1 (コミットアニメ経路)
#
# 判断は スクショ 中の counter と ?perf=1 HUD (`next(L) #N ... img:ok`) で行う。
# 詳細な方法論と CLI 裏付けは .claude/skills/ios-simulator/references/tap-driving.md。

set -euo pipefail

usage() {
    echo "Usage: $0 <viewer-url> [--screenshot PATH] [--udid UDID]" >&2
    echo "  <viewer-url>            開くビューア URL (?perf=1 推奨: HUD 有効)" >&2
    echo "  --screenshot PATH       最終スクショの保存先 (既定: /tmp/sim-battery.png)" >&2
    echo "  --udid UDID             tap/swipe を流す Simulator UDID (既定: booted)" >&2
    exit 1
}

[ $# -ge 1 ] || usage
URL="$1"; shift

SHOT="/tmp/sim-battery.png"
TARGET_UDID=""
while [ $# -gt 0 ]; do
    case "$1" in
        --screenshot)
            [ $# -ge 2 ] || usage
            SHOT="$2"; shift 2 ;;
        --udid)
            [ $# -ge 2 ] || usage
            TARGET_UDID="$2"; shift 2 ;;
        *) usage ;;
    esac
done

command -v idb >/dev/null 2>&1 || {
    echo "Error: idb がありません。brew tap facebook/fb && brew install idb-companion && pipx install fb-idb" >&2
    exit 1
}

if [ -z "$TARGET_UDID" ]; then
    TARGET_UDID=$(xcrun simctl list devices booted -j 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(next((dev['udid'] for runtimes in d.get('devices',{}).values() for dev in runtimes if dev.get('state')=='Booted'), ''))" 2>/dev/null || true)
    [ -n "$TARGET_UDID" ] || {
        echo "Error: booted Simulator がありません。xcrun simctl boot \"iPhone 17\" を先に実行。" >&2
        exit 1
    }
fi

tap()  { idb ui tap "$1" "$2" --duration "${3:-0.15}" --udid "$TARGET_UDID" >/dev/null 2>&1; }
swipe(){ idb ui swipe "$1" "$2" "$3" "$4" --duration "${5:-0.15}" --udid "$TARGET_UDID" >/dev/null 2>&1; }
wait_(){ sleep "${1:-1.2}"; }

echo "→ 開く: $URL (r= でキャッシュ退避済み)"
./scripts/open-simulator.sh "$URL" >/dev/null 2>&1 || true
sleep 6   # 描画待ち (openurl コールドブート分)

echo "→ 静止タップ ×2"
tap 78 500; wait_; tap 78 500; wait_
echo "→ 12px ドリフト ×2 (F2: ゆるタップ)"
swipe 78 500 90 502 0.15; wait_; swipe 78 500 90 502 0.15; wait_
echo "→ 22px fast (0.05s)"
swipe 78 500 100 502 0.05; wait_
echo "→ 22px slow (0.25s)"
swipe 78 500 100 502 0.25; wait_
echo "→ 40px slow スワイプ (0.4s, コミット)"
swipe 60 500 100 502 0.4; wait_ 1.4

./scripts/open-simulator.sh screenshot "$SHOT" >/dev/null 2>&1
echo "✅ 7 ジェスチャ完了。counter は 1 + 7 = 8 期待。スクショ: $SHOT"
echo "   HUD のページ番号が連番増加していない箇所 (禁じ二重めくり) が疑わしい場合は screenshot を開く"
