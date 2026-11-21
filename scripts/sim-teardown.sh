#!/bin/bash
set -euo pipefail
# Usage: ./scripts/sim-teardown.sh [--dev-env <name>] [--udid <u>] [--keep-clones] [--keep-idb] [--skip-process-kills] [--audit-only] [--force]
#   --audit-only: 停止・削除・kill を一切行わず診断レポート + CPU 監査のみ
#   --force: sim ownership を無視して全 booted sim / 全 idb_companion を対象にする
#     (他エージェントの検証が本当に無いと確信できる時だけ使う)
# sim を使った検証の終了時に必須実行する片付け+CPU監査 (冪等)。
#   1. --dev-env 指定の dev 環境を down (user-state volume は保持)
#   2. booted sim を shutdown (--udid 指定時はその1台のみ)
#      /tmp/bookq-sim-owners/u<uid>/<udid>.lock の flock が取れない sim は
#      他エージェントの検証中とみなしてスキップ (#596)。操作は flock 保持
#      中に行うので check〜実行の間に新しい run が claim する競合もない。
#      --force で無視可。
#   3. xcodebuild テスト Clone デバイスを delete (--keep-clones で保持)
#   4. ~/Library/Developer/XCTestDevices の残留データを削除
#   5. idb_companion 停止 — 生存 owner のいる sim の companion は残す
#      (次回使用時に自動再接続)
#   6. 残留 simctl diagnose / 孤児 spindump を kill
#   7. sim 関連プロセスの CPU 残留を監査 — 残っていれば非0終了
# 他セッション所有の xcodebuild / dev 環境は kill せず報告のみ。

DEV_ENV=""
TARGET_UDID=""
KEEP_CLONES=0
KEEP_IDB=0
SKIP_PROC_KILLS=0
AUDIT_ONLY=0
FORCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dev-env) DEV_ENV=$2; shift 2 ;;
    --udid) TARGET_UDID=$2; shift 2 ;;
    --keep-clones) KEEP_CLONES=1; shift ;;
    --keep-idb) KEEP_IDB=1; shift ;;
    --skip-process-kills) SKIP_PROC_KILLS=1; shift ;;
    --audit-only) AUDIT_ONLY=1; shift ;;
    --force) FORCE=1; shift ;;
    *) echo "Usage: $0 [--dev-env <name>] [--udid <u>] [--keep-clones] [--keep-idb] [--skip-process-kills] [--audit-only] [--force]" >&2; exit 2 ;;
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

# /tmp/bookq-sim-owners/u<uid>/<udid>.{lock,json} — verify.py の
# SimulatorDriver が run 中に取得する所有権 (#596)。排他の真実は .lock の
# flock(2) で、プロセス死亡時に kernel が自動解放する。json は {pid,label}
# の表示用情報ファイル。このスクリプトも破壊操作の前に同じ flock を
# ノンブロッキングで取得する — 取得できなければ生存 run が所有している
# のでスキップ、取得できれば操作完了まで保持するため、チェックと操作の
# 間に新しい run が claim する TOCTOU が起きない。--force で全て無視。
SIM_OWNER_DIR="/tmp/bookq-sim-owners/u$(id -u)"

