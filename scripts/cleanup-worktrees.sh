#!/usr/bin/env bash
# マージ済み worktree の安全掃除。既定は dry-run (候補一覧のみ)。
#
#   ./scripts/cleanup-worktrees.sh          # 候補一覧
#   ./scripts/cleanup-worktrees.sh --apply  # 実際に削除
#
# 削除条件 (全て満たすもののみ):
#   - detached HEAD でない (review 用スナップショットは手動判断)
#   - main ブランチの worktree でない
#   - 実行中の worktree でない
#   - dirty でない (git status --porcelain が空)
#   - origin/main の ancestor (マージ済み) かつ ahead=0
#
# dev 環境 (`./dev.sh down`) とは連動しない。
# worktree 名と環境名が一致するとは限らないため、環境は名指しで down すること。
set -euo pipefail

APPLY=false
if [ $# -gt 1 ]; then
  echo "usage: $0 [--apply]" >&2
  exit 1
fi
if [ "${1:-}" = "--apply" ]; then
  APPLY=true
elif [ -n "${1:-}" ]; then
  echo "usage: $0 [--apply]" >&2
  exit 1
fi

START_TOP="$(git rev-parse --show-toplevel)"
cd "$START_TOP"

git fetch origin main -q

REMOVED=0
CANDIDATES=0

while IFS= read -r wt; do
  [ -n "$wt" ] || continue
  # 実行中の worktree は触らない
  if [ "$wt" = "$START_TOP" ]; then
    continue
  fi
  # detached HEAD は手動判断 (review 用スナップショット等)
  branch="$(git -C "$wt" symbolic-ref --short HEAD 2>/dev/null || true)"
  if [ -z "$branch" ]; then
    echo "SKIP (detached): $wt"
    continue
  fi
  if [ "$branch" = "main" ]; then
    continue
  fi
  # dirty は触らない
  if [ -n "$(git -C "$wt" status --porcelain 2>/dev/null)" ]; then
    echo "SKIP (dirty): $wt ($branch)"
    continue
  fi
  # マージ済み (ancestor) かつ独自コミットなし
  if ! git merge-base --is-ancestor "$branch" origin/main 2>/dev/null; then
    continue
  fi
  ahead="$(git rev-list --count "origin/main..$branch" 2>/dev/null || echo 1)"
  if [ "$ahead" != "0" ]; then
    echo "SKIP (ahead=$ahead): $wt ($branch)"
    continue
  fi
  CANDIDATES=$((CANDIDATES + 1))
  if [ "$APPLY" = true ]; then
    git worktree remove "$wt"
    echo "REMOVED: $wt ($branch)"
    REMOVED=$((REMOVED + 1))
  else
    echo "CANDIDATE: $wt ($branch)"
  fi
done < <(git worktree list --porcelain | awk '/^worktree /{print substr($0, 10)}')

git worktree prune

if [ "$APPLY" = true ]; then
  echo "done: removed $REMOVED worktree(s)"
else
  echo "done: $CANDIDATES candidate(s) (dry-run; add --apply to remove)"
fi
