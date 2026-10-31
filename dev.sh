#!/usr/bin/env bash
# bookq — 開発環境マネージャ（固定 10 スロットプール）
#
# 各開発環境は本番 (docker-compose.yml) と同じ構成パターンの独立した compose
# プロジェクト（viewer + tailscale サイドカー）。URL はプールスロットから決定的に
# 決まる https://dev-XX.<tailnet>.ts.net（XX は 01..10 ゼロパディング）で、
# 環境名 (ENV) はスロットにマッピングされる。プールサイズは 10、docker ラベル
# com.bookq.pool_slot / com.bookq.name から状態を導出するため外部の
# 状態ファイルは不要。Free プランでは tailnet ノードを API で削除できないため、
# スロット（= hostname dev-XX / project bookq-dev-pool-XX）を再利用し、
# down は通常ボリュームを保持、--full のみで state をリセットする。
#
#   ./dev.sh up [name] [--ref REF]   起動（初回は空きスロット 01..10 を割当、既存なら同じスロットを再利用）
#   ./dev.sh up [name] [--ref REF] [--fixture]   起動（--fixture: 実データの代わりに合成テストツリーをマウント）
#   ./dev.sh up [name] [--rs]        viewer-rs（Rust バックエンド）構成で起動。viewer も同居して
#                                    フォールスルー先になり、tailscale serve は viewer-rs を指す
#                                    （com.bookq.backend=rs ラベルで再 up 時もモードを維持）
#   ./dev.sh up [name] [--local [--port N] [--lan-allow]] [--fixture]   起動（--local: tailscale 無しで http://127.0.0.1:<port> に公開。2つ目の同時環境には --port が必要）
#   ./dev.sh down [name] [--full]    停止・削除（--full 無しはボリューム保持でスロット再利用 / --full は state ボリュームまで削除してスロット解放）
#   ./dev.sh down --all [--full] [--yes] [--no-prune-volumes]   全環境を削除（--all --full は --yes 必須）
#                                    --full: ボリューム・イメージ・ノード登録まで完全撤去
#                                    （プールスロット dev-01..dev-10 の再利用では API 削除は不要）
#                                    --all は実行前に既に孤立していたボリュームも回収する
#                                    （--no-prune-volumes で無効化）
#   ./dev.sh list                    一覧（ENV → SLOT マッピングを表示）
#   ./dev.sh url [name]              URL だけ出力（プールスロット dev-XX ベース）
#   ./dev.sh logs [name] [-f ...]    ログ
#   ./dev.sh gc                      未参照・dangling イメージの回収（up 成功後にも自動実行）
#   ./dev.sh gc-volumes [--dry-run]  孤立ボリュームの回収（コンテナの無い bookq-dev-* の残骸）
#                                    down --all からも自動実行される
#
# 例:
#   ./dev.sh up review-42 --ref origin/feature/foo   # 空きスロット（例: dev-03）に割当
#   ./dev.sh up review-42                            # 同じスロット dev-03 を再利用（冪等）
#   ./dev.sh down review-42                          # dev-03 のスロット解放（state は保持）
#   ./dev.sh up            # 現在の worktree の作業ツリーから
#
# 本番には絶対に触らない: compose ファイルを常に -f で明示し、プロジェクト名が
# 本番のものになる場合は実行を拒否する。
# プール枯渇時 (10/10) は `pool exhausted (10/10) - use down to free a slot. Current allocation: ...` で停止する。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

DEV_COMPOSE="docker-compose.dev.yml"
LOCAL_COMPOSE="docker-compose.local.yml"
DEV_RS_COMPOSE="docker-compose.dev-rs.yml"
LOCAL_RS_COMPOSE="docker-compose.local-rs.yml"
PROJECT_PREFIX="bookq-dev-"
PROD_PROJECT="bookq"
DEV_LABEL="com.bookq.role=dev"
SLUG_MAXLEN=32
POOL_SIZE=10
POOL_PREFIX="bookq-dev-pool-"

die()  { printf '❌ %s\n' "$*" >&2; exit 1; }
warn() { printf '⚠️  %s\n' "$*" >&2; }

# ============================================================ 前提チェック

require_repo_root() {
    [ -f "$SCRIPT_DIR/$DEV_COMPOSE" ] || die "$DEV_COMPOSE が見つかりません。リポジトリ（または worktree）のルートで実行してください。"
    [ "$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || true)" = "$(git rev-parse --show-toplevel 2>/dev/null || true)" ] \
        && [ -n "$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || true)" ] \
        || die "このリポジトリ（bookq）の git 管理下で実行してください（リポジトリ内の任意フォルダから可）"
    command -v docker >/dev/null 2>&1 || die "docker が見つかりません。"
    docker info >/dev/null 2>&1 || die "Docker が動いていません。Docker Desktop を起動してください。"
}

repo_root() { git rev-parse --show-toplevel; }

# メインのチェックアウト（.env が置いてある場所）
main_checkout() {
    local common
    common="$(git rev-parse --git-common-dir)"
    case "$common" in
        /*) ;;
        *) common="$(repo_root)/$common" ;;
    esac
    ( cd "$common/.." && pwd )
}

# .env から値を取り出す。worktree には .env が無いのでメインのチェックアウトも見る。
# 囲みクォートと行末 CR だけ剥がす（値の中の空白は保持 — パスに空白があり得る）。
dotenv_value() {
    local key="$1" f v
    for f in "$(repo_root)/.env" "$(main_checkout)/.env"; do
        [ -f "$f" ] || continue
        v="$(grep -E "^${key}=.+" "$f" 2>/dev/null | tail -1 | cut -d= -f2- \
            | sed -e 's/\r$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/")"
        [ -n "$v" ] && { printf '%s' "$v"; return 0; }
    done
    return 1
}

# サイドカーの認証キー
find_authkey() {
    if [ -n "${TS_AUTHKEY:-}" ]; then printf '%s' "$TS_AUTHKEY"; return 0; fi
    dotenv_value TS_AUTHKEY
}

# ============================================================ 名前の導出

# compose のプロジェクト名として妥当な形に落とす。
# 先頭の claude/ と bookq- は冗長なので落とす。
# 許すのは英数字とハイフンだけ — この slug はそのまま tailnet ノード名
# dev-<slug> になる。アンダースコアは DNS ホスト名として無効で、Tailscale が
# 勝手に置き換えるため、dev.sh が組み立てる FQDN と実際のノード名がズレる。
slugify() {
    printf '%s' "$1" \
        | tr '[:upper:]' '[:lower:]' \
        | sed -e 's#^refs/heads/##' -e 's#^origin/##' -e 's#^claude/##' -e 's#^bookq-##' \
        | sed -e 's/[^a-z0-9-]/-/g' -e 's/-\{2,\}/-/g' -e 's/^-*//' -e 's/-*$//' \
        | cut -c "1-$SLUG_MAXLEN" \
        | sed -e 's/-*$//'
}

# 引数があればそれ、無ければ現在のブランチ、detached なら worktree のディレクトリ名。
env_name() {
    local raw="${1:-}"
    if [ -z "$raw" ]; then
        raw="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
        if [ -z "$raw" ] || [ "$raw" = "HEAD" ]; then
            raw="$(basename "$PWD")"
        fi
    fi
    local slug
    slug="$(slugify "$raw")"
    [ -n "$slug" ] || die "環境名を '$raw' から導出できませんでした。./dev.sh up <name> で明示してください。"
    printf '%s' "$slug"
}

project_of() {
    local project="${PROJECT_PREFIX}$1"
    [ "$project" != "$PROD_PROJECT" ] || die "プロジェクト名が本番 ($PROD_PROJECT) と一致します。中止しました。"
    [ "$project" != "$PROJECT_PREFIX" ] || die "環境名が空です。"
    printf '%s' "$project"
}

# ============================================================ プール