# flock 取得 → 成功なら op を実行 (flock は open file description に
# 紐付くので exec/子プロセスへ継承され、操作中ずっと保持される)。
# rc: 0=実行した / 3=生存 owner がいてスキップ / その他=エラー。
# stderr の "OWNER pid=.. label=.." 行はスキップ理由の表示用。
sim_flock_op() { # <udid> <shutdown|delete|killcomp> [pid]
  python3 - "$SIM_OWNER_DIR" "$@" <<'PY'
import fcntl, json, os, signal, subprocess, sys

owner_dir, udid, mode = sys.argv[1], sys.argv[2], sys.argv[3]
rest = sys.argv[4:]
try:
    os.makedirs(owner_dir, exist_ok=True)
    fd = os.open(os.path.join(owner_dir, udid + ".lock"),
                 os.O_RDWR | os.O_CREAT, 0o644)
except OSError as e:
    print(f"owner lock open failed: {e}", file=sys.stderr)
    sys.exit(2)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    try:
        with open(os.path.join(owner_dir, udid + ".json")) as f:
            info = json.load(f)
    except Exception:
        info = {}
    print("OWNER pid={} label={!r}".format(
        info.get("pid"), info.get("label")), file=sys.stderr)
    sys.exit(3)
os.set_inheritable(fd, True)
if mode == "shutdown":
    os.execvp("xcrun", ["xcrun", "simctl", "shutdown", udid])
elif mode == "delete":
    subprocess.run(["xcrun", "simctl", "shutdown", udid],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.execvp("xcrun", ["xcrun", "simctl", "delete", udid])
elif mode == "killcomp":
    try:
        os.kill(int(rest[0]), signal.SIGTERM)
    except (IndexError, ValueError, ProcessLookupError):
        pass
    sys.exit(0)
sys.exit(2)
PY
}

if [ "$AUDIT_ONLY" = 0 ]; then

echo "== sim shutdown"
if [ -n "$TARGET_UDID" ]; then
  if [ "$FORCE" = 0 ]; then
    owner_line=$(sim_flock_op "$TARGET_UDID" shutdown 2>&1) && op_rc=0 || op_rc=$?
    if [ "$op_rc" = 3 ]; then
      echo "  skip: $TARGET_UDID (${owner_line#OWNER } — 他エージェントの検証中; --force で強制)"
    elif [ "$op_rc" = 0 ]; then
      echo "  shutdown: $TARGET_UDID"
    else
      echo "  warn: $TARGET_UDID shutdown failed (rc=$op_rc ${owner_line%%$'\n'*})"
    fi
  else
    xcrun simctl shutdown "$TARGET_UDID" 2>/dev/null || true
  fi
else
  # booted デバイスを列挙: Clone かつ xcodebuild 生存中ならスキップ、
  # flock が取れない sim (生存 run が所有) もスキップ、それ以外を shutdown
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
      continue
    fi
    if [ "$FORCE" = 0 ]; then
      owner_line=$(sim_flock_op "$u" shutdown 2>&1) && op_rc=0 || op_rc=$?
      if [ "$op_rc" = 3 ]; then
        echo "  skip: $name (${owner_line#OWNER } — 他エージェントの検証中; --force で強制)"
      elif [ "$op_rc" = 0 ]; then
        echo "  shutdown: $name"
      fi
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
    if [ "$FORCE" = 0 ]; then
      owner_line=$(sim_flock_op "$u" delete 2>&1) && op_rc=0 || op_rc=$?
      if [ "$op_rc" = 3 ]; then
        echo "  skip: Clone $u (${owner_line#OWNER } — 他エージェントの検証中; --force で強制)"
      elif [ "$op_rc" = 0 ]; then
        echo "  deleted Clone $u"
      else
        echo "  warn: Clone $u delete failed (rc=$op_rc)"
      fi
    else
      xcrun simctl shutdown "$u" 2>/dev/null || true
      xcrun simctl delete "$u" && echo "  deleted Clone $u"
    fi
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
  # companion を UDID 単位で整理 — 生存 owner のいる sim の companion は残す。
  # companion を殺すと相手の idb AX/tap が dtuhid 断絶で死ぬ (#587 調査済み)。
  # 通常時の列挙は "idb_companion" と "--udid" の両方を要求 — `tail -f
  # idb_companion.log` のような無関係プロセスを巻き込まない。
  # --force 時は従来通り broad pattern で全 companion を停止する。
  if [ "$FORCE" = 0 ]; then
    companion_pat="idb_companion.*--udid"
  else
    companion_pat="idb_companion"
  fi
  idb_killed=0
  idb_kept=0
  while read -r cpid ccmd; do
    [ -n "$cpid" ] || continue
    cudid=$(printf '%s' "$ccmd" | sed -n 's/.*--udid \([A-Za-z0-9-]*\).*/\1/p')
    if [ "$FORCE" = 0 ]; then
      # 対象 sim の flock を取れた時だけ kill — 取れなければ生存 run の
      # companion なので保持 (check〜kill の間の claim も flock が防ぐ)
      if [ -n "$cudid" ] && sim_flock_op "$cudid" killcomp "$cpid" 2>/dev/null; then
        idb_killed=$((idb_killed + 1))
      else
        idb_kept=$((idb_kept + 1))
      fi
    else
      kill "$cpid" 2>/dev/null && idb_killed=$((idb_killed + 1)) || true
    fi
  done < <(pgrep -fl "$companion_pat" 2>/dev/null || true)
  [ "$idb_killed" -gt 0 ] && echo "== idb_companion 停止 ($idb_killed 件)" || true
  [ "$idb_kept" -gt 0 ] && echo "== idb_companion 保持 ($idb_kept 件 — 生存 owner の sim)" || true
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
