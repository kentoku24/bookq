#!/usr/bin/env bash
# 汎用デプロイエンジン: ビルド → デプロイ → healthcheck → URL 検証。
#
#   ./rebuild.sh prd build <sha>   本番イメージを決定論ビルド（archive 由来・dirty 除外）
#   ./rebuild.sh prd deploy         本番を prebuilt image で起動＋検証
#   ./rebuild.sh dev build <name> [ref]   開発環境のイメージをビルド
#   ./rebuild.sh dev deploy               開発環境を起動（dev.sh が呼ぶ）
#   ./rebuild.sh dev verify               起動済み環境の healthcheck + URL 検証
#
# 本番は commit 済み内容のみ（dirty 除外）。worktree ガードは prd.sh 側。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

DEV_COMPOSE="docker-compose.dev.yml"

die()  { printf '❌ %s\n' "$*" >&2; exit 1; }
info() { printf '   %s\n' "$*" >&2; }

usage() {
    sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
}

# ============================================================ 共通: healthcheck
# viewer はホストにポートを公開していないので localhost は叩けない。
# docker-compose.yml / docker-compose.dev.yml の healthcheck（コンテナ内から
# /api/health）をコンテナ側から待つ。
wait_healthy() {
    local cid="$1" status=""
    echo ""
    echo "⏳ viewer コンテナの healthcheck を待っています..."
    for _ in $(seq 1 60); do
        status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid" 2>/dev/null || true)"
        case "$status" in
            healthy) echo "✅ viewer は healthy です"; return 0 ;;
            none)    echo "⚠️  healthcheck が定義されていません。待機をスキップします"; return 0 ;;
            *)       sleep 1 ;;
        esac
    done
    die "viewer が healthy になりませんでした (status=${status:-なし})"
}

# ============================================================ 共通: URL 検証
# ここが本物のエンドツーエンド確認。「コンテナは動いているのに serve が
# 配信できていない」（= viewer がネットワークにいなくて 502）という障害を検出できる。
verify_url() {
    local url="$1" code=""
    echo ""
    echo "⏳ URL を確認しています: $url"
    # 30 回まで。各試行は curl の --max-time 10 で打ち切るため実質最大 ~6 分
    # （成功すればすぐ抜ける。dev 環境の初回は tailnet 参加と証明書発行で
    # ここに時間がかかることがある。本番は従来どおり早い）。
    for _ in $(seq 1 30); do
        code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$url/api/health" || true)"
        [ "$code" = "200" ] && break
        sleep 2
    done
    if [ "${code:-}" = "200" ]; then
        echo "✅ $url は正常に応答しています"
    else
        die "$url が 200 を返しません (HTTP ${code:-なし})"
    fi
}

# ============================================================ 本番
# prd build: sha のコミット済み内容だけを git archive で取り出し、決定論タグの
# prod image（viewer＋sidecar）をビルドする。作業ツリーは一切見ない。
# dev_build の git archive パターンと同型。同 sha＝同内容のため、既存タグは
# docker image inspect で再利用する。出力は "VIEWER_TAG SIDECAR_TAG"。
prd_build_image() {
    [ -n "${1:-}" ] || { usage >&2; die "prd build には sha が必要です: rebuild.sh prd build <sha>"; }
    local sha="$1" sha12 tmp build_date viewer_tag sidecar_tag
    sha12="${sha:0:12}"

    tmp="$(mktemp -d)"
    trap "rm -rf \"$tmp\"" EXIT
    git archive --format=tar "$sha" | tar -x -C "$tmp" \
        || die "sha '$sha' を archive できません。git fetch が必要かもしれません。"
    [ -f "$tmp/Dockerfile" ] || die "archive に Dockerfile がありません (sha=$sha)。"
    [ -f "$tmp/requirements.txt" ] || die "archive に requirements.txt がありません (sha=$sha)。"
    [ -d "$tmp/app" ] || die "archive に app/ がありません (sha=$sha)。"
    # compose の build: ./tailscale は context 内の既定名 Dockerfile を使う。
    [ -f "$tmp/tailscale/Dockerfile" ] || die "archive に tailscale/Dockerfile がありません (sha=$sha)。"
    # sidecar は別 context のため、親参照（../）があると壊れる。存在確認と同時に検査する。
    if grep -rn 'COPY \.\.' "$tmp/tailscale/" 2>/dev/null | grep -q .; then
        die "tailscale context に親参照（COPY ../）があります (sha=$sha)。"
    fi

    viewer_tag="bookq-prd-viewer:${sha12}"
    sidecar_tag="bookq-prd-tailscale:${sha12}"
    if docker image inspect "$viewer_tag" >/dev/null 2>&1 \
        && docker image inspect "$sidecar_tag" >/dev/null 2>&1; then
        info "イメージ再利用: $viewer_tag $sidecar_tag (${sha12})"
    else
        build_date="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        info "ビルド中: ${sha12} → $viewer_tag $sidecar_tag"
        docker build -q --build-arg "GIT_SHA=${sha12}" --build-arg "BUILD_DATE=${build_date}" \
            -t "$viewer_tag" "$tmp" >/dev/null \
            || die "viewer のビルドに失敗しました (sha=$sha)"
        docker build -q -t "$sidecar_tag" "$tmp/tailscale" >/dev/null \
            || die "sidecar のビルドに失敗しました (sha=$sha)"
    fi
    printf '%s %s' "$viewer_tag" "$sidecar_tag"
}