normalize_pool_slot() {
    local slot="${1:-}"
    [ -n "$slot" ] || die "スロットが空です。"
    case "$slot" in
        ''|*[!0-9]*) die "不正なスロット: ${slot}（01..10 を指定）" ;;
    esac
    local n=$((10#$slot))
    [ "$n" -ge 1 ] && [ "$n" -le 10 ] || die "不正なスロット: ${slot}（01..10 を指定）"
    printf '%02d' "$n"
}

pool_slot_padded() {
    normalize_pool_slot "${1:-}"
}

pool_project_of() {
    local slot="$1" env_name="${2:-}"
    # slot may be "01" or 1 — normalize to zero-padded
    slot="$(normalize_pool_slot "$slot")"
    if [ -n "$env_name" ]; then
        # 表示上は dev-01-main のようにブランチを含めるが、ボリュームはスロット固定で再利用
        # プロジェクト名にブランチを含めても一意判別しやすくするため
        printf 'bookq-%s-%s' "$(pool_hostname_of "$slot")" "$env_name"
    else
        printf '%s%s' "$POOL_PREFIX" "$slot"
    fi
}

pool_hostname_of() {
    local slot="$1"
    slot="$(normalize_pool_slot "$slot")"
    printf 'dev-%s' "$slot"
}

# 環境→プロジェクト/スロットの引きは com.bookq.name（viewer + viewer-rs 両方に
# 付く）で行う。role=dev だけだと、down 途中で viewer だけ消えた残骸環境の
# viewer-rs を見失う（down で overlay 未読込 → orphan 残りの経験あり）。
find_project_for_env() {
    local want="$1"
    docker ps -a --filter "label=com.bookq.name" \
        --format '{{.Label "com.docker.compose.project"}}|{{.Label "com.bookq.name"}}|{{.Label "com.bookq.pool_slot"}}' 2>/dev/null \
        | awk -F'|' -v want="$want" '$2 == want {print $1; exit}'
}

find_slot_for_env() {
    local want="$1"
    docker ps -a --filter "label=com.bookq.name" \
        --format '{{.Label "com.bookq.name"}}|{{.Label "com.bookq.pool_slot"}}' 2>/dev/null \
        | awk -F'|' -v want="$want" '$1 == want {print $2; exit}'
}

find_free_slot() {
    local i slot ids
    for i in $(seq 1 "$POOL_SIZE"); do
        slot="$(pool_slot_padded "$i")"
        # スロットが空かはラベル pool_slot で判定（プロジェクト名は dev-01-main のようにブランチを含むため、プロジェクト名では判定できない）
        ids="$(docker ps -a --filter "label=com.bookq.pool_slot=$slot" --format '{{.ID}}' 2>/dev/null || true)"
        if [ -z "$ids" ]; then
            printf '%s' "$slot"
            return 0
        fi
    done
    printf ''
}

# ============================================================ URL

# tailnet ドメイン（例: <your-tailnet>.ts.net）を導出する。
# 優先順位: DEV_TS_DOMAIN > メイン .env の BASE_URL（接頭辞 bookq. を落とす）。
tailnet_domain() {
    local base
    base="${DEV_TS_DOMAIN:-$(dotenv_value BASE_URL || true)}"
    [ -n "$base" ] || return 1
    printf '%s' "$base" | sed -E 's#^https?://(bookq\.)?##; s#/.*$##' | tr -d '[:space:]'
}

# プールスロットの FQDN（例: dev-03.<tailnet>）
pool_fqdn_of() {
    local slot="$1" domain hostname
    hostname="$(pool_hostname_of "$slot")"
    domain="$(tailnet_domain)" || { warn "tailnet ドメインを導出できません。DEV_TS_DOMAIN か .env の BASE_URL を設定してください。"; return 1; }
    printf '%s.%s' "$hostname" "$domain"
}

# sidecar が tailnet に実際登録した FQDN（例: dev-03-1.<tailnet>）。同名の
# ゴーストノードが残っていると Tailscale が自動で -1 等の尾番を付けるため、
# 期待名と一致しないことがある。空の場合は tailscaled 未起動か status 未応答。
actual_dev_fqdn() {
    local slot="$1" gw dns=""
    gw="bookq-dev-tailscaleGateway-dev-${slot}"
    for _ in $(seq 1 10); do
        dns="$(docker exec "$gw" tailscale --socket=/var/run/tailscale/tailscaled.sock status --json 2>/dev/null \
            | python3 -c 'import sys,json; print(json.load(sys.stdin).get("Self",{}).get("DNSName","").rstrip("."))' 2>/dev/null || true)"
        [ -n "$dns" ] && break
        sleep 1
    done
    printf '%s' "$dns"
}

# ローカル検出はラベル com.bookq.local=true のみ（shell export は
# 後続プロセスに見えないため labels が唯一の受渡し手段 — Todo 2 契約）。
# 成功時は http://<bind>:<port> を出力して 0、非ローカル/不在は 1。
# port/bind 空ラベルは防御既定 8080 / 127.0.0.1。
local_url_for() {
    local want="$1" row local port bind
    # rs 環境では local ラベルを持つのは viewer-rs（role=dev-rs）。name ラベルは
    # 両サービスに付くので name で拾い、local=true の行を採る。
    row="$(docker ps -a --filter "label=com.bookq.name" \
        --format '{{.Label "com.bookq.name"}}|{{.Label "com.bookq.local"}}|{{.Label "com.bookq.local_port"}}|{{.Label "com.bookq.local_bind"}}' \
        2>/dev/null | awk -F'|' -v want="$want" '$1 == want && $2 == "true" {print; exit}' || true)"
    [ -n "$row" ] || return 1
    local="${row#*|}"; local="${local%%|*}"
    [ "$local" = "true" ] || return 1
    port="$(printf '%s' "$row" | cut -d'|' -f3)"
    bind="$(printf '%s' "$row" | cut -d'|' -f4)"
    [ -n "$port" ] || port="8080"
    [ -n "$bind" ] || bind="127.0.0.1"
    printf 'http://%s:%s' "$bind" "$port"
}

# ============================================================ compose ラッパ

# 常に -f と -p を明示。down / logs のようにイメージが本質的に不要な操作でも
# compose がファイルを解釈できるよう、解決済みの値（無ければダミー）を渡す。
compose_env_defaults() {
    DEV_IMAGE="${DEV_IMAGE:-bookq-dev:placeholder}"
    DEV_RS_IMAGE="${DEV_RS_IMAGE:-bookq-dev:rs-placeholder}"
    DEV_BACKEND="${DEV_BACKEND:-py}"
    export DEV_RS_IMAGE DEV_BACKEND
    DEV_NAME="${DEV_NAME:-unknown}"
    DEV_ENV_NAME="${DEV_ENV_NAME:-$DEV_NAME}"
    DEV_POOL_SLOT="${DEV_POOL_SLOT:-00}"
    case "$DEV_POOL_SLOT" in
        00|''|*[!0-9]*) : ;; # down/logs 用ダミー — dying helper を通さない
        *) DEV_POOL_SLOT="$(normalize_pool_slot "$DEV_POOL_SLOT")" \
            || die "不正な DEV_POOL_SLOT: ${DEV_POOL_SLOT}（./dev.sh down $DEV_NAME --full で作り直してください）" ;;
    esac
    DEV_HOSTNAME="${DEV_HOSTNAME:-dev-$DEV_POOL_SLOT}"
    export DEV_IMAGE DEV_NAME DEV_ENV_NAME DEV_POOL_SLOT DEV_HOSTNAME
    # compose の補間はカレントの .env しか見ないため、worktree から実行すると
    # DOWNLOADS_HOST_DIR が既定値に落ちてしまう。TS_AUTHKEY と同様に
    # メインのチェックアウトの .env からも解決して渡す。
    if [ -z "${DOWNLOADS_HOST_DIR:-}" ]; then
        DOWNLOADS_HOST_DIR="$(dotenv_value DOWNLOADS_HOST_DIR || true)"
    fi
    if [ -z "${DOWNLOADS_HOST_DIR:-}" ]; then
        DOWNLOADS_HOST_DIR="$SCRIPT_DIR/downloads"
    fi
    [ -z "${DOWNLOADS_HOST_DIR:-}" ] || export DOWNLOADS_HOST_DIR
    if [ -z "${TS_AUTHKEY:-}" ]; then
        TS_AUTHKEY="$(find_authkey || true)"
    fi
    export TS_AUTHKEY
}

compose() {
    local project="$1"; shift
    compose_env_defaults
    # rs 環境は overlay を同梱しないと viewer-rs が compose から見えず、
    # down/logs で orphan 残りになる。viewer 削除済みの残骸でも検出できるよう
    # backend=rs ラベルをプロジェクト全体から探す（viewer/viewer-rs 両方に付く）。
    local files=(-f "$SCRIPT_DIR/$DEV_COMPOSE")
    if [ -n "$(docker ps -aq --filter "label=com.docker.compose.project=$project" \
            --filter "label=com.bookq.backend=rs" 2>/dev/null | head -1)" ]; then
        files+=(-f "$SCRIPT_DIR/$DEV_RS_COMPOSE")
    fi
    docker compose "${files[@]}" -p "$project" "$@"
}

