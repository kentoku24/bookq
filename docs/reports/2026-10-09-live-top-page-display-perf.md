# トップページ表示完了パフォーマンス実測レポート (2026-10-09, dev 環境, 実データ)

計測日 (UTC): 2026-10-09. 対象ブランチ: `ai/live-top-page-display-perf`. 本レポートは plan `.omo/plans/live-top-page-display-perf.md` の T8 成果物であり, 数値はすべて証跡ファイルの実測値のみを用いる. 推測値は含まない.

## 1. 目的と対象

トップページが最後のサムネイルまで表示し終わるまでの時間 (display-complete) を, 開発環境・実データ・認証スキップの条件で実測し, 性能ドキュメントの未記入欄 (T9) の材料とする. 対象は `GET /` のトップページ表示完了であり, 本番環境には一切触れていない. 条件は以下である.

- dev 環境 (`--local`, loopback 公開のみ, tailnet 経路なし)
- 実データを読み取り専用でマウント
- `BOOKQ_TEST_MODE=true` による認証スキップ (ログインリダイレクトなし)
- localhost (`http://127.0.0.1:8080`), Chromium の iPhone プロファイル (390x844 DPR3, mobile + touch)

## 2. 環境

証跡: `.omo/evidence/live-top-page-display-perf/00-env.md` (MAIN チェックアウト側の絶対パス `/Users/kentokumatsunami/Documents/GitHub/bookq/.omo/evidence/live-top-page-display-perf/00-env.md`).

| 項目 | 値 | 証跡 |
|---|---|---|
| env 名 | `live-top-page-display-perf` | `00-env.md:130-131` |
| URL / ポート | `http://127.0.0.1:8080` / `8080` (8099 フォールバック未使用) | `00-env.md:8,131` |
| スロット / プロジェクト | `02` / `bookq-dev-02-live-top-page-display-perf` | `00-env.md:9,132-138` |
| viewer コンテナ | `bookq-dev-viewer-dev-02` | `00-env.md:26,143-144` |
| `/app/downloads` マウント元 | `/Volumes/San500g/books`, Mode `ro`, `RW:false` (読み取り専用) | `00-env.md:153,156` |
| トップレベル件数 | 4 (画像ライブラリ構成を確認) | `00-env.md:157` |
| `/api/version` | `{"version":"2.0.0","sha":"","build_date":""}` | `00-env.md:162-164` |
| `/api/health` | `{"status":"ok"}` | `00-env.md:168-170` |
| `/api/folders?depth=1` | 2330 B, フォルダ数 4 (STOP 条件解消) | `00-env.md:174-175,191` |
| `GET /` ステータス | 200 (ログインリダイレクトなし, `BOOKQ_TEST_MODE=true` で認証スキップ) | `00-env.md:183-187` |
| サーバ | hypercorn 単一ワーカー (`ENTRYPOINT ["hypercorn", "app.main:app"]`, bind `0.0.0.0:8080`) | worktree `Dockerfile:50-51` |
| プール状況 | 2/10 使用 (本 env + `fix-high-severity-risks` slot 01), 枯渇なし | `00-env.md:135-138` |

初回 `up` は空ディレクトリのマウントによりトップレベル件数 0 で STOP 条件に該当したため, `DOWNLOADS_HOST_DIR=/Volumes/San500g/books` を指定して `down` 後に `up` し直している (履歴は `00-env.md:86-93,104-126` に保持, 現行の有効値は上表の re-up 後セクション).

## 3. 手法

### 3.1 canonical 計測 (measure.py, T3)

証跡: `.omo/evidence/live-top-page-display-perf/01-measure/baseline.md`, `baseline.json`, `measure-run.log`.

- `ONLY=A uv run --with playwright --with-requirements requirements-dev.txt python scripts/perf/measure.py http://127.0.0.1:8080 --runs 3 --out .omo/evidence/live-top-page-display-perf/01-measure` を実行 (measure.py 自体の改変なし, `ONLY=B` / `ONLY=A_cpu4x` は未実行).
- プロファイル A_iphone (Playwright chromium). 実行ログは `measure-run.log:1-4` (run 1/3, 2/3, 3/3, done).
- 本番 URL は指定していない. 他の計測タスクと時間帯を重ねていない (厳格直列).

