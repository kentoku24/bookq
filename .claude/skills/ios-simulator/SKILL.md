---
name: ios-simulator
description: Use when verifying bookq on real Safari via iOS Simulator — PWA offline, Service Worker, storage 0/0 fallback, spread/rotation, or any Safari-specific behavior that Chromium/WebKit emulation cannot reproduce. Trigger on "simulatorで確認", "iOSシミュレータ", "Safariで確認", "実機Safari", "ios-simulator", or when Tier2 manual QA is needed.
---

# iOS Simulator

Real Safari via `xcrun simctl` (Tier2). `BROWSER=webkit` is Tier1 emulation on CI; this skill is the **actual WebKit** on macOS.

**bookq is PWA — do not `simctl install`.** Open dev URL in Safari: `xcrun simctl openurl booted <dev-url>`.

## When to Use

- Safari-only: `Service Worker`/`Cache API`, `storage.estimate()` 0/0, `persist()` toast, spread/rotation, PWA `offline.html`
- `BROWSER=webkit` passes but real Safari fails, or `video-verification` (Chromium) insufficient
- "check on iOS / Simulator / real Safari"

Not for: pure API/logic (`pytest`), Chromium motion (`video-verification`), 7-day wait (see Constraints).

## Quick Reference

| Task | Command |
|---|---|
| Open (default iPhone 13) | `./scripts/open-simulator.sh https://dev-demo.<your-tailnet>.ts.net` |
| Open current env | `./scripts/open-simulator.sh $(./dev.sh url demo)` |
| Open local env (no tailscale) | `./dev.sh up demo --local --fixture` → `./scripts/open-simulator.sh http://127.0.0.1:8080` |
| Device override | `SIMULATOR_DEVICE="iPad Pro 13-inch (M4) (16GB)" ./scripts/open-simulator.sh <url>` |
| Next device (Android) | Android 15 1920x1200 Unisoc T620 2.2GHz / Mali-G57 — real hardware |
| Preflight | `./scripts/open-simulator.sh check` |
| List devices | `./scripts/open-simulator.sh list` |
| **Drive taps (agent)** | `idb ui tap x y --udid <udid>` / **`./scripts/sim-gesture-battery.sh <url>`** — details `references/tap-driving.md` |
| Screenshot | `./scripts/open-simulator.sh screenshot /tmp/sim.png` |
| Video (Simulator) | `xcrun simctl io booted recordVideo /tmp/sim.mp4` |
| Full verify | `./scripts/open-simulator.sh verify` |
| メモリ削減 | `brew install mobai-app/tap/simslim` — helper が boot 後に自動適用 (`--except web`); `SIMSLIM=0` で無効化 |

Details: `references/quickstart.md`, `references/devices.md`.

## Flow

1. **Have env**: `./dev.sh list` or `./dev.sh up demo --fixture` (synthetic tree, no real data). If code changed, run `dev-lifecycle` first.
   **No tailscale on host** (`tailscale status` fails, no `.env`, `dev.sh up` dies with 「tailnet ドメインを導出できません」) → use `./dev.sh up demo --local --fixture` instead. It publishes `http://127.0.0.1:8080` with no sidecar and needs no `TS_AUTHKEY`/`BASE_URL`. A second concurrent local env needs `--port N`. Details: `references/overview.md` (Local mode).
2. **Preflight**: `./scripts/open-simulator.sh check` — see `references/troubleshooting.md` on failure.
3. **Open**: `./scripts/open-simulator.sh $(./dev.sh url demo)` (tailnet) or `./scripts/open-simulator.sh http://127.0.0.1:8080` (local) — helper polls `bootstatus -b` max 30s, warns on multiple `Booted`. Simulator shares the host network, so loopback and tailnet both reach it directly; the helper accepts `https://…` and loopback `http://` only (a bare `http://<lan-ip>` is not a Secure Context and is rejected). See `references/overview.md`.
   First paint may show **「0 folders」** — the first `/api/folders` scan is slow. Reload (`open_url` again) before suspecting the mount; confirm with `docker inspect <viewer> --format '{{json .Mounts}}'` only if it persists.
4. **Drive feature**: Test offline / storage bar / spread / PWA in real Safari. Do not rely on `curl`.
   タップ・ゆるドリフト・スワイプまでエージェントが叩くなら `./scripts/sim-gesture-battery.sh <url>`
   (idb 必須, 方法論と罠は `references/tap-driving.md` — 座標はポイント, `?perf=1` は `#` の前, キャッシュ退避に shutdown→boot)。
5. **Capture**: `./scripts/open-simulator.sh screenshot .omo/evidence/sim.png` (or `recordVideo`).
6. **Cleanup (optional)**: `./scripts/open-simulator.sh shutdown` — avoids next `openurl booted` ambiguity.

## Constraints

- 7-day LRU not reproducible in Simulator — verify `touch` logic only; real wait needs iPad hardware. Source: `README.md: Safari 7日注意`.
- `video-verification` is Chromium-only; Simulator uses `recordVideo`.
- Booted sim は ~160 の背景デーモンで数GBを常駐させる。`simslim` 導入済みなら `open-simulator.sh` が open 時に自動 slim (永続 override、実測 6.6GB→2.7GB)。`web` カテゴリは Safari 検証のため残す。検証後は `shutdown` で解放。

## Related

- Skill detail: `references/overview.md`, `references/quickstart.md`, `references/devices.md`, `references/troubleshooting.md`, `references/tap-driving.md`, `references/tier-strategy.md`
- Human guide (thin stub): `docs/ios-simulator.md` → this skill
- Tier strategy: `references/tier-strategy.md` (and `tests/README.md` stub)
- Deployment: `docs/deployment.md` (why `dev-*` nodes need no port)
