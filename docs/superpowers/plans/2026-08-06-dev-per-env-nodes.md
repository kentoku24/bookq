> Superseded 2026-08 by the 10-slot pool design in `dev.sh` / `docs/deployment.md`. Kept for history.
# dev環境を per-env ノード構成に移行する実装プラン

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 開発環境を本番と同じ「viewer + tailscale サイドカー」の独立コンテナスタックに移行し、ポート採番・serve 収束・排他ロックの仕組みごと消す。

**Architecture:** 現在の開発環境は「共有ゲートウェイ 1 ノード + ポート割り当て + serve の reconcile + 排他ロック」で動いている。これを「1 環境 = 1 compose プロジェクト = 1 tailnet ノード（`dev-<名前>`）」に置き換える。ノード名がそのまま HTTPS URL（`https://dev-<名前>.<tailnet>.ts.net`）になり決定的なので、ポート・serve・ロックが構造的に不要になる。`tailscale/entrypoint.sh` は無変更（`TS_SERVE_TARGET` 未指定 = 既定の `http://viewer:8080` → :443 serve がそのまま使える）。

**Tech Stack:** bash / docker compose / Tailscale（entrypoint 既存機能のみ使用。jq・lsof・tailscale CLI 依存は dev.sh からすべて消える）

**前提（確認済み）:** ホストは tailnet に参加済み。localhost 直接アクセスは廃止してよい。ACL 変更は許容。dev 環境は 10 個未満・tailnet ノードは 100 枠なのでノード数は問題にならない。

---

### Task 1: docker-compose.dev.yml を本番パターンに書き換え、ゲートウェイを削除

**Files:**
- Rewrite: `docker-compose.dev.yml`
- Delete: `docker-compose.gateway.yml`

- [ ] **Step 1: `docker-compose.dev.yml` を以下の内容で書き換える（全置換）**

```yaml
# bookq — 開発環境（1 環境 = 1 プロジェクト = tailnet ノード 1 つ）
#
# 本番 (docker-compose.yml) と同じ構成パターン: viewer + tailscale サイドカー。
# 到達経路も同じく https://dev-<名前>.<tailnet>.ts.net（HTTPS :443）だけ。
# ホストにポートを公開しない — ホストからのアクセスも HTTPS 経由
# （ホストは tailnet に参加していることが前提）。
#
# イメージは dev.sh が事前にビルドして DEV_IMAGE で渡す。ここで build: を
# 書かないのは、--ref 指定時に「作業ツリーではなく特定コミットの中身」を
# git archive からビルドするため（同じ SHA なら常に同じイメージになる）。
#
# 直接 docker compose を叩かないこと。必須変数 (:?) が未設定だと失敗する。

services:
  viewer:
    image: "${DEV_IMAGE:?DEV_IMAGE is required — use ./dev.sh up}"
    container_name: "bookq-dev-${DEV_NAME:?DEV_NAME is required — use ./dev.sh up}-viewer"

    # 本番と同じ画像ツリーを *読み取り専用* でマウント。
    volumes:
      - "${DOWNLOADS_HOST_DIR:-/Users/you/Documents/GitHub/library/downloads}:/app/downloads:ro"

    environment:
      # OAuth を丸ごとバイパス（app/auth.py が import 時に一度だけ読む）。
      - BOOKQ_TEST_MODE=true
      - SECRET_KEY=dev-not-a-secret
      - WEB_CONCURRENCY=1

    # dev.sh はこのラベルだけを情報源にする（状態ファイルを持たない）。
    labels:
      com.bookq.role: dev
      com.bookq.name: "${DEV_NAME}"
      com.bookq.ref: "${DEV_REF:-worktree}"
      com.bookq.commit: "${DEV_COMMIT:-unknown}"
      com.bookq.branch: "${DEV_BRANCH:-unknown}"
      com.bookq.worktree: "${DEV_WORKTREE:-unknown}"

    # イメージは python:3.13-slim で curl が無いため python で叩く。
    healthcheck:
      test:
        - CMD
        - python
        - -c
        - import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health')
      interval: 2s
      timeout: 3s
      retries: 15
      start_period: 2s

    # Mac のスリープや Docker 再起動をまたいで生き残らせる。
    restart: unless-stopped

  # 本番と同じサイドカー。entrypoint の既定 serve（http://viewer:8080 → :443）を
  # そのまま使い、ノード名 dev-<名前> がそのまま HTTPS URL になる。
  # 環境ごとに state ボリュームを持つので、down 後もノードの登録が残り
  # （オフライン表示）、再 up で同じ名前・同じ認証状態に戻る。
  tailscale:
    build: ./tailscale
    container_name: "bookq-dev-${DEV_NAME}-tailscale"
    hostname: "dev-${DEV_NAME}"
    cap_add:
      - NET_ADMIN
      - NET_RAW
    environment:
      - TS_AUTHKEY=${TS_AUTHKEY}
      - TS_HOSTNAME=dev-${DEV_NAME}
    volumes:
      - tailscale-state:/var/lib/tailscale
    restart: unless-stopped
    depends_on:
      - viewer

volumes:
  tailscale-state:
```

注意点:
- `TS_AUTHKEY=${TS_AUTHKEY}` は `:?` にしない — compose は `down` / `logs` でもこのファイルを補間するので、読み取り系コマンドを巻き添えで失敗させない（`docker-compose.gateway.yml` の L26-29 と同じ理由）。
- ラベル `com.bookq.port` / `com.bookq.alias` は削除（ポートもエイリアスも存在しなくなった）。
- 外部ネットワーク `bookq-dev` と `networks:` ブロックは削除 — viewer とサイドカーはプロジェクト既定ネットワークで繋がり、`viewer` はサービス名として名前解決される（本番と同じ）。

- [ ] **Step 2: `docker-compose.gateway.yml` を削除する**