# ローカル専用: viewer だけを起動する追加フラグメント付き。tailscale サービスは
# 決して作らない（up -d viewer で明示）。labels のみが Todo 3 への受渡し手段。
compose_local() {
    local project="$1"; shift
    compose_env_defaults
    docker compose -f "$SCRIPT_DIR/$DEV_COMPOSE" -f "$SCRIPT_DIR/$LOCAL_COMPOSE" -p "$project" "$@"
}

# --local --rs 専用: viewer-rs にポート公開と local ラベルを集約する
# （local-rs フラグメントで viewer-rs が LOCAL_PORT を受ける）。
compose_local_rs() {
    local project="$1"; shift
    compose_env_defaults
    docker compose -f "$SCRIPT_DIR/$DEV_COMPOSE" -f "$SCRIPT_DIR/$DEV_RS_COMPOSE" -f "$SCRIPT_DIR/$LOCAL_RS_COMPOSE" -p "$project" "$@"
}

# ローカル verify: 127.0.0.1 のみ。rebuild.sh dev_verify は DEV_FQDN 必須 +
# https 硬直のため使わない（既存 verify と同じ 30 回リトライの忍耐）。
local_verify() {
    local port="$1" code="" body=""
    local base="http://127.0.0.1:$port"
    printf '\n⏳ ローカル URL を確認しています: %s\n' "$base"
    for _ in $(seq 1 30); do
        code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$base/api/health" || true)"
        [ "$code" = "200" ] && break
        sleep 2
    done
    [ "${code:-}" = "200" ] || die "$base が 200 を返しません (HTTP ${code:-なし})"
    printf '✅ %s は正常に応答しています\n' "$base"
    body="$(curl -s --max-time 10 "$base/api/version" || true)"
    case "$body" in
        *version*) printf '✅ %s/api/version は version JSON を返しました\n' "$base" ;;
        *) die "$base/api/version が version JSON を返しません" ;;
    esac
}

all_projects() {
    docker ps -a --filter "label=$DEV_LABEL" \
        --format '{{.Label "com.docker.compose.project"}}' 2>/dev/null | sort -u || true
}

# ============================================================ ボリューム

# 孤立ボリューム = コンテナが 1 つも無い bookq-dev-* プロジェクトの残骸。
# down はボリュームを保持する仕様（再 up で同じ認証状態に戻す）ため、
# コンテナだけ消えた環境の state がディスクに残り続ける。これを回収する。
#
# compose が自動命名する 3 種だけを対象にするので、次のものは構造的に
# 対象外になる:
#   - 本番のボリューム (bookq_user-state 等) — bookq-dev- 接頭辞を持たない
#   - bookq-dev-pool-XX_tailscale-state — スロット固定の tailnet identity で、
#     Free プランでのスロット再利用設計上あえて保持する（自動命名 3 種ではない）
# 環境名は slugify で [a-z0-9-] に正規化されるためプロジェクト名に '_' は
# 現れず、末尾の '_<volume>' を削ればプロジェクト名が一意に戻る。
AUTO_VOLUME_SUFFIXES="user-state thumb-cache warmup-cache"

project_of_volume() {
    local vol="$1" suffix
    for suffix in $AUTO_VOLUME_SUFFIXES; do
        case "$vol" in
            *"_$suffix") printf '%s' "${vol%_$suffix}"; return 0 ;;
        esac
    done
    return 1
}

# compose プロジェクトのうち Docker 上にコンテナ（停止済みも含む）を持つもの。
# ここに現れないプロジェクトのボリュームだけが孤立と判定される。
live_compose_projects() {
    docker ps -a --format '{{.Label "com.docker.compose.project"}}' 2>/dev/null | sort -u || true
}

# 孤立ボリューム名を 1 行 1 件で出力する（副作用なし）。
list_orphan_volumes() {
    local live vol project
    live="$(live_compose_projects)"
    while IFS= read -r vol; do
        [ -n "$vol" ] || continue
        case "$vol" in
            "$PROJECT_PREFIX"*) ;;
            *) continue ;;
        esac
        project="$(project_of_volume "$vol")" || continue
        # bookq-dev-pool-XX（旧形式）の自動命名ボリュームも対象にする:
        # 現行の up は bookq-dev-XX-<name> しか作らず、死んだ旧プロジェクトの
        # ボリュームを再利用する経路は無い（保持が必要な tailscale-state は
        # 自動命名 3 種ではないので suffix 判定で既に弾かれている）。
        # プロジェクトが生きていれば（コンテナが 1 つでもあれば）残す。
        if printf '%s\n' "$live" | grep -qxF -- "$project"; then
            continue
        fi
        printf '%s\n' "$vol"
    done < <(docker volume ls --format '{{.Name}}' 2>/dev/null || true)
}

# 受け取ったリストのボリュームを削除する。1 件の失敗で全体を止めない
# （使用中・レースで消えた等は警告してスキップ）。
prune_volumes_from_list() {
    local list="$1" vol removed=0 failed=0
    if [ -z "$list" ]; then
        printf 'ℹ️  孤立ボリュームはありません。\n'
        return 0
    fi
    printf '🧹 孤立ボリューム（コンテナの無い bookq-dev-* の残骸）を回収します:\n'
    while IFS= read -r vol; do
        [ -n "$vol" ] || continue
        if docker volume rm "$vol" >/dev/null 2>&1; then
            printf '   🗑️  %s\n' "$vol"
            removed=$((removed + 1))
        else
            warn "ボリューム '$vol' を削除できませんでした（使用中の可能性）。スキップします。"
            failed=$((failed + 1))
        fi
    done <<< "$list"
    printf '♻️  孤立ボリューム: %d 件削除（%d 件スキップ）\n' "$removed" "$failed"
}

# ============================================================ サブコマンド

