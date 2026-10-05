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

if tailscale --socket="$SOCKET" status >/dev/null 2>&1; then
    echo "[tailscale] Already authenticated, updating hostname..."
    tailscale --socket="$SOCKET" up --hostname="$HOSTNAME"
else
    # 認証キーが要るのは初回だけ。state ボリュームが残っていれば上の分岐に入る。
    if [ -z "$AUTHKEY" ]; then
        echo "[tailscale] ERROR: not authenticated and TS_AUTHKEY is empty." >&2
        echo "[tailscale] Set TS_AUTHKEY (env or .env) for the first start." >&2
        exit 1
    fi
    echo "[tailscale] Authenticating as $HOSTNAME..."
    tailscale --socket="$SOCKET" up --authkey="$AUTHKEY" --hostname="$HOSTNAME"
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
