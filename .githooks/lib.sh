#!/usr/bin/env bash
# git hook 共通処理。pre-commit / post-commit の両方から source される。
#
# 方針: フックは *本番に触らない*。変更されたコードは、その worktree 専用の
# 開発環境（./dev.sh）に反映する。本番デプロイは ./prd.sh を手で叩く操作に限る。

# 再ビルドが必要になるパス
REBUILD_PATTERNS=(
    "app/.*\.py$"
    "app/templates/.*"
    "app/static/.*"
    "requirements\.txt$"
    "Dockerfile$"
)

# 引数のファイル一覧に再ビルド対象が含まれるか
needs_rebuild() {
    local file pattern
    for file in "$@"; do
        for pattern in "${REBUILD_PATTERNS[@]}"; do
            if printf '%s' "$file" | grep -qE "$pattern"; then
                return 0
            fi
        done
    done
    return 1
}

# マーカーは worktree ごとに独立させる。
# --git-path は linked worktree では .git/worktrees/<name>/ 以下を返すので、
# 複数の worktree で同時にコミットしても干渉しない。
marker_path() {
    git rev-parse --git-path rebuild-marker
}

# 開発環境を更新できる状態かどうか。できない場合は理由を出して 1 を返す
# （コミットはブロックしない — フックはあくまで補助）。
dev_available() {
    local root
    root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
    if [ -z "$root" ] || [ ! -x "$root/dev.sh" ]; then
        echo "⚠️  dev.sh が見つかりません。開発環境の更新をスキップします。"
        return 1
    fi
    if ! docker info >/dev/null 2>&1; then
        echo "⚠️  Docker が動いていません。開発環境の更新をスキップします。"
        return 1
    fi
    # dev.sh は tailscale サイドカーの初回認証に TS_AUTHKEY を必須にしている。
    # ここで見ておかないと dev_up が途中で die し、pre-commit の「起動失敗」扱いで
    # コミットがブロックされる（フックは補助なので、無ければ黙ってスキップする）。
    if ! authkey_available "$root"; then
        echo "⚠️  TS_AUTHKEY が見つかりません（環境変数か .env）。開発環境の更新をスキップします。"
        return 1
    fi
    return 0
}

# TS_AUTHKEY が取れるか。dev.sh の find_authkey と同じ探索順
# （環境変数 → この worktree の .env → メインのチェックアウトの .env）。
authkey_available() {
    local root="$1" common main
    [ -z "${TS_AUTHKEY:-}" ] || return 0
    grep -qE '^TS_AUTHKEY=.+' "$root/.env" 2>/dev/null && return 0
    common="$(git rev-parse --git-common-dir)"
    case "$common" in
        /*) ;;
        *) common="$root/$common" ;;
    esac
    main="$(cd "$common/.." && pwd)"
    grep -qE '^TS_AUTHKEY=.+' "$main/.env" 2>/dev/null && return 0
    return 1
}

# この worktree の開発環境を起動・更新する。引数は dev.sh up にそのまま渡す。
# 環境名はここで固定する — 名前なしで --ref を渡すと dev.sh が ref から
# 名前を導出してしまい、この worktree の環境と別物になるため。
dev_up() {
    local root name
    root="$(git rev-parse --show-toplevel)"
    name="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
    if [ -z "$name" ] || [ "$name" = "HEAD" ]; then
        name="$(basename "$root")"
    fi
    ( cd "$root" && ./dev.sh up "$name" "$@" )
}

# 開発環境の URL（取得できなければ空）
dev_url() {
    local root
    root="$(git rev-parse --show-toplevel)"
    ( cd "$root" && ./dev.sh url 2>/dev/null ) || true
}
