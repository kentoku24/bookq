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
#   1. 静止タップ ×2         → ターン数 +2
#   2. 12px ドリフト ×2      → ターン数 +2 … ai/viewer-tap-turn-fix (#19) の
#    「24px 未満ドリフト=即タップ」が入っているビルド前提。未マージの main
#    単体では 12px ドリフトはバウンスバック (ターン数は -2)
#   3. 22px fast (0.05s) ×1  → ターン数 +1 (#19 前提経路では即時、main では無反応)
#   4. 22px slow (0.25s) ×1  → ターン数 +1 (同上)
#   5. 40px slow スワイプ ×1 → ターン数 +1 (コミットアニメ経路, main でも成立)
#
# 合否は スクショ の counter と ?perf=1 HUD (`next(L) #N ... img:ok`) で行う。
# 本スクリプトは「実行して画面に証跡を残す」まで。方法論と判読は
# .claude/skills/ios-simulator/references/tap-driving.md。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OPEN_SIM="$REPO_ROOT/scripts/open-simulator.sh"

usage() {
    echo "Usage: $0 <viewer-url> [--screenshot PATH] [--udid UDID]" >&2
    echo "  <viewer-url>            開くビューア URL (?perf=1 推奨: HUD 有効。# の前の search 部)" >&2
    echo "  --screenshot PATH       最終スクショの保存先 (既定: .omo/evidence/sim-battery.png)" >&2
    echo "  --udid UDID             操作対象の Simulator (既定: booted)" >&2
    exit 1
}

[ $# -ge 1 ] || usage
URL="$1"; shift

SHOT=".omo/evidence/sim-battery.png"
TARGET_UDID="booted"
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

# 開く URL の search 部 (# より前) に r=<n> を差し込む。
# Safari が直前の /static/app.js を検証末に再利用する事態を崩すため。
add_cache_buster() {
    local url="$1" head tail
    if printf '%s' "$url" | grep -q "[?&]r="; then
        printf '%s' "$url"; return 0
    fi
    if [[ "$url" == *'#'* ]]; then
        head="${url%%'#'*}"; tail="${url#*#}"    # tail keeps everything except its leading '#'
        tail="${url#"$head"}"                     # "#...}}" form (keep '#' itself)
        if [[ "$head" == *\?* ]]; then
            printf '%s%s' "$head&r=$(date +%s)" "$tail"
        else
            printf '%s%s' "$head?r=$(date +%s)" "$tail"
        fi
    elif [[ "$url" == *\?* ]]; then
        printf '%s' "$url&r=$(date +%s)"
    else
        printf '%s' "$url?r=$(date +%s)"
    fi
}

BODY_URL="$(add_cache_buster "$URL")"

# --- open (失敗は握りつぶさない: この後に続ければ全部ズレる) ---
echo "→ 開く: $BODY_URL"
if [ "$TARGET_UDID" = "booted" ]; then
    xcrun simctl openurl booted "$BODY_URL"
else
    xcrun simctl openurl "$TARGET_UDID" "$BODY_URL"
fi
sleep 6   # 描画待ち (コールドブート分)

# --- ジェスチャ (idb は open-simulator.sh の tap/swipe 経由: UDID 自動検出と
#     idb 未導入時の分かりやすいエラー処理をそちらに集約)。
#     stderr は握りつぶさず、失敗時はジェスチャ名とともに set -e で止まる。 ---
tap()   { echo "    idb tap: $*"; "$OPEN_SIM" tap "$@" ; }
swipe() { echo "    idb swipe: $*"; "$OPEN_SIM" swipe "$@" ; }
wait_() { sleep "${1:-1.2}"; }

echo "→ 静止タップ ×2"
tap 78 500; wait_; tap 78 500; wait_
echo "→ 12px ドリフト ×2 (#19 の F2: ゆるタップ)"
swipe 78 500 90 502 --duration 0.15; wait_; swipe 78 500 90 502 --duration 0.15; wait_
echo "→ 22px fast (0.05s)"
swipe 78 500 100 502 --duration 0.05; wait_
echo "→ 22px slow (0.25s)"
swipe 78 500 100 502 --duration 0.25; wait_
echo "→ 40px slow スワイプ (0.4s, コミット)"
swipe 60 500 100 502 --duration 0.4; wait_ 1.4

# --- 証跡 ---
mkdir -p "$(dirname "$SHOT")"
if [ "$TARGET_UDID" = "booted" ]; then
    xcrun simctl io booted screenshot "$SHOT"
else
    xcrun simctl io "$TARGET_UDID" screenshot "$SHOT"
fi
echo "✅ 7 ジェスチャ実行完了 — 合否判定はスクショで: counter は (前提 ハッシュ内 F2 ありで) 1+7=8 期待。"
echo "   スクショ: $SHOT — HUD のページ番号が連番から外れた箇所が二重めくり / ターン落ちの疑い"
