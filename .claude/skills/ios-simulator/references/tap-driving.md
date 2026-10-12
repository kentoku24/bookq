# Tap-driving reference — iOS Simulator を実際の指で動かす

本シミュレータでのビューア挙動検証 (タップ・ゆるいタップ・スワイプ) をエージェントが自力で
叩くための手引き。`quickstart.md` の「Simulator を開く」まで済んでいる前提。

## 1. 前提

- **idb** が必要。`./scripts/open-simulator.sh check` で有無を確認。
  無ければ: `brew tap facebook/fb && brew install idb-companion && pipx install fb-idb`
- Simulator GUI (Simulator.app) は**不要**。`simctl` / `idb` はヘッドレスで完結する
  (本リポジトリの検証は GUI 無しで動く。Simulator.app が見つからない環境でも作業可能)。
- 座標は**ポイント** (iPhone 17 なら 393×852)。Retina ピクセル (1179×2556) ではない。
  スクショはピクセル座標 (÷3 = ポイント) で読むこと。

## 2. 基本コマンド

```bash
UD=6726F26B-...  # xcrun simctl list devices | grep Booted から

# タップ (press は短い)。人間の指に寄せるなら --duration を付ける
idb ui tap 78 500 --duration 0.15 --udid $UD

# ゆるいドリフト付きタップ (指が数pxずれる典型)
idb ui swipe 78 500 90 502 --duration 0.15 --udid $UD

# しっかりスワイプ (コミット)
idb ui swipe 78 500 138 502 --duration 0.12 --udid $UD

# ダブルタップ
idb ui tap 78 500 --udid $UD; sleep 0.15; idb ui tap 78 500 --udid $UD
```

`duration` は「タッチダウンからアップまで」の時間。既定 (省略) は極端に短くて
デバイス寄りなので、人間が「タップした」という手応えは ~0.15s で模倣するのが良い。

## 3. 検証ループの型

```bash
xcrun simctl openurl booted "http://127.0.0.1:8094/?perf=1&r=$RANDOM#/view/...?p=0"
sleep 6                                  # 検証前に描画待ち
idb ui tap 78 500 --udid $UD             # 静止タップ
sleep 1.2
idb ui swipe 78 500 90 502 --duration 0.15 --udid $UD   # 12px ドリフト
sleep 1.2
./scripts/open-simulator.sh screenshot /tmp/sim.png      # counter と HUD を可視化
```

判断は**スクショ読み** (`look_at` や Read)。見る点:

- `counter` 値 (1回のジェスチャで +1 なら 1ターン、+2 なら二重めくり、0 なら無反応)
- グリーンの perf HUD (`?perf=1` 必須)。行の意味:

```
next(L) #12 move1081 snap@82 win0 lay0
       │       │        │
       │       │        └ tapAt(touchstart) → ターン確定までの ms
       │       └ 変換 (transform) が動いたと検知までの ms (アニメ継続中の伸びは無視)
       └ 移動先ページ番号 (スプレッドの先頭ページ挿入値)
img:ok  = 移動先のページ画像はタップ時点で手元にある (MISSING なら未取得)
```

- snap 値の目安: 静止タップ ~6-20ms、ドリフトは指を含むためキー時間下では読めない
  (実測は "確定が指を離した直後" が正)。

## 4. よく踏む罠

| 罠 | 対処 |
|---|---|
| `?perf=1` は**ハッシュの前**の search 部に置く | `.../?perf=1#/view/...` (hash 直後だと効かない) |
| コードを直したのに挙動が浅い | Simulator の Safari が古い `static/app.js` をキャッシュしている。**shutdown→boot で fresh セッション**にするか URL に `&r=` を付けて開き直す |
| re-up で local モードが外れてポートが消える | `./dev.sh up <name>` はモードを引き継がない。**必ず `--local --port N` 付きで呼ぶ**。`./dev.sh list` の URL が `http://127.0.0.1:port` なら OK、`https://dev-*` なら loopback から届かない |
| HUD が消えて counter だけ伸びる | HUD は `?perf=1` で起きたときしか作られない。URL に付けて reload |
| JS デバッグを counter に入れる | `snapTo` が counter を素直に書き換えるので消える。デバッグは `__perfLine` (のみ HUD) に積む |
| idb が tap できない | `check` → idb が足りないと tap/swipe 全滅 |
| `video-verification` スキルは Chromium 専用 | Simulator の動きは `xcrun simctl io booted recordVideo` |

## 5. 便利な組み込みヘルパー

```bash
./scripts/open-simulator.sh screenshot [path]   # スクショ (<デバイス> 全体)
./scripts/open-simulator.sh check               # Xcode / simctl / Runtime 前提チェック
./scripts/open-simulator.sh tap 78 500 [--udid X]    # idb tap (省略時 booted 自動取得)
```

組み込みを用いたジェスチャバッテリー (`scripts/sim-gesture-battery.sh`) は以下を
一発で実行する: 静止タップ→12px ドリフト→22px fast→22px slow→40px slow スワイプ。
詳細は `./scripts/sim-gesture-battery.sh --help`。

## 6. ジェスチャ分類とビューア側の受取

ビューアの左タップは 3 経路 (`app/static/app.js`):

1. `touchend` 即時タップ (drag 判定に達しない) → 押した位置の zone で即ターン
2. **24px 未満のドリ付きドラグ** → 同じく即タップ経路 (ゆるタップ例外)
3. 24px 以上 → 速度/距離判定でコミット (スライドアニメ ~250ms)

فاع的には「ユーザの慣性は (2) を狙っている」ので、ドリフトありで正常に動くなら
「指が開けば正常に動く」状態。UIは `?perf=1` の snap 値でこまめに確認できる。
