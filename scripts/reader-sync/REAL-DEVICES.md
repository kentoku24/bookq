# REAL-DEVICES.md — S12 (#87) 実機受入ランbook

親 Issue: #75 / 本 Issue: #87 (S12)。**人間が物理 iPhone 2 台で実行する
最終受入手順**と、機械判定可能な記録フォーマット。runner は
`--driver real` で物理端末を「人間が操作するクライアント」として扱い、
制御チャネル経由の全自動 op (pair/turn/latency/resume/…) をそのまま
実行する。物理アクション (PWA install・回転・ロック・実指タップ) だけが
手順書の手動ステップになる。

成果物はすべて `result.json` + 本手順の表で機械的に採れる。

---

## 0. 前提 (prerequisites)

| 物 | 条件 |
|----|------|
| iPhone ×2 | Tailscale app で tailnet 参加、**または** Mac と同じ Wi-Fi。どちらか一方でよい (後述) |
| Mac | この repo の worktree。`docker` / `uv` / `xcrun` 不要 (real driver は simctl/idb/Playwright を使わない) |
| dev 環境 | 下記のどちらかで起動。**works は `--fixture` の合成ツリー推奨** — page 名が証跡に出ても安全 (実データでも可: folder 名は redact されるが page 名は証跡に出る) |

### dev 環境の立て方 — 2 経路

```bash
# (a) tailnet 経路 — iPhone 側も Tailscale を ON にする
#     bookq が https://dev-XX.<tailnet>.ts.net で届き、fixture 制御ページは
#     Mac の tailscale IP で配信される。
./dev.sh up s12-real --ref main --fixture

# (b) LAN 経路 — iPhone は Mac と同じ Wi-Fi のみでよい (Tailscale 不要)
#     viewer が http://0.0.0.0:8080 で listen し、verify.py が --dev-env の
#     0.0.0.0 を Mac の LAN IP に書き換える。
./dev.sh up s12-real --local --lan-allow --ref main --fixture
```

どちらを使うか: **(a) は「実際の利用経路」をそのまま検証する本番相当**、
**(b) は tailnet を張れない状況や切り分け用**。spec の「ネットワーク条件」に
どちらで測ったかを必ず書くこと。`./dev.sh url s12-real` で実 URL 確認。

---

## 1. 実行コマンド (exact commands)

```bash
cd <repo-worktree>

# runner (real driver): 端末への自動化なし、websockets は bystander 観測用
uv run --with websockets \
    python scripts/reader-sync/verify.py \
    --driver real --dev-env s12-real \
    --scenarios scripts/reader-sync/scenarios-real.json \
    --repeat 3
```

起動すると runner が **両端末分の URL を大きく表示する**。`--dev-env` は
`./dev.sh url` で解決される;直に `--url http(s)://<host>:<port>` を渡してもよい。

```
[real] ======================================================================
[real] Open these URLs in Safari on each PHYSICAL device
[real] (share → Add to Home Screen instead for the PWA pass):
[real]    LEADER: http://192.168.1.5:49152/fixture?id=leader&role=leader&...
[real]  FOLLOWER: http://192.168.1.5:49152/fixture?id=follower&role=follower&...
[real]    LEADER (short): http://192.168.1.5:49152/r/leader
[real]  FOLLOWER (short): http://192.168.1.5:49152/r/follower
[real] The sync WebSocket still targets the --url env —
[real] only this fixture/control page is LAN-served.
```

人間は **短い方** (`/r/leader`, `/r/follower`) を各 iPhone の Safari で
手打ちするだけでよい (302 で完全 URL に飛ぶ)。各端末が開き次第 hello が
runner に届き scenario が自動開始される。以降の pair→open_work→遅延
100 ops×2 方向×3 run→disconnect→resume→reload→leave は **runner が
全部駆動する** — 人間は画面を見るだけでよい。

### headless スモーク (人間なしで同ファイルが通るか CI 的に)

```bash
uv run --with playwright --with websockets --with pillow \
    python scripts/reader-sync/verify.py \
    --driver chrome --dev-env <name> \
    --scenarios scripts/reader-sync/scenarios-real.json --repeat 1
```

## 2. scenario 表 (spec 13 項目)

「AUTO = runner が scenario 内 op で検証 / MANUAL = 人間の端末操作」。
結果は証跡 dir `.omo/evidence/reader-sync/<run-id>/` の `result.json`
(per-scenario status + assertions + latency_ms) と trace-*.jsonl が
machine-readable。画面証跡は各端末で **電源+音量上 同時押し**の screenshot を
撮り AirDrop で Mac に集める。状態正しさは `result.json` の
converged/marker/img assertions + trace と両端末 screenshot の照合で
判定し、「見えた」報告だけを根拠にしない。

