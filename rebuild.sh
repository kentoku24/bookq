#!/usr/bin/env bash
# 汎用デプロイエンジン: ビルド → デプロイ → healthcheck → URL 検証。
#
#   ./rebuild.sh prd build <sha>   本番イメージを決定論ビルド（archive 由来・dirty 除外）
#   ./rebuild.sh prd deploy         本番を prebuilt image で起動＋検証
#   ./rebuild.sh dev build <name> [ref]   開発環境のイメージをビルド
#   ./rebuild.sh dev deploy               開発環境を起動（dev.sh が呼ぶ）
#   ./rebuild.sh dev verify               起動済み環境の healthcheck + URL 検証
#   ./rebuild.sh dev heal-fqdn            tailnet 登録名と DEV_FQDN のずれを自動復旧（dev.sh が呼ぶ）
#
# 本番は commit 済み内容のみ（dirty 除外）。worktree ガードは prd.sh 側。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

DEV_COMPOSE="docker-compose.dev.yml"

die()  { printf '❌ %s\n' "$*" >&2; exit 1; }
info() { printf '   %s\n' "$*" >&2; }
warn() { printf '⚠️  %s\n' "$*" >&2; }

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

# ============================================================ 共通: ノード名衝突の自動復旧
# state ボリューム（= machine key）が失われたまま tailnet に旧登録が残っていると、
# sidecar は別ノードとして認証され control plane が -1 等の尾番を付ける
# （docs/deployment.md「ノード名の衝突」）。期待 FQDN が offline の旧登録を指した
# ままだと verify_url が ~6 分ハングしてから HTTP 000 で落ちるだけなので、
# ここで能動的に直す: tag:bookq-dev を持つ（= この repo の authkey 由来）かつ
# offline の旧登録だけを admin API で削除 → 自分を期待名に rename → sidecar
# restart で serve を正しい FQDN に張り直す。無タグ・他プロジェクト・
# オンラインの同名ノードは巻き込み防止のため触らず警告に留める。

# sidecar の実登録 FQDN（例: dev-03-1.<tailnet>）。tailscaled 起動待ちを兼ねて
# ~15s リトライする。応答なし・未起動なら空出力。
sidecar_dns_name() {
    local c="$1" dns=""
    for _ in $(seq 1 15); do
        dns="$(docker exec "$c" tailscale --socket=/var/run/tailscale/tailscaled.sock \
            status --json 2>/dev/null \
            | python3 -c 'import sys,json; print(json.load(sys.stdin).get("Self",{}).get("DNSName","").rstrip("."))' 2>/dev/null || true)"
        [ -n "$dns" ] && break
        sleep 1
    done
    printf '%s' "$dns"
}

# sidecar の netmap 上で fqdn/IP の peer がオンラインか。終了コード 0=online、
# 1=offline/peer 不在/解析不能（offline 側に倒して削除判断は呼び出し側）。
peer_online() {
    docker exec "$1" tailscale --socket=/var/run/tailscale/tailscaled.sock \
        status --json 2>/dev/null | python3 -c '
import json, sys
fqdn, ip = sys.argv[1], sys.argv[2]
try:
    peers = json.load(sys.stdin).get("Peer") or {}
except Exception:
    sys.exit(1)
for p in peers.values():
    if (p.get("DNSName") or "").rstrip(".") == fqdn or ip in (p.get("TailscaleIPs") or []):
        sys.exit(0 if p.get("Online") else 1)
sys.exit(1)' "$2" "$3"
}

# admin API の認証。OAuth client 短命トークンを優先し、無ければ TS_API_KEY の
# basic auth（dev.sh の down_full と同じ解決順）。結果は TS_AUTH_ARGS に入れる。
ts_admin_auth() {
    local bearer api_key
    bearer="$("$SCRIPT_DIR/scripts/ensure-ts-authkey.sh" --token 2>/dev/null || true)"
    if [ -n "$bearer" ]; then
        TS_AUTH_ARGS=(-H "Authorization: Bearer $bearer")
        return 0
    fi
    api_key="${TS_API_KEY:-}"
    [ -n "$api_key" ] || api_key="$(grep -E '^TS_API_KEY=' .env 2>/dev/null | tail -1 \
        | cut -d= -f2- | tr -d '"' | tr -d "'" | tr -d '[:space:]' || true)"
    if [ -n "$api_key" ]; then
        TS_AUTH_ARGS=(-u "$api_key:")
        return 0
    fi
    return 1
}

