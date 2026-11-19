# 意味論インベントリ — 規則 × ランタイムの台帳

Baseline: `3ff4a07`（origin/main, 2026-11-16）·
関連: `docs/adr/0002-server-resolved-work-model.md`（PR #508）· `docs/adr/0003-cross-runtime-contract-scope.md`

本書は、ドメイン規則が**どのランタイムに実装され、最終的にどこに置かれるべきか**を記録する唯一の台帳である。
ADR 0002（サーバ解決）の進捗と、ADR 0003（クロスランタイム契約の範囲）の適用状況はどちらもここで追う。

## 1. 読み方

- **分類**（ADR 0003 の Decision 7 の順で決める）
  - `server` — ADR 0002 でサーバに寄せる。クライアント実装は削除する
  - `server-only` — 既にサーバにしか無い
  - `server-authoritative` — 正はサーバ。クライアントはローカルキャッシュの自明な比較だけを持つ
  - `protocol` — ワイヤ上でバイト一致が要る。**全ポートが同じ golden を読む**（クロスランタイム契約の唯一の対象）
  - `platform-local` — 端末状態に依存するビュー方針。各ランタイムの単体テストで守り、クロスランタイム契約にしない
  - `未決` — 判断待ち（判断条件を備考に書く）
- **シンボル**は `file::symbol` で書き、行番号・行数は書かない（ドリフトするため）。
- iOS の純粋規則は `ios/Packages/BookqCore`（#534）に、Android は `android/core` にある。表中では `BookqCore/` `core/` と略す。
- 再計測: 表のシンボルを `git grep` で引き、実装の有無を確認する。

## 2. 台帳

