---
name: ios-device-deploy
description: Use when installing or updating the native Bookq iOS app on a paired physical device (iPhone/iPad) — builds via xcodebuild and installs/launches via devicectl. Trigger on "実機にインストール", "実機デプロイ", "実機アプリを更新", "deploy to device", "install on my iPhone", "ios-device-deploy". Not for iOS Simulator (that's `open-simulator.sh` / the ios-simulator skill) or Android devices.
---

# iOS Device Deploy

Build `ios/Bookq` for a paired physical device and install + launch it. One script does the whole pipeline — do not hand-run the xcodebuild/devicectl steps.

## Quick Reference

| Task | Command |
|---|---|
| Deploy dev app (Debug, install+launch) | `./scripts/ios-device-deploy.sh` |
| Deploy prd app (**main/origin-main only**) | `./scripts/ios-device-deploy.sh --prd` |
| Release build | `./scripts/ios-device-deploy.sh --release` |
| Install only (no launch) | `./scripts/ios-device-deploy.sh --no-launch` |
| Pick a specific device | `./scripts/ios-device-deploy.sh --udid <udid>` or `DEVICE_UDID=<udid>` |
| List candidates | `xcrun devicectl list devices` |

## Variant / branch rule (basic rule)

| App | Flag | Bundle ID | Deploy source |
|---|---|---|---|
| **prd** `Bookq.app` | `--prd` | `dev.bookq.reader` | **`main` = `origin/main` のみ** |
| dev `BookqDev.app` (default) | (none) / `--dev` | `dev.bookq.reader.dev` | 任意ブランチ・worktree |

- prd deploy: main checkout で `git fetch origin && git merge --ff-only origin/main` してから `./scripts/ios-device-deploy.sh --prd`。スクリプトが `HEAD == origin/main` と tracked 差分なしを強制する (緊急回避: `BOOKQ_PRD_SKIP_MAIN_CHECK=1`)。
- dev deploy: 作業中の worktree/ブランチからそのまま `./scripts/ios-device-deploy.sh`。
- 両アプリは bundle ID が別なので同一端末に共存可 (ストレージ/Keychain も分離)。

## Flow

1. **Device up**: iPhone paired (USB or same network), unlocked once, Developer Mode on (`設定 → プライバシーとセキュリティ → デベロッパモード`). Script auto-picks the single physical + paired + booted device; errors with a list if ambiguous.
2. **Run**: `./scripts/ios-device-deploy.sh` from the repo root (any worktree). Builds `Bookq` scheme for `id=<udid>` with `-allowProvisioningUpdates`, resolves the `.app` via `-showBuildSettings`, then `devicectl install` + `process launch`.
3. **Verify**: launch line prints `Launched application with dev.bookq.reader`. For UI verification use the on-device UI directly or `xcrun devicectl` — there is no screenshot/idb path for physical devices.

## Constraints

- **Signing**: `DEVELOPMENT_TEAM = H3643W6HXA` is set in `project.pbxproj` (#343) — no override flag needed. If signing fails, check `security find-identity -v -p codesigning` for a valid `Apple Development` cert.
- **Xcode account required**: Personal Team の自動プロビジョニングには Xcode `Settings → Accounts` への Apple ID サインインが必須。未サインインだと `No Accounts: Add a new account in Accounts settings` / `No profiles for '<bundle>' were found` で build 失敗 → ユーザーに Xcode でのサインインを依頼する (エージェントは資格情報を入力できない)。
- **Personal Team**: provisioning expires in **7 days** — re-deploy to renew. First install ever requires 開発元の信頼 (`設定 → 一般 → VPNとデバイス管理`).
- **Developer trust on device**: install 後の launch が `FBSOpenApplicationErrorDomain error 3` (profile has not been explicitly trusted) で失敗 → 端末で `設定 → 一般 → VPNとデバイス管理` から開発元を信頼 → install 済みなので launch のみ再試行 (`xcrun devicectl device process launch --device <udid> <bundle id>`)。
- **Locked device**: launch が `error 10002` / `Locked` で失敗 → 端末のロックを解除してホーム画面を出したまま launch を再試行。
- **Not a git sync**: the script builds the *current worktree's* code — which branch lands is controlled by the variant rule above, not by the script.
- Simulator deploy is a different path (`open-simulator.sh` opens the PWA in Safari; `xcodebuild ... -destination 'platform=iOS Simulator'` for the native app in sim).
