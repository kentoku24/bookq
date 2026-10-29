# iOS Simulator での確認

> **Source of truth: `.claude/skills/ios-simulator/SKILL.md`**
> 本ファイルは人間向けの薄い入口。エージェントはスキル経由で実行すること。詳細は `references/` を参照。

## 前提

- bookq は PWA。`simctl install` は不要。Safari で `xcrun simctl openurl booted <dev-url>` で開く。
- Simulator は host ネットワークを共有。host が Tailnet 参加済みなら `https://dev-*.ts.net` に到達できる。

## 起動から確認まで

```bash
# 1. シミュレータを起動
xcrun simctl boot "iPhone 13" 2>/dev/null; open -a Simulator
# 2. dev 環境を立てる（fixture 推奨）
./dev.sh up demo --fixture
# 3. Safari で開く
xcrun simctl openurl booted "https://dev-demo.<your-tailnet>.ts.net"
# 4. スクショ
xcrun simctl io booted screenshot /tmp/sim.png
```

Tailscale の無い Mac（`.env` 無し）では step 2-3 をローカルモードに置き換える。Simulator は host network を共有するので `127.0.0.1` に届く。

```bash
./dev.sh up demo --local --fixture              # http://127.0.0.1:8080（2つ目は --port N）
./scripts/open-simulator.sh http://127.0.0.1:8080
```

初回表示が「0 folders」なら初回スキャン待ちなので reload する。詳細は `references/overview.md: Local mode`。

ヘルパー: `./scripts/open-simulator.sh https://dev-demo.<your-tailnet>.ts.net`（`SIMULATOR_DEVICE` で変更可。`https://` と loopback `http://` のみ受け付ける）  
別デバイス例: `SIMULATOR_DEVICE="iPad Pro 13-inch (M4) (16GB)" ./scripts/open-simulator.sh <url>`  
次の検証端末: Android 15 1920x1200 Unisoc T620 2.2GHz / Mali-G57（実機）

詳細: `references/quickstart.md`, `references/devices.md`, `references/troubleshooting.md`, `references/tier-strategy.md`  
運用の棲み分け: `references/tier-strategy.md` / `tests/README.md` stub

## タップ駆動テスト (エージェント)

Simulator GUI 無しで `simctl`/`idb` が完結。エージェントが実タッチ級の操作を打てる:

```bash
./scripts/open-simulator.sh check                       # idb 有無
./scripts/sim-gesture-battery.sh "$URL"                 # タップ/ゆるドリフト/スワイプ 7連 → スクショ
./scripts/open-simulator.sh tap 78 500                  # 単発タップ (座標はポイント)
```

URL は `?perf=1#` の形 (`#` の前) — ビューア内蔵 HUD がタップ→ターン間の計測を見せる。詳細は `.claude/skills/ios-simulator/references/tap-driving.md`。

## 手動 QA（GitHub Actions では実行しない）

GHA の macOS ランナーは tailnet に参加していないため dev-XX に到達できない。手元で次を実行する。

    xcrun simctl boot "iPhone 15"
    BOOKQ_TEST_MODE=true BROWSER=webkit uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest -k "not offline" -q

No TS_AUTHKEY auto — GHA runners not tailnet-authenticated without TS_AUTHKEY, so macos must be manual.
