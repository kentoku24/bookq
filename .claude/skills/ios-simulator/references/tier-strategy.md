# Tier Strategy — WebKit vs Simulator vs Appium

棲み分け: **一般的なブラウザ互換は CI で自動、Safari/iOS 固有はローカル Simulator で解消**。

| Tier | 対象 | 実行環境 | コマンド | 用途 |
|---|---|---|---|---|
| Tier1 | Playwright WebKit エミュレーション | ubuntu CI 自動 | `BROWSER=webkit uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/ -q -k ios`（`tests/test_ios.py` は companion branch `ai/ios-browse-swipe-flicker` で提供、未配置時は `-k ios` で代替） | **一般的なブラウザ互換** は CI で自動（PR毎） |
| Tier2 | Simulator 本物 Safari (`simctl openurl`) | macOS ローカル手動 | `bash scripts/open-simulator.sh https://dev-demo.<your-tailnet>.ts.net` + `xcrun simctl io booted screenshot` | **Safari/iOS 固有** はローカル mac で解消 — SW / 容量バー 0/0 / 回転・spread / PWA offline（7日 touch は存在確認のみ） |
| Tier3 | Appium+WebDriverAgent | 将来 | `appium` + `xcuitest` driver + 署名 | 完全自動化が必要な時のみ（手順のみ） |

GHA では `ubuntu-latest` は従来どおり `pytest tests/ -q` のみ自動実行。Simulator を使う `xcrun simctl` は将来 `macos-14` の `workflow_dispatch` 手動予定（現状未配線 — ローカル mac で `scripts/open-simulator.sh verify` を実行）。

<details><summary>Tier3 Appium 準備手順（将来 — 実装しない）</summary>

> **実装はしない** — 完全自動化が必要な時のみ以下で準備する。現時点ではコード・依存を追加しない。

```bash
brew install appium
appium driver install xcuitest
# WebDriverAgent 署名（Apple Developer Program 加入が必要）
# Xcodeで WebDriverAgent.xcodeproj を開き Team を設定してビルド
appium --allow-cors
```

capabilities 例:

```json
{
  "platformName": "iOS",
  "deviceName": "iPad Pro 13-inch (M4)",
  "browserName": "Safari",
  "automationName": "XCUITest"
}
```

クラウドは使用しない。ローカル mac の Simulator で完結する。

</details>

## opencode からの呼び出し例

```bash
# 合成ツリーで dev 環境を立てる
./dev.sh up demo --fixture
# Simulator で開く（dev.sh url 出力を渡す）
bash scripts/open-simulator.sh $(./dev.sh url demo)
# スクショ（証跡は .omo/evidence/ 配下）
xcrun simctl io booted screenshot .omo/evidence/sim.png

# 自動テスト
uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/ -q
BROWSER=webkit uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/ -q -k ios
```

`verify` は上記を `example.com` で到達性のみ検証する一連として集約:

```bash
bash scripts/open-simulator.sh verify  # check → boot "iPhone 17" → open https://example.com → screenshot .omo/evidence/sim-verify.png
test -x scripts/open-simulator.sh && bash scripts/open-simulator.sh verify 2>&1 | grep -q "Simulator ready\|skip" && echo ok
```

軽量タップ（`idb` 経由、Appium不要）:

```bash
bash scripts/open-simulator.sh tap 200 400 # idb ui tap
bash scripts/open-simulator.sh tap 200 400 --udid <UDID>
```