| # | spec scenario | 実施方法 | PASS 判定 | 証跡 | PASS/FAIL/BLOCKED |
|---|----------------|----------|-----------|------|-------------------|
| 1 | Safari 接続 | AUTO — 両端末 Safari で fixture URL を開き `real_ws_acceptance` が pair→leave 完走 | scenario status=pass (presence=2, converged, END both) | result.json + trace | |
| 2 | PWA 接続 | MANUAL — 両端末で fixture ページを share→ホーム画面に追加→webclip 起動してから `--scenario real_ws_acceptance` を再実行 (hello `standalone=true` が証跡に出る) | scenario pass + devices.follower/leader.standalone=true | result.json devices + scenario | |
| 3 | Safari×PWA 混在 | MANUAL — 片側のみ webclip、もう片側 Safari で 2 と同じ再実行 | scenario pass + standalone が片側のみ true | result.json devices | |
| 4 | 端末間直接 (P2P) 経路での接続 | AUTO — `real_peer_latency_dc` (transports: webrtc-dc)。確立されなければ scenario が env_unavailable | dc_open_both + plane_switched assertions pass; 不成立時は理由を BLOCKED 明記 | result.json assertions | |
| 5 | 左右配置と交換 | AUTO (swap_sides ×2) + 人間: 実際の左右端末が画面 side と一致するか目視 | sides_swapped pass + 目視一致 | result.json + 両端末 screenshot | |
| 6 | コード接続 | MANUAL — leader の fixture で 開始 tap → 表示 6 桁を follower の code 欄に手入力 → 参加 → leader で 承認 tap (fixture 画面の実ボタン) | presence=2 になり双方同一 spread 表示 | 両端末 screenshot + (runner 側は `--scenario real_ws_acceptance` で pair を手動に置き換えるか、UI 操作後に `expect` op で確認) | |
| 7a | ページ送り leader→follower | AUTO — `real_ws_acceptance` の turn ops + latency leader→follower | converged + marker/img assertions pass | result.json | |
| 7b | ページ送り follower→leader | AUTO — turn actor=follower + latency follower→leader | 同上 | result.json | |
| 8 | 章移動 | AUTO — `goto`/`open_work` の chapter 横断。discover が単一 chapter の work を選んだ場合は MANUAL: 実 UI の複数 chapter 作品で leader が章跨ぎ turn → follower 追従を目視 | converged assertions pass | result.json + screenshot | |
| 9 | leader 作品変更 | AUTO — `real_work_change` (open_work で index:1 の別 work に切替 → 両端末追従)。対象 env に ≥2 works 必要 — 無ければ env_unavailable 記録でよい | scenario pass (新 manifest の converged+initial_page) | result.json | |
| 10 | 回転 | MANUAL — 読書中に片方の端末だけ回転 → spread/side/marker 不変・見開き破綻なしを目視。runner は直前直後で `expect` 状態を読める (別 run で `--scenario real_ws_acceptance` 実行中に回転しても op が継続する様子でも可) | 回転後も同一 committed spread + レイアウト崩れなし | 両端末 screenshot | |
| 11 | 画面ロックからの復帰 | MANUAL — 片端末を電源ボタンで 30 秒ロック → 解除 → ページが自動 resume し committed spread 復帰 (fixture は reload 時に credential RESUME を行う) | unlock 後に session 復帰 (fixture の rev/spread 表示が peer と一致) | 両端末 screenshot + trace | |
| 12 | 明示解除 | AUTO — `leave` op (END reason 'left' が両端末に届く)。人間は fixture の 解除 ボタンでも同経路を確認可 | ended_both pass | result.json | |
| 13 | 遅延計測 (spec 遅延項目) | AUTO — `real_peer_latency_ws` + `real_peer_latency_dc`、--repeat 3 で両方向各 100 ops×3 run | §3 の表に集計 + p95 ≤300ms 暫定目標の合否 | result.json latency_ms | |

> scenario が「課題付き FAIL/BLOCKED」の場合は必ず理由を右列に書く。
> 実機が使えない場合は **Issue を open のまま BLOCKED 記録** — Simulator
> 結果で最終受入済みにしない。

## 3. 遅延結果表 (latency tables)

`result.json` の各 scenario の `latency_ms` から転記。キーは
`peer:<transport>:<actor>→<peer>`。run ごとに 1 行。

### server-ws

| run | direction | n | p50 ms | p95 ms | max ms | failures | PASS? (p95≤300) |
|-----|-----------|---|--------|--------|--------|----------|------------------|
| 1 | leader→follower | 100 | | | | | |
| 1 | follower→leader | 100 | | | | | |
| 2 | leader→follower | 100 | | | | | |
| 2 | follower→leader | 100 | | | | | |
| 3 | leader→follower | 100 | | | | | |
| 3 | follower→leader | 100 | | | | | |

### webrtc-dc (確立できた場合)

| run | direction | n | p50 ms | p95 ms | max ms | failures | PASS? (p95≤300) |
|-----|-----------|---|--------|--------|--------|----------|------------------|
| 1 | leader→follower | 100 | | | | | |
| 1 | follower→leader | 100 | | | | | |
| 2 | leader→follower | 100 | | | | | |
| 2 | follower→leader | 100 | | | | | |
| 3 | leader→follower | 100 | | | | | |
| 3 | follower→leader | 100 | | | | | |