### 3.2 display-complete 精密計測 (T4 iPhone cold x3, T5 desktop cold x1)

証跡: `.omo/evidence/live-top-page-display-perf/02-display/iphone-run1.json`, `iphone-run2.json`, `iphone-run3.json`, `iphone-summary.json`, `desktop-run1.json`.

- run ごとに新規ブラウザコンテキスト (fresh `browser.new_context`, 空 HTTP キャッシュ, SW なし, localStorage なし) で cold 性を保証し, `ignoreCache` でナビゲートした. `chrome-devtools_*` ツールがワーカーセッションに存在しなかったため, 証跡用一時スクリプト (`02-display/fallback_measure.py`, `02-display/fallback_measure_desktop.py`, プロダクトコードではない) で同手順を代替した.
- iPhone 条件: viewport 390x844, device_scale_factor 3, `is_mobile=True`, `has_touch=True`, iPhone 相当 UA. デスクトップ条件: viewport 1280x800, DPR 上書きなし, mobile/touch フラグなし, 既定デスクトップ UA.
- SW 非登録を各 run で確認し, `sw_registrations` として JSON に記録した (全 run 0).
- settle 定義: `#tile-grid` の全 `img` が `img.complete` になった時刻 (`PERF_INIT_JS` のポーリング + MutationObserver, `COLLECT_JS` で集計). measure.py と同一の定義であり, テスト側 (`tests/test_top_display_perf.py`) のタイル限定セレクタより広い点に注意する.
- T5 は T4 の全ページ close 後に実行した. T4/T5 とも settle タイムアウト (30 秒) なし, 再試行なし.

### 3.3 Simulator スクリーンショット (T6)

証跡: `.omo/evidence/live-top-page-display-perf/03-simulator.png` (テキスト参照のみ, 画像の埋め込みや複写はしない).

- 既定デバイス iPhone 17, `./scripts/open-simulator.sh http://127.0.0.1:8080` で開き, `xcrun simctl io booted screenshot` で取得した. ms 計測はしていない (Tier2 は screenshot 中心).
- 初回撮影は初回ペイント前の白画面だったため開き直して再撮影した (手順上の再試行であり, 計測値への影響はない).

### 3.4 手法上の注意 (caveat)

- run3 (115 ms) と desktop (370.2 ms) は中間レンダーで settle した (settle 時の img 数が少ない). 記録のまま残し, 中央値の頑健性で代表させる. 詳細は §6 を参照する.
- run1 (11738.7 ms) はサーバ cold (キャッシュ TTL ミスによる全量再スキャン) を捉えた. 除外せず記録する.
- 経路は localhost 上の Chromium iPhone プロファイルであり, tailnet HTTPS でも実機 Safari でもない.

## 4. 結果

### 4.1 display-complete (本命, iPhone プロファイル cold x3)

証跡: `02-display/iphone-summary.json:1-23`.

| run | display-complete (ms) | first-tile (ms) | 証跡 |
|---|---|---|---|
| run1 | 11738.7 | 6594.5 | `02-display/iphone-run1.json:3-4` |
| run2 | 503.8 | 72.1 | `02-display/iphone-run2.json:3-4` |
| run3 | 115.0 | 59.8 | `02-display/iphone-run3.json:3-5` |
| 中央値 | 503.8 | 72.1 (3 値 6594.5 / 72.1 / 59.8 の中央値) | `02-display/iphone-summary.json:2` |

- run1 はサーバ cold 寄り (`02-display/iphone-summary.json:23` の注記). `?depth=1` が 6464.5 ms を要した (`02-display/iphone-run1.json:42-47`).
- run2 が中央値であり, 代表値として採用する.
- run3 は settle 時に `#tile-grid img` が 3 件のみ (`02-display/iphone-run3.json:47-48`) であり, 中間レンダーでの早期 settle である. 記録のまま残す (除外や再取得はしていない).
- 全 run で `tile_count` 4, `sw_registrations` 0 (`02-display/iphone-run1.json:63-67`, `iphone-run2.json:63-67`, `iphone-run3.json:47-51`).
- `tile_img_count` は run1 27, run2 27, run3 3 (`02-display/iphone-run1.json:64`, `iphone-run2.json:64`, `iphone-run3.json:48`).

