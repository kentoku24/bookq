#!/usr/bin/env bash
# bookq spec-mock entrypoint (#237): REST + WS mocks for client-only dev.
#
#   ./scripts/api/mock.sh [--port 4010] [--python|--prism]   # REST mock
#   ./scripts/api/mock.sh ws [--port 4011] | ws list | ws show <case-id>
#
# REST default is the stdlib python mock (no install). --prism uses
# `prism mock` when @stoplight/prism-cli is available (needs node/npx).
# Scenario switching on both: `Prefer: example=<name>`, `?__example=<name>`,
# `?__status=<code>` (python mock; Prism honors Prefer/__example natively).
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

usage() {
  sed -n '2,12p' "$0"
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then usage; exit 0; fi

if [[ "${1:-}" == "ws" ]]; then
  shift
  if [[ "${1:-}" == "list" ]]; then exec python3 "$REPO/scripts/api/ws_mock.py" list; fi
  if [[ "${1:-}" == "show" ]]; then exec python3 "$REPO/scripts/api/ws_mock.py" show "${2:?usage: mock.sh ws show <case-id>}"; fi
  PORT=4011
  while [[ $# -gt 0 ]]; do
    case "$1" in --port) PORT="${2:?}"; shift 2;; --port=*) PORT="${1#--port=}"; shift;; *) break;; esac
  done
  exec python3 "$REPO/scripts/api/ws_mock.py" serve --port "$PORT"
fi

PORT=4010
MODE=auto
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="${2:?}"; shift 2;;
    --port=*) PORT="${1#--port=}"; shift;;
    --prism) MODE=prism; shift;;
    --python) MODE=python; shift;;
    -h|--help) usage; exit 0;;
    *) echo "unknown arg: $1" >&2; usage >&2; exit 2;;
  esac
done

if [[ "$MODE" == "prism" || ( "$MODE" == "auto" && "${BOOKQ_MOCK_PRISM:-0}" == "1" ) ]]; then
  if ! command -v npx >/dev/null 2>&1; then
    echo "npx not found — falling back to stdlib python mock" >&2
  else
    echo "[mock] prism mock $REPO/api/openapi.v1.yaml --port $PORT"
    exec npx --yes @stoplight/prism-cli mock "$REPO/api/openapi.v1.yaml" \
      --port "$PORT" --host 127.0.0.1 --errors
  fi
fi

BUNDLE="$REPO/api/dist/examples.json"
if [[ ! -f "$BUNDLE" ]]; then
  echo "[mock] regenerating stale/missing bundle…" >&2
  python3 "$REPO/scripts/api/extract-examples.py" --out "$BUNDLE"
fi
exec python3 "$REPO/scripts/api/mock_server.py" --port "$PORT" --bundle "$BUNDLE"
