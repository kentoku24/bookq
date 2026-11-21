# Mutation testing リスク重点テスト補完 (2026-11-19)

ベースライン (`2026-11-19-mutation-testing-baseline.md`) の survived /
no-coverage を「カバレッジ数」ではなく「壊れた時にユーザへ見える壊れ方・
情報漏洩・データ損失」でトリアージし、優先領域に単体テストを追加した記録。

## 追加したテスト (計 36 件、全パス)

### Rust (viewer-rs) — `cargo test` 154 件全パス

| 対象 | テスト数 | 守る UX/リスク |
|---|---|---|
| `search.rs` | 17 | 検索 relevance (大小無視部分一致・重み・複数語)、artist/tag フィルタ、空クエリ、**隠しルート除外 (fail-closed プライバシー境界)**、決定的ランキング、結果 JSON 形状、facets のパススコープ/隠し除外、大小区別 dedup + 大小無視ソート、bool パラメータ解釈 |
| `tree.rs` | 6 | tempdir ベースの scan_tree/assemble — 単一子チェーン flatten、兄弟あり/自前メディアで flatten 停止、ドット dir・非メディア無視 (AGENTS.md 記載の重要仕様) |
| `state.rs` | 3 | `covered_by_roots` 境界 (exact/descendant/prefix 非一致/空/複数 roots) — 可視性フィルタの根拠 |

### JS — `vitest run` 153 件全パス (ベースライン 143 → +10)

| 対象 | テスト数 | 守る UX/リスク |
|---|---|---|
| `offline-core.js` | 4 | `wipeTargets`/`wipeClientData` — データキャッシュ (work-/api-/images-) のみ削除、shell キャッシュと `bookq_harness` 保持、legacy+scoped 全 progress キー・WIPE_* セッションキー除去、`ProgressSync.remove` 呼出、空環境安全性 (アカウントデータ分離) |
| `facets.js` | 3 | `_visibleResults` が tiles/list/cards 全レンダラに効くこと、showAll バイパス、`uiPresentation` の className/badgeHtml/buttonHtml 適用 (隠しフォルダ漏洩防止) |
| `hash.js` | 3 | デッド browse パスでハッシュが canonical root に戻る、viewer ハッシュが decode 済みフォルダで loadImages、hidden ガードが `redirectHiddenToTop` へ逃がし loadImages 非呼出 |

## スコープ付き mutation 再測定

ベースラインの全量は時間がかかるため、テストを追加したファイルのみを
`-f` / `--mutate` で再測定。

### JS (Stryker) — 対象 3 ファイル、2,464 mutants、7 分 21 秒

| file | before | after | Δ |
|---|---|---|---|
| offline-core.js | 27.99% | **37.31%** | +9.3pt (killed 208→277) |
| browse/hash.js | 36.69% | **40.95%** | +4.3pt (killed 324→361) |
| search/facets.js | 44.98% | **46.67%** | +1.7pt (killed 373→390) |

- 効果が最大だったのは `offline-core.js` — WIPE_* 配列要素や
  isDataCacheName 系の StringLiteral/Conditional 変異を wipe テストが
  69 件 kill。
- facets/hash の上積みは小幅 — 残りの survived は `isCurrent` 世代ガードや
  DOM 結合部の no-coverage が中心で、unit 層の限界。

### Rust (cargo-mutants) — `search.rs`/`tree.rs`/`state.rs` (+ glob で `reader_sync/state.rs` も捕捉)、425 mutants、16 分

| file | before (missed/caught) | after score | Δ |
|---|---|---|---|
| search.rs | 64 / **0** | **81.2%** (49 caught, 12 missed, 3 timeout) | 全滅 → 健全化 |
| tree.rs | 70 / 7 | **22.1%** (17 caught, 60 missed, 28 unviable) | flatten 契約は固定、assemble 深部は残存 |
| state.rs | 93 / 64 | **43.9%** (69 caught, 88 missed) | +3pt — `covered_by_roots` のみ微増、position 系は未着手 |
| reader_sync/state.rs | (対象外・glob 偶発) | 51.2% | 参考値 |

- `-f state.rs` が `reader_sync/state.rs` にもマッチしたため 425 mutants
  (狙いは 3 ファイル ~298)。結果的に pair session state の現状値も取得。
- `view_caps_and_flattens` (thumbs の 60s 超テスト) は対象 3 ファイルと
  無関係なため `--cargo-test-arg="--" --cargo-test-arg="--skip"
  --cargo-test-arg="view_caps_and_flattens"` で skip — baseline
  34s test → 2s、mutant あたり ~10s に短縮 (全量なら ~90 分のところ 16 分)。
- search.rs の残 missed 12 件は `api_search`/`api_facets` の HTTP 層と
  building フラグ応答など、unit ではなく handler 結合テスト側の領域。

## 残課題 (次のトリアージ候補)

- `archive.rs` (missed 740) — RAR/ZIP エッジケース。fixture ベースの
  境界テストが必要で別タスク級。
- `main.rs` / `folders.rs` / `scans.rs` / `meta.rs` — ルート層・スキャン系。
- JS の `spread.js` / `v1.js` no-coverage 領域 — DOM/ストレージ結合部は
  Playwright 側の守備範囲。
- state.rs の position 系関数 (`list_positions`/`put_position`) は
  scoped 再測定でも missed が残る見込み — 読書位置の UX 直結なので
  次点のテスト追加候補。