cmd_up() {
    require_repo_root

    local name="" ref="" fixture="" local_mode="" local_port="8080" local_port_set="" lan_allow="" rs_mode=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --ref)  [ $# -ge 2 ] || die "--ref には値が必要です";  ref="$2";  shift 2 ;;
            --ref=*)  ref="${1#--ref=}";  shift ;;
            --fixture) fixture=1; shift ;;
            --rs) rs_mode=1; shift ;;
            --local) local_mode=1; shift ;;
            --lan-allow) lan_allow=1; shift ;;
            --port) [ $# -ge 2 ] || die "--port には値が必要です"; local_port="$2"; local_port_set=1; shift 2 ;;
            --port=*) local_port="${1#--port=}"; local_port_set=1; shift ;;
            -*) die "不明なオプション: $1" ;;
            *) [ -z "$name" ] && name="$1" || die "引数が多すぎます: $1"; shift ;;
        esac
    done

    # 名前を決める: 明示 > --ref の名前 > 現在のブランチ
    name="$(env_name "${name:-$ref}")"

    # --port/--lan-allow は --local 専用。tailnet 経路にポートの概念は無い。
    if [ -z "$local_mode" ]; then
        [ -z "$lan_allow" ] || die "--lan-allow は --local と併用してください"
        [ -z "$local_port_set" ] || die "--port は --local と併用してください"
    fi

    # --local のポート検証はビルド前に即確定する。数値 + 1024-65535 のみ
    # （privileged <1024 は拒否）。LOCAL_BIND は既定 127.0.0.1、
    # --lan-allow でのみ 0.0.0.0。
    local local_bind="127.0.0.1"
    if [ -n "$local_mode" ]; then
        case "$local_port" in
            ''|*[!0-9]*) die "invalid --port '$local_port': 1024-65535 の範囲で数値指定してください（privileged ports <1024 は使用できません）" ;;
        esac
        local_port="$((10#$local_port))"
        if [ "$local_port" -lt 1024 ] || [ "$local_port" -gt 65535 ]; then
            die "invalid --port '$local_port': 1024-65535 の範囲で指定してください（privileged ports <1024 は使用できません）"
        fi
        if [ -n "$lan_allow" ]; then
            local_bind="0.0.0.0"
        fi
    fi

    # --fixture: 実データの代わりに合成テストツリー（tests/fixtures/uiux_tree.py）を
    # マウントする。DOWNLOADS_HOST_DIR に export するので compose のマウントが
    # そのまま /tmp/bookq-fixture-<name>/downloads を指す（.env の値より優先）。
    if [ -n "$fixture" ]; then
        local fixture_dir="/tmp/bookq-fixture-$name"
        printf '🧪 テストフィクスチャツリーを生成します: %s/downloads\n' "$fixture_dir"
        mkdir -p "$fixture_dir"
        uv run --project "$SCRIPT_DIR" python "$SCRIPT_DIR/tests/fixtures/uiux_tree.py" "$fixture_dir/downloads" >/dev/null \
            || die "フィクスチャツリーの生成に失敗しました (tests/fixtures/uiux_tree.py)"
        DOWNLOADS_HOST_DIR="$fixture_dir/downloads"
        export DOWNLOADS_HOST_DIR
    fi

    # プール割当: 既存なら再利用（冪等）、無ければ空きスロットを取得
    local slot="" project="" hostname="" fqdn=""
    local existing_project existing_slot
    existing_project="$(find_project_for_env "$name" || true)"
    if [ -n "$existing_project" ]; then
        # 既存環境の backend モードをラベルから継承（--rs 無しの再 up でも
        # viewer-rs 構成を維持する）。--rs 指定は py 環境を rs に切替える。
        # viewer 不在の残骸でも判定できるよう backend=rs ラベルを直接探す。
        if [ -n "$(docker ps -aq --filter "label=com.docker.compose.project=$existing_project" \
                --filter "label=com.bookq.backend=rs" 2>/dev/null | head -1)" ]; then
            rs_mode=1
        fi
        case "$existing_project" in
            "$POOL_PREFIX"*)
                slot="$(normalize_pool_slot "${existing_project#$POOL_PREFIX}")" \
                    || die "不正なスロット '${existing_project#$POOL_PREFIX}' です。./dev.sh down $name --full で作り直してください"
                project="$existing_project"
                ;;
            "$PROJECT_PREFIX"*)
                # 現行プール形式（bookq-dev-<slot>-<name>）。pool_slot ラベルから
                # スロットを再利用する（冪等）。ラベルが無い残骸は down --full で作り直す。
                project="$existing_project"
                existing_slot="$(find_slot_for_env "$name" || true)"
                if [ -n "$existing_slot" ]; then
                    slot="$(normalize_pool_slot "$existing_slot")" \
                        || die "不正な pool_slot ラベル '$existing_slot' です。./dev.sh down $name --full で作り直してください"
                else
                    die "pool_slot ラベルの無い環境 '$name' です。./dev.sh down --full $name で作り直してください"
                fi
                ;;
            *)
                project="$existing_project"
                slot="$(find_slot_for_env "$name" || true)"
                if [ -n "$slot" ]; then
                    slot="$(normalize_pool_slot "$slot")" \
                        || die "不正な pool_slot ラベル '$slot' です。./dev.sh down $name --full で作り直してください"
                fi
                ;;
        esac
        if [ -n "$slot" ]; then
            hostname="$(pool_hostname_of "$slot")"
            if [ -n "$local_mode" ]; then
                fqdn=""
            else
                fqdn="$(pool_fqdn_of "$slot")" || die "tailnet ドメインを導出できません。DEV_TS_DOMAIN か .env の BASE_URL を確認してください。"
            fi
        else
            die "pool_slot ラベルの無い環境 '$name' です。./dev.sh down --full $name で作り直してください"
        fi
    else
        slot="$(find_free_slot)"
        if [ -z "$slot" ]; then
            local current_alloc
            current_alloc="$(cmd_list 2>/dev/null | tail -n +2 | tr '\n' ',' | sed 's/,$//' || true)"
            [ -n "$current_alloc" ] || current_alloc="(none)"
            die "pool exhausted (10/10) - use down to free a slot. Current allocation: $current_alloc"
        fi
        project="$(pool_project_of "$slot" "$name")"
        hostname="$(pool_hostname_of "$slot")"
        if [ -n "$local_mode" ]; then
            fqdn=""
        else
            fqdn="$(pool_fqdn_of "$slot")" || die "tailnet ドメインを導出できません。DEV_TS_DOMAIN か .env の BASE_URL を確認してください。"
        fi
    fi

    # --local はポート衝突をビルド前に fail fast（自プロジェクトの再 up は除外）。
    if [ -n "$local_mode" ]; then
        local _pub
        _pub="$(docker ps --format '{{.Label "com.docker.compose.project"}} {{.Ports}}' 2>/dev/null || true)"
        if printf '%s\n' "$_pub" | awk -v proj="$project" -v pat=":$local_port->" '$1 != proj && index($0, pat) {found=1} END {exit !found}'; then
            die "port $local_port is already published by another container (--port で別ポートを指定してください。2つ目の同時環境には --port が必要です)"
        fi
    fi

    # サイドカーの初回認証に必要。本番 (prd.sh) と同じく必須（--local は skip）。
    # OAuth client 設定時は期限切れの authkey をここで自動更新する（警告のみ、失敗しない）。
    if [ -z "$local_mode" ]; then
        [ -x "$SCRIPT_DIR/scripts/ensure-ts-authkey.sh" ] \
            && "$SCRIPT_DIR/scripts/ensure-ts-authkey.sh" || true
        find_authkey >/dev/null || die "TS_AUTHKEY が見つかりません。環境変数に設定するか $(main_checkout)/.env に書いてください。"
    fi

    # --rs は rebuild.sh の dev build/deploy も読む（rs イメージのビルドと
    # compose overlay の追加）。環境変数で渡す。
    if [ -n "$rs_mode" ]; then export DEV_RS=1; fi

    # ビルド（時間がかかるが、他の環境と競合しない）。
    local built tag sha rs_tag=""
    built="$("$SCRIPT_DIR/rebuild.sh" dev build "$name" "$ref")"
    read -r tag sha rs_tag <<< "$built"

    printf '🔧 開発環境 "%s" を起動します (project=%s, slot=%s, hostname=%s, ref=%s)\n\n' \
        "$name" "$project" "$slot" "$hostname" "${ref:-作業ツリー}"
    if [ -n "$rs_mode" ]; then printf '   （backend: viewer-rs / proxy fall-through → viewer:8080）\n\n'; fi
    if [ -n "$local_mode" ]; then
        printf '   （ローカル: tailscale 無し / port=%s bind=%s)\n\n' "$local_port" "$local_bind"
    fi

    export DEV_IMAGE="$tag"
    if [ -n "$rs_mode" ]; then
        export DEV_RS_IMAGE="$rs_tag"
        export DEV_BACKEND="rs"
    fi
    export DEV_NAME="$name"
    export DEV_ENV_NAME="$name"
    export DEV_POOL_SLOT="$(normalize_pool_slot "$slot")"
    export DEV_HOSTNAME="$hostname"
    export DEV_REF="${ref:-worktree}"
    export DEV_COMMIT="$sha"
    export DEV_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
    export DEV_WORKTREE="$PWD"
    export DEV_PROJECT="$project"
    export DEV_FQDN="$fqdn"
    if [ -n "$local_mode" ]; then
        export DEV_LOCAL=1
        export LOCAL_PORT="$local_port"
        export LOCAL_BIND="$local_bind"
    fi

    # DOWNLOADS_HOST_DIR / TS_AUTHKEY をメインの .env から解決して export する
    # （rebuild.sh は compose を直接叩くので、ここでの解決が必須）。
    compose_env_defaults

    # デプロイ → healthcheck + 公開 URL の検証（tailnet 参加・証明書発行の待ちも
    # verify が吸収する）。--local は rebuild.sh dev deploy/verify を使わない
    #（-f $DEV_COMPOSE 固定 + DEV_FQDN/https 硬直のため bypass）。
    # ローカルは viewer のみ・localhost curl のみ。
    if [ -n "$local_mode" ]; then
        if [ -n "$rs_mode" ]; then
            compose_local_rs "$project" up -d viewer viewer-rs \
                || die "ローカル環境 (viewer-rs) の起動に失敗しました。ログ: ./dev.sh logs $name"
        else
            compose_local "$project" up -d viewer \
                || die "ローカル環境の起動に失敗しました。ログ: ./dev.sh logs $name"
        fi
        local_verify "$local_port"
    else
        "$SCRIPT_DIR/rebuild.sh" dev deploy
        # 実登録名が期待と違う（= 同名ゴーストノード残存で -1 付与）場合は
        # まず自動復旧を試みる。残った場合だけ警告しつつ実名で verify する。
        # 期待名のまま待つと verify_url が offline ノードへ最大 ~6 分ハングする
        #（verify は 30×10s リトライ）。
        actual_fqdn="$(actual_dev_fqdn "$slot")"
        if [ -n "$actual_fqdn" ] && [ "$actual_fqdn" != "$fqdn" ]; then
            if "$SCRIPT_DIR/rebuild.sh" dev heal-fqdn; then
                actual_fqdn="$(actual_dev_fqdn "$slot")"
            fi
        fi
        if [ -n "$actual_fqdn" ] && [ "$actual_fqdn" != "$fqdn" ]; then
            warn "tailnet 登録名が期待と異なります: 期待 $fqdn → 実際 $actual_fqdn"
            warn "同名のゴーストノードが tailnet に残っています。admin コンソールで旧 $hostname ノードを削除して sidecar を docker restart すると $fqdn で使えます（docs/deployment.md「ノード名の衝突」参照）。"
            export DEV_FQDN="$actual_fqdn"
        fi
        "$SCRIPT_DIR/rebuild.sh" dev verify
    fi

    # up 成功後の自動お掃除。gc の失敗は up を失敗させない。
    cmd_gc || warn "gc の自動実行に失敗しました（up 自体には影響ありません）。"

    printf '\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n'
    printf '✅ 開発環境 "%s" が起動しました（認証なし / データは読み取り専用）\n' "$name"
    printf '   %s @ %s\n\n' "${ref:-作業ツリー}" "$sha"
    if [ -n "$local_mode" ]; then
        printf '   http://127.0.0.1:%s\n' "$local_port"
        if [ "$local_bind" = "0.0.0.0" ]; then
            printf '   (LAN 公開中 0.0.0.0:%s — 未認証ツリーが LAN に出ます。lab 用のみ)\n\n' "$local_port"
        else
            printf '   (slot %s / loopback のみ。LAN 公開は --lan-allow)\n\n' "$slot"
        fi
        printf '   2つ目の同時環境には --port <n> が必要です（既定 8080 は衝突します）\n\n'
    else
        printf '   https://%s\n' "$DEV_FQDN"
        printf '   (slot %s / hostname %s, tailnet 内のどの端末からでも。ポートは公開しません — ホストからも HTTPS)\n\n' "$slot" "$hostname"
    fi
    printf '停止:  ./dev.sh down %s\n' "$name"
    printf '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n'
}