```bash
git rm docker-compose.gateway.yml
```

- [ ] **Step 3: compose ファイルが補間可能であることを検証する**

Run: `DEV_IMAGE=test-image DEV_NAME=smoke docker compose -f docker-compose.dev.yml config --quiet`
Expected: 終了コード 0（出力なし）

Run: `DEV_IMAGE= docker compose -f docker-compose.dev.yml config --quiet 2>&1 || true` で、`DEV_IMAGE:?` が無い場合にエラーが出ること（＝素の `docker compose up` が構造的に失敗するガードが生きている）を確認する。
Expected: `DEV_IMAGE is required` を含むエラーが stderr に出る

- [ ] **Step 4: Commit**

```bash
git add docker-compose.dev.yml docker-compose.gateway.yml
git commit -m "refactor: 開発環境を本番と同じ viewer + tailscale サイドカー構成に（共有ゲートウェイを廃止）"
```

---

### Task 2: rebuild.sh — dev の検証を公開 URL ベースに変更

**Files:**
- Modify: `rebuild.sh:44-61`（`verify_url` の待ち回数）と `rebuild.sh:117-128`（`dev_verify`）

- [ ] **Step 1: `verify_url` の待ち回数を 20 → 30 に変更する**

`rebuild.sh` の `verify_url()` 内、`for _ in $(seq 1 20); do` を以下に置換（コメントも更新）:

```bash
    # 30 回 × 2s = 最大 60s。dev 環境の初回は tailnet 参加と証明書発行で
    # ここに時間がかかることがある（成功すればすぐ抜ける。本番は従来どおり早い）。
    for _ in $(seq 1 30); do
```

- [ ] **Step 2: `dev_verify` を DEV_PORT ベースから DEV_FQDN ベースに変更する**

`rebuild.sh` の `dev_verify()` 全体と直前のセクションコメント（L117-119）を以下に置換:

```bash
# dev verify: healthcheck + 公開 URL 検証（dev.sh が呼ぶ）。
# 検証は本番 (deploy_prd) と同じ「公開 URL への実 curl」。serve の設定は
# サイドカーの entrypoint が起動時に行うので、ここで待てば「コンテナは
# 動いているのに配信されていない」障害を検出できる。
dev_verify() {
    : "${DEV_PROJECT:?DEV_PROJECT が未設定です。dev.sh 経由で実行してください。}"
    : "${DEV_FQDN:?DEV_FQDN が未設定です。dev.sh 経由で実行してください。}"
    local cid
    cid="$(docker ps -q --filter "label=com.docker.compose.project=$DEV_PROJECT" --filter "label=com.bookq.role=dev" | head -1)"
    [ -n "$cid" ] || die "開発環境 ($DEV_PROJECT) の viewer コンテナが見つかりません。"
    wait_healthy "$cid"
    verify_url "https://$DEV_FQDN"
}
```

- [ ] **Step 3: スクリプトが構文的に正しいことを確認する**

Run: `bash -n rebuild.sh && bash -n prd.sh`
Expected: 出力なし・終了コード 0

- [ ] **Step 4: Commit**

```bash
git add rebuild.sh
git commit -m "refactor: rebuild.sh の dev 検証を公開 URL ベースに（本番と同一パスに）"
```

---

### Task 3: dev.sh を縮小書き換え（ロック・ポート・ゲートウェイ・reconcile を全削除）

**Files:**
- Rewrite: `dev.sh`

- [ ] **Step 1: `dev.sh` を以下の内容で全置換する**

