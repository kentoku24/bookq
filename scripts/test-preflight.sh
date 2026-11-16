#!/usr/bin/env bash
# test-preflight.sh — pytest 開始前のディスク健全性チェック + 安全な残骸回収
#
# tests/conftest.py の pytest_sessionstart が全 pytest 起動時に自動実行する。
# 手動実行も可:
#   scripts/test-preflight.sh           # check → 不足時は掃除ラダー → 再check
#   scripts/test-preflight.sh --check   # 閾値チェックのみ (disk-guard --check 同等)
#   scripts/test-preflight.sh --dry-run # 掃除対象の列挙のみ (削除しない)
#   scripts/test-preflight.sh --clean   # 閾値を問わず掃除ラダーを実行
#
# 環境変数:
#   BOOKQ_MIN_FREE_GB   この空き容量 (GiB) を下回ると掃除→それでも不足なら abort (default 25, disk-guard と同一)
#   BOOKQ_WARN_FREE_GB  この空き容量 (GiB) 未満で警告のみ (default 40)
#   BOOKQ_STALE_DAYS    この日数更新のない残骸を stale とみなす (default 7)
#   BOOKQ_SKIP_PREFLIGHT=1 を pytest 起動前に付けるとフック自体を回避
#
# 掃除ラダー (再取得・再生成可能なものだけ):
#   1. .worktrees/ 直下で git worktree list に登録されていない stale な孤児 dir
#   2. scripts/cleanup-worktrees.sh --apply (マージ済み clean worktree の削除)
#   3. 自分以外の worktree で stale or マージ済みの rust/target, ios/build,
#      android ビルド出力, .pytest_cache (実行中プロセスの cwd がある worktree は除外)
#   4. uv cache prune
#   5. 1日超古い pytest-* tmp dir / bookq-thumbs
#   6. ms-playwright の旧バージョンブラウザ (prefix ごと最新のみ残す)
set -uo pipefail