| # | rule id | rust | js | swift | kotlin | 分類 | 既存ケース | 備考 |
|---|---|---|---|---|---|---|---|---|
| 1 | `tree.flatten` | `tree.rs`（scan/assemble） | — | — | — | server-only | — | クライアントは「`name` に `/` が入りうる」契約を消費するだけ |
| 2 | `tree.lookup` | `tree.rs::find_tree_node` / `reader_sync/catalog.rs::find_container` | `app.js::findNodeByPath` / `findParentOf` | `BookqCore/BrowseModel::node(at:)` / `Library/LibraryStore::node(at:)` | `core/browse/BrowseRules::nodeAt` | server | `path-lookup`（4 言語） | 0002 Phase 4: ノードに `path` が載り消える |
| 3 | `tree.siblings` | `catalog.rs::sibling_leaf_folders` | `app.js::getSiblingLeafFolders` | `BookqCore/ReaderModel::siblingLeaves` | `core/reader/ReaderWork::siblingLeaves` | server | `work-chapters`（4 言語） | 0002 Phase 2–3: `Work.chapters` に吸収 |
| 4 | `work.chapters` | `catalog.rs::chapter_pages`（`conformance_vectors.rs` に**テスト内再実装**あり） | `app.js` `loadImages` 内 | `BookqCore/ReaderModel::chapters(for:)` | `ReaderWork::chapters` | server | `work-chapters`（4 言語） | 再実装は #521 で解消 |
| 5 | `work.page-order` | `bookq-core/src/scans.rs::natural_cmp` / `sort_file_cmp` | — | — | — | server-only | — | |
| 6 | `path.encode` | `thumbs.rs::unquote`（復号側） | `offline-core.js::encodePath` | `BookqCore/ImageURL::encodePath` | `core/api/ImagePath::encodePath` | server | `encode-path`（4 言語） | 0002 Phase 4: サーバが URL を発行 |
| 7 | `layout.spread-plan` | `reader_sync/model.rs::build_spread_plan`（ペア専用。単独読書との等価性は未検証） | `viewer/spread.js::rebuildSpreads` | `BookqCore/ReaderModel::rebuildSpreads` | `ReaderWork::rebuildSpreads` | server（§2.4 が Go のとき） | `solo-spreads`（js/swift/kotlin） | #518 で判定。No-Go なら「解消予定の重複」として vectors 維持 |
| 8 | `layout.chapter-padding` | 同上 | `viewer/spread.js::padChapterSpacers` | `BookqCore/ReaderModel::assemble` | `ReaderWork::assemble` | server（§2.4 が Go のとき） | `chapter-padding`（js/swift/kotlin） | 同上 |
| 9 | `layout.page-mode` | — | `app.js` 内に inline（関数名なし） | `BookqCore/ReaderModel::pageMode(for:landscapeSpread:)` | `ReaderWork::pageMode` | platform-local | — | 画面の縦横・設定に依存 |
| 10 | `layout.spread-anchor` | — | `app.js::getChapterSpreadRange` | `BookqCore/ReaderModel::chapterSpreadRange` | `ReaderWork::chapterSpreadRange` / `chapterStartSpread` | platform-local | — | スライダー等 UI 部品への写像。見開き表がサーバ化されても入力が変わるだけ |
| 11 | `layout.viewport-fit` | — | `viewer/spread.js::spreadMetrics` | `BookqCore/ReaderModel::spreadFit`（呼び出しは `Reader/ReaderView` / `External/ExternalDisplayView`） | `ReaderWork::spreadFit` + `reader/ExtDisplay` | platform-local | — | 画面比・外部ディスプレイ |
| 12 | `state.position-merge` | `state.rs`（LWW + tombstone + 未来 ts 拒否） | `progress/sync.js`（送信制御。比較はサーバ任せ） | `Reader/ProgressSync` + `Library/ReadingProgressStore` | `core/progress/ProgressBook::mergeRemote` | server-authoritative | `positions-cases`（rust） | クライアントは「ローカルより新しければ採用」のみ。stale PUT はサーバが `stale` で返す |
| 13 | `state.visibility-filter` | `state.rs::covered_by_roots`（search は `search.rs`） | `browse/visibility.js::isHidden` / `hiddenRootFor` | `Visibility/VisibilityStore::isHidden` | `core/visibility/VisibilityRules::isHidden` | server | — | 0002 Phase 4: `visibility=apply\|annotate` |
| 14 | `offline.leaf-paths` | — | `offline/storeAdapter.js::collectLeafPaths` / `getLeafPathsFor` | `Offline/OfflineStore` | `core/offline/OfflineRules::leafPaths` / `leafMost` | server | — | 0002 Phase 2–3: Work 1 回の取得で保存対象が決まる |
| 15 | `search.facet-filter` | —（search API は `artist`/`tag` で絞れる） | `search/facets.js::_visibleResults` | —（サーバ結果を表示） | `core/search/FacetRules::nodeMatches` | 未決 | — | 入力 `subtree_facets` はサーバ由来。0002 Phase 4 後に server へ寄せるか判断 |
| 16 | `sync.canonical-json` | `reader_sync/catalog.rs::canonical` | `viewer/pairClient.js::canonicalJson` | `BookqCore/CanonicalJSON` | `core/sync/CanonicalJson::encode` | **protocol** | 部分（下記 §3） | |
| 17 | `sync.manifest-digest` | `catalog.rs::manifest_digest` | `pairClient.js::manifestDigestHex` | `BookqCore/CanonicalJSON::manifestDigest` | `CanonicalJson::manifestDigest` | **protocol** | 部分（下記 §3） | |
| 18 | `sync.frame-validate` | `reader_sync/protocol.rs` | `viewer/pairModel.js` / `pairClient.js` | `BookqCore/PairModels` | `core/sync/PairModels` + `PairProto` | **protocol** | 部分（下記 §3） | Android は `PairSpecTest` で `asyncapi.yaml` と照合。iOS には同等の照合が無い |
| 19 | `search.facet-aggregate` | `search.rs::facets_for` | — | — | — | server-only | — | |

### 分類の集計

| 分類 | 規則 | 0002 完了後にクロスランタイムで残るもの |
|---|---|---|
| server | 2, 3, 4, 6, 13, 14（+ 7, 8 は §2.4 次第） | なし（Rust の単体テストのみ） |
| server-only | 1, 5, 19 | なし |
| server-authoritative | 12 | なし（`positions-cases` は rust のみで正しい） |
| **protocol** | 16, 17, 18 | **reader-sync golden 1 本を 4 ポートが読む** |
| platform-local | 9, 10, 11 | なし（各ランタイムの単体テスト） |
| 未決 | 15 | Phase 4 後に判断 |

数え方の細部（同一ランタイム内の複数関数を 1 と数える等）に依存する合計値は載せない。
意思決定に使うのは「どの分類にいくつ残るか」だけである。

## 3. protocol 規則の現状（ADR 0003 Follow-up 1 の対象）