# prd deploy の事後検証: /api/version の sha が期待 sha と一致すること。
# version 側の sha 桁数が不明なため双方向 prefix 比較。空文字に対する
# startswith は常に True になるため、先に空値 die する。
verify_prd_version() {
    local expected="${1:?verify_prd_version には期待 sha が必要です}"
    local base_url actual
    base_url="${BASE_URL:-$(grep -E '^BASE_URL=' .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'\''' | tr -d '[:space:]' || true)}"
    [ -n "$base_url" ] || die "BASE_URL が分からないため version 照合ができません。"
    actual="$(curl -s --max-time 10 "$base_url/api/version" 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("sha") or "")' 2>/dev/null || true)"
    [ -n "$actual" ] && [ -n "$expected" ] \
        || die "version の sha が空です (expected=${expected:-空} actual=${actual:-空})。空文字に対する startswith は常に True になるため検証できません。"
    case "$expected" in
        "$actual"*) ;;
        *)
            case "$actual" in
                "$expected"*) ;;
                *) die "デプロイされた version の sha が一致しません (expected=$expected actual=$actual)。" ;;
            esac
            ;;
    esac
    echo "✅ version の sha が一致しています ($actual)"
}

deploy_prd() {
    : "${PRD_VIEWER_IMAGE:?PRD_VIEWER_IMAGE が未設定です。rebuild.sh prd build <sha> でビルドしてください。}"
    : "${PRD_TAILSCALE_IMAGE:?PRD_TAILSCALE_IMAGE が未設定です。rebuild.sh prd build <sha> でビルドしてください。}"
    : "${PRD_EXPECT_SHA:?PRD_EXPECT_SHA が未設定です。rebuild.sh prd build <sha> でビルドしてください。}"
    echo "🚀 本番コンテナを prebuilt image で起動します..."
    docker compose -f docker-compose.yml -f docker-compose.prd-image.yml up -d --no-build

    local cid
    cid="$(docker compose ps -q viewer)"
    [ -n "$cid" ] || die "viewer コンテナが見つかりません。"
    wait_healthy "$cid"

    # クォート付きの値（compose の補間では有効）でも curl できるよう剥がす
    local base_url
    base_url="${BASE_URL:-$(grep -E '^BASE_URL=' .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'\''' | tr -d '[:space:]' || true)}"
    if [ -z "$base_url" ]; then
        echo "⚠️  BASE_URL が分からないため公開 URL の確認をスキップします。"
        return 0
    fi
    verify_url "$base_url"
    verify_prd_version "$PRD_EXPECT_SHA"
}

