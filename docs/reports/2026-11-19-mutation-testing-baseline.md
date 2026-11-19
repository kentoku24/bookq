# Mutation testing ベースライン (2026-11-19)

初回全量実行の記録。仕組み・再現手順は `docs/mutation-testing.md`、
spec は `.omo/plans/mutation-testing.md`。

## JS (StrykerJS + vitest)

- 対象: `web/static/` コア 8 ファイル (offline-core, api/endpoints,
  api/v1, thumbs/resolveCover, viewer/pairModel, viewer/spread,
  search/facets, browse/hash)
- ユニットテスト: 143 件 (`web/tests/unit/`)、vitest dry run 全パス
- 実行: `npx stryker run` — **17 分 7 秒**、4,227 mutants
- 結果: `.reports/mutation/stryker.{html,json}` (gitignore、再生成可能)

### スコア

| file | score | killed | timeout | survived | no cov |
|---|---|---|---|---|---|
| api/endpoints.js | 81.82% | 18 | 0 | 4 | 0 |
| viewer/pairModel.js | 91.00% | 180 | 2 | 18 | 0 |
| thumbs/resolveCover.js | 68.25% | 86 | 0 | 31 | 9 |
| api/v1.js | 59.58% | 340 | 2 | 203 | 29 |
| search/facets.js | 44.98% | 373 | 3 | 242 | 218 |
| viewer/spread.js | 39.81% | 317 | 17 | 104 | 401 |
| browse/hash.js | 36.69% | 324 | 0 | 241 | 318 |
| offline-core.js | 27.99% | 208 | 0 | 49 | 486 |
| **all** | **44.28%** (covered 67.70%) | 1846 | 24 | 892 | 1461 |

### 観察

- **高スコア**: `pairModel.js` (91%) と `endpoints.js` (81.8%) は
  純粋ロジックとして十分に固定されている。
- **no coverage が大きい**: `offline-core.js` (486) と `spread.js` (401)、
  `hash.js` (318) は未踏領域が広い — offline-core の後半 (store/cache 系)
  や spread.js の DOM 演出部 (snapTo/ジェスチャ)、hash.js の
  restoreBrowse 深部など、unit ではなく結合テスト側の守備範囲。
- **survived の主な内訳**: ConditionalExpression (未テスト分岐) と
  StringLiteral (エラーメッセージ・HTML/CSS 文字列) が大半。
  例: `offline-core.js` の `WIPE_STORAGE_KEYS`/`WIPE_SESSION_KEYS` 配列
  要素の文字列変異、`v1.js`/`facets.js` の条件式反転。
- **error 4 件**: テストランナー起動時のクラッシュ (mutant がトップレベルで
  即死するケース) — 実害なし、再実行で改善見込み。

## Rust (cargo-mutants)

- 対象: `rust/` ワークスペース全体、3,972 mutants
- 設定: `rust/.cargo/mutants.toml`、実行は `scripts/mutation/rust.sh`
  (TMPDIR symlink 必須 — `docs/mutation-testing.md` 参照)
- baseline: `cargo test` 全パス確認済み (123s build + 34s test)
- **実行中** (2026-11-19 00:38 開始、-j4)。`rust/mutants.out/mutants.out/outcomes.json`
  に逐次書き込まれる。完了後に本節を確定値で更新する。

### 途中経過 (~52% 時点、暫定)

| file | missed | caught | timeout |
|---|---|---|---|
| bookq-core/src/archive.rs | 740 | 463 | 15 |
| bookq-core/src/scans.rs | 65 | 4 | 0 |
| bookq-core/src/meta.rs | 49 | 5 | 0 |
| bookq-core/src/pdf.rs | 39 | 58 | 2 |
| bookq-core/src/lib.rs | 11 | 53 | 5 |
| viewer-rs/src/main.rs | 100 | 3 | 0 |
| viewer-rs/src/library.rs | 101 | 52 | 0 |
| viewer-rs/src/folders.rs | 80 | 21 | 0 |
| viewer-rs/src/auth.rs | 15 | 72 | 0 |
| viewer-rs/src/oauth.rs | 2 | 0 | 0 |

暫定 decided=1955, missed=1202, caught=731, timeout=22 →
**暫定 score ≈ 38.5%** (残りファイル: thumbs.rs, reader_sync, serve.rs,
shell.rs, state.rs, tree.rs 等 — 大物がこれから処理される)

観察: `archive.rs` (RAR/ZIP パーサ) と `scans.rs`/`meta.rs` は単体テストが
ほぼ効いていない。`viewer-rs/src/main.rs` のルートハンドラも同様。
`auth.rs` は良く固定されている。大きなサムネイル/配信系ファイルの結果待ち。

## トリアージ方針 (次アクション候補)

1. `pairModel`/`endpoints`/`resolveCover` は既に良好 — 回帰防止の
   conformance vector 追加で維持。
2. `v1.js`/`facets.js` の survived ConditionalExpression は
   `isCurrent` 世代ガード・`include_hidden` 分岐など意味のある条件 —
   優先的にテスト追加候補。
3. `offline-core.js`/`spread.js`/`hash.js` の no-coverage 領域は
   DOM/ストレージ結合が主 — unit 拡張か Playwright 側強化かを
   領域ごとに判断。
4. Rust 側は完了後に missed 上位ファイルを同じ 3 分類で評価。