| ポート | 読んでいる golden | 状態 |
|---|---|---|
| rust | `api/reader-sync-cases.json`（`reader_sync/golden.rs`） | ✓ |
| python / ws mock | `api/reader-sync-cases.json`（`tests/test_reader_sync_golden.py` / `scripts/api/ws_mock.py`） | ✓ |
| swift | `tests/fixtures/reader_sync_cases.json`（`BookqCoreTests/CanonicalDigestTests` / `BookqTests/PairSyncTests`） | **別ファイル・別形式** |
| kotlin | なし（`core/sync/PairSyncTest.kt` に digest を定数で直書き） | **golden を読んでいない** |
| js | なし（`pairClient.js` の canonical / digest を直接叩くテストが無い） | **golden を読んでいない** |

到達状態: 1 つの golden ファイルを rust / js / swift / kotlin が読み、宣言検査（`test_vectors_declaration.py` と同等）がそれを保証する。

## 4. クロスランタイム契約に含めないもの（意図的除外）

| 対象 | 現状の実装 | 除外理由 |
|---|---|---|
| platform-local 規則（#9–11） | 上表 | 端末ごとに調整されるべきビュー方針。一致を強制するとプラットフォーム別の UX 調整ができなくなる |
| `offline.cache-name`（Cache Storage 名 / ディレクトリ名） | `offline-core.js::cacheNameFor` / `OfflineStore` / `ScopedFiles.segment` | プラットフォームのストレージ識別子。共有すると各プラットフォームのストレージ移行を縛る |
| HTTP リトライ / 202 Retry-After / 304 | `api/v1.js` / `BookqAPI` / `BookqApi.kt` | I/O とスケジューリング。wire 契約は `v1-semantics.md` が統治 |
| ジェスチャ閾値・アニメーション時間・タップゾーン比率 | `viewer/gesture.js` / `ReaderView` / `ZoomGestures.kt` / `zoneAction*` | 知覚とデバイス特性の関数 |
| reader-sync クライアント状態機械（3 実装） | `viewer/pair*.js` / `PairStore.swift` / `PairStore.kt` | 0002 Phase 5（#529）で別途判断 |

## 5. 意図的な差異

| 差異 | 記録場所 | 扱い |
|---|---|---|
| iOS は章跨ぎ操作を許可、web/Android は章内 clamp | `docs/ios-app.md` + 実装コメント | 入力（`ChapterPolicy.crossChapterTurns` 相当）として表現。章構成自体は 0002 でサーバ解決 |
| sibling autoload の深さゲート | web `SIBLING_AUTOLOAD_MIN_DEPTH = 2`、iOS は #274 で一致済み | 差異は解消済み。`core/reader/ReaderWork.kt` 冒頭の「iOS drops that gate」は古い記述 |
| Android の `beyondViewportPageCount` | `docs/android-app.md` | platform-local（UI） |

## 6. 進捗

### 6.1 サーバ解決（ADR 0002）

| 段階 | issue | 対象規則 | 状態 | 完了時に台帳で行うこと |
|---|---|---|---|---|
| Phase 0 §2.4 判定 | #518 | 7, 8 | 未着手 | 結果（Go / No-Go と不一致ケース）を 7・8 の備考に記録 |
| Phase 0 spec | #519, #520 | — | 未着手 | — |
| Phase 1 サーバドメイン層 | #521, #522 | 4 | 未着手 | `conformance_vectors.rs` の再実装が消えたことを #4 の rust 欄に反映 |
| Phase 2 web | #523 | 3, 4, 14（+ 7, 8） | 未着手 | js 欄を `—` にし、対応 vector の `languages` から `js` を外す |
| Phase 3 iOS / Android | #524, #525 | 3, 4, 14（+ 7, 8） | 未着手 | swift / kotlin 欄を `—` にし、`languages` を更新 |
| Phase 4 解決済みツリー + 可視性 | #526, #527, #528 | 2, 6, 13（+ 15 を判断） | 未着手 | 同上。`path-lookup` / `encode-path` を rust のみにする |
| Phase 5 判断点 | #529 | reader-sync 状態機械 | 未着手 | §4 の該当行を更新 |

### 6.2 クロスランタイム契約（ADR 0003）

| 項目 | 状態 |
|---|---|
| protocol golden の統合（§3 の swift / kotlin / js） | 未着手 |
| ネイティブのリリース手順に「サーバ先行デプロイ」を明記 | 未着手 |
| テスト内再実装・ソース切り出しの解消（rust: #521、js: #523） | 未着手 |

**更新ルール**: 規則の実装を追加・削除・移動する PR は、§2 の該当行と §6 を同じ PR で更新する。