### 4.2 ナビゲーション timing (display 計測 run ごと)

| run | nav TTFB (ms) | nav DCL (ms) | nav load (ms) | 証跡 |
|---|---|---|---|---|
| run1 | 18.5 | 124.9 | 124.9 | `02-display/iphone-run1.json:5-7` |
| run2 | 11.8 | 65.6 | 65.6 | `02-display/iphone-run2.json:5-7` |
| run3 | 11.4 | 53.0 | 53.0 | `02-display/iphone-run3.json:5-7` |
| desktop 参考 | 30.7 | 201.8 | 202.6 | `02-display/desktop-run1.json:5-7` |

### 4.3 `/api/folders` (shallow 先行 + full 追従)

shallow (`?depth=1`) が full (`/api/folders`) より先に開始していることを各 run で確認した.

| run | shallow 開始 / 所要 / 転送 / 復号 | full 開始 / 所要 / 転送 / 復号 | 証跡 |
|---|---|---|---|
| run1 | 開始 124.6, 所要 6464.5 ms, 転送 1997 B, 復号 3982 B | 開始 6595.5, 所要 230.5 ms, 転送 345615 B, 復号 8955133 B (2 回目は 304 応答相当: 転送 300 B) | `02-display/iphone-run1.json:40-62` |
| run2 | 開始 65.4, 所要 5.4 ms, 転送 1997 B, 復号 3982 B | 開始 72.5, 所要 91.0 ms, 転送 345615 B, 復号 8955133 B (2 回目は転送 300 B) | `02-display/iphone-run2.json:40-62` |
| run3 | 開始 52.8, 所要 5.4 ms, 転送 1997 B, 復号 3982 B | 開始 60.2, 所要 61.3 ms, 転送 345615 B, 復号 8955133 B | `02-display/iphone-run3.json:31-45` |
| desktop 参考 | 開始 201.2, 所要 6.4 ms, 転送 1997 B, 復号 3982 B (full 取得は settle 前に観測されず) | 観測なし | `02-display/desktop-run1.json:31-39` |

- run1 の shallow 所要 6464.5 ms はサーバ cold 時の全量再スキャンに対応する (first_tile 6594.5 ms の直前に shallow が完了している).
- run2/run3 の shallow 所要はいずれも 5.4 ms であり, warm 時の典型値を示す.

### 4.4 `/thumb` 数とバイト数 (run ごと)

| run | thumb 件数 | thumb 転送 (B) | thumb 復号 (B) | 証跡 |
|---|---|---|---|---|
| run1 | 86 | 587470 | 1561773 | `02-display/iphone-run1.json:34-38` |
| run2 | 49 | 475135 | 807965 | `02-display/iphone-run2.json:34-38` |
| run3 | 3 | 49919 | 49019 | `02-display/iphone-run3.json:25-29` |
| desktop 参考 | 3 | 49919 | 49019 | `02-display/desktop-run1.json:25-29` |

- run3 と desktop の thumb 件数が少ないのは §4.1 と同じ早期 settle の帰結であり, 追従分の読み込みが settle 後に続く構成である.

### 4.5 リソース総数と総転送 (run ごと, `resource_agg` の合算)

| run | リソース総数 | 総転送 (B) | 総転送 (KB, 1024 換算) | 証跡 |
|---|---|---|---|---|
| run1 | 105 (css 1 + js 12 + api 6 + thumb 86) | 1055433 | 1031 | `02-display/iphone-run1.json:18-39` |
| run2 | 68 (css 1 + js 12 + api 6 + thumb 49) | 943098 | 921 | `02-display/iphone-run2.json:18-39` |
| run3 | 21 (css 1 + js 12 + api 5 + thumb 3) | 517582 | 505 | `02-display/iphone-run3.json:9-30` |
| desktop 参考 | 20 (css 1 + js 12 + api 4 + thumb 3) | 171967 | 168 | `02-display/desktop-run1.json:9-30` |

総数は各カテゴリ件数の和, 総転送は各カテゴリ転送量の和である. KB 換算は 1024 除算による表示上の換算であり, 合否判定にはバイト値を用いる.

### 4.6 longtask 数 (display 計測)