# 期待 FQDN を握る同名ノードを掃除して自分を期待名に直す。
# 使い方: heal_fqdn_collision <sidecar_container> <expected_fqdn>
# 0 = 最終的に期待名で登録済み / 1 = 不一致が残る（警告済み）。
heal_fqdn_collision() {
    local sidecar="$1" expected="$2"
    local actual self_id rows code
    actual="$(sidecar_dns_name "$sidecar")"
    if [ -z "$actual" ]; then
        warn "sidecar ($sidecar) の tailnet 登録名を取得できません。衝突判定をスキップします。"
        return 1
    fi
    [ "$actual" = "$expected" ] && return 0

    warn "tailnet 登録名が期待と異なります: 期待 $expected → 実際 $actual"
    info "同名のゴーストノードが残っています（docs/deployment.md「ノード名の衝突」）。自動復旧を試みます。"

    if ! ts_admin_auth; then
        warn "Tailscale admin API の資格情報がありません（TS_OAUTH_CLIENT_ID/SECRET または TS_API_KEY）。"
        warn "手動復旧: admin コンソールで旧ノード削除 → docker restart $sidecar"
        return 1
    fi

    local devices http
    devices="$(curl -sS -m 15 -w '\n%{http_code}' "${TS_AUTH_ARGS[@]}" \
        "https://api.tailscale.com/api/v2/tailnet/-/devices" 2>/dev/null || true)"
    http="$(printf '%s\n' "$devices" | tail -1)"
    devices="$(printf '%s\n' "$devices" | sed '$d')"
    if [ "$http" != "200" ]; then
        warn "tailnet デバイス一覧の取得に失敗しました (HTTP ${http:-なし})"
        return 1
    fi

    # SELF\t<id> / GHOST\t<id>\t<ip>\t<tags-csv> の行を出す。name は control
    # plane 確定済みの FQDN（suffix 付き）なので、期待名を持つ別 id がゴースト。
    rows="$(printf '%s\n' "$devices" | python3 -c '
import json, sys
expected, actual = sys.argv[1], sys.argv[2]
try:
    devs = json.load(sys.stdin).get("devices", [])
except Exception:
    sys.exit(2)
self_id = ""
for d in devs:
    if (d.get("name") or "").rstrip(".") == actual:
        self_id = d.get("id", "")
print("SELF\t" + self_id)
for d in devs:
    if (d.get("name") or "").rstrip(".") == expected and d.get("id", "") != self_id:
        addr = (d.get("addresses") or [""])[0]
        print("GHOST\t%s\t%s\t%s" % (d.get("id", ""), addr, ",".join(d.get("tags") or [])))
' "$expected" "$actual")" || { warn "デバイス一覧の解析に失敗しました"; return 1; }

    self_id="$(printf '%s\n' "$rows" | awk -F'\t' '$1=="SELF"{print $2}')"
    if [ -z "$self_id" ]; then
        warn "自分自身の device id が見つかりません ($actual)"
        return 1
    fi

    local blocked=0 id ip tags
    while IFS=$'\t' read -r kind id ip tags; do
        [ "$kind" = "GHOST" ] || continue
        case ",$tags," in
            *",tag:bookq-dev,"*) ;;
            *)  warn "ゴースト $id ($expected) は tag:bookq-dev を持たないため自動削除しません（別プロジェクト/手動登録の可能性）。admin コンソールで確認してください。"
                blocked=1; continue ;;
        esac
        # netmap の Online 反映は数秒〜1分遅れる。死にたてのゴーストを「生存
        # 中」と誤認して放置しないよう、offline になるまで待ってから判定する。
        # それでも online なら本当に生存する別環境なので触らない。
        if peer_online "$sidecar" "$expected" "$ip"; then
            local waited=0
            while peer_online "$sidecar" "$expected" "$ip" && [ "$waited" -lt 6 ]; do
                sleep 10; waited=$((waited+1))
            done
            if peer_online "$sidecar" "$expected" "$ip"; then
                warn "ゴースト $id ($expected) がオンラインのままです — 生存している別環境が名前を保持しています。自動削除しません。"
                blocked=1; continue
            fi
        fi
        code="$(curl -s -o /dev/null -w '%{http_code}' -m 15 -X DELETE \
            "${TS_AUTH_ARGS[@]}" "https://api.tailscale.com/api/v2/device/$id" || true)"
        case "$code" in
            200|404) info "ゴーストノード $id ($expected) を削除しました" ;;
            *) warn "ゴーストノード $id の削除に失敗しました (HTTP ${code:-なし})"; blocked=1 ;;
        esac
    done <<< "$rows"
    [ "$blocked" = 0 ] || return 1

    # 自分を期待名に rename。up --hostname は control plane 確定済み名を
    # 上書きしないため admin API を使う。ゴーストが見つからない場合もここに来る
    #（外部で削除済みなら rename だけで直る）。削除直後の伝播待ちは retry で吸収。
    local host_label="${expected%%.*}"
    code=""
    for _ in 1 2 3; do
        code="$(curl -s -o /dev/null -w '%{http_code}' -m 15 -X POST "${TS_AUTH_ARGS[@]}" \
            -H 'Content-Type: application/json' -d "{\"name\":\"$host_label\"}" \
            "https://api.tailscale.com/api/v2/device/$self_id/name" || true)"
        [ "$code" = "200" ] && break
        sleep 3
    done
    if [ "$code" != "200" ]; then
        warn "ノード名の変更に失敗しました (HTTP ${code:-なし})。手動: admin コンソールで $actual を $host_label に rename"
        return 1
    fi

    # restart で entrypoint の serve 再設定も兼ねる（serve は FQDN 単位で残る）。
    docker restart "$sidecar" >/dev/null 2>&1 || { warn "sidecar の restart に失敗しました"; return 1; }
    for _ in $(seq 1 20); do
        sleep 3
        actual="$(docker exec "$sidecar" tailscale --socket=/var/run/tailscale/tailscaled.sock \
            status --json 2>/dev/null \
            | python3 -c 'import sys,json; print(json.load(sys.stdin).get("Self",{}).get("DNSName","").rstrip("."))' 2>/dev/null || true)"
        if [ "$actual" = "$expected" ]; then
            info "✅ tailnet 登録名を $expected に復旧しました"
            # 改名直後の FQDN は ACME dns-01 の発行遅延で最初の TLS が失敗し
            # うる。ここで1回温めておくと後続の verify_url が即通る
            #（失敗しても verify_url のリトライがカバーするので best-effort）。
            docker exec "$sidecar" tailscale --socket=/var/run/tailscale/tailscaled.sock \
                cert "$expected" >/dev/null 2>&1 || true
            return 0
        fi
    done
    warn "rename 後も登録名が $actual のままです。手動確認してください。"
    return 1
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

    # tailnet 登録名が BASE_URL とずれている（= 同名ゴースト残存で -1 付与）と
    # BASE_URL は offline ノードを指すので、verify_url 前に自動復旧を試みる。
    local expected_fqdn sidecar
    expected_fqdn="$(printf '%s' "$base_url" | sed -E 's#^https?://##; s#/.*$##')"
    sidecar="$(docker compose -f docker-compose.yml -f docker-compose.prd-image.yml ps -q tailscale 2>/dev/null || true)"
    if [ -n "$sidecar" ]; then
        heal_fqdn_collision "$sidecar" "$expected_fqdn" || true
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

