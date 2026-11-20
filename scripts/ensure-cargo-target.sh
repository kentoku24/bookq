#!/usr/bin/env bash
# ensure-cargo-target.sh — rust/target を外付け共有 target dir への symlink にする
#
# worktree ごとの rust/target (~1.5GB) が内蔵 SSD を埋める問題の恒久策。
# 全 worktree が一つの共有 dir を指すので、依存クレートの成果物は
# worktree 間で再利用される。
#
# 挙動:
#   - rust/target が無い        → 共有 dir を作って symlink
#   - 既に symlink              → そのまま (指し先が違っても触らない)
#   - 実ディレクトリ            → 中身を消さないよう警告だけ出して何もしない
#   - 共有先が外部マウント外    → 警告だけ (未マウント時に内蔵側へ phantom dir を作らない)
#   - BOOKQ_SHARED_TARGET_DIR 未設定 → 何もしない
#
# 共有先は BOOKQ_SHARED_TARGET_DIR で指定する (checkout 名は付けず verbatim、
# マシン固有パスを repo に持ち込まないため既定値は無い)。
# dev.sh up と tests/rs_spawn.py から自動で呼ばれる。手動実行も可:
#   BOOKQ_SHARED_TARGET_DIR=/Volumes/<disk>/cargo-target/bookq scripts/ensure-cargo-target.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_LINK="$REPO_ROOT/rust/target"
SHARED_ROOT="${BOOKQ_SHARED_TARGET_DIR:-}"

warn() { printf '⚠️  ensure-cargo-target: %s\n' "$*" >&2; }
info() { printf 'ℹ️  ensure-cargo-target: %s\n' "$*" >&2; }

# path の祖先のどれかが /Volumes/ 直下の実マウントなら true。
# アンマウント後に残る /Volumes/<name> の幻影 dir (内蔵 SSD 上) への
# 書き込みを防ぐ — rs_spawn._external_root_ok と同じ判定。
external_root_ok() {
    local p="$1"
    while [ "$p" != "/" ] && [ -n "$p" ]; do
        case "$p" in
            /Volumes/*)
                mount | grep -q " on $p (" && return 0
                ;;
        esac
        p="$(dirname "$p")"
    done
    return 1
}

if [ -L "$TARGET_LINK" ]; then
    exit 0
fi
if [ -e "$TARGET_LINK" ]; then
    warn "rust/target が実ディレクトリです — 共有化するには退避・削除してから再実行してください: $TARGET_LINK"
    exit 0
fi
if [ -z "$SHARED_ROOT" ]; then
    warn "BOOKQ_SHARED_TARGET_DIR が未設定です — rust/target は内蔵側のままにします"
    exit 0
fi
if ! external_root_ok "$SHARED_ROOT"; then
    warn "$SHARED_ROOT はマウント済み外付けボリューム上にありません — rust/target は内蔵側のままにします"
    exit 0
fi

mkdir -p "$SHARED_ROOT"
ln -s "$SHARED_ROOT" "$TARGET_LINK"
info "rust/target → $SHARED_ROOT"