```bash
#!/usr/bin/env bash
# bookq — 開発環境マネージャ
#
# 各開発環境は本番 (docker-compose.yml) と同じ構成パターンの独立した compose
# プロジェクト（viewer + tailscale サイドカー）。URL はノード名から決定的に
# 決まる https://dev-<名前>.<tailnet>.ts.net なので、ポート採番も serve の
# 収束も、それらを守るロックも存在しない。環境間で共有するリソースが無い。
#
#   ./dev.sh up [name] [--ref REF]   起動（既存なら同じ環境を作り直し）
#   ./dev.sh down [name] | --all     停止・削除（ボリュームとノード登録は保持）
#   ./dev.sh list                    一覧
#   ./dev.sh url [name]              URL だけ出力
#   ./dev.sh logs [name] [-f ...]    ログ
#   ./dev.sh prune                   停止済み環境を掃除
#   ./dev.sh sweep [--dry-run]       古い環境（ブランチ削除済み／worktree 消失／
#                                    main に取込済み）をボリュームごと一括削除
#
# 例:
#   ./dev.sh up review-42 --ref origin/feature/foo
#   ./dev.sh up            # 現在の worktree の作業ツリーから
#
# 本番には絶対に触らない: compose ファイルを常に -f で明示し、プロジェクト名が
# 本番のものになる場合は実行を拒否する。

set -euo pipefail

DEV_COMPOSE="docker-compose.dev.yml"
PROJECT_PREFIX="bookq-dev-"
PROD_PROJECT="bookq"
DEV_LABEL="com.bookq.role=dev"
SLUG_MAXLEN=32

die()  { printf '❌ %s\n' "$*" >&2; exit 1; }
warn() { printf '⚠️  %s\n' "$*" >&2; }
info() { printf '   %s\n' "$*" >&2; }

# ============================================================ 前提チェック

require_repo_root() {
    [ -f "$DEV_COMPOSE" ] || die "$DEV_COMPOSE が見つかりません。リポジトリ（または worktree）のルートで実行してください。"
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
slugify() {
    printf '%s' "$1" \
        | tr '[:upper:]' '[:lower:]' \
        | sed -e 's#^refs/heads/##' -e 's#^origin/##' -e 's#^claude/##' -e 's#^bookq-##' \
        | sed -e 's/[^a-z0-9_-]/-/g' -e 's/-\{2,\}/-/g' -e 's/^-*//' -e 's/-*$//' \
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

# ============================================================ URL

# tailnet ドメイン（例: <your-tailnet>.ts.net）を導出する。
# 優先順位: DEV_TS_DOMAIN > メイン .env の BASE_URL（接頭辞 bookq. を落とす）。
tailnet_domain() {
    local base
    base="${DEV_TS_DOMAIN:-$(dotenv_value BASE_URL || true)}"
    [ -n "$base" ] || return 1
    printf '%s' "$base" | sed -E 's#^https?://(bookq\.)?##; s#/.*$##' | tr -d '[:space:]'
}

# 環境の FQDN。ノード名 dev-<名前> はサイドカーの TS_HOSTNAME と一致する。
dev_fqdn() {
    local name="$1" domain
    domain="$(tailnet_domain)" || { warn "tailnet ドメインを導出できません。DEV_TS_DOMAIN か .env の BASE_URL を設定してください。"; return 1; }
    printf 'dev-%s.%s' "$name" "$domain"
}

# ============================================================ compose ラッパ

# 常に -f と -p を明示。down / logs のようにイメージが本質的に不要な操作でも
# compose がファイルを解釈できるよう、解決済みの値（無ければダミー）を渡す。
compose_env_defaults() {
    DEV_IMAGE="${DEV_IMAGE:-bookq-dev:placeholder}"
    DEV_NAME="${DEV_NAME:-unknown}"
    export DEV_IMAGE DEV_NAME
    # compose の補間はカレントの .env しか見ないため、worktree から実行すると
    # DOWNLOADS_HOST_DIR が既定値に落ちてしまう。TS_AUTHKEY と同様に
    # メインのチェックアウトの .env からも解決して渡す。
    if [ -z "${DOWNLOADS_HOST_DIR:-}" ]; then
        DOWNLOADS_HOST_DIR="$(dotenv_value DOWNLOADS_HOST_DIR || true)"
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
    docker compose -f "$DEV_COMPOSE" -p "$project" "$@"
}

all_projects() {
    docker ps -a --filter "label=$DEV_LABEL" \
        --format '{{.Label "com.docker.compose.project"}}' 2>/dev/null | sort -u || true
}

# ============================================================ サブコマンド

cmd_up() {
    require_repo_root

    local name="" ref=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --ref)  ref="${2:-}";  shift 2 ;;
            --ref=*)  ref="${1#--ref=}";  shift ;;
            -*) die "不明なオプション: $1" ;;
            *) [ -z "$name" ] && name="$1" || die "引数が多すぎます: $1"; shift ;;
        esac
    done

    # 名前を決める: 明示 > --ref の名前 > 現在のブランチ
    name="$(env_name "${name:-$ref}")"
    local project
    project="$(project_of "$name")"

    # サイドカーの初回認証に必要。本番 (prd.sh) と同じく必須。
    find_authkey >/dev/null || die "TS_AUTHKEY が見つかりません。環境変数に設定するか $(main_checkout)/.env に書いてください。"

    # ビルド（時間がかかるが、他の環境と競合しない）。
    local built tag sha
    built="$(./rebuild.sh dev build "$name" "$ref")"
    tag="${built%% *}"; sha="${built##* }"

    printf '🔧 開発環境 "%s" を起動します (project=%s, ref=%s)\n\n' \
        "$name" "$project" "${ref:-作業ツリー}"

    export DEV_IMAGE="$tag"
    export DEV_NAME="$name"
    export DEV_REF="${ref:-worktree}"
    export DEV_COMMIT="$sha"
    export DEV_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
    export DEV_WORKTREE="$PWD"
    export DEV_PROJECT="$project"
    export DEV_FQDN="$(dev_fqdn "$name")"

    # デプロイ → healthcheck + 公開 URL の検証（tailnet 参加・証明書発行の待ちも
    # verify が吸収する）。
    ./rebuild.sh dev deploy
    ./rebuild.sh dev verify

    printf '\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n'
    printf '✅ 開発環境 "%s" が起動しました（認証なし / データは読み取り専用）\n' "$name"
    printf '   %s @ %s\n\n' "${ref:-作業ツリー}" "$sha"
    printf '   https://%s\n' "$DEV_FQDN"
    printf '   (tailnet 内のどの端末からでも。ポートは公開しません — ホストからも HTTPS)\n\n'
    printf '停止:  ./dev.sh down %s\n' "$name"
    printf '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n'
}

cmd_down() {
    require_repo_root

    if [ "${1:-}" = "--all" ]; then
        local project found=0
        for project in $(all_projects); do
            [ -n "$project" ] || continue
            case "$project" in
                "$PROJECT_PREFIX"*) ;;
                *) warn "開発環境ではないプロジェクト '$project' を飛ばしました。"; continue ;;
            esac
            printf '🧹 %s を削除します\n' "$project"
            compose "$project" down
            found=1
        done
        [ "$found" = 1 ] || printf 'ℹ️  削除する開発環境はありません。\n'
        return
    fi

    local name project
    name="$(env_name "${1:-}")"
    project="$(project_of "$name")"
    printf '🧹 開発環境 "%s" (%s) を削除します\n' "$name" "$project"
    compose "$project" down
    printf '✅ 削除しました（ボリュームは保持。tailnet ノードの登録も残ります — 完全に消すのは ./dev.sh sweep）\n'
}

cmd_list() {
    require_repo_root
    local rows domain
    rows="$(docker ps -a --filter "label=$DEV_LABEL" \
        --format '{{.Label "com.bookq.name"}}|{{.State}}|{{.Label "com.bookq.ref"}}|{{.Label "com.bookq.commit"}}' \
        2>/dev/null | sort -u || true)"

    if [ -z "$rows" ]; then
        printf 'ℹ️  開発環境はありません。./dev.sh up で作成できます。\n'
        return
    fi

    domain="$(tailnet_domain 2>/dev/null || true)"
    printf '%-30s %-9s %-24s %-13s %s\n' "ENV" "STATE" "REF" "COMMIT" "URL"
    printf '%s\n' "$rows" | while IFS='|' read -r name state ref commit; do
        [ -n "$name" ] || continue
        if [ -n "$domain" ]; then
            printf '%-30s %-9s %-24s %-13s %s\n' "$name" "$state" "$ref" "$commit" "https://dev-$name.$domain"
        else
            printf '%-30s %-9s %-24s %-13s %s\n' "$name" "$state" "$ref" "$commit" "(DEV_TS_DOMAIN 未設定)"
        fi
    done
}

cmd_url() {
    require_repo_root
    local name fqdn
    name="$(env_name "${1:-}")"
    fqdn="$(dev_fqdn "$name")" || die "tailnet ドメインを導出できません。DEV_TS_DOMAIN か .env の BASE_URL を確認してください。"
    printf 'https://%s\n' "$fqdn"
}

cmd_logs() {
    require_repo_root
    local raw="" name project
    if [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then raw="$1"; shift; fi
    name="$(env_name "$raw")"
    project="$(project_of "$name")"
    compose "$project" logs "$@"
}

cmd_prune() {
    require_repo_root
    local rows project state removed=0
    rows="$(docker ps -a --filter "label=$DEV_LABEL" \
        --format '{{.Label "com.docker.compose.project"}}|{{.State}}' 2>/dev/null | sort -u || true)"
    while IFS='|' read -r project state; do
        [ -n "$project" ] || continue
        [ "$state" = "running" ] && continue
        case "$project" in "$PROJECT_PREFIX"*) ;; *) continue ;; esac
        printf '🧹 停止中の環境を削除: %s\n' "$project"
        compose "$project" down
        removed=1
    done <<< "$rows"
    [ "$removed" = 1 ] || printf 'ℹ️  掃除する停止中の環境はありません。\n'
}

# prune は「停止中」だけを見るので、動いたままの古い環境は拾えない。
# sweep は実行状態に関係なく、次のいずれかで「もう用済み」と判定する:
#   - worktree からの環境で、そのブランチが削除済み（マージ後の消し忘れ）
#   - worktree からの環境で、worktree 自体がもう存在しない（迷子）
#   - コミットが既に origin/main に取り込まれている（--ref 環境も含む）
# "main" ブランチを追跡する環境は意図的なものなので対象外にする。
# 削除は down -v で行い、tailnet ノードの登録と state ボリュームも消す。
cmd_sweep() {
    require_repo_root

    local dry_run=0
    case "${1:-}" in
        --dry-run) dry_run=1 ;;
        "") ;;
        *) die "sweep のオプションは --dry-run のみです" ;;
    esac

    # 判定を最新の main に合わせる。ネットワーク不通でも古い情報で続行する。
    git fetch origin main --quiet 2>/dev/null || true

    local rows
    rows="$(docker ps -a --filter "label=$DEV_LABEL" \
        --format '{{.Label "com.docker.compose.project"}}|{{.Label "com.bookq.name"}}|{{.Label "com.bookq.ref"}}|{{.Label "com.bookq.commit"}}|{{.Label "com.bookq.branch"}}|{{.Label "com.bookq.worktree"}}' \
        2>/dev/null | sort -u || true)"

    if [ -z "$rows" ]; then
        printf 'ℹ️  開発環境はありません。\n'
        return
    fi

    local project name ref commit branch worktree reason found=0
    while IFS='|' read -r project name ref commit branch worktree; do
        [ -n "$project" ] || continue
        case "$project" in "$PROJECT_PREFIX"*) ;; *) continue ;; esac

        reason=""
        if [ "$ref" = "worktree" ] && [ "$branch" != "main" ]; then
            if [ -n "$worktree" ] && [ "$worktree" != "unknown" ] && [ ! -d "$worktree" ]; then
                reason="worktree が存在しません ($worktree)"
            elif [ -n "$branch" ] && [ "$branch" != "unknown" ] \
                 && ! git show-ref --verify --quiet "refs/heads/$branch"; then
                reason="ブランチ '$branch' が削除されています"
            fi
        fi
        if [ -z "$reason" ] && [ -n "$commit" ] && [ "$commit" != "unknown" ] \
           && [ "$branch" != "main" ] \
           && git merge-base --is-ancestor "$commit" origin/main 2>/dev/null; then
            reason="コミット $commit は origin/main に取り込み済み"
        fi
        [ -n "$reason" ] || continue
        found=1

        if [ "$dry_run" = 1 ]; then
            printf '🔍 %-30s %s\n' "$name" "$reason"
        else
            printf '🧹 開発環境 "%s" (%s) を削除します — %s\n' "$name" "$project" "$reason"
            compose "$project" down -v
        fi
    done <<< "$rows"

    if [ "$found" = 0 ]; then
        printf 'ℹ️  古い開発環境はありません。\n'
        return
    fi
    [ "$dry_run" = 1 ] && { printf '\n(--dry-run: 実際には削除していません。削除するには ./dev.sh sweep)\n'; return; }
    printf '✅ 掃除しました\n'
}

usage() { sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; }

case "${1:-}" in
    up)        shift; cmd_up "$@" ;;
    down)      shift; cmd_down "${1:-}" ;;
    list|ls)   shift; cmd_list ;;
    url)       shift; cmd_url "${1:-}" ;;
    logs)      shift; cmd_logs "$@" ;;
    prune)     shift; cmd_prune ;;
    sweep)     shift; cmd_sweep "${1:-}" ;;
    ""|-h|--help|help) usage ;;
    *) die "不明なサブコマンド: $1（--help を参照）" ;;
esac
```

