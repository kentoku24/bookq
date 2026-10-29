#!/usr/bin/env bash
# Build the bookq_rs PyO3 wheel (Phase 1 W1, issue #163).
# No-op success when cargo is absent: the app falls back to the Python impl
# (try/except ImportError in app/scanner.py), so the extension is optional.
# Prints the wheel path on success for `--with <wheel>` injection into uv run.
set -euo pipefail

cd "$(dirname "$0")/.."
# cargo proxies live outside PATH with brew rustup (/opt/homebrew/opt/rustup/bin)
# and with upstream rustup (~/.cargo/bin) — add both if they exist.
for d in "$HOME/.cargo/bin" /opt/homebrew/opt/rustup/bin /usr/local/cargo/bin; do
    [ -d "$d" ] && PATH="$d:$PATH"
done
if ! command -v cargo >/dev/null 2>&1; then
    echo "cargo not found — skipping bookq_rs build (Python fallback will be used)" >&2
    exit 0
fi

WHEEL_DIR=rust/bookq_rs/target/wheels
mkdir -p "$WHEEL_DIR"
if command -v uvx >/dev/null 2>&1; then
    (cd rust/bookq_rs && uvx maturin build --release)
else
    (cd rust/bookq_rs && python3 -m pip install --quiet "maturin>=1.5,<2" && python3 -m maturin build --release)
fi
ls "$WHEEL_DIR"/bookq_rs-*.whl | head -1
