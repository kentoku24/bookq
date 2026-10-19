#!/usr/bin/env bash
# TS_AUTHKEY の期限切れを検知し、可能なら OAuth client で自動更新する。
#
#   scripts/ensure-ts-authkey.sh [--quick] [--force] [--token]
#
# 動作（終了コードは常に 0 — 呼び出し側のデプロイをブロックしない）:
#   1. .env の TS_AUTHKEY_EXPIRES (YYYY-MM-DD) が残り 14 日を切る、未記録、
#      または --force 指定なら「要更新」とみなす。
#   2. OAuth client (TS_OAUTH_CLIENT_ID / TS_OAUTH_CLIENT_SECRET) があれば
#      /api/v2/oauth/token → POST /tailnet/<tn>/keys で 90 日の reusable
#      キーを mint し、メイン checkout の .env の TS_AUTHKEY /
#      TS_AUTHKEY_EXPIRES / TS_AUTHKEY_ID を書き換える。旧キーは記録済み
#      id があれば DELETE で失効させる（best-effort）。
#   3. OAuth が無い・失敗する場合は警告だけ出して戻る。
#   --quick は mint を行わず期限の静的警告だけ出す（dev.sh list 用）。
#   --token は検査を行わず admin API 用の access token を1行だけ stdout に
#   出す（OAuth 未設定や失敗時は空出力）。down_full のノード削除が
#   TS_API_KEY の代わりに使う。
#
# OAuth client の発行: 管理コンソール → Trust credentials。
# 必要 scope は auth_keys（ノード削除にも使うなら devices も）。client
# secret 自体は期限切れにならないので、設定後の更新作業は不要になる。
# 注意: OAuth mint の authkey は tailnet 所有・タグ付き専用（API の仕様）。
# 既定で tag:bookq-dev を付ける（TS_AUTHKEY_TAGS で変更可）— tailnet の
# tagOwners にそのタグが無いと mint は "must have tags set" で失敗する。
set -uo pipefail

WARN_DAYS=14
AUTHKEY_LIFETIME=7776000   # 90 days（authkey の API 上限）

QUICK="" FORCE="" TOKEN_ONLY=""
for a in "$@"; do
    case "$a" in
        --quick) QUICK=1 ;;
        --force) FORCE=1 ;;
        --token) TOKEN_ONLY=1 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

warn() { printf '⚠️  %s\n' "$*" >&2; }
info() { printf '   %s\n' "$*" >&2; }