書き換えで削除されたもの（旧 dev.sh からの削除物を確認に使う）: `acquire_lock` / `release_lock` / reap ロック、`resolve_port` / `existing_port` / `claimed_ports_excluding` / `port_listening` / `owns_port`、`ensure_network` / `gateway_up` / `gateway_fqdn` / `gateway_running` / `gateway_cid` / `ts()`、`desired_serve` / `current_serve` / `reconcile_serve`、`cmd_gateway` / `cmd_reconcile`、`DEV_PORT`・`PORT_MIN`・`PORT_MAX`・`NETWORK`・`GATEWAY_*`・`TS_SOCK`・`LOCK_*` の定数。

- [ ] **Step 2: 構文チェックとヘルプ表示**

Run: `bash -n dev.sh`
Expected: 出力なし・終了コード 0

Run: `./dev.sh --help`
Expected: 新しいサブコマンド一覧（gateway / reconcile が消えている）が表示される

- [ ] **Step 3: 静的な整合チェック（grep で残骸がないこと）**

Run: `grep -n "DEV_PORT\|PORT_MIN\|reconcile\|gateway\|serve" dev.sh || echo "clean"`
Expected: `clean`（`serve` はコメントに `serve の収束` という語が残るが、コード上の参照は無いこと。コメントのみ許容）

