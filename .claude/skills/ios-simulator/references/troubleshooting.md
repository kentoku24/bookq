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
| コードを直したのに Simulator の挙動が古い | Safari が旧 `static/app.js` をキャッシュ (`max-age` はなしでも重複ヒットする) | `xcrun simctl shutdown` → `boot` で fresh セッション、または URL の search 部 (`#` の前) に `&r=<n>` を付けて開き直す(`sim-gesture-battery.sh` は自動付与) |
| `:8094` など local dev 環境に届かない (curl `000`) | re-up 時に `--local --port` を付け忘れ tailnet モードで再作成され、ポートが publish されていない | `./dev.sh list` で URL が `http://127.0.0.1:<port>` か確認。`https://dev-*` なら `./dev.sh up <name> --local --port <n>` で作り直す |
| `idb ui tap` が全滅・tap/swipe コマンドが無い | idb 未導入 | `brew tap facebook/fb && brew install idb-companion && pipx install fb-idb`; `./scripts/open-simulator.sh check` |
| Simulator GUI (Simulator.app) が存在しない | 環境依存で GUI バンドルが無い | 不要 — `simctl`/`idb` はヘッドレスで完結 (`openurl`・`screenshot`・`recordVideo`・`idb ui tap`) |
| HUD 計測が表示されない | URL に `?perf=1` が無い / hash の後ろに付けている | `.../?perf=1#/view/...` の形 (`#` の前)。HUD は `app.js` が `location.search` を読む |
| Activity Monitor で SimulatorTrampoline / DeviceHub / sim のメモリが大きい | booted sim が ~160 デーモン (GB級) を常駐。SimulatorTrampoline 本体は ~7MB で、sim プロセスのランチャーなので無効化不可 | `brew install mobai-app/tap/simslim` — helper が open 時に自動 slim (`--except web`、永続化)。使い終わったら `xcrun simctl shutdown all`。DeviceHub は quit でよい (オンデマンド再起動) |

`video-verification` スキルは Chromium 前提。Simulator の動画は `xcrun simctl io booted recordVideo /tmp/sim.mp4` を使う。

Tailnet ドメイン例は各自の `DEV_TS_DOMAIN` に読み替え（正準は `docs/deployment.md`）。