cmd_down() {
    require_repo_root

    # cmd_up と同じループスタイルでパース。--full は名前導出より先に消費する
    # （./dev.sh down --full が 'full' という環境名を導出しないように）。
    local name="" all=0 full=0 yes=0 prune=1 project found=0 slot hostname orphan_vols=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --full) full=1; shift ;;
            --all)  all=1; shift ;;
            --yes)  yes=1; shift ;;
            --no-prune-volumes) prune=0; shift ;;
            -*) die "不明なオプション: $1" ;;
            *) [ -z "$name" ] && name="$1" || die "引数が多すぎます: $1"; shift ;;
        esac
    done

    if [ "$all" = 1 ] && [ -n "$name" ]; then
        die "--all と環境名は併用できません"
    fi
    if [ "$all" = 1 ] && [ "$full" = 1 ] && [ "$yes" = 0 ]; then
        die "--all --full は全環境の完全撤去です。--yes を付けて確認してください"
    fi
    if [ "$prune" = 0 ] && [ "$all" = 0 ]; then
        die "--no-prune-volumes は --all と併用してください"
    fi

    if [ "$all" = 1 ]; then
        # 孤立ボリュームは teardown の前に確定させる。後で数えると、この
        # down で停止しただけの環境（ボリュームは保持が仕様）まで孤立に見え、
        # 保持すべき state を消してしまう。
        if [ "$prune" = 1 ]; then
            orphan_vols="$(list_orphan_volumes)"
        fi
        for project in $(all_projects); do
            [ -n "$project" ] || continue
            case "$project" in
                "$POOL_PREFIX"*|"$PROJECT_PREFIX"*) ;;
                *) warn "開発環境ではないプロジェクト '$project' を飛ばしました。"; continue ;;
            esac
            # derive name/slot for logging
            local env_for_project pool_slot_for_project
            env_for_project="$(docker ps -a --filter "label=com.docker.compose.project=$project" --format '{{.Label "com.bookq.name"}}' 2>/dev/null | head -1 || true)"
            pool_slot_for_project="$(docker ps -a --filter "label=com.docker.compose.project=$project" --format '{{.Label "com.bookq.pool_slot"}}' 2>/dev/null | head -1 || true)"
            if [ -z "$env_for_project" ]; then
                # fallback derive from project suffix
                case "$project" in
                    "$POOL_PREFIX"*) env_for_project="(pool ${project#$POOL_PREFIX})" ;;
                    "$PROJECT_PREFIX"*) env_for_project="${project#$PROJECT_PREFIX}" ;;
                    *) env_for_project="$project" ;;
                esac
            fi
            if [ "$full" = 1 ]; then
                # need name for down_full first arg; use env label or project suffix
                local down_name
                down_name="$(docker ps -a --filter "label=com.docker.compose.project=$project" --format '{{.Label "com.bookq.name"}}' 2>/dev/null | head -1 || true)"
                [ -n "$down_name" ] || down_name="${project#$PROJECT_PREFIX}"
                [ -n "$down_name" ] || down_name="${project#$POOL_PREFIX}"
                down_full "$down_name" "$project"
            else
                printf '🧹 %s を削除します' "$project"
                if [ -n "$pool_slot_for_project" ]; then
                    printf ' (slot %s)' "$pool_slot_for_project"
                fi
                printf '\n'
                compose "$project" down
                if [ -n "$pool_slot_for_project" ]; then
                    printf '✅ 削除しました（ボリュームは保持。再 up で同じスロット %s を再利用） slot dev-%s freed\n' "$pool_slot_for_project" "$pool_slot_for_project"
                else
                    printf '✅ 削除しました（ボリュームは保持。再 up で同じ名前・同じ認証状態に戻ります）\n'
                fi
            fi
            found=1
        done
        [ "$found" = 1 ] || printf 'ℹ️  削除する開発環境はありません。\n'
        if [ "$prune" = 1 ]; then
            prune_volumes_from_list "$orphan_vols"
        fi
        return
    fi

    name="$(env_name "$name")"
    project="$(find_project_for_env "$name" || true)"
    if [ -z "$project" ]; then
        # legacy fallback: try conventional project name
        local legacy_project
        legacy_project="$(project_of "$name")"
        if [ -n "$(docker ps -a --filter "label=com.docker.compose.project=$legacy_project" --format '{{.ID}}' 2>/dev/null || true)" ]; then
            project="$legacy_project"
        else
            die "environment not found: $name"
        fi
    fi
    slot="$(find_slot_for_env "$name" || true)"
    if [ -n "$slot" ]; then
        slot="$(normalize_pool_slot "$slot")" \
            || die "不正な pool_slot ラベル '$slot' です。./dev.sh down $name --full で作り直してください"
    fi
    # also derive slot from project name if label missing but project is pool
    if [ -z "$slot" ]; then
        case "$project" in
            "$POOL_PREFIX"*)
                slot="$(normalize_pool_slot "${project#$POOL_PREFIX}")" \
                    || die "不正なスロット '${project#$POOL_PREFIX}' です。./dev.sh down $name --full で作り直してください"
                ;;
        esac
    fi
    if [ "$full" = 1 ]; then
        down_full "$name" "$project"
    else
        if [ -n "$slot" ]; then
            hostname="$(pool_hostname_of "$slot")"
            printf '🧹 開発環境 "%s" (%s, slot %s / %s) を削除します\n' "$name" "$project" "$slot" "$hostname"
        else
            printf '🧹 開発環境 "%s" (%s) を削除します\n' "$name" "$project"
        fi
        compose "$project" down
        if [ -n "$slot" ]; then
            printf '✅ 削除しました（ボリュームは保持。再 up で同じスロットを再利用） slot dev-%s freed\n' "$slot"
        else
            printf '✅ 削除しました（ボリュームは保持。再 up で同じ名前・同じ認証状態に戻ります）\n'
        fi
    fi
}