- [ ] **Step 4: フック契約の確認（後で Task 4 で jq を外すが、up/url の契約は変わらない）**

Run: `./dev.sh url 2>&1 || true`
Expected: `https://dev-<現在のブランチslug>.<tailnetドメイン>` の形の URL が出る（Docker 不要で動く — `require_repo_root` は docker を要求するので、Docker が止まっている場合は「Docker が動いていません」の die でも可。エラーメッセージが分かりやすいこと）

- [ ] **Step 5: Commit**

```bash
git add dev.sh
git commit -m "refactor: dev.sh からポート採番・serve 収束・排他ロックを廃止（per-env ノード化）"
```

---

### Task 4: .githooks/lib.sh から jq チェックを除去

**Files:**
- Modify: `.githooks/lib.sh:45-54`

- [ ] **Step 1: `dev_available()` から jq チェックのブロックを削除する**

`.githooks/lib.sh` の `dev_available()` 内、`if ! docker info ...` ブロックの後にある以下の 5 行を削除:

```bash
    # dev.sh は jq を必須にしている。ここで見ておかないと dev_up が
    # 途中で die し、pre-commit の「起動失敗」扱いでコミットがブロックされる。
    if ! command -v jq >/dev/null 2>&1; then
        echo "⚠️  jq が見つかりません（brew install jq）。開発環境の更新をスキップします。"
        return 1
    fi
```

結果として `dev_available()` は以下になる:

```bash
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
    return 0
}
```

- [ ] **Step 2: 構文チェック**

Run: `bash -n .githooks/lib.sh .githooks/pre-commit .githooks/post-commit`
Expected: 出力なし・終了コード 0

- [ ] **Step 3: Commit**

```bash
git add .githooks/lib.sh
git commit -m "chore: lib.sh から jq チェックを除去（dev.sh が jq 不要になったため）"
```

---

### Task 5: ドキュメントを per-env ノード構成に合わせて更新

**Files:**
- Modify: `README.md`
- Modify: `docs/deployment.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: README.md を更新する**

以下の各箇所を置換する（README 全体の書き換えではない。見出しの位置は現状ファイルを確認して対応させる）:

(a) 冒頭の構成図 — dev 部分を置換:

```markdown
**開発環境** — 環境ごとに独立した tailnet ノード（本番と同じ構成パターン）:

```
                                        ┌─ dev-feat-a.….ts.net ─▶ viewer (ブランチ A)
ブラウザ ── HTTPS ──▶ tailnet ──────────┼─ dev-feat-b.….ts.net ─▶ viewer (ブランチ B)
 (Tailnet内)        （ノード dev-* が各 1 個）└─ dev-3ebd9e9.….ts.net ─▶ viewer (特定コミット)
```
```

(b) 「コンテナ」表から `gateway` の行を削除:

```markdown
| コンテナ | 役割 |
|---|---|
| `viewer` | 画像ビューア + API (FastAPI) |
| `tailscale` | 本番の Tailscale ノード、TLS 終端、tailnet 内公開（開発環境は環境ごとに `dev-<名前>` ノードを持つ） |
```

(c) 「開発」節の比較表 — URL と構成の行を置換:

```markdown
| | 本番 | 開発環境 |
|---|---|---|
| 起動/停止 | `./prd.sh up` / `./prd.sh down`（メインのチェックアウトのみ） | `./dev.sh up` / `./dev.sh down`（どこからでも、何個でも） |
| URL | `https://bookq.<your-tailnet>.ts.net` | `https://dev-<名前>.<your-tailnet>.ts.net` |
| 認証 | Google OAuth | なし（`BOOKQ_TEST_MODE=true`） |
| 画像ツリー | 読み書き可 | **読み取り専用** (`:ro`) |
| コンテナ | viewer + tailscale サイドカー | viewer + tailscale サイドカー（ノードは環境ごとに 1 つ） |
```

(d) 本番 compose を検証に流用しない warning の段落 — 理由を更新（dev compose にもサイドカーが入ったが `:?` 必須変数で素の `docker compose up` は失敗する）:

```markdown
> **本番の compose を検証に流用しないこと。** worktree で素の `docker compose up` を打つと、ホスト名 `bookq` を要求する 2 つ目の tailscale ノードが立ち上がって本番と名前を争います。`prd.sh` は worktree からの実行を拒否し、`docker-compose.dev.yml` は `DEV_IMAGE` / `DEV_NAME` 等の必須変数（`:?`）が無いと起動自体が失敗するので、`dev.sh` を使う限りこの事故は起きません。
```

(e) 「3. 開発環境」のコマンド例を置換（`--port` を削除、URL の形を変更）:

```markdown
```bash
./dev.sh up                                       # 現在の作業ツリーから
./dev.sh up review-42 --ref main                  # 名前と ref を明示
./dev.sh up old --ref 3ebd9e9                     # 任意のコミットでも可
```

