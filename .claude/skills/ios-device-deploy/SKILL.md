---
name: ios-device-deploy
description: Use when installing or updating the native Bookq iOS app on a paired physical device (iPhone/iPad) — builds via xcodebuild and installs/launches via devicectl. Trigger on "実機にインストール", "実機デプロイ", "実機アプリを更新", "deploy to device", "install on my iPhone", "ios-device-deploy". Not for iOS Simulator (that's `open-simulator.sh` / the ios-simulator skill) or Android devices.
---

# iOS Device Deploy

Build `ios/Bookq` for a paired physical device and install + launch it. One script does the whole pipeline — do not hand-run the xcodebuild/devicectl steps.

## Quick Reference

| Task | Command |
|---|---|
| Deploy (Debug, install+launch) | `./scripts/ios-device-deploy.sh` |
| Release build | `./scripts/ios-device-deploy.sh --release` |
| Install only (no launch) | `./scripts/ios-device-deploy.sh --no-launch` |
| Pick a specific device | `./scripts/ios-device-deploy.sh --udid <udid>` or `DEVICE_UDID=<udid>` |
| List candidates | `xcrun devicectl list devices` |

## Flow

1. **Device up**: iPhone paired (USB or same network), unlocked once, Developer Mode on (`設定 → プライバシーとセキュリティ → デベロッパモード`). Script auto-picks the single physical + paired + booted device; errors with a list if ambiguous.
2. **Run**: `./scripts/ios-device-deploy.sh` from the repo root (any worktree). Builds `Bookq` scheme for `id=<udid>` with `-allowProvisioningUpdates`, resolves the `.app` via `-showBuildSettings`, then `devicectl install` + `process launch`.
3. **Verify**: launch line prints `Launched application with dev.bookq.reader`. For UI verification use the on-device UI directly or `xcrun devicectl` — there is no screenshot/idb path for physical devices.

## Constraints

- **Signing**: `DEVELOPMENT_TEAM = H3643W6HXA` is set in `project.pbxproj` (#343) — no override flag needed. If signing fails, check `security find-identity -v -p codesigning` for a valid `Apple Development` cert.
- **Personal Team**: provisioning expires in **7 days** — re-deploy to renew. First install ever requires 開発元の信頼 (`設定 → 一般 → VPNとデバイス管理`).
- **Not a git sync**: the script builds the *current worktree's* code — deploy after `git pull` (or from the feature worktree) depending on what should land.
- Simulator deploy is a different path (`open-simulator.sh` opens the PWA in Safari; `xcodebuild ... -destination 'platform=iOS Simulator'` for the native app in sim).
