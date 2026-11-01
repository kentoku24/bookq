#!/usr/bin/env bash
# 本番 bookq の起動・停止を行う。デプロイ対象は --ref のコミット済み内容のみ
# （作業ツリーの dirty は含まない）。既定 origin/main、origin/* のみ指定可。
#
#   ./prd.sh [up] [--ref origin/main]   起動（デフォルト）。ビルド → 起動 → healthcheck → 公開 URL 確認
#   ./prd.sh down   停止。認証情報などのボリュームは保持される
#
# 未検証コードの動作確認にこれを使ってはいけない。開発環境は ./dev.sh up。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

usage() {
    cat <<'EOF'
bookq 本番環境の起動・停止

 使い方:
   ./prd.sh [up] [--ref <ref>]    起動（デフォルト）。ビルド → 起動 → healthcheck → 公開 URL 確認
   ./prd.sh down    停止。ボリューム（認証情報）は保持される
   ./prd.sh help    このヘルプを表示

 --ref:
   デプロイ対象の ref（既定 origin/main）。origin/* のみ指定可。
   デプロイされるのはそのコミット済み内容だけで、作業ツリーの dirty は含まない。
   ref が解決できない場合は先に git fetch origin を実行すること（自動 fetch はしない）。

 例:
   ./prd.sh       # 起動（origin/main をデプロイ）
   ./prd.sh up    # 起動（origin/main をデプロイ）
   ./prd.sh up --ref origin/release-1.2  # 指定 ref のコミット済み内容をデプロイ
   ./prd.sh down  # 停止
EOF
}

# 本番を worktree から誤って操作しないためのガード。
# このガードは本番にだけ必要なので rebuild.sh には置かず、ここで済ませる。
# linked worktree では --git-dir と --git-common-dir が食い違う。
guard_main_checkout() {
    if [ "$(git rev-parse --git-dir 2>/dev/null || true)" != "$(git rev-parse --git-common-dir 2>/dev/null || true)" ]; then
        echo "❌ ここは git worktree です。本番の操作はメインのチェックアウトから実行してください。" >&2
        echo "   開発環境を立てたい場合は: ./dev.sh up" >&2
        exit 1
    fi
}

# 表示用に BASE_URL を .env から拾う（rebuild.sh の検証用とは別）。
base_url() {
    local url
    url="${BASE_URL:-$(grep -E '^BASE_URL=' .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'\''' | tr -d '[:space:]' || true)}"
    echo "${url:-https://<tailscale-hostname>}"
}

die() { printf '❌ %s\n' "$*" >&2; exit 1; }

cmd_up() {
    # dev.sh cmd_up と同型のループ。--ref のみ受け付け、未知の -* は die。
    local ref="origin/main"
    while [ $# -gt 0 ]; do
        case "$1" in
            --ref)   [ $# -ge 2 ] || die "--ref には値が必要です"; ref="$2"; shift 2 ;;
            --ref=*) ref="${1#--ref=}"; shift ;;
            -*) die "不明なオプション: $1" ;;
            *)  die "引数が多すぎます: $1" ;;
        esac
    done
    [ -n "$ref" ] || die "--ref には値が必要です"

    case "$ref" in
        origin/*) ;;
        *) die "--ref は origin/* のみ指定できます: ${ref}（ローカルブランチ・タグ・生 SHA は不可）" ;;
    esac

    local sha
    sha="$(git rev-parse --verify --quiet "${ref}^{commit}")" || true
    [ -n "$sha" ] || die "ref '$ref' を解決できません。git fetch origin が必要かもしれません（自動 fetch はしません）"

    local sha12="${sha:0:12}"
    local subject
    subject="$(git log -1 --format='%s (%ci)' "$sha")"

    # 切替後の復旧情報用に、現在動いている viewer の image を控える（読取のみ）。
    local prev_image
    prev_image="$(docker inspect --format '{{.Config.Image}}' "$(docker compose ps -q viewer 2>/dev/null)" 2>/dev/null || true)"

    echo "🚀 本番に $ref ($sha12) をデプロイします（作業ツリーの dirty は含みません）"
    echo "   $subject"
    echo "   ref が古い場合は先に git fetch origin を実行してください（自動 fetch はしません）"
    echo ""

    # OAuth client 設定時は期限切れの authkey をここで自動更新する（警告のみ）。
    [ -x "$SCRIPT_DIR/scripts/ensure-ts-authkey.sh" ] \
        && "$SCRIPT_DIR/scripts/ensure-ts-authkey.sh" || true

    # TS_AUTHKEY は tailscale/entrypoint.sh が必須にしている。
    # シェル環境に無ければ .env からの compose 補間に任せる。
    if [ -z "${TS_AUTHKEY:-}" ] && ! grep -qE '^TS_AUTHKEY=.+' .env 2>/dev/null; then
        echo "❌ TS_AUTHKEY が設定されていません。"
        echo ""
        echo "発行: https://login.tailscale.com/admin/settings/keys"
        echo "その後:"
        echo "  export TS_AUTHKEY=tskey-auth-..."
        echo "  ./prd.sh"
        echo ""
        echo "（.env に TS_AUTHKEY= を書いておく方法でも構いません）"
        exit 1
    fi

    # ビルド・起動・healthcheck 待ち・公開 URL の確認はすべて rebuild.sh に集約している。
    # worktree ガードはこのスクリプト（prd.sh）の責務で、上の case で済ませている。
    # 二段化が必須: read ... <<< "$(...)" の一発形は、コマンド置換の失敗が
    # read の成功にマスクされ set -e が発火しない。
    local out viewer_tag sidecar_tag rs_tag
    out="$("$SCRIPT_DIR/rebuild.sh" prd build "$sha")" || die "prd build に失敗しました（ref=${ref}）"
    read viewer_tag sidecar_tag rs_tag <<< "$out"
    export PRD_VIEWER_IMAGE="$viewer_tag" PRD_TAILSCALE_IMAGE="$sidecar_tag" PRD_EXPECT_SHA="${sha:0:12}"
    # Dockerfile.viewer-rs がその ref にあったときのみ pin（無い ref への
    # rollback を壊さない）。空なら prd-image.yml の :- 既定値が効く。
    [ -n "$rs_tag" ] && export PRD_RS_IMAGE="$rs_tag"
    "$SCRIPT_DIR/rebuild.sh" prd deploy

    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "✅ bookq (本番) が動いています"
    echo "   $(base_url)"
    echo "   今回: $viewer_tag / $sidecar_tag${rs_tag:+ / $rs_tag} ($sha12)"
    echo "   前回: ${prev_image:-（なし・初回デプロイの可能性）}"
    echo ""
    echo "戻す場合（自動ロールバックはしません）:"
    echo "   ./prd.sh up --ref <previous-ref>"
    echo ""
    echo "停止:"
    echo "   ./prd.sh down"
    echo ""
    echo "開発環境を立てる:"
    echo "   ./dev.sh up"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}

cmd_down() {
    echo "🛑 bookq (本番) を停止します..."
    echo ""
    docker compose down
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "✅ bookq (本番) を停止しました"
    echo "   認証情報などのボリュームは保持されています"
    echo ""
    echo "再起動:"
    echo "   ./prd.sh up"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}

# ガードはコマンドを決めたあとに掛ける。help / 不明なコマンドの usage は
# worktree からでも読めないと、そもそも何を叩けばいいのか分からない。
CMD="${1:-up}"
case "$CMD" in
    up)   guard_main_checkout; shift $(( $# > 0 )); cmd_up "$@" ;;
    down) guard_main_checkout; cmd_down ;;
    help|-h|--help) usage ;;
    *)
        echo "❌ 不明なコマンド: $CMD" >&2
        echo "" >&2
        usage >&2
        exit 1
        ;;
esac