# ============================================================ 開発環境
# dev build: ref 指定時は git archive でそのコミットの中身から直接ビルドする。
# 作業ツリーの状態に一切影響されない。タグ方式は ref の種別で 2 分岐:
#   ・シンボリック ref（ブランチ / リモート / タグ。ブランチ上の HEAD も含む）
#     → 可変エイリアスタグ bookq-dev:ref-<slug>。既存タグがあっても必ず
#       再ビルドして上書きする。同一内容ならレイヤキャッシュで即完了、内容が
#       変わっていれば新 ID になり旧イメージは dangling 化する — 同一 ref の
#       イメージはたかだか 1 枚。
#   ・純 hex SHA / detached など不変参照 → 従来どおりコミット専用タグ
#     bookq-dev:<sha12>。存在すれば再利用（同じ内容 = 常に同じ ID）。
# 出力は "TAG SHORT_SHA"（dev.sh がラベルと表示に使う）。
dev_build() {
    local name="${1:?dev build には環境名が必要です}" ref="${2:-}" tag sha sym slug

    # dev.sh:84-91 slugify() と同一規則のローカル複製。スタンドアロン 2 スクリプト
    # の前提を崩さないため export しない — 規則を変えるときは必ず両側を同期する。
    slug_ref() {
        printf '%s' "$1" \
            | tr '[:upper:]' '[:lower:]' \
            | sed -e 's#^origin/##' -e 's#^claude/##' -e 's#^bookq-##' \
            | sed -e 's/[^a-z0-9-]/-/g' -e 's/-\{2,\}/-/g' -e 's/^-*//' -e 's/-*$//' \
            | cut -c "1-32" \
            | sed -e 's/-*$//'
    }

    if [ -n "$ref" ]; then
        sha="$(git rev-parse --verify --quiet "${ref}^{commit}")" \
            || die "ref '$ref' を解決できません。git fetch が必要かもしれません。"
        # シンボリック名かどうかは出力の「中身」で判定する（終了コードは git の
        # バージョンで揺れるため信じない）。HEAD はブランチ上なら
        # refs/heads/<branch> を返すので、post-commit フックの --ref HEAD も
        # この経路で拾われる。
        sym="$(git rev-parse --symbolic-full-name --quiet "$ref" 2>/dev/null || true)"
        case "$sym" in
            refs/heads/*|refs/remotes/*|refs/tags/*)
                slug="$(slug_ref "${sym#refs/*/}")"
                tag="bookq-dev:ref-${slug}"
                info "ビルド中（エイリアスタグ上書き）: ${ref} = ${sha:0:12} → $tag"
                git archive --format=tar "$sha" \
                    | docker build -q --label com.bookq.dev-image=1 -t "$tag" - >/dev/null \
                    || die "ビルドに失敗しました (ref=$ref)"
                ;;
            *)
                # sym が空（純 hex・detached・HEAD~N 等）、または refs/stash /
                # refs/notes/* 等のその他の名前空間 = 内容が変わらない参照。
                # 従来どおりコミット専用タグで再利用。
                tag="bookq-dev:${sha:0:12}"
                if docker image inspect "$tag" >/dev/null 2>&1; then
                    info "イメージ再利用: $tag (${ref} = ${sha:0:12})"
                else
                    info "ビルド中: ${ref} = ${sha:0:12}"
                    git archive --format=tar "$sha" \
                        | docker build -q --label com.bookq.dev-image=1 -t "$tag" - >/dev/null \
                        || die "ビルドに失敗しました (ref=$ref)"
                fi
                ;;
        esac
        printf '%s %s' "$tag" "${sha:0:12}"
    else
        tag="bookq-dev:wt-${name}"
        info "ビルド中: 現在の作業ツリー"
        docker build -q --label com.bookq.dev-image=1 -t "$tag" "$SCRIPT_DIR" >/dev/null \
            || die "ビルドに失敗しました"
        sha="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
        printf '%s %s' "$tag" "${sha:0:12}"
    fi
}

# dev deploy: 起動（dev.sh が呼ぶ。DEV_* 変数は環境から来る）。
# --build は本番 (deploy_prd) と同じ理由で必須。viewer は image: 指定なので
# 影響を受けないが、tailscale サイドカーは build: ./tailscale なので、付けないと
# 一度作られたイメージが二度と更新されず、tailscale/ への変更が dev に届かない。
dev_deploy() {
    : "${DEV_PROJECT:?DEV_PROJECT が未設定です。dev.sh 経由で実行してください。}"
    docker compose -f "$DEV_COMPOSE" -p "$DEV_PROJECT" up -d --build \
        || die "開発環境の起動に失敗しました。ログ: ./dev.sh logs $DEV_NAME"
}

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

case "${1:-}" in
    prd)
        shift
        case "${1:-}" in
            build)  shift; prd_build_image "$@" ;;
            deploy) deploy_prd ;;
            "") usage >&2; die "prd のサブコマンドを指定してください: build <sha> / deploy";;
            *) die "prd の不明なサブコマンド: $1（build <sha> / deploy）" ;;
        esac
        ;;
    dev)
        shift
        case "${1:-}" in
            build)  shift; dev_build "$@" ;;
            deploy) dev_deploy ;;
            verify) dev_verify ;;
            "") die "dev のサブコマンドを指定してください: build / deploy / verify";;
            *) die "dev の不明なサブコマンド: $1（build / deploy / verify）" ;;
        esac
        ;;
    -h|--help|help) usage ;;
    "") die "ターゲットを指定してください: prd | dev";;
    *) die "不明なターゲット: $1（prd | dev）" ;;
esac