# dev heal-fqdn: dev.sh up が sidecar の実登録名と期待名のずれを検出したときに
# 呼ばれる。DEV_PROJECT / DEV_FQDN は dev.sh が export 済み。
dev_heal_fqdn() {
    : "${DEV_PROJECT:?DEV_PROJECT が未設定です。dev.sh 経由で実行してください。}"
    : "${DEV_FQDN:?DEV_FQDN が未設定です。dev.sh 経由で実行してください。}"
    local sidecar
    sidecar="$(docker ps -q \
        --filter "label=com.docker.compose.project=$DEV_PROJECT" \
        --filter "label=com.docker.compose.service=tailscale" | head -1)"
    [ -n "$sidecar" ] || die "開発環境 ($DEV_PROJECT) の sidecar コンテナが見つかりません。"
    heal_fqdn_collision "$sidecar" "$DEV_FQDN"
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
            heal-fqdn) dev_heal_fqdn ;;
            "") die "dev のサブコマンドを指定してください: build / deploy / verify / heal-fqdn";;
            *) die "dev の不明なサブコマンド: $1（build / deploy / verify / heal-fqdn）" ;;
        esac
        ;;
    -h|--help|help) usage ;;
    "") die "ターゲットを指定してください: prd | dev";;
    *) die "不明なターゲット: $1（prd | dev）" ;;
esac
