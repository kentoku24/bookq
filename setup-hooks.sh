#!/usr/bin/env bash
# git フックをインストールする。クローン後に一度実行する。
#
# フックはコミット時に *この worktree の開発環境* を更新する（本番には触らない）。
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

HOOKS_SOURCE=".githooks"
# worktree では .git はファイルなので .git/hooks を直接組み立ててはいけない。
# --git-path は共有の hooks ディレクトリを返す（= 全 worktree で同じフックが使われる）。
HOOKS_TARGET="$(git rev-parse --git-path hooks)"

echo "📦 git フックをインストールします..."

if [ ! -d "$HOOKS_SOURCE" ]; then
    echo "⚠️  フックのソースディレクトリが見つかりません: $HOOKS_SOURCE" >&2
    exit 1
fi

mkdir -p "$HOOKS_TARGET"

for hook in "$HOOKS_SOURCE"/*; do
    [ -f "$hook" ] || continue
    hook_name="$(basename "$hook")"
    # lib.sh はフックではない。フックは worktree 側の .githooks/lib.sh を source する。
    [ "$hook_name" = "lib.sh" ] && continue
    cp "$hook" "$HOOKS_TARGET/$hook_name"
    chmod +x "$HOOKS_TARGET/$hook_name"
    echo "  ✅ $hook_name"
done

echo ""
echo "✅ インストール完了: $HOOKS_TARGET"
echo ""
echo "ワークフロー:"
echo "  1. アプリコードを変更する"
echo "  2. git add <files>"
echo "  3. git commit"
echo "     → pre-commit がこの worktree の開発環境を更新し、URL を表示してコミットを止める"
echo "     → その URL を開いて動作確認する（iPad などからも見られる）"
echo "  4. もう一度 git commit"
echo "     → マーカーがあるので再ビルドせずコミットが通る"
echo ""
echo "監視対象:"
echo "  app/*.py, app/templates/*, app/static/*, requirements.txt, Dockerfile"
echo ""
echo "フックは全 worktree で共有されます（.git/hooks は共通ディレクトリ）。"
echo "開発環境の手動操作:  ./dev.sh up | list | down"
echo "本番のデプロイ:      ./prd.sh （メインのチェックアウトからのみ）"