# 完全撤去（--full）: 素の down の削除に加えて、tailscale-state ボリュームと
# viewer/サイドカーイメージを削除し、TS_API_KEY 設定時は tailnet のノード登録
# dev-<名前> を API で抹消する（同じ名前での再 up が可能になる）。キー未設定・
# API 失敗は警告だけで続行し、down 自体は失敗させない。
# プールスロット（dev-01..dev-10）は Free プランでの API 削除を不要にするため、
# hostname が dev-[0-9][0-9] の場合は API 削除をスキップして state ボリュームの
# 再利用で identity を保つ。
down_full() {
    local name="$1" project="$2"
    local labels commit="" ref="" image_tag="" rest="" node_done=0 api_key bearer="" ids code devices found=0 http deleted=0 slot_for_api=""
    local api_auth_args=()

    # pool slot を導出して API スキップ判定に使う（数値は repair、ラベル有りの不正値は die）
    slot_for_api="$(docker ps -a --filter "label=com.docker.compose.project=$project" --format '{{.Label "com.bookq.pool_slot"}}' 2>/dev/null | head -1 || true)"
    if [ -n "$slot_for_api" ]; then
        slot_for_api="$(normalize_pool_slot "$slot_for_api")" \
            || die "不正な pool_slot ラベル '$slot_for_api' です。./dev.sh down $name --full で作り直してください"
    fi
    if [ -z "$slot_for_api" ]; then
        case "$project" in
            "$POOL_PREFIX"*)
                slot_for_api="$(normalize_pool_slot "${project#$POOL_PREFIX}")" \
                    || die "不正なスロット '${project#$POOL_PREFIX}' です。./dev.sh down $name --full で作り直してください"
                ;;
        esac
    fi
    case "$slot_for_api" in
        0[1-9]|10) slot_for_api="$slot_for_api" ;;
        *) slot_for_api="" ;;
    esac

    if [ -n "$slot_for_api" ]; then
        printf '🧹 開発環境 "%s" (%s, slot %s) を完全撤去します（コンテナ・ボリューム %s_tailscale-state・イメージ）\n' \
            "$name" "$project" "$slot_for_api" "$project"
    else
        printf '🧹 開発環境 "%s" (%s) を完全撤去します（コンテナ・ボリューム %s_tailscale-state・イメージ・ノード登録 dev-%s）\n' \
            "$name" "$project" "$project" "$name"
    fi

    # Step 0: compose down で消える前に viewer コンテナのラベルを読む。
    # com.bookq.role=dev で viewer に絞る（サイドカーにはこのラベルが無い）。
    # 環境が既に無い場合はラベルが空 → Step 3 のイメージ削除をスキップして続行。
    labels="$(docker ps -a \
        --filter "label=com.docker.compose.project=$project" \
        --filter "label=com.bookq.role=dev" \
        --format '{{.Label "com.bookq.commit"}}|{{.Label "com.bookq.ref"}}|{{.Label "com.bookq.dev-image-tag"}}' \
        2>/dev/null | head -1 || true)"
    if [ -n "$labels" ]; then
        commit="${labels%%|*}"
        rest="${labels#*|}"
        ref="${rest%%|*}"
        image_tag="${rest#*|}"
    fi

    # Step 1: ノード登録 dev-<名前> の削除（API 先行・非致命的）。プールスロット
    # dev-01..dev-10 は Free プランの再利用設計のため API 削除をスキップする。
    # TS_API_KEY は環境変数、無ければメインの .env から。
    if [ -n "$slot_for_api" ]; then
        printf 'ℹ️  プールスロット dev-%s は再利用のためノード登録の API 削除をスキップします（state ボリュームは compose down -v で削除されます）\n' "$slot_for_api"
        node_done=1
    else
        # admin API の認証は OAuth client の短命トークンを優先（tskey-api- は
        # 最長90日で失効する）。未設定なら従来の TS_API_KEY basic auth に落ちる。
        bearer=""
        if [ -x "$SCRIPT_DIR/scripts/ensure-ts-authkey.sh" ]; then
            bearer="$("$SCRIPT_DIR/scripts/ensure-ts-authkey.sh" --token 2>/dev/null || true)"
        fi
        api_key="${TS_API_KEY:-}"
        [ -n "$api_key" ] || api_key="$(dotenv_value TS_API_KEY || true)"
        if [ -n "$bearer" ]; then
            api_auth_args=(-H "Authorization: Bearer $bearer")
        elif [ -n "$api_key" ]; then
            api_auth_args=(-u "$api_key:")
        fi
        if [ ${#api_auth_args[@]} -eq 0 ]; then
            warn "TS_API_KEY / OAuth client が未設定のため、ノード登録 dev-$name は削除しません。"
        elif ! command -v python3 >/dev/null 2>&1; then
            warn "python3 が見つからないため、ノード登録 dev-$name を削除できません。"
        else
            devices="$(curl -s --max-time 10 -w '\n%{http_code}' "${api_auth_args[@]}" \
                "https://api.tailscale.com/api/v2/tailnet/${TS_TAILNET:--}/devices" 2>/dev/null || true)"
            http="$(printf '%s' "$devices" | tail -1)"
            devices="$(printf '%s' "$devices" | sed '$d')"
            case "$http" in
                200)
                    if ! ids="$(printf '%s' "$devices" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
want = sys.argv[1] if len(sys.argv) > 1 else ""
for d in data.get("devices", []):
    if d.get("hostname") == want:
        print(d.get("id", ""))
' "dev-$name")"; then
                        warn "ノード一覧の解析に失敗しました（ノード登録 dev-$name は残存します）。"
                    else
                        node_done=1
                        found=0
                        deleted=0
                        while IFS= read -r id; do
                            [ -n "$id" ] || continue
                            found=1
                            code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -X DELETE \
                                "${api_auth_args[@]}" "https://api.tailscale.com/api/v2/device/$id" || true)"
                            case "$code" in
                                200) printf '🗑️  ノード登録 dev-%s を削除しました\n' "$name"; deleted=1 ;;
                                404) printf 'ℹ️  ノード登録 dev-%s は既に存在しません\n' "$name" ;;
                                401|403)
                                    warn "Tailscale API キーが不正です（ノード登録 dev-$name は残存します）。"
                                    node_done=0 ;;
                                *)
                                    warn "ノード登録 dev-$name の削除に失敗しました（HTTP $code）。"
                                    node_done=0 ;;
                            esac
                        done <<< "$ids"
                        if [ "$found" = 0 ]; then
                            printf 'ℹ️  ノード登録 dev-%s は存在しません（削除不要）\n' "$name"
                        fi
                    fi
                    ;;
                401|403)
                    warn "Tailscale API キーが不正です（ノード登録 dev-$name は残存します）。"
                    ;;
                *)
                    warn "Tailscale API に接続できませんでした（ノード登録 dev-$name は残存します）。"
                    ;;
            esac
        fi
    fi

    # Step 2: コンテナ・ネットワーク・tailscale-state ボリューム・サイドカー
    # イメージ（<project>_tailscale）を削除（--rmi local）。
    compose "$project" down -v --rmi local
    if [ -n "$slot_for_api" ]; then
        printf 'ℹ️  slot dev-%s freed (state volume removed, hostname will be reused on next up)\n' "$slot_for_api"
    fi

    # Step 3: viewer イメージを削除。(a) 新方式: コンテナラベル
    # com.bookq.dev-image-tag に記録されたタグ（エイリアス bookq-dev:ref-<slug>
    # または sha12）を最優先で削除 — ラベルは dev_build の実際の出力なので、
    # 環境名と ref slug が一致しない場合（up <name> --ref <branch> 等）も正しい
    # タグを指す。(b) フォールバック: 新ラベルを持たない旧環境向けに従来ロジックを
    # elif チェーンで「ラベルが無い時だけ」実行する。連続 if にすると新方式の
    # --ref 環境でも bookq-dev:<sha12> を剥がしてしまい、他の --ref 環境が
    # 再利用する共有 SHA キャッシュ（rebuild.sh が作る不変タグ）を壊す。worktree
    # 環境の wt-<名前> も同様。不存在・使用中は寛容にスキップ。
    if [ -n "$image_tag" ] && [ "$image_tag" != "unknown" ]; then
        docker image rm "$image_tag" >/dev/null 2>&1 || true
    elif [ "$ref" = "worktree" ]; then
        docker image rm "bookq-dev:wt-$name" >/dev/null 2>&1 || true
    elif [ -n "$commit" ] && [ "$commit" != "unknown" ]; then
        docker image rm "bookq-dev:$commit" >/dev/null 2>&1 || true
    fi

    # viewer-rs イメージも同じ dev-image-tag ラベルで記録されている（rs 環境のみ）。
    local rs_tag
    rs_tag="$(docker ps -a \
        --filter "label=com.docker.compose.project=$project" \
        --filter "label=com.docker.compose.service=viewer-rs" \
        --format '{{.Label "com.bookq.dev-image-tag"}}' 2>/dev/null | head -1 || true)"
    if [ -n "$rs_tag" ] && [ "$rs_tag" != "unknown" ]; then
        docker image rm "$rs_tag" >/dev/null 2>&1 || true
    fi

    if [ -n "$slot_for_api" ]; then
        printf '✅ 完全撤去しました（ボリューム・イメージ。slot dev-%s は次回 up で再利用されます）\n' "$slot_for_api"
    elif [ "$node_done" = 1 ] && [ "$found" = 1 ] && [ "$deleted" = 1 ]; then
        printf '✅ 完全撤去しました（ボリューム・イメージ・ノード登録 dev-%s）\n' "$name"
    elif [ "$node_done" = 1 ] && [ "$found" = 1 ]; then
        printf '✅ 完全撤去しました（ボリューム・イメージ。ノード登録 dev-%s は既に存在しません）\n' "$name"
    elif [ "$node_done" = 1 ]; then
        printf '✅ 完全撤去しました（ボリューム・イメージ。ノード登録 dev-%s は存在しません）\n' "$name"
    else
        printf '✅ ローカル資源は完全撤去しました（ノード登録 dev-%s は残存）\n' "$name"
        warn "ノード登録 dev-$name は残存します。同じ名前で再 up するには管理コンソール（login.tailscale.com/admin/machines）で削除するか、TS_API_KEY を設定してください。"
    fi
}