# dev.sh と同じ解決順: worktree から実行されても .env はメイン checkout を見る。
main_checkout() {
    local common
    common="$(git -C "$REPO_ROOT" rev-parse --git-common-dir 2>/dev/null || true)"
    [ -n "$common" ] || { printf '%s' "$REPO_ROOT"; return; }
    case "$common" in /*) ;; *) common="$REPO_ROOT/$common" ;; esac
    ( cd "$common/.." && pwd )
}
ENV_FILE="$(main_checkout)/.env"

dotenv_value() {
    local key="$1" f v
    for f in "$REPO_ROOT/.env" "$ENV_FILE"; do
        [ -f "$f" ] || continue
        v="$(grep -E "^${key}=.+" "$f" 2>/dev/null | tail -1 | cut -d= -f2- \
            | sed -e 's/\r$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/")"
        [ -n "$v" ] && { printf '%s' "$v"; return 0; }
    done
    return 1
}
getv() { local n="$1"; if [ -n "${!n:-}" ]; then printf '%s' "${!n}"; else dotenv_value "$n" || true; fi; }

days_until() {  # YYYY-MM-DD → 整数日（過去は負）。解釈不能は空出力+非0。
    python3 -c 'import sys,datetime
try: print((datetime.date.fromisoformat(sys.argv[1].strip())-datetime.date.today()).days)
except Exception: sys.exit(1)' "$1" 2>/dev/null
}

set_dotenv() {   # KEY VALUE — ENV_FILE の行を置換、無ければ追記
    [ -f "$ENV_FILE" ] || return 1
    python3 - "$1" "$2" "$ENV_FILE" <<'PY'
import sys, re
key, val, path = sys.argv[1:4]
lines = open(path).read().splitlines(keepends=True)
out, done = [], False
for ln in lines:
    if not done and re.match(rf'^{re.escape(key)}=', ln):
        out.append(f'{key}={val}\n'); done = True
    else:
        out.append(ln)
if not done:
    if out and not out[-1].endswith('\n'): out[-1] += '\n'
    out.append(f'{key}={val}\n')
open(path, 'w').writelines(out)
PY
}

OAUTH_ID="$(getv TS_OAUTH_CLIENT_ID)"
OAUTH_SECRET="$(getv TS_OAUTH_CLIENT_SECRET)"
TAILNET="$(getv TS_TAILNET)"; TAILNET="${TAILNET:--}"
# OAuth mint の authkey は tailnet 所有のためタグ必須。カンマ区切りで複数可。
AUTHKEY_TAGS="$(getv TS_AUTHKEY_TAGS)"; AUTHKEY_TAGS="${AUTHKEY_TAGS:-tag:bookq-dev}"
TAGS_JSON="$(printf '%s' "$AUTHKEY_TAGS" | sed 's/[[:space:]]//g;s/,/","/g;s/^/"/;s/$/"/')"

# admin API 用 access token（1h TTL）。失敗時は空出力+非0。
oauth_token() {
    [ -n "$OAUTH_ID" ] && [ -n "$OAUTH_SECRET" ] || return 1
    curl -sS -m 10 -u "$OAUTH_ID:$OAUTH_SECRET" \
        -d 'grant_type=client_credentials' \
        'https://api.tailscale.com/api/v2/oauth/token' 2>/dev/null \
        | python3 -c 'import sys,json;print(json.load(sys.stdin).get("access_token","") or "")' 2>/dev/null \
        | grep . || return 1
}

if [ -n "$TOKEN_ONLY" ]; then
    oauth_token || true
    exit 0
fi

AUTHKEY="$(getv TS_AUTHKEY)"
AUTH_EXP="$(getv TS_AUTHKEY_EXPIRES)"
API_KEY="$(getv TS_API_KEY)"
API_EXP="$(getv TS_API_KEY_EXPIRES)"
OLD_KEY_ID="$(getv TS_AUTHKEY_ID)"

# ------------------------------------------------- 静的な期限チェック
NEEDS_RENEW=""
if [ -z "$AUTHKEY" ]; then
    warn "TS_AUTHKEY が未設定です。新規 dev env / sidecar の登録に失敗します。"
fi
if [ -z "$AUTH_EXP" ]; then
    [ -n "$AUTHKEY" ] && warn "TS_AUTHKEY_EXPIRES が未記録のため期限を予測できません。.env に TS_AUTHKEY_EXPIRES=YYYY-MM-DD を追記してください（再発行時に更新）。"
    NEEDS_RENEW=1
else
    days="$(days_until "$AUTH_EXP")" || days=""
    if [ -z "$days" ]; then
        warn "TS_AUTHKEY_EXPIRES='${AUTH_EXP}' は YYYY-MM-DD 形式で記録してください。"
        NEEDS_RENEW=1
    elif [ "$days" -lt 0 ]; then
        warn "TS_AUTHKEY は期限切れです (${AUTH_EXP})。"
        NEEDS_RENEW=1
    elif [ "$days" -le "$WARN_DAYS" ]; then
        warn "TS_AUTHKEY があと ${days} 日で期限切れです (${AUTH_EXP})。"
        NEEDS_RENEW=1
    fi
fi
[ -n "$FORCE" ] && NEEDS_RENEW=1

# TS_API_KEY は OAuth 設定後は不要になるため、設定されている場合だけ期限を見る。
if [ -n "$API_KEY" ]; then
    if [ -z "$API_EXP" ]; then
        warn "TS_API_KEY_EXPIRES が未記録です。.env に TS_API_KEY_EXPIRES=YYYY-MM-DD を追記してください。"
    else
        days="$(days_until "$API_EXP")" || days=""
        if [ -z "$days" ]; then
            warn "TS_API_KEY_EXPIRES='${API_EXP}' は YYYY-MM-DD 形式で記録してください。"
        elif [ "$days" -lt 0 ]; then
            warn "TS_API_KEY は期限切れです (${API_EXP})。down --full のノード API 削除が失敗します。OAuth client を設定すれば不要になります。"
        elif [ "$days" -le "$WARN_DAYS" ]; then
            warn "TS_API_KEY があと ${days} 日で期限切れです (${API_EXP})。down --full のノード API 削除が失敗します。OAuth client を設定すれば不要になります。"
        fi
    fi
fi

# ------------------------------------------------- OAuth による自動更新
if [ -z "$NEEDS_RENEW" ]; then
    exit 0
fi
if [ -n "$QUICK" ]; then
    [ -n "$OAUTH_ID" ] && info "（./dev.sh up 時に OAuth client 経由で自動更新を試みます）"
    exit 0
fi
if [ -z "$OAUTH_ID" ] || [ -z "$OAUTH_SECRET" ]; then
    warn "自動更新できません: .env に TS_OAUTH_CLIENT_ID / TS_OAUTH_CLIENT_SECRET を設定すると以降は自動化されます。"
    warn "手動再発行: https://login.tailscale.com/admin/settings/keys （reusable / 非 ephemeral。発行後 TS_AUTHKEY_EXPIRES も更新）"
    exit 0
fi
command -v python3 >/dev/null 2>&1 || { warn "python3 が無いため自動更新できません。"; exit 0; }

info "TS_AUTHKEY を OAuth client 経由で自動更新します..."
token="$(oauth_token || true)"
if [ -z "$token" ]; then
    warn "OAuth client のトークン取得に失敗しました（client 無効またはネットワーク不通）。手動再発行: https://login.tailscale.com/admin/settings/keys"
    exit 0
fi

resp="$(curl -sS -m 10 -w '\n%{http_code}' -X POST \
    -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
    -d "{\"capabilities\":{\"devices\":{\"create\":{\"reusable\":true,\"ephemeral\":false,\"preauthorized\":true,\"tags\":[${TAGS_JSON}]}}},\"expirySeconds\":$AUTHKEY_LIFETIME,\"description\":\"bookq-authkey-auto\"}" \
    "https://api.tailscale.com/api/v2/tailnet/${TAILNET}/keys" 2>/dev/null || true)"
http="$(printf '%s\n' "$resp" | tail -1)"
body="$(printf '%s\n' "$resp" | sed '$d')"

case "$http" in
    200) ;;
    401|403)
        warn "OAuth client に権限がありません（auth_keys scope が必要。HTTP ${http}）。手動再発行: https://login.tailscale.com/admin/settings/keys"
        exit 0 ;;
    *)  warn "authkey の mint に失敗しました（HTTP ${http:-なし}）。手動再発行: https://login.tailscale.com/admin/settings/keys"
        exit 0 ;;
esac

parsed="$(printf '%s' "$body" | python3 -c 'import sys,json
d=json.load(sys.stdin)
print(d.get("key","") or "")
print(d.get("id","") or "")
print((d.get("expires") or "")[:10])' 2>/dev/null || true)"
new_key="$(printf '%s\n' "$parsed" | sed -n '1p')"
new_id="$(printf '%s\n' "$parsed" | sed -n '2p')"
new_exp="$(printf '%s\n' "$parsed" | sed -n '3p')"
if [ -z "$new_key" ] || [ -z "$new_exp" ]; then
    warn "authkey mint の応答を解釈できませんでした。手動再発行: https://login.tailscale.com/admin/settings/keys"
    exit 0
fi

if ! set_dotenv TS_AUTHKEY "$new_key"; then
    warn ".env (${ENV_FILE}) が見つからず新しい authkey を保存できません。"
    exit 0
fi
set_dotenv TS_AUTHKEY_EXPIRES "$new_exp" || true
[ -n "$new_id" ] && set_dotenv TS_AUTHKEY_ID "$new_id" || true
printf '✅ TS_AUTHKEY を自動更新しました（expires %s。%s を書き換え済み）\n' "$new_exp" "$ENV_FILE" >&2

# 旧キーの失効（best-effort）。env に TS_AUTHKEY_ID が記録されていれば DELETE する。
if [ -n "$OLD_KEY_ID" ] && [ "$OLD_KEY_ID" != "$new_id" ]; then
    code="$(curl -s -o /dev/null -w '%{http_code}' -m 10 -X DELETE \
        -H "Authorization: Bearer $token" \
        "https://api.tailscale.com/api/v2/tailnet/${TAILNET}/keys/${OLD_KEY_ID}" 2>/dev/null || true)"
    [ "$code" = "200" ] && info "旧 authkey (${OLD_KEY_ID}) を失効させました" || true
fi

# シェル環境に export 済みの古い TS_AUTHKEY が .env を上書きし続ける罠を指摘する。
if [ -n "${TS_AUTHKEY:-}" ] && [ "$TS_AUTHKEY" != "$new_key" ]; then
    warn "シェル環境の TS_AUTHKEY が .env より優先されます。unset TS_AUTHKEY してください。"
fi
exit 0