表示された `https://dev-review-42.<your-tailnet>.ts.net` は tailnet 内なら **iPad からもそのまま開けます**。ホストからも同じ HTTPS URL でアクセスします（ポートは公開しません — ホストは tailnet に参加している前提）。
```

(f) コマンド表を置換（gateway / reconcile の行を削除、down の説明を更新）:

```markdown
| コマンド | 動作 |
|---|---|
| `./dev.sh up [name] [--ref REF]` | 起動（既存なら同じ環境を作り直し） |
| `./dev.sh down [name]` / `--all` | 撤去（ボリュームと tailnet ノードの登録は保持） |
| `./dev.sh list` | 一覧（名前・状態・ref・commit・URL） |
| `./dev.sh url [name]` | URL だけ出力（スクリプト用） |
| `./dev.sh logs [name] [-f]` | ログ |
| `./dev.sh prune` | 停止済みの環境を掃除 |
| `./dev.sh sweep [--dry-run]` | 古い環境（ブランチ削除済み／worktree 消失／main 取込済み）をボリュームごと一括削除 |
```

環境名を省略すると現在のブランチから導出されます（`origin/`・`claude/`・`bookq-` の接頭辞は落とす）。**URL はノード名 `dev-<名前>` から決定的に決まるので、ポートの割り当ては存在しません。**

(g) 「4. 複数エージェント／worktree での並行開発」の保証の箇条書きを置換:

```markdown
同時に叩いても壊れないのは、ロックや状態管理の成果ではなく**構造**によるものです。

- 各環境は独立した compose プロジェクト（別ネットワーク・別コンテナ・別ボリューム・別 tailnet ノード）で、環境間で共有するリソースが存在しない
- URL は名前から決定的（`https://dev-<名前>.ts.net`）。ブックマークは環境名を変えない限り無効にならない
- 同じ名前の環境は 1 つだけ — 別の worktree が同じブランチで `up` すると作り直す（従来と同じ「最後勝ち」）
- ノード名が衝突した場合（同じ名前の環境を 2 つ同時に up した等）は、URL 検証が失敗して明示的に分かる

`tailscale serve` の設定はサイドカーの entrypoint が起動時に固定で行うため、`reconcile` のような収束操作は存在しません。使い終わったら `down` してください。`restart: unless-stopped` なのでスリープや Docker 再起動をまたいで生き残ります。
```

(h) ポート範囲の段落（「ポートは `8100`–`8199` の範囲…」）を削除。

(i) 「5. Git Hook」の節 — 変更不要（フックは `./dev.sh up` と `./dev.sh url` を呼ぶだけ。URL の形が変わるだけで動作は同じ）。

(j) 「困ったときは」の表を置換:

```markdown
| 症状 | 対処 |
|---|---|
| 本番が 502 | viewer が compose ネットワークから外れています。`docker inspect $(docker compose ps -q viewer) --format '{{json .NetworkSettings.Networks}}'` で確認し、迷子のコンテナを消して `docker compose up -d --build viewer` |
| 開発環境の HTTPS に届かない | tailnet ACL で `dev-*` ノード宛の HTTPS (443) を許可する（旧構成の `8100-8199` 許可は不要）。それでも駄目なら `./dev.sh list` で環境の状態を確認 |
| ノード名が衝突した | 同じ名前の環境を 2 つ立てようとしています。`./dev.sh list` で確認して不要な方を `down` してから `up` |
| 開発環境が中途半端な状態 | `./dev.sh down <name>` してから `up` し直す |
```

(k) 「ファイル構成」表から `docker-compose.gateway.yml` の行を削除し、`dev.sh` の説明を更新:

```markdown
| `dev.sh` | 開発環境マネージャ（up / down / list / url / logs / prune / sweep） |
| `docker-compose.dev.yml` | **開発環境**: viewer + tailscale サイドカー（1 環境 = 1 プロジェクト） |
```

- [ ] **Step 2: docs/deployment.md を更新する**

「開発環境」節（L17-91、構成図・使い方・並行実行の保証・iPad・ゲートウェイの初回起動まで）を丸ごと以下に置換:

```markdown
## 開発環境

### 構成

各開発環境は本番と同じ構成パターン（viewer + tailscale サイドカー）の独立した compose プロジェクトで、**環境ごとに tailnet ノードを 1 つ持つ**:

```
dev-feat-a.<your-tailnet>.ts.net ──▶ viewer (ブランチ feat-a)
dev-feat-b.<your-tailnet>.ts.net ──▶ viewer (ブランチ feat-b)
dev-3ebd9e9.<your-tailnet>.ts.net ──▶ viewer (特定コミット)
```

ノード名 `dev-<名前>` がそのまま HTTPS URL（:443）になるため、**ポートの割り当ては存在しない**。serve の設定はサイドカーの entrypoint（`tailscale/entrypoint.sh` の既定 `http://viewer:8080`）が起動時に固定で行う。

環境間に共有するリソース（ネットワーク・serve 設定・ポート・ボリューム）が無いので、**排他ロックも serve の収束も不要**。並行実行の安全性は「構造的に干渉しない」ことで保証される。同じ名前の環境は 1 つだけ（後から `up` した方が作り直す）。ノード名が衝突すると URL 検証が失敗して明示的に分かる。

各 viewer はホストにポートを公開しない。ホストからのアクセスも HTTPS 経由（ホストは tailnet に参加していることが前提）。

### 使い方