cmd_list() {
    require_repo_root
    local rows domain
    rows="$(docker ps -a --filter "label=$DEV_LABEL" \
        --format '{{.Label "com.bookq.name"}}|{{.Label "com.bookq.pool_slot"}}|{{.State}}|{{.Label "com.bookq.ref"}}|{{.Label "com.bookq.commit"}}|{{.Label "com.bookq.local"}}|{{.Label "com.bookq.local_port"}}|{{.Label "com.bookq.local_bind"}}' \
        2>/dev/null | sort -u || true)"

    if [ -z "$rows" ]; then
        printf 'ℹ️  開発環境はありません。./dev.sh up で作成できます。\n'
        return
    fi

    domain="$(tailnet_domain 2>/dev/null || true)"
    printf '%-30s %-6s %-9s %-24s %-13s %s\n' "ENV" "SLOT" "STATE" "REF" "COMMIT" "URL"
    printf '%s\n' "$rows" | while IFS='|' read -r name slot state ref commit is_local local_port local_bind; do
        [ -n "$name" ] || continue
        # legacy fallback: slot が空なら hostname 由来や "-" を表示
        # "-" /空センチネルは dying helper に通さない（set -e 下の while-read を abort するため）
        if [ -n "$slot" ]; then
            case "$slot" in
                "-"|"") : ;;
                *) slot="$(normalize_pool_slot "$slot")" \
                    || die "不正な pool_slot ラベル '$slot' です。./dev.sh down $name --full で作り直してください" ;;
            esac
        else
            # try to derive from project hostname label; fallback to "-"
            local proj_for_row
            proj_for_row="$(find_project_for_env "$name" || true)"
            case "$proj_for_row" in
                "$POOL_PREFIX"*)
                    slot="$(normalize_pool_slot "${proj_for_row#$POOL_PREFIX}")" \
                        || die "不正なスロット '${proj_for_row#$POOL_PREFIX}' です。./dev.sh down $name --full で作り直してください"
                    ;;
                *) slot="-" ;;
            esac
        fi
        local url
        if [ "$is_local" = "true" ]; then
            [ -n "$local_port" ] || local_port="8080"
            [ -n "$local_bind" ] || local_bind="127.0.0.1"
            url="http://$local_bind:$local_port"
        elif [ -n "$domain" ]; then
            case "$slot" in
                0[1-9]|10)
                    url="https://dev-$slot.$domain"
                    ;;
                "-")
                    url="https://dev-$name.$domain"
                    ;;
                *)
                    # slot が数値以外（legacy）なら dev-<name> で URL を組む
                    url="https://dev-$name.$domain"
                    ;;
            esac
        else
            url="(DEV_TS_DOMAIN 未設定)"
        fi
        printf '%-30s %-6s %-9s %-24s %-13s %s\n' "$name" "$slot" "$state" "$ref" "$commit" "$url"
    done

    # キー期限切れの静的警告（ネットワーク不要の --quick モード）
    [ -x "$SCRIPT_DIR/scripts/ensure-ts-authkey.sh" ] \
        && "$SCRIPT_DIR/scripts/ensure-ts-authkey.sh" --quick || true
}

cmd_url() {
    require_repo_root
    local name slot fqdn domain
    name="$(env_name "${1:-}")"
    local local_url
    if local_url="$(local_url_for "$name")"; then
        printf '%s\n' "$local_url"
        return 0
    fi
    slot="$(find_slot_for_env "$name" || true)"
    if [ -n "$slot" ]; then
        slot="$(normalize_pool_slot "$slot")" \
            || die "不正な pool_slot ラベル '$slot' です。./dev.sh down $name --full で作り直してください"
    fi
    if [ -z "$slot" ]; then
        # also try derive from project name if pool
        local proj
        proj="$(find_project_for_env "$name" || true)"
        case "$proj" in
            "$POOL_PREFIX"*)
                slot="$(normalize_pool_slot "${proj#$POOL_PREFIX}")" \
                    || die "不正なスロット '${proj#$POOL_PREFIX}' です。./dev.sh down $name --full で作り直してください"
                ;;
        esac
    fi
    if [ -n "$slot" ]; then
        case "$slot" in
            0[1-9]|10)
                fqdn="$(pool_fqdn_of "$slot")" || die "tailnet ドメインを導出できません。DEV_TS_DOMAIN か .env の BASE_URL を確認してください。"
                printf 'https://%s\n' "$fqdn"
                return 0
                ;;
        esac
    fi
    warn "environment '$name' not allocated. run: ./dev.sh up $name"
    return 1
}

