# Troubleshooting — Simulator

| Symptom | Cause | Fix |
|---|---|---|
| `xcrun not found` | Xcode 未導入 / 未選択 | `xcode-select --install`; `xcode-select -p` で `/Applications/Xcode.app/...` を確認 |
| `No iOS Runtime` | Runtime 未ダウンロード | Xcode > Settings > Platforms > iOS |
| `https://dev-*.ts.net` が解決しない | Host が Tailnet 未参加 / dev 環境未起動 | `tailscale status` → `open -a Tailscale`（`tailscale up` は対話でハングするので GUI で）；`./dev.sh list` → `./dev.sh up demo --fixture` |
| `dev.sh up` が「tailnet ドメインを導出できません」で止まる | Host に Tailscale アプリ / `.env` が無い（`/usr/local/bin/tailscale` が `Tailscale.app` 不在で失敗する等） | tailnet を諦めて `./dev.sh up demo --local --fixture` → `./scripts/open-simulator.sh http://127.0.0.1:8080`（`overview.md: Local mode`） |
| helper が `URL must be https://... or http://127.0.0.1[:port]` | `http://<lan-ip>` は Secure Context でないので拒否 | loopback `http://127.0.0.1:<port>` か tailnet `https://` を使う |
| 開いたページが「0 folders」 | 初回 `/api/folders` スキャン待ち | reload。続くなら `docker inspect <viewer> --format '{{json .Mounts}}'` で `/app/downloads` の bind を確認 |
| `openurl` が初回 boot で失敗 | コールドブートは 10-15秒 | ヘルパーは `bootstatus -b` で 30秒ポーリング済み；手動なら `xcrun simctl bootstatus "$DEVICE" -b` |
| `openurl booted` が別端末に開く | 複数 Booted | `xcrun simctl list \| grep Booted` → `xcrun simctl shutdown <UDID>` |
| `screenshot` が黒 | 描画未完了 | `openurl` 後 1-2秒待って再試行 |
| `check` が Linux で exit 0 | 正常 | Linux CI に `xcrun` は無い — 警告を出して exit 0 し CI を壊さない |
| 7日 LRU が再現しない | 仕様 | Simulator では 7日待機しても削除は発火しない。`touch` ロジックの存在確認のみ。実機 iPad が必要。詳細は `README.md: Safari 7日注意` |

`video-verification` スキルは Chromium 前提。Simulator の動画は `xcrun simctl io booted recordVideo /tmp/sim.mp4` を使う。

Tailnet ドメイン例は各自の `DEV_TS_DOMAIN` に読み替え（正準は `docs/deployment.md`）。
