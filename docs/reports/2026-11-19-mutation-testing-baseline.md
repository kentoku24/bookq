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
- 実行: `cargo mutants -j4`、**5 時間**で全量完了 (2026-11-19 00:38–05:38 JST)
- 結果: `rust/mutants.out/mutants.out/` (gitignore、`outcomes.json` +
  `missed.txt`/`caught.txt`/`timeout.txt`/`unviable.txt` + mutant ごとの log/diff)

### スコア

| | 件数 |
|---|---|
| total mutants | 3,972 |
| caught | 1,675 |
| missed | 2,066 |
| timeout | 31 |
| unviable | 200 |
| **score** (caught+timeout / caught+missed+timeout) | **45.2%** |

### ファイル別 (missed / caught / timeout)

| file | missed | caught | timeout | 備考 |
|---|---|---|---|---|
| bookq-core/src/archive.rs | 740 | 463 | 15 | RAR/ZIP パーサ。最大の穴 |
| viewer-rs/src/reader_sync/mod.rs | 139 | 131 | 2 | |
| viewer-rs/src/thumbs.rs | 127 | 222 | 4 | 最も mutant が多いが kill 率はまずまず |
| viewer-rs/src/library.rs | 101 | 52 | 0 | |
| viewer-rs/src/main.rs | 100 | 3 | 0 | ルートハンドラがほぼ無防備 |
| viewer-rs/src/serve.rs | 89 | 119 | 0 | |
| viewer-rs/src/folders.rs | 80 | 21 | 0 | |
| viewer-rs/src/reader_sync/catalog.rs | 71 | 53 | 0 | |
| viewer-rs/src/tree.rs | 70 | 7 | 0 | scan_tree の flatten 規則が未固定 |
| bookq-core/src/scans.rs | 65 | 4 | 0 | |
| viewer-rs/src/state.rs | 93 | 64 | 0 | |
| viewer-rs/src/search.rs | 64 | 0 | 0 | **全滅 — テスト皆無** |
| viewer-rs/src/shell.rs | 56 | 122 | 0 | |
| viewer-rs/src/reader_sync/protocol.rs | 53 | 43 | 0 | |
| bookq-core/src/meta.rs | 49 | 5 | 0 | |
| viewer-rs/src/reader_sync/state.rs | 41 | 43 | 0 | |
| viewer-rs/src/oauth.rs | 36 | 57 | 0 | |
| bookq-core/src/pdf.rs | 39 | 58 | 2 | |
| bookq-core/src/lib.rs | 11 | 53 | 5 | kanji_digit/kanji_unit の match arm 欠落が目立つ |
| viewer-rs/src/auth.rs | 15 | 72 | 0 | 良好 |
| viewer-rs/src/reader_sync/model.rs | 25 | 62 | 3 | |
| viewer-rs/src/pair_report.rs | 2 | 21 | 0 | |

missed の genre 内訳: BinaryOperator 1203, FnValue 461, MatchArm 166,
UnaryOperator 147, MatchArmGuard 87 — 比較演算子・関数戻り値・match arm の
削除系が中心で、「分岐を通るが結果を見ていない」テストの典型。

### 観察

- **archive.rs が突出** (missed 740): 圧縮アーカイブのエッジケースを
  unit test がほとんど踏んでいない。結合テスト経由のカバレッジと思われる。
- **search.rs は caught 0**: 実装はあるが単体テストが存在しない。
- **main.rs / folders.rs / tree.rs / scans.rs / meta.rs**: ルート層と
  スキャン系は振る舞い固定が薄い。`tree.rs` の flatten 規則は
  AGENTS.md 記載の重要仕様なので優先度高。
- **auth.rs / shell.rs / thumbs.rs / pair_report.rs** は健全。
- timeout 31 件は大半 archive.rs — 大きなアーカイブ fixture の
  パース時間が延びる mutant で、実質 kill に近いが遅いテストが原因の
  可能性もあり個別確認要。

## トリアージ方針 (次アクション候補)

1. `pairModel`/`endpoints`/`resolveCover` は既に良好 — 回帰防止の
   conformance vector 追加で維持。
2. `v1.js`/`facets.js` の survived ConditionalExpression は
   `isCurrent` 世代ガード・`include_hidden` 分岐など意味のある条件 —
   優先的にテスト追加候補。
3. `offline-core.js`/`spread.js`/`hash.js` の no-coverage 領域は
   DOM/ストレージ結合が主 — unit 拡張か Playwright 側強化かを
   領域ごとに判断。
4. Rust は `search.rs` (caught 0) → `tree.rs` (flatten 規則) →
   `scans.rs`/`meta.rs`/`main.rs` の順で単体テスト追加が効く。
   `archive.rs` は fixture ベースの境界テストが必要で別タスク級。
5. 増分確認は `scripts/mutation/rust.sh --in-diff` または
   `-f <file>` で数十分に収まる。全量は四半期棚卸し用途。