cmd_logs() {
    require_repo_root
    local raw="" name project slot
    if [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then raw="$1"; shift; fi
    name="$(env_name "$raw")"
    project="$(find_project_for_env "$name" || true)"
    if [ -z "$project" ]; then
        project="$(project_of "$name")"
        # verify existence; if no container for that project, still try compose logs (will error informatively)
        if [ -z "$(docker ps -a --filter "label=com.docker.compose.project=$project" --format '{{.ID}}' 2>/dev/null || true)" ]; then
            # also check pool fallback: maybe slot allocated but name lookup failed due to label timing
            # try free-slot check? just die with not found if truly absent
            local check_any
            check_any="$(docker ps -a --filter "label=$DEV_LABEL" --format '{{.Label "com.bookq.name"}}' 2>/dev/null | grep -Fx "$name" || true)"
            if [ -z "$check_any" ]; then
                warn "environment '$name' not found; trying legacy project $project"
            fi
        fi
    fi
    compose "$project" logs "$@"
}

# 未参照・dangling イメージの回収（./dev.sh gc / up 成功後の自動実行）。
#
# 対象と安全設計:
#   1. 自作ラベル com.bookq.dev-image=1 の付いた dangling イメージ。
#   2. compose ビルドのサイドカーイメージの dangling。ただしプロジェクトラベル
#      com.docker.compose.project が bookq-dev-* に一致するものだけ。
#   3. bookq-dev リポジトリのうち dev コンテナから参照されていないイメージ。
#      BOOKQ_GC_GRACE_MIN（既定 30 分）より新しいものは回収しない — 並行
#      エージェントが別環境を up 中（build 完了・コンテナ作成前）の新イメージを
#      誤回収すると相手の up が失敗するため（docs/deployment.md の並行安全性原則）。
#
# 本番プロジェクト bookq のイメージには一切触れない: フィルタは自作ラベル /
# bookq-dev リポジトリ / bookq-dev-* プロジェクト接頭辞に厳密限定。
# rmi に force は使わない — 停止コンテナから参照されている場合は削除に失敗するが、
# それを寛容にスキップするのが設計（-f は停止環境のイメージを壊すので禁止）。
# 年齢は docker inspect の {{.Created}}（RFC3339 UTC）を epoch 化して比較する
# （docker images の {{.CreatedAt}} は TZ 略称が付いて BSD date でパースできない）。
# 解析不能なタイムスタンプは「若い」扱いでスキップ（fail-closed）— 誤って「古い」
# 扱いになると並行保護が静かに失効するため。
cmd_gc() {
    local labeled_del=0 labeled_skip=0 sidecar_del=0 sidecar_skip=0
    local sweep_del=0 skip_inuse=0 skip_young=0
    local id cid img rid ts epoch now grace age_min in_use
    local ts_ref proj iproj ref_ids img_map tags

    # 猶予分数は非負整数に限定する。非数値のまま `[ -lt ]` に渡すと test 自体が
    # エラー終了し、if 条件内では false 扱いになって「猶予なし」で全削除に
    # 落ちる（fail-open）。ここで既定値へ落として fail-closed を保つ。
    grace="${BOOKQ_GC_GRACE_MIN:-30}"
    case "$grace" in ''|*[!0-9]*) grace=30 ;; esac

    # --- 1) 自作ラベル付き dangling ---
    while IFS= read -r id; do
        [ -n "$id" ] || continue
        if docker image rm "$id" >/dev/null 2>&1; then
            labeled_del=$((labeled_del + 1))
        else
            labeled_skip=$((labeled_skip + 1))
        fi
    done < <(docker images -f dangling=true --filter label=com.bookq.dev-image=1 --format '{{.ID}}')
    printf '♻️  ラベル付き dangling: %d 枚削除（%d 枚は参照中のためスキップ）\n' "$labeled_del" "$labeled_skip"

    # --- 2) サイドカー dangling（bookq-dev-* プロジェクトのみ）---
    # compose ビルドのサイドカーイメージが com.docker.compose.project ラベルを
    # 持つかはライブで確認してから分岐する（2026-08 実測: 持つ —
    # bookq-dev-main-tailscale:latest → {"com.docker.compose.project": ...}）。
    ts_ref="$(docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -m1 -E '[-_]tailscale:' || true)"
    proj=""
    if [ -n "$ts_ref" ]; then
        proj="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$ts_ref" 2>/dev/null || true)"
        [ "$proj" = "<no value>" ] && proj=""
    fi
    if [ -z "$proj" ]; then
        printf 'ℹ️  サイドカーの自動回収は不可(ラベル無し)\n'
    else
        while IFS= read -r id; do
            [ -n "$id" ] || continue
            iproj="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$id" 2>/dev/null || true)"
            case "$iproj" in
                bookq-dev-*)
                    if docker image rm "$id" >/dev/null 2>&1; then
                        sidecar_del=$((sidecar_del + 1))
                    else
                        sidecar_skip=$((sidecar_skip + 1))
                    fi
                    ;;
            esac
        done < <(docker images -f dangling=true --format '{{.ID}}')
        printf '♻️  サイドカー dangling: %d 枚削除（%d 枚は参照中のためスキップ）\n' "$sidecar_del" "$sidecar_skip"
    fi

    # --- 3) 未参照 sweep（30 分の猶予期間ガード付き）---
    now="$(date -ju +%s)"

    # 全 dev コンテナ（稼働・停止両方）が参照するイメージ ID の集合 R。
    # コンテナ側 {{.Image}} は sha256:<64hex>、docker images の {{.ID}} は 12 桁の
    # 短縮 ID — 前方一致で照合する。
    # role=dev ではなく com.bookq.name で拾う: viewer-rs (role=dev-rs) が参照する
    # rs イメージも「使用中」と判定するため（停止環境の誤回収防止）。
    ref_ids=" "
    while IFS= read -r cid; do
        [ -n "$cid" ] || continue
        img="$(docker inspect -f '{{.Image}}' "$cid" 2>/dev/null || true)"
        [ -n "$img" ] && ref_ids="$ref_ids${img#sha256:} "
    done < <(docker ps -aq --filter "label=com.bookq.name")

    # 候補は bookq-dev リポジトリのタグ付きイメージのみ（dangling は手順 1/2）。
    # 削除は ID ではなく「その ID の持つ bookq-dev タグ」に対して行う:
    # FROM ベースだけのイメージはベースと同一 ID になり、ID 指定の rmi は
    # 「referenced in multiple repositories」で必ず失敗するため。この方式なら
    # python:3.13-slim 等の非 dev タグに触れようがない（本件の安全設計と一致）。
    img_map="$(docker images bookq-dev --format '{{.ID}} {{.Repository}}:{{.Tag}}' | sort -u || true)"
    while IFS= read -r id; do
        [ -n "$id" ] || continue
        in_use=0
        for rid in $ref_ids; do
            case "$rid" in "$id"*) in_use=1; break ;; esac
        done
        if [ "$in_use" = 1 ]; then
            skip_inuse=$((skip_inuse + 1))
            continue
        fi
        ts="$(docker inspect -f '{{.Created}}' "$id" 2>/dev/null || true)"
        epoch=""
        if [ -n "$ts" ]; then
            epoch="$(date -ju -f '%Y-%m-%dT%H:%M:%S' "$(printf '%s' "$ts" | cut -c1-19)" +%s 2>/dev/null || true)"
        fi
        if [ -z "$epoch" ]; then
            skip_young=$((skip_young + 1))   # fail-closed: 解析不能は「若い」扱い
            continue
        fi
        age_min=$(( (now - epoch) / 60 ))
        if [ "$age_min" -lt "$grace" ]; then
            skip_young=$((skip_young + 1))
            continue
        fi
        tags="$(printf '%s\n' "$img_map" | awk -v id="$id" '$1 == id {print $2}')"
        [ -n "$tags" ] || continue
        if docker image rm $tags >/dev/null 2>&1; then
            sweep_del=$((sweep_del + 1))
        else
            skip_inuse=$((skip_inuse + 1))
        fi
    done < <(printf '%s\n' "$img_map" | awk '{print $1}' | sort -u)
    printf '♻️  未参照 sweep: %d 枚削除（使用中 %d 枚・猶予未満 %d 枚はスキップ / 猶予 %s 分）\n' \
        "$sweep_del" "$skip_inuse" "$skip_young" "$grace"
}

# 孤立ボリュームの回収。down --all からも呼ばれるが、単体で先払いで掃除
# したいとき用に --dry-run で対象の確認だけもできる。
cmd_gc_volumes() {
    local dry=0 list vol
    while [ $# -gt 0 ]; do
        case "$1" in
            --dry-run) dry=1; shift ;;
            -*) die "不明なオプション: $1" ;;
            *) die "引数が多すぎます: $1" ;;
        esac
    done

    list="$(list_orphan_volumes)"
    if [ "$dry" = 1 ]; then
        if [ -z "$list" ]; then
            printf 'ℹ️  孤立ボリュームはありません。\n'
            return 0
        fi
        printf '🔍 削除対象の孤立ボリューム（--dry-run のため未削除）:\n'
        while IFS= read -r vol; do
            [ -n "$vol" ] && printf '   - %s\n' "$vol"
        done <<< "$list"
        return 0
    fi
    prune_volumes_from_list "$list"
}

# Print the whole leading comment block — deriving the range from the comments
# themselves, so adding or removing a subcommand can't truncate --help.
usage() { awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"; }

case "${1:-}" in
    up)        shift; cmd_up "$@" ;;
    down)      shift; cmd_down "$@" ;;
    list|ls)   shift; cmd_list ;;
    url)       shift; cmd_url "${1:-}" ;;
    logs)      shift; cmd_logs "$@" ;;
    gc)        shift; cmd_gc "$@" ;;
    gc-volumes) shift; cmd_gc_volumes "$@" ;;
    ""|-h|--help|help) usage ;;
    *) die "不明なサブコマンド: $1（--help を参照）" ;;
esac
