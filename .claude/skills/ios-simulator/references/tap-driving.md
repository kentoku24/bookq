# Tap-driving reference — iOS Simulator を実際の指で動かす

Simulator 上のビューア挙動検証 (タップ・ゆるいドリフト・スワイプ) をエージェントが自力で
叩くための手引き。`quickstart.md` の「Simulator を開く」まで済んでいる前提。

## 1. 前提

- **idb** が必要 (`./scripts/open-simulator.sh check` で確認)。無ければ
 `brew tap facebook/fb && brew install idb-companion && pipx install fb-idb`
- Simulator GUI (Simulator.app) は**不要**。`simctl` / `idb` はヘッドレスで完結する
 (`openurl`・`screenshot`・`recordVideo`・`idb ui tap` すべて GUI 無しで動く)。
- 座標は**ポイント** (iPhone 17 なら 393×852)。Retina ピクセル (1179×2556) ではない。
 スクショはピクセル座標 — コードで読むときは ÷3 でポイントに戻す。

## 2. 基本コマンド

```bash
UD=6726F26B-... # xcrun simctl list devices | grep Booted から

# タップ (--duration で押圧時間を延ばすと人間の押しに近い)
idb ui tap 78 500 --duration 0.15 --udid $UD

# ゆるいドリフト付きタップ (指が数 px ずれる典型)
idb ui swipe 78 500 90 502 --duration 0.15 --udid $UD

# しっかりスワイプ (コミット経路)
idb ui swipe 78 500 138 502 --duration 0.12 --udid $UD
```

`--duration` は touch-start から touch-end までの時間。省略時は極端に短く、
ビューア側から見た「人間がタップした」には ~0.15s が近い。

**ダブルタップは作れない**: `idb ui tap` を 2 回呼ぶ「連打」は呼び出しごとにプロセス起動
コストが掛かり、 ja 完全なシングルタップ 2 回としてビューア側に届く。`dblclick` や
ダブルタップズーム抑止の検証には使えない (期待値は「ターン数 +2」でしかない)。

## 3. 検証ループの型

```bash
xcrun simctl openurl booted "http://127.0.0.1:8094/?perf=1#/view/...?p=0"
sleep 6
idb ui tap 78 500 --udid $UD # 静止タップ
sleep 1.2
idb ui swipe 78 500 90 502 --duration 0.15 --udid $UD # 12px ドリフト
sleep 1.2
./scripts/open-simulator.sh screenshot /tmp/sim.png # counter と HUD を読む
```

スクショを見る点 (`look_at` や Read):

- `counter` 値 — 1 回のジェスチャで **+1 なら正常** (満たさない場合 +2 = 二重めくり / 0 = 無反応)
- グリーンの perf HUD (`?perf=1` 必須)。行の意味:

```
next(L) #12 move1081 snap@82 win0 lay0
 │ │ │
 │ │ └ tapAt(touchstart) → ターン確定までの ms
 │ └ transform が動いたと検知するまでの ms (アニメ継続中は溜まる)
 └ 移動先ページ番号 (spread 先頭ページの +1)
img:ok = 移動先ページはタップ時点で手元にある / MISSING なら未取得
```

注意: `#N` は **移動先ページ** の番号なので行ごとの増分が必ずしも +1 にならない
(最新のターンは強制カットになり得るため)。2 連続同じ `#N` なら無反応 / バウンス、1 つ飛び (`#5 → #7`)
なら二重めくりとして判別する。

## 4. ジェスチャ分類とビューア側の受け取り

左タップは `app.js` touchend で 3 経路に分かれる (#19 マージ済み main の挙動):

1. **静止タップ** (drag 判定に達しない) — 落地位置の zone で即時ターン
2. **20px 以下 (`TAP_MAX_TRAVEL`) の横ドリフト** — 「実質タップ」として**落地位置**の zone で即時ターン
3. **20px 超** — 速度 / 距離判定でコミット (スライドアニメ ~250ms)

`?perf=1` の snap 値はビルドに応じた期待値との照合に使う (main は #19 済み)。

## 5. よく踏む事故集

| 罠 | 対処 |
|---|---|
| `?perf=1` でなく HUD が出ない。`#` の**後**に付けても効かない | `.../?perf=1#/view/...` の形 |
| コード修正後も挙動が旧い | Safari が `static/app.js` を HTTP キャッシュで再利用。**`shutdown` → `boot`** (fresh セッション) するか `search` 部に `&r=<n>` を付けて開き直す |
| re-up 後 `:port` に届かない (curl 000) | `./dev.sh up` はモードを引継ぎしない。`--local --port N` を必ず付ける。`./dev.sh list` の URL が `https://dev-*` なら tailnet モード = loopback 経路なし |
| HUD が永続化されない・`counter` に入れた JS Debug が消える | `snapTo` が counter を都度書き直すため。Debug は `window.__perfLine` (?perf=1 起動必須) に積む |
| `idb` が tap 不可 | `check` → 未導入なら `brew tap facebook/fb && brew install idb-companion && pipx install fb-idb` |
| 動画 | Chromium 前提の `video-verification` スキルではなし、Simulator は `xcrun simctl io booted recordVideo /tmp/sim.mp4` |

## 6. 組み込みヘルパー

```bash
./scripts/open-simulator.sh screenshot [path]         # スクショ
./scripts/open-simulator.sh check                    # Xcode / idb / Runtime 前提チェック
./scripts/open-simulator.sh tap 78 500 [--udid X]    # idb tap (udid 省略時 booted)
./scripts/open-simulator.sh swipe 78 500 90 502 --duration 0.15
./scripts/sim-gesture-battery.sh "$URL" [--screenshot PATH] [--udid X] # 7 連バッテリー
```

バッテリー (`sim-gesture-battery.sh`) の期待値は **counter は 1 + 7** (#19 マージ済み main 前提)。