```bash
./dev.sh up                                   # 現在の作業ツリーから
./dev.sh up review-42 --ref main              # 名前と ref を明示
./dev.sh up old --ref 3ebd9e9                 # 任意のコミットでも可

./dev.sh list                # 一覧（名前・状態・ref・commit・URL）
./dev.sh url [name]          # URL だけ出力（スクリプト・git hook 用）
./dev.sh logs [name] [-f]    # ログ
./dev.sh down [name]         # 削除（ボリュームとノード登録は保持）
./dev.sh down --all          # 全部削除
./dev.sh prune               # 停止済みの環境を掃除
./dev.sh sweep [--dry-run]   # 古い環境をボリュームごと一括削除
```

環境名を省略すると現在のブランチから導出される（`origin/`・`claude/`・`bookq-` の接頭辞は落とす）。URL の tailnet ドメインは `DEV_TS_DOMAIN` かメインの `.env` の `BASE_URL` から導出する。

### `--ref` と作業ツリー

| 指定 | ビルド元 | 用途 |
|---|---|---|
| `--ref <branch\|tag\|SHA>` | `git archive` でそのコミットから直接 | **決定的**。作業ツリーの状態に一切影響されず、同じ SHA なら必ず同じイメージ。worktree を用意する必要もない |
| 省略 | 現在の作業ツリー | 未コミットの変更を確認したいとき |

`--ref` のイメージは `bookq-dev:<短縮SHA>` としてタグ付けされ、同じ SHA なら再ビルドせず再利用する。

### down / sweep とノードのライフサイクル

- `down` は本番と同じく**ボリュームを保持する**。tailnet ノードの登録も残る（オフライン表示）ので、再 `up` で同じ名前・同じ認証状態に戻る。
- 完全に消す（ノードの登録と state ボリュームも消す）のは `./dev.sh sweep`（`down -v` 相当）。ブランチ削除済み・worktree 消失・main 取り込み済みの環境を対象にする。

### iPad から見る

`./dev.sh up` が表示する `https://dev-<名前>.<your-tailnet>.ts.net` をそのまま Safari で開く。

**届かない場合:** tailnet の ACL が HTTPS (443) を絞っている可能性がある。ACL 設定で `dev-*` ノード宛の到達を許可する（旧構成の `bookq-dev` 宛 `8100-8199` の許可は不要になる）。

### 初回認証

サイドカーの認証キーは環境ごとの state ボリュームに残る。`dev.sh` は次の順で探す:

1. 環境変数 `TS_AUTHKEY`
2. 実行しているリポジトリ／worktree の `.env`
3. メインのチェックアウトの `.env`（worktree には `.env` が無いのでここに落ちてくる）

一度認証すれば以後の `up` でキーは不要（本番と同じ）。
```

その後、旧構成の記述（共有ゲートウェイ・ポート採番・reconcile・排他ロック）への参照が本文に残っていないことを確認する。本番節（L95-143）と git フック節（L147-162）は変更不要。

- [ ] **Step 3: CLAUDE.md を更新する**

以下の各箇所を置換する:

(a) 「Commands」節の dev 環境の説明行を置換:

```markdown
Bring up a throwaway development environment for the current worktree (Docker, no OAuth, reachable from the tailnet — each environment is its own tailnet node `dev-<name>`, so no port allocation or shared gateway):
```

(b) 「Production vs. development environments」節の冒頭パラグラフと表を置換:

```markdown
- Production is `docker-compose.yml`, project `bookq`, viewer + tailscale sidecar, HTTPS on `bookq.…ts.net`, real OAuth.
- A development environment is `docker-compose.dev.yml`, project `bookq-dev-<slug>`, viewer + its own tailscale sidecar (`dev-<slug>` node), `BOOKQ_TEST_MODE=true`, downloads `:ro`. Managed entirely through `./dev.sh`.
- A dev environment's URL is its node name: `https://dev-<slug>.…ts.net`. Deterministic — no port allocation, no shared gateway, no serve reconciliation, no lock. Environments share nothing (separate projects/networks/volumes/nodes), so concurrent agents cannot interfere structurally.
```

(c) 同節の 3 properties の箇条書き（ロック・serve 導出・状態ファイル）を置換:

```markdown
Three properties make it safe under parallel agents, and they are the ones to preserve:
  1. **Environments share nothing.** Separate compose projects, networks, volumes, and tailnet nodes. No shared mutable resource exists, so there is no lock.
  2. **URLs are deterministic.** `https://dev-<slug>.…ts.net` derives from the node name; the same slug always means the same URL. A slug collision (two envs with the same name) fails loudly at the URL verification step.
  3. **No state file.** Name, ref, commit, branch, worktree all live in `com.bookq.*` labels, readable even on stopped containers — that is what keeps `up` idempotent and URLs stable.
