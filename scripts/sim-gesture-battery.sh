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
# 何を検証するか (app.js touchend の3経路。#19 マージ済み main 前提):
#   1. 静止タップ ×2         → ターン数 +2
#   2. 12px ドリフト ×2      → ターン数 +2 … F2 (8px〜20px の横ドリフトは
#      着地位置の zone で即タップとしてターン)
#   3. 22px fast (0.05s) ×1  → ターン数 +1 (速度コミット経路)
#   4. 22px slow (0.25s) ×1  → ターン数 +1 (同上。低速でも 20px 超はフリック)
#   5. 40px slow スワイプ ×1 → ターン数 +1 (コミットアニメ経路)
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

# open-simulator.sh の tap/swipe に渡す UDID 指定。省略時 (booted) は付けず
# helper 側の自動検出に任せ、明示時のみ --udid を末尾に付与する (両 helper とも
# 位置引数の後に [options] を取るため末尾付与で valid)。
UDID_ARGS=()
if [ "$TARGET_UDID" != "booted" ]; then
    UDID_ARGS=(--udid "$TARGET_UDID")
fi
# 空配列の "${UDID_ARGS[@]}" は `set -u` 下の古い bash (macOS 標準 3.2) で
# unbound variable エラーになるため、長さで分岐して呼び分ける。
with_udid() {
    if [ "${#UDID_ARGS[@]}" -gt 0 ]; then
        "$@" "${UDID_ARGS[@]}"
    else
        "$@"
    fi
}

# 開く URL の search 部 (# より前) に r=<n> を差し込む。
# Safari が直前の /static/app.js を検証末に再利用する事態を崩すため。
add_cache_buster() {
    local url="$1" head tail
    if printf '%s' "$url" | grep -q "[?&]r="; then
        printf '%s' "$url"; return 0
    fi
    if [[ "$url" == *'#'* ]]; then
        head="${url%%'#'*}"
        tail="${url#"$head"}"                     # "#..." form (keep '#' itself)
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
# open-simulator.sh の open 経由にしない理由: helper の open は --udid を取らず
# 常に booted を開くため、--udid 指定時と挙動が割れる。起動待ち・URL 検証が
# 欲しければ事前に ./scripts/open-simulator.sh <url> で開いておくこと。
echo "→ 開く: $BODY_URL"
if [ "$TARGET_UDID" = "booted" ]; then
    xcrun simctl openurl booted "$BODY_URL"
else
    xcrun simctl openurl "$TARGET_UDID" "$BODY_URL"
fi
sleep 6   # 描画待ち (コールドブート分)

# --- ジェスチャ (idb は open-simulator.sh の tap/swipe 経由: UDID 自動検出と
#     idb 未導入時の分かりやすいエラー処理をそちらに集約)。
#     --udid 明示時は UDID_ARGS で全ジェスチャを同一デバイスに固定する
#     (付け忘れると open/screenshot と操作対象が割れる)。
#     stderr は握りつぶさず、失敗時はジェスチャ名とともに set -e で止まる。 ---
tap()   { echo "    idb tap: $*"; with_udid "$OPEN_SIM" tap "$@" ; }
swipe() { echo "    idb swipe: $*" ; with_udid "$OPEN_SIM" swipe "$@" ; }
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
echo "✅ 7 ジェスチャ実行完了 — 合否判定はスクショで: counter は 1+7=8 期待 (#19 マージ済み main 前提)。"
echo "   スクショ: $SHOT — HUD のページ番号が連番から外れた箇所が二重めくり / ターン落ちの疑い"