| run | longtask 件数 | 内訳 (開始 / 所要 ms) | 証跡 |
|---|---|---|---|
| run1 | 2 | 6859.5 / 162, 8161.7 / 73 | `02-display/iphone-run1.json:8-17` |
| run2 | 2 | 194.6 / 148, 436.1 / 67 | `02-display/iphone-run2.json:8-17` |
| run3 | 0 | なし | `02-display/iphone-run3.json:8` |
| desktop 参考 | 0 | なし | `02-display/desktop-run1.json:8` |

### 4.7 desktop 参考値 (cold x1, 参考扱い)

証跡: `02-display/desktop-run1.json:1-45`.

- display-complete 370.2 ms, first-tile 229.6 ms (`02-display/desktop-run1.json:3-4`).
- `tile_count` 4 に対し settle 時の `tile_img_count` 3 であり, run3 と同じ早期 settle の形である. 記録のまま残し, 参考値として扱う. 本命は iPhone 3 run の中央値 503.8 ms である.
- nav timing と folders/thumb の値は §4.2, §4.3, §4.4 の desktop 行を参照する.
- スクリーンショットは `02-display/desktop-run1.png` (54774 B) に保存されている (画像の埋め込みや複写はしない).

### 4.8 measure.py baseline (canonical, T3, 表の転記)

証跡: `01-measure/baseline.md:1-26` (20 行の指標表をそのまま転記する).

| Metric | A_iphone |
|---|---|
| Nav TTFB (ms) | 12.7 |
| DOMContentLoaded (ms) | 63.2 |
| load (ms) | 63.2 |
| First tile rendered (ms) | 80.1 |
| Top: resource count | 90 |
| Top: total transfer | 918 KB |
| Top: long tasks (count) | 2 |
| /api/folders size | 1 KB |
| /api/folders TTFB (ms) | 7.4 |
| /api/folders total (ms) | 8.4 |
| Open leaf: click -> first img (ms) | 167.9 |
| Open leaf: image requests | 9 |
| Open leaf: image bytes | 7,013 KB |
| First page image bytes | 854 KB |
| First page image fetch (ms) | 135.1 |
| Page turn: median (ms, to loaded img) | 3.1 |
| Page turn: max (ms) | 7.6 |
| Page turn: long tasks | 0 |
| Page turn: rAF gaps >50ms | 0 |
| JS total (decoded) | 333 KB |

上表の裏付け (すべて `01-measure/baseline.json` の中央値または該当 run 値).

- Nav TTFB 12.7 / DCL 63.2 / load 63.2 / first tile 80.1 ms は `variants.A_iphone.median` の `ttfb_ms`, `dcl_ms`, `load_ms`, `first_tile_ms` (`01-measure/baseline.json:1289-1293`).
- Top リソース数 90 / 総転送 939932 B (918 KB) / longtask 2 は同 median の `top_resources_count`, `top_resources_transfer`, `top_longtasks` (`01-measure/baseline.json:1294-1296`). 総転送の内訳 (中央値 run): css 9623 + js 105001 + api 351495 + thumb 473813 B (`01-measure/baseline.json:447-468`).
- `/api/folders` 1215 B (gzip 転送) / TTFB 7.4 ms / total 8.4 ms は同 median の `api_folders_bytes`, `api_folders_ttfb_ms`, `api_folders_total_ms` (`01-measure/baseline.json:1297-1299`). `?depth=1` の gzip 転送 1215 B に対し復号後は 2330 B であり (`01-measure/baseline.json:469-480`), `00-env.md` の 2330 B と一致する (同一の 4 トップフォルダ, 矛盾なし).
- Open leaf (click から初画像 167.9 ms, 画像 9 件, 7013 KB 相当, 初画像 874533 B を 135.1 ms) は同 median の `click_to_first_img_ms`, `open_image_requests`, `open_image_bytes`, `first_image_bytes`, `first_image_ms` (`01-measure/baseline.json:1300-1304`).
- Page turn (中央値 3.1 ms, 最大 7.6 ms, longtask 0, rAF gap 0) は同 median の `turn_median_ms`, `turn_max_ms`, `turn_longtasks`, `turn_raf_gaps` (`01-measure/baseline.json:1305-1308`).
- JS 復号合計 341203 B (333 KB) は同 median の `js_decoded_total` (`01-measure/baseline.json:1309`).
- 全 3 run で `tiles_settled` true, タイル 4, タイル画像 23 件 (`01-measure/baseline.json:14-15,441-444,869-871` および各 run の対応行).
- run1 は cold 寄り (first_tile 206.5 ms, `01-measure/baseline.json:14`), 中央値は warm 状態を反映する.
- full `/api/folders` の復号は約 8.9 MB (`01-measure/baseline.json:521-523` の decoded 8943840 B) であり, 実データ規模の帰結である.