```

(d) 「Never run a bare `docker compose up` in a worktree.」の段落の後半（guard の説明）を置換:

```markdown
- **Never run a bare `docker compose up` in a worktree.** Compose derives the project name from the directory, so you get a second stack *including a tailscale sidecar* that fights production for the hostname `bookq`. `prd.sh` refuses to run from a linked worktree (it compares `--git-dir` with `--git-common-dir`); `dev.sh` passes `-p` explicitly and `docker-compose.dev.yml` refuses to interpolate without the `DEV_IMAGE`/`DEV_NAME` (`:?`) variables, so a bare `docker compose up` fails before it can start anything. `rebuild.sh` deliberately has no guard because it is the shared deploy engine for both prd and dev.
```

(e) 上記 (c) の書き換えに伴い、dev 環境の HTTPS が共有ゲートウェイ経由だった記述（「the same port is also published on the host bound to 127.0.0.1 only」等）が残っていないことを確認して削除する。

- [ ] **Step 4: ドキュメント内の旧構成参照の残骸チェック**

Run: `grep -rn "bookq-dev.javanese\|8100\|--port\|reconcile\|gateway" README.md docs/deployment.md CLAUDE.md | grep -v "tailnet ノード" || echo "clean"`
Expected: 旧構成への参照が残っていない（`gateway` は「共有ゲートウェイ」という語が一切消えていること。「tailnet ノード」等の新しい語だけが残ること）。残骸があれば修正して再チェック。

- [ ] **Step 5: Commit**

```bash
git add README.md docs/deployment.md CLAUDE.md
git commit -m "docs: 開発環境の構成を per-env ノード方式に合わせて更新"
```

---

### Task 6: 実環境で E2E 検証（dev-lifecycle）

**Files:** 変更なし（検証のみ）

**前提:** Docker 稼働・tailnet 到達・メインの `.env` に `TS_AUTHKEY` と `BASE_URL` があること。旧構成の残骸（`bookq-dev-gateway` プロジェクト、外部ネットワーク `bookq-dev`、ゲートウェイの state ボリューム）が残っていれば先に掃除する。

- [ ] **Step 1: 旧構成の残骸を掃除する（残っている場合のみ）**

```bash
# 旧ゲートウェイ（あったら）
docker compose -f docker-compose.gateway.yml -p bookq-dev-gateway down 2>/dev/null || true
# 旧共有ネットワーク（旧環境を全部 down した後）
docker network rm bookq-dev 2>/dev/null || true
# 旧ゲートウェイの state ボリューム
docker volume rm bookq-dev-gateway_gateway-state 2>/dev/null || true
```

Run: `./dev.sh list`
Expected: 旧構成の dev 環境が残っていれば一覧に出る（残っていれば `./dev.sh down --all` で撤去。新コードでも旧環境を down できる — プロジェクト名は同じ `bookq-dev-<slug>`）

- [ ] **Step 2: dev 環境を 1 つ立てる（--ref HEAD で決定的ビルド）**

Run: `./dev.sh up e2e-test --ref HEAD`
Expected:
- ビルド（初回は tailscale イメージのビルドも入る）→ デプロイ → `viewer は healthy です` → `https://dev-e2e-test.<domain>.ts.net は正常に応答しています`
- 最後に `✅ 開発環境 "e2e-test" が起動しました` と `https://dev-e2e-test.<domain>.ts.net` が表示される

- [ ] **Step 3: 実 URL が 200 を返すことを確認する**

Run: `curl -s -o /dev/null -w '%{http_code}\n' "https://$(./dev.sh url e2e-test)/api/health"`
Expected: `200`（iPad 相当の外部端末からも届くことは ACL 設定次第だが、tailnet 内 HTTPS として 200 が返ること）

- [ ] **Step 4: 管理コマンド一式を確認する**

Run: `./dev.sh list`
Expected: `e2e-test  running  HEAD  <sha>  https://dev-e2e-test.<domain>.ts.net` の行が出る

Run: `./dev.sh url e2e-test`
Expected: `https://dev-e2e-test.<domain>.ts.net`

Run: `./dev.sh logs e2e-test 2>&1 | head -5`
Expected: ログが出力される（エラーにならない）

Run: `./dev.sh sweep --dry-run`
Expected: 正常に終了する（e2e-test は `--ref HEAD` 環境なので、HEAD が origin/main に無ければ対象外。「取り込み済み」と出る場合は想定どおり）

- [ ] **Step 5: テストスイートが通ることを確認する（アプリは無変更のサニティ）**

Run: `uv run --with pytest --with playwright --with-requirements requirements.txt pytest tests/ -q`
Expected: 全テストパス（dev 環境の変更はアプリに影響しないが、回帰がないことの確認）

- [ ] **Step 6: prd.sh が無変更であることを確認する**

Run: `git diff HEAD -- prd.sh docker-compose.yml tailscale/entrypoint.sh .env.example`
Expected: 出力なし（本番周りは一切触っていない）

- [ ] **Step 7: 作った環境を撤去する**

Run: `./dev.sh down e2e-test`
Expected: `✅ 削除しました（ボリュームは保持。tailnet ノードの登録も残ります…）`

（ノード登録ごと消したい場合は `./dev.sh sweep` の対象にするか `docker compose -p bookq-dev-e2e-test down -v`。検証後は node 登録を消すため `-v` で消してよい。）

- [ ] **Step 8: 最終確認 — 全スクリプトの構文と diff の全体像**

Run: `bash -n dev.sh rebuild.sh prd.sh setup-hooks.sh .githooks/lib.sh && git status --short`
Expected: 構文 OK・変更ファイルが想定どおり（docker-compose.dev.yml / rebuild.sh / dev.sh / .githooks/lib.sh / README.md / docs/deployment.md / CLAUDE.md / 削除された docker-compose.gateway.yml）

---

## セルフレビュー結果（プラン作成時に実施済み）

- **Spec 網羅**: ①ポート採番廃止 → Task 3（dev.sh 全置換）+ Task 1（compose から ports 削除）②serve 収束廃止 → Task 1（サイドカー追加・entrypoint 既定 serve）③ロック廃止 → Task 3（ロック関数全削除）④URL の決定性 → Task 3 の `dev_fqdn` ⑤ホスト localhost 廃止 → Task 1（ports ブロック削除）⑥jq 依存除去 → Task 3 + Task 4 ⑦ドキュメント → Task 5 ⑧実環境検証 → Task 6。全要件にタスクが割り当てられている。
- **Placeholder スキャン**: コードステップはすべて完全な内容を含む（プレースホルダなし）。
- **型・名前の一貫性**: `DEV_FQDN` は Task 2 の `dev_verify`（rebuild.sh）と Task 3 の `cmd_up`（export）で同一名。`compose_env_defaults` は引数なしに統一（Task 3 内で一貫）。`down -v` は Task 3 の `cmd_sweep` のみ。ラベル `com.bookq.*`（name/ref/commit/branch/worktree）は Task 1・Task 3 で同一。フックの契約 `./dev.sh up <name> [--ref ...]` と `./dev.sh url` は Task 3・Task 4 で不変。
