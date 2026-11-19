#!/bin/bash
set -euo pipefail
# Usage: ./scripts/sim-teardown.sh [--dev-env <name>] [--udid <u>] [--keep-clones] [--keep-idb] [--skip-process-kills] [--audit-only]
#   --audit-only: 停止・削除・kill を一切行わず診断レポート + CPU 監査のみ
# sim を使った検証の終了時に必須実行する片付け+CPU監査 (冪等)。
#   1. --dev-env 指定の dev 環境を down (user-state volume は保持)
#   2. booted sim を shutdown (--udid 指定時はその1台のみ)
#   3. xcodebuild テスト Clone デバイスを delete (--keep-clones で保持)
#   4. ~/Library/Developer/XCTestDevices の残留データを削除
#   5. idb_companion 停止 (次回使用時に自動再接続)
#   6. 残留 simctl diagnose / 孤児 spindump を kill
#   7. sim 関連プロセスの CPU 残留を監査 — 残っていれば非0終了
# 他セッション所有の xcodebuild / dev 環境は kill せず報告のみ。

DEV_ENV=""
TARGET_UDID=""
KEEP_CLONES=0
KEEP_IDB=0
SKIP_PROC_KILLS=0
AUDIT_ONLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dev-env) DEV_ENV=$2; shift 2 ;;
    --udid) TARGET_UDID=$2; shift 2 ;;
    --keep-clones) KEEP_CLONES=1; shift ;;
    --keep-idb) KEEP_IDB=1; shift ;;
    --skip-process-kills) SKIP_PROC_KILLS=1; shift ;;
    --audit-only) AUDIT_ONLY=1; shift ;;
    *) echo "Usage: $0 [--dev-env <name>] [--udid <u>] [--keep-clones] [--keep-idb] [--skip-process-kills] [--audit-only]" >&2; exit 2 ;;
  esac
done

if [ -n "$DEV_ENV" ]; then
  echo "== dev env: ./dev.sh down $DEV_ENV (user-state volume は保持)"
  ./dev.sh down "$DEV_ENV" || echo "warn: dev.sh down failed (別セッション所有なら手動で確認)"
fi

if ! command -v xcrun >/dev/null 2>&1 || ! xcrun --find simctl >/dev/null 2>&1; then
  echo "xcrun/simctl not found — sim 片付けをスキップ (Linux CI?)" >&2
  exit 0
fi

# 実行中 xcodebuild test があるか — ある間は Clone デバイスを他セッションの
# テスト資産として shutdown/delete 対象から外す (殺すと他セッションのテストが死ぬ)
live_xcodebuild=$(pgrep -f "xcodebuild.*test" | head -1 || true)

if [ "$AUDIT_ONLY" = 0 ]; then

echo "== sim shutdown"
if [ -n "$TARGET_UDID" ]; then
  xcrun simctl shutdown "$TARGET_UDID" 2>/dev/null || true
else
  # booted デバイスを列挙: Clone かつ xcodebuild 生存中ならスキップ、それ以外を shutdown
  xcrun simctl list devices booted -j 2>/dev/null | python3 -c '
import json,sys
d=json.load(sys.stdin)
for devs in d.get("devices",{}).values():
    for dev in devs:
        if dev.get("state") == "Booted":
            print("%s\t%s" % (dev["udid"], dev.get("name","")))
' | while IFS=$'\t' read -r u name; do
    if [ -n "$live_xcodebuild" ] && [ "${name#Clone }" != "$name" ]; then
      echo "  skip: $name (xcodebuild test 実行中 pid $live_xcodebuild — 他セッション所有の可能性)"
    else
      xcrun simctl shutdown "$u" 2>/dev/null && echo "  shutdown: $name" || true
    fi
  done
fi