### 環境メタデータ (必須記入)

| field | value |
|-------|-------|
| network 条件 | tailnet / 同一 Wi-Fi (SSID 種別・帯域・おおよその RSSI) |
| leader 端末 | model / iOS / Safari or PWA |
| follower 端末 | model / iOS / Safari or PWA |
| target build_sha | (result.json `build_sha`) |
| run_id | (result.json `run_id`) |
| 証跡 dir | `.omo/evidence/reader-sync/<run-id>/` |

**計測メソッド記録 (そのまま転記可):**
> 計測は runner (操作元) の単一 monotonic clock で「intent 送信 → 相手側
> decode 済み表示反映イベント受信」までを測った **上界** であり、コマンド
> 配送 + 同期経路 + peer decode + event 返送を含む。cold 画像取得時間は
> 別分布として集計した (遅い sample を恣意的に削除していない — failures
> 列に timeouts/refusals を計上)。double-rAF を物理発光時刻とは呼ばず、
> 異なる端末の performance.now 同士の引き算は行っていない。
> (peer 側の `at` は performance.now だが比較には使わない。)

**cold 画像取得の別集計**: `peer_latency` は warm (ページキャッシュが効く
隣接 spread 往復)。cold は別途、未開封の大きめ folder を初回 open した
際の感覚値+trace timestamps を記録する (自動計測対象外)。

## 4. 人間評価フォーム (spec verbatim — 全項目必須、合否+自由記述)

| 項目 | 合否 (OK/NG) | 自由記述 |
|------|--------------|----------|
| (a) 実指での代表操作 (タップ/スワイプ) の誤操作有無 | | |
| (b) 2 台のベゼル/画面サイズ差による読みにくさ | | |
| (c) 見開きとしての読みやすさ | | |
| (d) 接続・左右交換手順の理解しやすさ | | |

**未記入項目が残る状態を完了にしない。** NG があれば残課題として書き、
UI 調整で解決するものは S08/S06 等の境界へ戻す旨明記する。

## 5. Issue コメント用テンプレート (#87 へ投稿)

```markdown
## S12 実機受入結果

- run_id: `<run-id>` / build_sha: `<sha>` / route: <tailnet|lan>
- 環境: leader=<model, iOS, Safari|PWA>, follower=<model, iOS, Safari|PWA>
- network 条件: <tailnet / Wi-Fi 帯域・RSSI>

### scenario 表

(§2 の表を結果込みで貼る — 全 13 行に PASS/FAIL/BLOCKED)

### 人間評価フォーム

(§4 の表をそのまま貼る)

### 遅延 (transport別、双方向 100 ops × 3 run)

(§3 の表を server-ws / webrtc-dc それぞれ貼る + メソッド注記)
- server-ws p95 合否: <OK/NG> (暫定目標 ≤300ms)
- webrtc-dc p95 合否: <OK/NG/BLOCKED-not-established>

### 採用推奨データプレーン

<transport 別実測に基づく推奨。P2P 不成立・劣後でも事実+理由を記録>

### 証跡

- `.omo/evidence/reader-sync/<run-id>/` (result.json, trace-*.jsonl)
- 端末 screenshot: <枚数・内容の対応>

### 残課題

- <目標未達/未実施/端末不足/UX 課題。後続 Issue 化するものは根拠付きで>
```

## 6. runner 内部メモ (human は読み飛ばしてよい)

- `--driver real` は device 自動化を一切持たない。`launch()` は URL を
  印字し、実際の gate は各端末の fixture `hello` (PWA の `standalone`
  フラグも hello が報告する)。`caps` は `ws_bystander` のみ + hello の
  `hasRTC` で `webrtc` が乗る — だから device_inputs/pwa 必須の
  scenario は正直に `env_unavailable` に落ちる。
- fixture は常に `0.0.0.0` バインド + `--url` 経路に応じた Mac の IP
  (LAN なら LAN IP、tailnet なら tailscale IP) を印刷する — `--url` が
  loopback の real 実行は exit 2 で早期失敗。
- `peer_latency` は runner の単一 monotonic clock で
  `cmd(turn)→peer 側 /ctrl/event 到着` を計測する **上界** (配送+同期+
  decode+返送込み)。fixture 側は `setImg` が terminal (ok/blank/error/
  faulted) に到達した瞬間に「現在の apply の revision」だけを POST
  する (superseded apply は emit しない staleness guard 付き)。
- 手動リロードも機械化済: real driver の `reload` は fixture の
  `reload_page` cmd (setTimeout 後の真 `location.reload()`) を叩き、
  runner は hello_seq+1 の待機で検証する — credential RESUME 経路を
  実機ブラウザで通る。
- 短縮 URL: `/r/leader` `/r/follower` が fixture 完全 URL に 302。
  `/r` は両リンクの index ページ。
