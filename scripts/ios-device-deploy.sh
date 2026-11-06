#!/bin/bash
set -euo pipefail
# Usage: ./scripts/ios-device-deploy.sh [--release] [--no-launch] [--udid <udid>] [--dev]
# 実機 (paired iPhone/iPad) へ Bookq.app を build → install → launch するヘルパー。
#   1. デバイス選択: --udid > DEVICE_UDID 環境変数 > .env の DEVICE_UDID
#      > devicectl 自動検出 (paired+booted の実機が1台ならそれ)
#      (.env は dev.sh と同じく worktree/メイン両方を見る)
#   2. xcodebuild で対象デバイス向けに build (DEVELOPMENT_TEAM は pbxproj 記載済み)
#   3. -showBuildSettings で .app の実パスを特定 (DerivedData の hash 非依存)
#   4. devicectl install + launch
# --dev は別 bundle ID (dev.bookq.reader.dev / BookqDev.app) の開発用バリアントを
# 入れる — 通常版とストレージ/Keychain/進捗が分離され同時インストール可。
# Personal Team プロビジョニングは 7 日で期限切れ。初回のみ端末側で
# 設定 → 一般 → VPNとデバイス管理 で開発元の信頼が必要。

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO_ROOT/ios"

# .env 解決 — dev.sh と同じ規約。worktree には .env が無いので
# リポジトリルートとメインチェックアウトの両方を見る。
main_checkout() {
    local common
    common="$(git -C "$REPO_ROOT" rev-parse --git-common-dir)"
    case "$common" in
        /*) ;;
        *) common="$REPO_ROOT/$common" ;;
    esac
    ( cd "$common/.." && pwd )
}
dotenv_value() {
    local key="$1" f v
    for f in "$REPO_ROOT/.env" "$(main_checkout)/.env"; do
        [ -f "$f" ] || continue
        v="$(grep -E "^${key}=.+" "$f" 2>/dev/null | tail -1 | cut -d= -f2- \
            | sed -e 's/\r$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/")"
        [ -n "$v" ] && { printf '%s' "$v"; return 0; }
    done
    return 1
}

CONFIG=Debug
LAUNCH=1
DEV=""
UDID="${DEVICE_UDID:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --release)   CONFIG=Release; shift ;;
    --no-launch) LAUNCH=0; shift ;;
    --dev)       DEV=1; shift ;;
    --udid)      UDID=$2; shift 2 ;;
    -h|--help)
      echo "Usage: $0 [--release] [--no-launch] [--udid <udid>] [--dev]" >&2
      echo "  --release    Release 構成で build (default: Debug)" >&2
      echo "  --no-launch  install まで (起動しない)" >&2
      echo "  --udid <u>   対象デバイスを明示 (DEVICE_UDID でも可)" >&2
      echo "  --dev        BookqDev (dev.bookq.reader.dev) として install" >&2
      exit 0 ;;
    *) echo "Usage: $0 [--release] [--no-launch] [--udid <udid>] [--dev]" >&2; exit 2 ;;
  esac
done

BUNDLE_ID=dev.bookq.reader
OVERRIDES=()
if [ -n "$DEV" ]; then
  BUNDLE_ID=dev.bookq.reader.dev
  OVERRIDES=(PRODUCT_BUNDLE_IDENTIFIER="$BUNDLE_ID" PRODUCT_NAME=BookqDev)
fi

# == 1. デバイス選択 ==
# 優先順位: --udid > DEVICE_UDID 環境変数 > .env の DEVICE_UDID > 自動検出
if [ -z "$UDID" ]; then
  UDID="$(dotenv_value DEVICE_UDID || true)"
fi
if [ -z "$UDID" ]; then
  JSON=/tmp/bookq-devices.$$.json
  trap 'rm -f "$JSON"' EXIT
  xcrun devicectl list devices --json-output "$JSON" >/dev/null
  # physical = deviceProperties.provider が CoreSimulator でない
  # (sim は com.apple.CoreSimulator.SimulatorCoreDevicePlugin を持つ)。
  # paired + booted で devicectl がオンデマンド tunnel を張れる状態。
  CANDS=()
  while IFS= read -r line; do
    CANDS+=("$line")
  done < <(python3 - "$JSON" <<'PY'
import json, sys
for dev in json.load(open(sys.argv[1]))["result"]["devices"]:
    dp = dev.get("deviceProperties", {})
    cp = dev.get("connectionProperties", {})
    if "CoreSimulator" in (dp.get("provider") or ""):
        continue
    if cp.get("pairingState") != "paired" or dp.get("bootState") != "booted":
        continue
    print(f"{dev['identifier']}\t{dp.get('name', '?')}")
PY
)
  if [ ${#CANDS[@]} -eq 0 ]; then
    echo "error: 利用可能な実機がありません — 接続/ペアリング/デベロッパモード/ロック解除を確認" >&2
    exit 1
  elif [ ${#CANDS[@]} -gt 1 ]; then
    echo "error: 実機が複数あります — --udid で指定してください:" >&2
    printf '  %s\n' "${CANDS[@]}" >&2
    exit 1
  fi
  UDID=${CANDS[0]%%$'\t'*}
  echo "== device: ${CANDS[0]#*$'\t'} ($UDID)"
fi

# == 2. build ==
echo "== build ($CONFIG${DEV:+ --dev} → id=$UDID)"
xcodebuild -project Bookq.xcodeproj -scheme Bookq \
  -destination "id=$UDID" -configuration "$CONFIG" \
  -allowProvisioningUpdates "${OVERRIDES[@]+"${OVERRIDES[@]}"}" build | tail -3

# == 3. .app 特定 (showBuildSettings で確実に — DerivedData hash を grep しない) ==
BUILD_DIR=$(xcodebuild -project Bookq.xcodeproj -scheme Bookq \
  -destination "id=$UDID" -configuration "$CONFIG" -showBuildSettings \
  "${OVERRIDES[@]+"${OVERRIDES[@]}"}" \
  | awk -F' = ' '/^ +TARGET_BUILD_DIR/ {print $2; exit}')
PRODUCT=$(xcodebuild -project Bookq.xcodeproj -scheme Bookq \
  -destination "id=$UDID" -configuration "$CONFIG" -showBuildSettings \
  "${OVERRIDES[@]+"${OVERRIDES[@]}"}" \
  | awk -F' = ' '/^ +FULL_PRODUCT_NAME/ {print $2; exit}')
APP="$BUILD_DIR/$PRODUCT"
[ -d "$APP" ] || { echo "error: $APP が見つかりません" >&2; exit 1; }

# == 4. install ==
echo "== install → $UDID"
xcrun devicectl device install app --device "$UDID" "$APP"

# == 5. launch ==
if [ "$LAUNCH" = 1 ]; then
  echo "== launch $BUNDLE_ID"
  # ロック中は process launch が error 10002 (Locked) で失敗する。
  # install は済んでいるので原因を案内して終了コードだけ立てる。
  if ! xcrun devicectl device process launch --device "$UDID" "$BUNDLE_ID"; then
    echo "warn: install は成功しましたが起動できません (端末のロック解除、または初回は 設定 → 一般 → VPNとデバイス管理 で開発元を信頼 → 再実行 or アイコンをタップ)" >&2
    exit 1
  fi
fi
echo "✅ deploy 完了 ($CONFIG → $UDID)"
