#!/bin/sh
set -e

STATE_DIR="/var/lib/tailscale"
SOCKET="/var/run/tailscale/tailscaled.sock"
AUTHKEY="${TS_AUTHKEY:-}"
HOSTNAME="${TS_HOSTNAME:-bookq}"

echo "[tailscale] Starting tailscaled (userspace networking)..."
tailscaled --state="$STATE_DIR/tailscaled.state" --socket="$SOCKET" --tun=userspace-networking &

echo "[tailscale] Waiting for socket..."
while [ ! -S "$SOCKET" ]; do sleep 0.2; done

# status の終了コードはログアウト時も 0 を返すため信用できない（削除済み
# ノードの state で "Already authenticated" と誤判定し Online:False のまま
# 止まった実績あり）。BackendState を見て判定する。ただし tailscaled 起動
# 直後は NoState/Starting の遷移期があり、Running/NeedsLogin が確定する
# まで待たないと誤った分岐を取る（pending の login URL を吐いて詰まった）。
backend_state() {
    tailscale --socket="$SOCKET" status --json 2>/dev/null \
        | grep -o '"BackendState":"[^"]*"' | cut -d'"' -f4
}

# 最大 ~10s。Running なら確定済み、NeedsLogin は変わらないので即抜けする。
wait_state() {
    local st i
    for i in $(seq 1 40); do
        st="$(backend_state)"
        case "$st" in Running|NeedsLogin) break ;; esac
        sleep 0.25
    done
    printf '%s' "$st"
}

if [ "$(wait_state)" = "Running" ]; then
    echo "[tailscale] Already authenticated, updating hostname..."
    tailscale --socket="$SOCKET" up --hostname="$HOSTNAME"
else
    if [ -n "$AUTHKEY" ]; then
        echo "[tailscale] Authenticating as $HOSTNAME..."
        tailscale --socket="$SOCKET" up --authkey="$AUTHKEY" --hostname="$HOSTNAME" || true
    fi
    if [ "$(wait_state)" != "Running" ]; then
        # state ボリュームのキーが tailnet から削除済み、または authkey
        # 無効/未設定。対話ログイン URL をログに残し、承認されるまで
        # ブロックする（承認後は serve 設定へ進む）。
        echo "[tailscale] Not authenticated (stale node key or no valid TS_AUTHKEY)."
        echo "[tailscale] Hint: expired authkeys auto-renew via scripts/ensure-ts-authkey.sh when OAuth client is configured in .env."
        echo "[tailscale] Interactive login required — open the printed URL to approve:"
        tailscale --socket="$SOCKET" login || {
            echo "[tailscale] ERROR: interactive login failed." >&2
            exit 1
        }
        tailscale --socket="$SOCKET" up --hostname="$HOSTNAME"
    fi
fi

# Both production and every dev environment serve a single fixed backend on :443,
# so nothing sets TS_SERVE_TARGET today and the default below is what runs.
# Note ${VAR-default}, not ${VAR:-default}: an explicitly empty value means
# "serve entries are managed from outside — don't touch them". Keep that escape
# hatch (and the no-colon form) so a future caller can opt out.
SERVE_TARGET="${TS_SERVE_TARGET-http://viewer:8080}"

if [ -n "$SERVE_TARGET" ]; then
    echo "[tailscale] Setting up serve → $SERVE_TARGET"
    tailscale --socket="$SOCKET" serve --https=443 off 2>/dev/null || true
    tailscale --socket="$SOCKET" serve --bg "$SERVE_TARGET"
else
    echo "[tailscale] No TS_SERVE_TARGET — serve entries are managed externally"
fi

echo "[tailscale] Serve status:"
tailscale --socket="$SOCKET" serve status || true

echo "[tailscale] Ready"
wait
