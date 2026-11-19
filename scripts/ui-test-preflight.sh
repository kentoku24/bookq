#!/usr/bin/env bash
# ui-test-preflight.sh — UI テスト実行前の環境衛生チェック+クリーン
#
# `.github/scripts/ios-ui-test.sh` の冒頭で自動実行されるほか、raw
# `xcodebuild test` の前に手動で実行する (AGENTS.md §7)。
#
#   1. テスト用ポート (:8080) のリスナーを検証 — 現行 viewer-rs の
#      /api/v1/version + /api/v1/tree が応答すれば「必要なサーバー」として
#      保持。応答しない stale/旧世代サーバーは、テスト用プロセスと識別
#      できる場合のみ kill する (識別不能なものは WARN で残す)。
#   2. サーバーが健全でも reading-positions に UI テストユーザの残存が
#      あれば WARN — stale state でページ位置系テストがフレークする
#      (2026-11-16: 孤立 uvicorn が古い位置 state を返し
#      ReaderUITests が連続失敗した実例)。
#   3. メモリ・SSD の空きを確認。安全な範囲のクリーン
#      (`simctl delete unavailable`) は自動実行し、他 worktree の
#      シミュレータや実行中テストに影響する対処 (sim-teardown.sh,
#      DerivedData 削除等) は WARN に留める。
#
# 終了コード: 0 = 実行可 / 1 = 未解決の問題あり (詳細は stdout)
#
# Env: UI_TEST_PORT (default 8080), UI_TEST_MEM_FREE_MIN (default 15, %),
#      UI_TEST_DISK_FREE_MIN_GIB (default 10)
set -uo pipefail

PORT="${UI_TEST_PORT:-8080}"
MEM_FREE_MIN="${UI_TEST_MEM_FREE_MIN:-15}"
DISK_FREE_MIN_GIB="${UI_TEST_DISK_FREE_MIN_GIB:-10}"
BASE="http://127.0.0.1:$PORT"

# UITestSupport.swift と同一の決定的テストトークン (署名なし JWT)。
# テストユーザ ui-test@bookq.local の reading-positions 残存を検査する。
UITEST_TOKEN="eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.eyJzdWIiOiJ1aS10ZXN0QGJvb2txLmxvY2FsIiwibmFtZSI6IlVJIFRlc3QiLCJleHAiOjQxMDI0NDQ4MDB9.sig"

fail=0
say() { printf '%s\n' "$*"; }

api_is_current() {
  # 現行 viewer-rs 判定: version endpoint が JSON を返し、tree が nodes を返す。
  local ver tree
  ver=$(curl -sf -m 3 "$BASE/api/v1/version" 2>/dev/null || true)
  tree=$(curl -sf -m 3 "$BASE/api/v1/tree" -H "Authorization: Bearer x" 2>/dev/null | head -c 40 || true)
  echo "$ver" | grep -q '"version"' && echo "$tree" | grep -q '"nodes"'
}

# --- 1. test port listeners ---------------------------------------------
say "== port :$PORT"
api_ok=0
pids=$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | sort -u)
if [ -z "$pids" ]; then
  say "  no listener — TEST_MODE viewer-rs を起動してからテストを実行すること"
else
  for pid in $pids; do
    cmd=$(ps -p "$pid" -o command= 2>/dev/null | head -1)
    if api_is_current; then
      api_ok=1
      say "  pid $pid ($cmd) — current viewer-rs, keep"
      continue
    fi
    # stale/旧世代サーバー: cmdline がテストサーバーと識別できる場合のみ
    # kill。macOS では ps eww でも他プロセスの環境変数は取得できないため
    # cmdline 判定のみ (viewer-rs は API 健全性を上で確認済みなので、
    # ここに来るのは応答しない stale なものだけ)。app.main 系は repo から
    # 削除済みの Python app なので host 上の残存は全て stale。
    case "$cmd" in
      *viewer-rs*|*uvicorn*app.main*|*python*app.main*|*hypercorn*app.main*)
        say "  killing stale test server pid=$pid cmd=$cmd"
        kill "$pid" 2>/dev/null || true
        sleep 1
        kill -9 "$pid" 2>/dev/null || true
        ;;
      *)
        say "  WARN: unrecognised listener pid=$pid cmd=$cmd — not killed; 手動で確認すること"
        fail=1
        ;;
    esac
  done
fi

# kill 後にポートが空いたか再確認 (別プロセスの残存に備える)
if [ "$api_ok" = 0 ] && [ -n "$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null)" ]; then
  say "  WARN: :$PORT still has a listener after cleanup — 手動で確認すること"
  fail=1
fi

# --- 2. stale reading-positions -----------------------------------------
if [ "$api_ok" = 1 ]; then
  pos=$(curl -sf -m 3 "$BASE/api/v1/reading-positions" \
        -H "Authorization: Bearer $UITEST_TOKEN" 2>/dev/null || true)
  if echo "$pos" | grep -q '"folder"'; then
    say "  WARN: reading-positions に UI テストユーザの残存エントリあり"
    say "        位置復元でページ系テストがフレークし得る — 失敗時は"
    say "        fresh な BOOKQ_STATE_DIR でサーバーを再起動すること"
  fi
fi

# --- 3. resources --------------------------------------------------------
say "== resources"
mem=$(memory_pressure 2>/dev/null \
      | awk -F: '/free percentage/{gsub(/[^0-9]/,"",$2); print $2}' | tail -1)
if [ -n "$mem" ] && [ "$mem" -lt "$MEM_FREE_MIN" ]; then
  say "  WARN: memory free ${mem}% < ${MEM_FREE_MIN}%"
  say "        ./scripts/sim-teardown.sh (実行中テストがない場合) と"
  say "        不要アプリの終了で空きを作ること"
  fail=1
else
  say "  memory free: ${mem:-unknown}%"
fi

avail=$(df -g "$(dirname "$0")" | awk 'NR==2{print $4}')
if [ -n "$avail" ] && [ "$avail" -lt "$DISK_FREE_MIN_GIB" ]; then
  say "  WARN: disk free ${avail}GiB < ${DISK_FREE_MIN_GIB}GiB — 安全な分だけクリーンを試行"
  xcrun simctl delete unavailable >/dev/null 2>&1 || true
  avail2=$(df -g "$(dirname "$0")" | awk 'NR==2{print $4}')
  say "  after 'simctl delete unavailable': ${avail2:-?}GiB"
  if [ -n "$avail2" ] && [ "$avail2" -lt "$DISK_FREE_MIN_GIB" ]; then
    say "  WARN: まだ不足 — DerivedData・パッケージキャッシュ等の再取得可能"
    say "        ファイルを削除して空きを作ること"
    fail=1
  fi
else
  say "  disk free: ${avail:-unknown}GiB"
fi

if [ "$fail" = 0 ]; then
  say "== preflight OK"
else
  say "== preflight: 未解決の問題あり (上記 WARN を対処してから再実行)"
fi
exit "$fail"