MODE=run
for arg; do
  case "$arg" in
    --check) MODE=check ;;
    --dry-run) MODE=dry ;;
    --clean) MODE=clean ;;
    -h|--help)
      sed -n '2,24p' "$0"
      exit 0
      ;;
    *)
      echo "usage: $0 [--check|--dry-run|--clean]" >&2
      exit 2
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
CURRENT_TOP="$(git -C "$REPO_ROOT" rev-parse --show-toplevel 2>/dev/null || echo "$REPO_ROOT")"
# .worktrees/ はメインチェックアウト直下にのみ存在する — worktree 内から
# 実行されても git-common-dir からメインルートを特定する
COMMON_GIT="$(git -C "$REPO_ROOT" rev-parse --git-common-dir 2>/dev/null || true)"
case "$COMMON_GIT" in
  /*) ;;
  "") COMMON_GIT="$REPO_ROOT/.git" ;;
  *) COMMON_GIT="$REPO_ROOT/$COMMON_GIT" ;;
esac
MAIN_TOP="$(dirname "$COMMON_GIT")"
MIN_GB="${BOOKQ_MIN_FREE_GB:-25}"
WARN_GB="${BOOKQ_WARN_FREE_GB:-40}"
STALE_DAYS="${BOOKQ_STALE_DAYS:-7}"
RECLAIMED_KB=0

say() { printf 'preflight: %s\n' "$*"; }

free_gb() {
  local kb
  kb="$(df -k "$REPO_ROOT" 2>/dev/null | awk 'NR==2{print $4}')"
  [ -n "$kb" ] && echo $((kb / 1024 / 1024)) || true
}

fmt_kb() { awk -v k="$1" 'BEGIN{ if (k>=1048576) printf "%.1fG", k/1048576; else printf "%.0fM", k/1024 }'; }

size_kb() { du -sk "$1" 2>/dev/null | awk '{print $1}'; }

# 引数 dir に STALE_DAYS 以内に更新されたファイルが1つも無ければ stale
is_stale() {
  [ -z "$(find "$1" -type f -mtime -"$STALE_DAYS" -print -quit 2>/dev/null)" ]
}

# ビルド/テスト系プロセスの cwd が worktree 配下なら「使用中」— その worktree には触らない
worktree_in_use() {
  local wt="$1" pid cwd
  # 判定ツールが無い環境では「使用中扱い」に倒す — 保護が無言で外れて
  # 稼働中 worktree の target を消す方が掃除漏れより悪い
  command -v lsof >/dev/null 2>&1 || return 0
  command -v pgrep >/dev/null 2>&1 || return 0
  while read -r pid; do
    [ -n "$pid" ] || continue
    cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)"
    case "$cwd" in
      "$MAIN_TOP"/.worktrees/*) : ;;                # 子 worktree 側のプロセス (メインを誤判定しない)
      "$wt" | "$wt"/*) return 0 ;;
    esac
  done < <(pgrep -f 'cargo (build|test|run)|rustc|viewer-rs|pytest|xcodebuild|gradlew|ios-ui-test' 2>/dev/null)
  return 1
}

# マージ済み (origin/main の ancestor) かつ独自コミットなし
worktree_merged() {
  local wt="$1" br ahead
  br="$(git -C "$wt" symbolic-ref --quiet --short HEAD 2>/dev/null)" || return 1
  [ "$br" = "main" ] && return 1
  git -C "$wt" merge-base --is-ancestor "$br" origin/main 2>/dev/null || return 1
  ahead="$(git -C "$wt" rev-list --count "origin/main..$br" 2>/dev/null || echo 1)"
  [ "$ahead" = "0" ]
}

maybe_rm() {
  local p="$1" kb
  [ -e "$p" ] || return 0
  kb="$(size_kb "$p")"
  kb="${kb:-0}"
  if [ "$MODE" = dry ]; then
    say "would remove $(fmt_kb "$kb")  $p"
  elif rm -rf "$p" 2>/dev/null; then
    say "removed      $(fmt_kb "$kb")  $p"
  else
    say "FAILED       $(fmt_kb "$kb")  $p"
    kb=0
  fi
  RECLAIMED_KB=$((RECLAIMED_KB + kb))
}

reclaim() {
  local d wt sub t pw prefix
  # --- 1. .worktrees/ 直下の孤児 dir (worktree list 未登録 & stale) ---
  local wt_list
  wt_list="$(git -C "$REPO_ROOT" worktree list --porcelain 2>/dev/null | awk '/^worktree /{print substr($0,10)}')"
  for d in "$MAIN_TOP"/.worktrees/*/; do
    [ -d "$d" ] || continue
    d="${d%/}"
    if ! printf '%s\n' "$wt_list" | grep -Fxq "$d"; then
      if is_stale "$d"; then
        maybe_rm "$d"
      else
        say "keep orphan (recent files): $d"
      fi
    fi
  done
  git -C "$REPO_ROOT" worktree prune 2>/dev/null || true

  # --- 2. マージ済み clean worktree の削除 (target ごと解放される最大レバー) ---
  if [ -x "$SCRIPT_DIR/cleanup-worktrees.sh" ]; then
    if [ "$MODE" = dry ]; then
      "$SCRIPT_DIR/cleanup-worktrees.sh" 2>/dev/null || true
    else
      "$SCRIPT_DIR/cleanup-worktrees.sh" --apply 2>/dev/null || true
    fi
  fi

  # --- 3. 残存 worktree 内のビルド残骸 (自分の worktree・使用中は除外) ---
  while IFS= read -r wt; do
    [ -n "$wt" ] || continue
    [ "$wt" = "$CURRENT_TOP" ] && continue
    [ -d "$wt" ] || continue
    if worktree_in_use "$wt"; then
      say "keep (process active): $wt"
      continue
    fi
    for sub in rust/target ios/build android/app/build android/build android/.gradle .pytest_cache; do
      t="$wt/$sub"
      [ -e "$t" ] || continue
      if is_stale "$t" || worktree_merged "$wt"; then
        maybe_rm "$t"
      fi
    done
  done < <(git -C "$REPO_ROOT" worktree list --porcelain 2>/dev/null | awk '/^worktree /{print substr($0,10)}')

  # --- 4. uv cache (cache が大きいと数分かかるので上限つき) ---
  if command -v uv >/dev/null 2>&1; then
    if [ "$MODE" = dry ]; then
      say "would run: uv cache prune"
    elif command -v timeout >/dev/null 2>&1; then
      timeout 120 uv cache prune >/dev/null 2>&1 || true
    else
      uv cache prune >/dev/null 2>&1 || true
    fi
  fi

  # --- 5. tmp 残骸 (pytest-<N> leaf のみ — 親 pytest-of-<user> は触らない, bookq-thumbs) ---
  local tbase
  tbase="${TMPDIR:-$(getconf DARWIN_USER_TEMP_DIR 2>/dev/null || echo /tmp)}"
  while IFS= read -r t; do
    [ -n "$t" ] && maybe_rm "$t"
  done < <({
    find "$tbase" /private/tmp -maxdepth 1 -type d -name 'pytest-[0-9]*' -mtime +1 2>/dev/null
    find "$tbase"/pytest-of-* /private/tmp/pytest-of-* -mindepth 1 -maxdepth 1 -type d -name 'pytest-[0-9]*' -mtime +1 2>/dev/null
  } | sort -u)
  for t in "$tbase/bookq-thumbs" /private/tmp/bookq-thumbs; do
    [ -e "$t" ] && is_stale "$t" && maybe_rm "$t"
  done

  # --- 6. ms-playwright 旧バージョン (prefix ごと最新のみ残す、stale のみ) ---
  pw="$HOME/Library/Caches/ms-playwright"
  if [ -d "$pw" ]; then
    local prev_prefix=""
    while IFS= read -r d; do
      [ -n "$d" ] || continue
      prefix="${d##*/}"
      prefix="${prefix%-[0-9]*}"
      if [ "$prefix" = "$prev_prefix" ]; then
        # 同 prefix の旧バージョン — playwright が更新で取り替えた残骸
        is_stale "$d" && maybe_rm "$d"
      else
        prev_prefix="$prefix"
      fi
    done < <(find "$pw" -mindepth 1 -maxdepth 1 -type d -print 2>/dev/null \
      | awk -F/ '{n=$NF; v=n; sub(/.*-/,"",v); p=n; sub(/-[0-9]+$/,"",p); print p"\t"v"\t"$0}' \
      | sort -t"$(printf '\t')" -k1,1 -k2,2nr | cut -f3-)
  fi
}