### 4.9 Simulator 目視確認 (T6)

- `03-simulator.png` (3108595 B) にトップページ全体 (ヘッダ, 検索バー, 4 フォルダタイルの 2x2 グリッド, フッタの件数・容量・バージョン表示) の描画を確認した. ファイルサイズの証跡は `02-display` と同列の `ls -l` 出力による (T6 記録). ms 値は取得していない.

## 5. KPI 比較

比較対象は `docs/performance.md` の現行目標値である (KPI 表: 初タイル `≤300ms`, トップ転送量 `≤1MB`; 実測値未記入欄: display-complete `<5000ms`).

| 指標 | 目標 (`docs/performance.md:5-11,54-61`) | 実測 (中央値) | 合否 | 証跡 |
|---|---|---|---|---|
| 初タイル描画 | ≤300 ms | 72.1 ms (display 計測 3 run の中央値; measure.py 中央値 80.1 ms も ≤300 ms) | 合格 | `02-display/iphone-summary.json:10-21`, `01-measure/baseline.json:1293` |
| トップ転送量 | ≤1 MB | 939932 B (918 KB) | 合格 | `01-measure/baseline.json:1295` |
| display-complete | <5000 ms | 503.8 ms | 合格 | `02-display/iphone-summary.json:2` |

- 初タイルの実測 72.1 ms は §4.1 の 3 値 (6594.5 / 72.1 / 59.8) の中央値である. run1 の 6594.5 ms はサーバ cold 時の値であり, 中央値には影響しない.
- トップ転送 939932 B は 1 MB (1048576 B) を下回る.
- display-complete 503.8 ms は 5000 ms を十分に下回る. ただし run1 (11738.7 ms) 単独では上回るため, cold 初回のばらつきは §6 に記録する.

## 6. 所見

所見は既定の事実引用のみとし, 推測は書かない.

- 全文 `/api/folders` JSON は TTL ミスごとに全量再取得される (`app/routes.py:32,158-196`). run1 の shallow 所要 6464.5 ms (`02-display/iphone-run1.json:44`) が cold 初回の支配項であり, display-complete 11738.7 ms の主因である.
- タイルごとの画像 fan-out は 1 タイルあたり 1 から 9 の img である (`app/static/browse/render.js:202-223`). lazy 解決は `app/static/thumbs/resolveCover.js:45` による. T3 中央値 run の thumb は 71 件 / 473813 B (`01-measure/baseline.json:463-467`), T4 run2 の settle 時 thumb は 49 件 / 475135 B (`02-display/iphone-run2.json:34-38`) である.
- `/` の HTML は gzip 対象外である (`app/compression.py:15`). Nav TTFB の中央値は measure.py で 12.7 ms (`01-measure/baseline.json:1290`), display run2 で 11.8 ms (`02-display/iphone-run2.json:5`) である.
- PIL リサイズは thumb キャッシュミスごとに発生する (`app/thumbs.py:260-280`). run1 の thumb 復号 1561773 B (`02-display/iphone-run1.json:38`) に対し run2 は 807965 B (`02-display/iphone-run2.json:38`) であり, ミス率の差が転送と復号に表れる.
- run3 (115.0 ms) と desktop (370.2 ms) の早期 settle は, settle 時の img 数 (各 3 件, `02-display/iphone-run3.json:48`, `02-display/desktop-run1.json:41`) が最終描画より少ない形であり, 記録のまま残す. 中央値 503.8 ms はこの外れ値に頑健である.

## 7. 次のアクション

性能改善の実装は本計画の範囲外であり, 改善案は後続の計画で扱う (T9 が本レポートの中央値を `docs/performance.md` の実測値未記入欄に記入する).