echo "== Clone デバイス削除"
if [ "$KEEP_CLONES" = 0 ]; then
  clone_udids=$(xcrun simctl list devices -j 2>/dev/null | python3 -c '
import json,sys
d=json.load(sys.stdin)
for devs in d.get("devices",{}).values():
    for dev in devs:
        if dev.get("name","").startswith("Clone"):
            print(dev["udid"])
' 2>/dev/null || true)
  for u in $clone_udids; do
    if [ -n "$live_xcodebuild" ]; then
      echo "  skip: Clone $u (xcodebuild test 実行中 pid $live_xcodebuild)"
      continue
    fi
    xcrun simctl shutdown "$u" 2>/dev/null || true
    xcrun simctl delete "$u" && echo "  deleted Clone $u"
  done
  [ -z "$clone_udids" ] && echo "  (Clone なし)"
fi

echo "== XCTestDevices 残留削除"
# xcodebuild test が作るテストデバイス data (~/Library/Developer/XCTestDevices/<uuid>) —
# 毎回生成され数GB〜十数GBに膨らむ。実行中テストの所有物を消すとテストが壊れるので
# live_xcodebuild 中は skip (Clone 削除と同じガード)。
XCTD="$HOME/Library/Developer/XCTestDevices"
if [ -n "$live_xcodebuild" ]; then
  echo "  skip: xcodebuild test 実行中 pid $live_xcodebuild"
elif [ -d "$XCTD" ]; then
  mb=$(( $(du -sk "$XCTD" 2>/dev/null | cut -f1 || echo 0) / 1024 ))
  rm -rf "$XCTD"/*/ 2>/dev/null || true
  echo "  pruned: ${mb} MB"
fi

# --skip-process-kills: 呼び出し元スクリプトの後片付け経路から呼ぶ場合、
# 並走する他セッションの検証 (idb 接続や simctl diagnose ログ収集) を
# 止めないよう global なプロセス kill を省略する。CPU 監査は常に行う。
if [ "$SKIP_PROC_KILLS" = 0 ] && [ "$KEEP_IDB" = 0 ]; then
  pkill -f "idb_companion" 2>/dev/null && echo "== idb_companion 停止" || true
fi

if [ "$SKIP_PROC_KILLS" = 0 ]; then
  echo "== 残留診断プロセス整理"
  # simctl diagnose はテスト完了後のログ収集。実行中 xcodebuild の子ではなく残留のみ対象。
  pkill -f "simctl diagnose" 2>/dev/null || true
  # 孤児化 spindump (diagnose の遺物)。root 所有だと kill できないので警告のみ。
  spin_pids=$(pgrep -f "^/usr/sbin/spindump$" || true)
  for p in $spin_pids; do
    kill "$p" 2>/dev/null || echo "warn: spindump $p は root 所有で kill 不可 — 自己終了を待つか 'sudo kill $p'"
  done
else
  echo "== 残留診断プロセス整理: --skip-process-kills のため省略"
fi

fi # AUDIT_ONLY

sleep 3

echo "== CPU 監査"
audit() { # sim 関連プロセスで CPU >20% のものを列挙
  ps -Ao pid=,pcpu=,command= | grep -E "simruntime|CoreSimulator|testmanagerd|liveactivitiesd|spindump" \
    | grep -v grep | awk '$2+0 > 20 {print}'
}
# cleanup 直後は clone 停止の一過性 churn で 20% を超え得る — 検出したら 10s 後に再監査
flagged=$(audit || true)
if [ -n "$flagged" ]; then
  sleep 10
  flagged=$(audit || true)
fi
leftover=0
if [ -n "$flagged" ]; then
  echo "$flagged" | while read -r pid cpu rest; do echo "  ⚠️  ${cpu}% CPU: pid $pid $rest"; done
  leftover=1
fi

# 実行中 xcodebuild は別セッション所有の可能性 — kill せず報告のみ
while read -r pid ppid cmd; do
  echo "  info: xcodebuild 実行中 pid $pid (他セッション所有なら放置; 自物で停滞なら手動 kill)" >&2
done < <(pgrep -fl "xcodebuild.*test" || true)

load=$(sysctl -n vm.loadavg 2>/dev/null || uptime | sed 's/.*load averages*: //')
echo "  loadavg: $load"
if [ "$leftover" = 1 ]; then
  if [ -n "$live_xcodebuild" ]; then
    echo "  → 実行中 xcodebuild の所有の可能性。テスト完了後に再実行して残留を確認" >&2
  fi
  echo "❌ sim 関連プロセスが高CPUで残留 — 上記 pid を確認 (root 所有なら sudo kill)" >&2
  exit 1
fi
echo "✅ teardown 完了 — sim 関連の CPU 残留なし"