print_suspects() {
  say "largest remaining reclaimable suspects (manual judgment required):"
  {
    for p in "$MAIN_TOP"/.worktrees/*/rust/target "$MAIN_TOP"/.worktrees/*/ios/build \
      "$MAIN_TOP/.omo" "$HOME/.omo" "$HOME/.cache" "$HOME/.cargo/registry" \
      "$HOME/.gradle" "$HOME/.npm" "$HOME/.android" "$HOME/Library/Android" \
      "$HOME/Library/Caches" "$HOME/Library/Developer"; do
      [ -e "$p" ] && du -sk "$p" 2>/dev/null
    done
  } | sort -rn | head -12 | awk '{printf "  %8.1fG  %s\n", $1/1048576, $2}'
}

free="$(free_gb)"
if [ -z "$free" ]; then
  say "cannot determine free space under $REPO_ROOT — skipping"
  exit 0
fi

case "$MODE" in
  check)
    say "free=${free}GiB min=${MIN_GB}GiB"
    [ "$free" -ge "$MIN_GB" ]
    exit $?
    ;;
  dry)
    say "dry-run (free=${free}GiB, min=${MIN_GB}GiB, stale>${STALE_DAYS}d):"
    reclaim
    say "dry-run total: $(fmt_kb "$RECLAIMED_KB") reclaimable"
    exit 0
    ;;
esac

if [ "$MODE" != clean ] && [ "$free" -ge "$MIN_GB" ]; then
  if [ "$free" -lt "$WARN_GB" ]; then
    say "warning: ${free}GiB free (< ${WARN_GB}GiB warn band)"
  fi
  exit 0
fi

[ "$MODE" = clean ] && say "clean requested (free=${free}GiB)" || say "free=${free}GiB < ${MIN_GB}GiB — reclaiming stale residues..."
reclaim
free_after="$(free_gb)"
say "reclaimed $(fmt_kb "$RECLAIMED_KB") → free=${free_after:-?}GiB"

if [ -n "$free_after" ] && [ "$free_after" -lt "$MIN_GB" ]; then
  print_suspects
  say "still below ${MIN_GB}GiB — free space manually or set BOOKQ_SKIP_PREFLIGHT=1 / BOOKQ_MIN_FREE_GB to override"
  exit 1
fi
exit 0