## 8. 証跡パス一覧

以下はすべて MAIN チェックアウト側の絶対パスである (`/Users/kentokumatsunami/Documents/GitHub/bookq/.omo/evidence/live-top-page-display-perf/` 配下の全ファイル, PR には載らない).

- `/Users/kentokumatsunami/Documents/GitHub/bookq/.omo/evidence/live-top-page-display-perf/00-env.md` (環境事実正本, T2 + re-up 記録)
- `/Users/kentokumatsunami/Documents/GitHub/bookq/.omo/evidence/live-top-page-display-perf/task-1-worktree.md` (T1 実行記録)
- `/Users/kentokumatsunami/Documents/GitHub/bookq/.omo/evidence/live-top-page-display-perf/task-2-devenv.log` (`dev.sh up` 全出力, 初回 + re-up 追記)
- `/Users/kentokumatsunami/Documents/GitHub/bookq/.omo/evidence/live-top-page-display-perf/01-measure/baseline.md` (canonical 20 行表)
- `/Users/kentokumatsunami/Documents/GitHub/bookq/.omo/evidence/live-top-page-display-perf/01-measure/baseline.json` (canonical 生データ, 中央値つき)
- `/Users/kentokumatsunami/Documents/GitHub/bookq/.omo/evidence/live-top-page-display-perf/01-measure/measure-run.log` (measure.py 実行ログ)
- `/Users/kentokumatsunami/Documents/GitHub/bookq/.omo/evidence/live-top-page-display-perf/02-display/iphone-run1.json` (display run1)
- `/Users/kentokumatsunami/Documents/GitHub/bookq/.omo/evidence/live-top-page-display-perf/02-display/iphone-run2.json` (display run2, 中央値 run)
- `/Users/kentokumatsunami/Documents/GitHub/bookq/.omo/evidence/live-top-page-display-perf/02-display/iphone-run3.json` (display run3, 早期 settle)
- `/Users/kentokumatsunami/Documents/GitHub/bookq/.omo/evidence/live-top-page-display-perf/02-display/iphone-summary.json` (3 run 中央値)
- `/Users/kentokumatsunami/Documents/GitHub/bookq/.omo/evidence/live-top-page-display-perf/02-display/desktop-run1.json` (desktop 参考値)
- `/Users/kentokumatsunami/Documents/GitHub/bookq/.omo/evidence/live-top-page-display-perf/02-display/iphone-run1.png` (run1 スクリーンショット, テキスト参照のみ)
- `/Users/kentokumatsunami/Documents/GitHub/bookq/.omo/evidence/live-top-page-display-perf/02-display/iphone-run2.png` (run2 スクリーンショット, テキスト参照のみ)
- `/Users/kentokumatsunami/Documents/GitHub/bookq/.omo/evidence/live-top-page-display-perf/02-display/iphone-run3.png` (run3 スクリーンショット, テキスト参照のみ)
- `/Users/kentokumatsunami/Documents/GitHub/bookq/.omo/evidence/live-top-page-display-perf/02-display/desktop-run1.png` (desktop スクリーンショット 54774 B, テキスト参照のみ)
- `/Users/kentokumatsunami/Documents/GitHub/bookq/.omo/evidence/live-top-page-display-perf/02-display/fallback_measure.py` (T4 証跡用一時スクリプト)
- `/Users/kentokumatsunami/Documents/GitHub/bookq/.omo/evidence/live-top-page-display-perf/02-display/fallback_measure_desktop.py` (T5 証跡用一時スクリプト)
- `/Users/kentokumatsunami/Documents/GitHub/bookq/.omo/evidence/live-top-page-display-perf/03-simulator.png` (T6 Simulator スクリーンショット 3108595 B, テキスト参照のみ)

実データツリー由来のフォルダ名やファイル名は本レポートのいかなる箇所にも転記していない (件数・バイト数・時刻のみ). サンプル URL は `/thumb/<redacted>?w=200` の形に redact する.

## 9. 後片付け

- dev 環境の解放は T7 が並行して実施する (`./dev.sh down live-top-page-display-perf`, `--all` / `--full` は使わない).
- マージ後の worktree 解放は以下のコマンドで行う.

```bash
git worktree remove .worktrees/live-top-page-display-perf
```
