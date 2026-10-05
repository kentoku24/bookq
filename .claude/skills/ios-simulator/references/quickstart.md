# Quickstart — 4-Step

```bash
# 1. シミュレータを起動
xcrun simctl boot "iPhone 17" 2>/dev/null; open -a Simulator
# 2. dev 環境を立てる（fixture 推奨：実データを汚さない）
./dev.sh up demo --fixture
# 3. Simulator の Safari で開く
xcrun simctl openurl booted "https://dev-demo.<your-tailnet>.ts.net"
# 4. スクショを取る
xcrun simctl io booted screenshot /tmp/sim.png
```

ヘルパー経由:

```bash
./scripts/open-simulator.sh https://dev-demo.<your-tailnet>.ts.net  # 既定 iPhone 17
./scripts/open-simulator.sh $(./dev.sh url demo)  # URL を dev.sh から取得
SIMULATOR_DEVICE="iPad Pro 13-inch (M4) (16GB)" ./scripts/open-simulator.sh https://dev-demo.<your-tailnet>.ts.net
```

tailscale の無い host（`.env` 無し / `tailscale status` 失敗）では step 2-3 を local mode に置き換える:

```bash
./dev.sh up demo --local --fixture              # http://127.0.0.1:8080 （2つ目は --port N）
./scripts/open-simulator.sh http://127.0.0.1:8080
```

helper は `https://…` と loopback の `http://127.0.0.1|localhost` だけ受け付ける。初回表示の「0 folders」は初回スキャン待ちなので reload する（詳細: `overview.md: Local mode`）。

`--fixture` は `tests/fixtures/uiux_tree.py` が生成する合成ツリー（9フォルダ + `metadata.toml` 3件: `simple_manga` / `series` / `mixed_ar`）を `/tmp/bookq-fixture-<name>/downloads` にマウントする。`README.md: 開発環境` と整合。

ピンチズーム・ページ送り・エピソード境界の目視 QA には `zoom_episodes/series/ep-1..3` を使う（単色ページは拡大しても差が見えない）。
720x1080 のページに 40px グリッド、巨大な `EP<n>` / `P<i>/<n>`、初頁 `START` / 末頁 `END` バッジ、`L`/`R` ラベル、拡大時のみ読める細字が入っている。

```bash
xcrun simctl openurl booted "http://127.0.0.1:8080/#/view/zoom_episodes/series/ep-1?p=4"   # ep-1 の最終頁
# 左 45% をタップ → EP2 START に切り替われば境界 OK。ピンチ後は細字 "EP1-P5/5 ..." が読めれば OK。
```

ピンチ後にズームが戻りきらないときは `?r=<n>` を変えた URL で開き直す（hash だけの変更ではページが再読込されず zoom が残る）。

ヘルパーは内部で `xcrun simctl bootstatus "$DEVICE" -b` ポーリング（最大30秒）で起動を待つため、初回 boot でも `sleep 2` 失敗を避けられる。

動画が必要な場合（Chromium の `video-verification` ではなく Simulator ネイティブ）:

```bash
xcrun simctl io booted recordVideo /tmp/sim.mp4  # 停止は Ctrl-C
```

`--fixture` で作られるツリーの詳細は `README.md` の「開発環境（実機・実データでの動作確認）」を参照。
