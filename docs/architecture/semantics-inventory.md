# 意味論インベントリ — 規則 × ランタイム棚卸し

Baseline: `f4d8ff7` (origin/main, 2026-11-15) · 関連: `docs/architecture/reader-semantics-kernel.md`
本書は**移行の進捗表を兼ねる**（§7）。Phase ごとに「カーネル外に残っている規則」が減ることを確認する。

## 1. 計測方法

- 対象はランタイム（`rust/viewer-rs` + `rust/bookq-core` / `web/static` / `ios/Bookq` / `android/core`+`:data`+`:app`）。
- 「ports」= **その規則を独立に実装しているランタイム数**（1〜4）。同一ランタイム内の複数関数は 1 と数える。
- 「合計 60」= 規則 × ランタイムの実装数（`Σ ports`）。同じ規則が 4 箇所で実装されているなら 4。
- 「ケース」= `api/vectors/*.json` または `api/reader-sync-cases.json` に既に固定されているか。
- 行番号は書かない（ドリフトするため）。`file` + シンボル名で追跡する。
- 再計測: `grep -rn "<symbol>" rust web/static ios android` と本書の表を突き合わせる。

カーネル化は ports の**総数を変えない**（4 実装は 4 実装のまま）。変わるのは「どこにあるか」と
「同じケースで検証されているか」である。したがって進捗は §7 の「移行済み / ports」で測る。

## 2. 棚卸し

| # | rule id | rust | js | swift | kotlin | ports | 既存ケース |
|---|---|---|---|---|---|---|---|
| 1 | `tree.flatten`（単一子フォルダの親への hoist） | `viewer-rs/src/tree.rs`（scan/assemble の flatten 部） | —（`name` に `/` が現れる契約を消費） | — | — | 1 | — |
| 2 | `tree.lookup`（path → node） | `tree.rs::find_tree_node` / `reader_sync/catalog.rs::find_container` | `app.js::findNodeByPath` / `app.js::findParentOf` | `Browse/BrowseModel.swift::node(at:in:)` / `Library/LibraryStore.swift::node(at:)` | `browse/BrowseRules.kt::nodeAt` | 4 | `path-lookup` ✓4 |
| 3 | `tree.siblings`（兄弟葉フォルダ列挙） | `reader_sync/catalog.rs::sibling_leaf_folders` | `app.js::getSiblingLeafFolders` | `Reader/ReaderModel.swift::siblingLeaves` | `reader/ReaderWork.kt::siblingLeaves` | 4 | `work-chapters` ✓4 |
| 4 | `work.chapters`（folder → 章列の選択） | `reader_sync/catalog.rs::chapter_pages`（+ `conformance_vectors.rs` に再実装） | `app.js` `loadImages` 内 | `ReaderModel.swift::chapters(for:in:)` | `ReaderWork.kt::chapters` | 4 | `work-chapters` ✓4 |
| 5 | `work.page-order`（自然順 + cover 先頭） | `bookq-core/src/scans.rs::natural_cmp` / `sort_file_cmp` | —（サーバ順を信頼） | — | — | 1 | — |
| 6 | `path.encode`（セグメント単位 percent-encode） | `thumbs.rs::unquote`（復号側） | `offline-core.js::encodePath` | `API/ImageURL.swift::encodePath` | `api/ImagePath.kt::encodePath` | 4 | `encode-path` ✓4 |
| 7 | `layout.spread-plan`（ページ → 見開き） | —（クライアント専用） | `viewer/spread.js::rebuildSpreads` | `ReaderModel.swift::rebuildSpreads` | `ReaderWork.kt::rebuildSpreads` | 3 | `solo-spreads` ✓3 |
| 8 | `layout.chapter-padding`（章境界のスペーサ挿入） | — | `viewer/spread.js::padChapterSpacers` | `ReaderModel.swift::assemble`（pad 式） | `ReaderWork.kt::assemble` | 3 | `chapter-padding` ✓3 |
| 9 | `layout.page-mode`（viewport → single/spread） | — | `app.js` 内に**関数名なしで inline** | `ReaderModel.swift::pageMode(for:landscapeSpread:)` | `ReaderWork.kt::pageMode` | 3 | — |
| 10 | `layout.spread-anchor`（章内レンジ・スライダー写像） | — | `app.js::getChapterSpreadRange` | `ReaderModel.swift::chapterSpreadRange` / `chapterSlider*` | `ReaderWork.kt::chapterSpreadRange` / `chapterStartSpread` | 3 | — |
| 11 | `layout.viewport-fit`（見開きの寸法決定） | — | `viewer/spread.js::spreadMetrics` | `ReaderModel.swift::spreadFit` | `ReaderWork.kt::spreadFit` + `reader/ExtDisplay.kt` | 3 | — |
| 12 | `state.position-merge`（LWW + tombstone + 未来 ts 拒否） | `state.rs::Slot`（store から分離済み） | `progress/sync.js` の判断部 | `Reader/ProgressSync.swift` + `Library/ReadingProgressStore.swift` | `progress/ProgressBook.kt` | 4 | `positions-cases`（rust のみ） |
| 13 | `state.visibility-filter`（hidden roots の被覆判定） | `state.rs::covered_by_roots` | `browse/visibility.js::isHidden` / `hiddenRootFor` | `Visibility/VisibilityStore.swift::isHidden` | `visibility/VisibilityRules.kt::isHidden` | 4 | — |
| 14 | `offline.leaf-paths`（作品 → 葉フォルダ列挙） | — | `offline/storeAdapter.js`（`collectLeafPaths`） | `Offline/OfflineStore.swift` | `offline/OfflineRules.kt::leafPaths` / `leafMost` | 3 | — |
| 15 | `search.facet-filter`（クライアント側の絞り込み） | `search.rs::facets_for`（集計 = 別 op） | `search/facets.js::_visibleResults` | —（サーバ結果を表示） | `search/FacetRules.kt::nodeMatches` | 3 | — |
| 16 | `sync.canonical-json`（キー整列・区切り固定） | `reader_sync/catalog.rs::canonical` | `viewer/pairClient.js::canonicalJson` | `Sync/CanonicalJSON.swift` | `sync/CanonicalJson.kt` | 4 | `reader-sync-cases`（部分） |
| 17 | `sync.manifest-digest`（content keys の sha256） | `catalog.rs::manifest_digest` | `pairClient.js::manifestDigestHex` | `CanonicalJSON.swift::manifestDigest` | `CanonicalJson.kt` | 4 | `reader-sync-cases`（部分） |
| 18 | `sync.frame-validate`（frame 検証・エラーコード） | `reader_sync/protocol.rs` | `pairModel.js` / `pairClient.js` | `Sync/PairModels.swift` | `sync/PairModels.kt` + `PairProto` | 4 | `reader-sync-cases`（部分） |
| | **合計** | | | | | **60** | **8/18 規則** |

凡例: ✓4 = 4 ポートすべてが同じベクタを読む。✓3 = 3 ポート。`部分` = 段駆動 golden の一部のみ。

### 集計

- 18 規則 / **60 実装**（規則 × ランタイム）。1 規則あたり 1〜4 ポート。
- 実行可能なケースがあるのは **8/18 規則**。4 ポート全部が揃うのは **3 規則**（`path-lookup` / `work-chapters` / `encode-path`）。
- クライアント専用（サーバに実装が無い）規則が 6 つ（`layout.*` の 5 つ + `offline.leaf-paths`）。
  これらは**サーバの契約テストでは守れない**。ベクタ以外に守る手段が無い。

## 3. カーネルに入れない規則（意図的除外）

| 規則 | 現状の実装 | 除外理由 |
|---|---|---|
| `offline.cache-name`（Cache Storage 名 / ディレクトリ名） | `offline-core.js::cacheNameFor` / `OfflineStore` / `ScopedFiles.segment` | プラットフォームのストレージ識別子。規則ではなく実装詳細。**共有すると各プラットフォームの移行を縛る** |
| HTTP リトライ / 202 Retry-After / 304 判断 | `api/v1.js::fetchWithBuildingRetry` / `BookqAPI` / `BookqApi.kt` | I/O とスケジューリング。wire 契約（`v1-semantics.md`）が既に統治 |
| ジェスチャ閾値・アニメーション時間 | `viewer/gesture.js` / `ReaderView.swift` `PageTurnMath` / `ZoomGestures.kt` | 知覚とデバイス特性の関数。共有すると体験の調整が不可能になる |
| タップゾーン比率 | `zoneActionAt`（js）/ `zoneAction`（swift/kotlin） | UI 契約。3 実装で一致しているが、カーネル化の便益より調整自由度が高い |

この 4 種を除いたものがカーネル対象である、という境界線自体を `api/domain/README.md` に明記する。

## 4. 契約の分断（現状の実測）

| 症状 | 実測 |
|---|---|
| 「宣言の正本」がテストコード | `tests/test_vectors_declaration.py` の `EXPECTED` / `READERS` dict。言語追加 = Python 編集。Rust は `include_str!` でパス直書き |
| 同じ golden が 2 つ | `api/reader-sync-cases.json`（31 KB, step 駆動; rust + python + `scripts/api/ws_mock.py` が読む）と `tests/fixtures/reader_sync_cases.json`（15 KB, 関数別; iOS だけが読む）。**内容も形式も別物** |
| iOS にだけ無い drift guard | Android には `PairSpecTest.kt`（`api/asyncapi.yaml` と `PairProto` の照合）がある。iOS には asyncapi 参照が 0 件 |
| 部分的な言語宣言 | `positions-cases.json` は `["rust"]` のみ。クライアントは LWW 判断を持つ（#12）が検証されない |
| ベクタの op が未定義 | `format: 1` には「どの関数を、どの向きで呼ぶか」が無い。結果、JS は関数を抽出し、Rust は復号側で**逆算**し、Kotlin はモデルへ写像する — 三者三様の解釈 |

## 5. 「分離はされているが純粋ではない」例

Phase 1 で最初に解消すべきパターン。

- `web/static/search/facets.js` は独立ファイルだが、入力ではなく `window.activeFilters` / `window.facets` /
  `window.searchResults` を直接読む（`_getActiveFilters` 等のアクセサ経由）。**関数として切り出されているのに
  テスト可能ではない**ため、`app.js` 内定義と同じ扱いになっている。
- `web/static/offline-core.js` は Service Worker（`importScripts`）とページの両方から読まれる
  二重用途モジュール。SW 用のキャッシュ名生成と、純粋なパス符号化が同居する。
- `rust/bookq-core` は「純粋」を意図しているが、`image` / `lopdf` / `pdfium-render` / `unrar` に依存する。
  規則の純粋性を**依存グラフで**検証するには、規則専用のクレートが要る（`bookq-domain`）。

## 6. 意図的な差異（コメントでしか存在しない仕様）

| 差異 | 現在の記録場所 | カーネル化後 |
|---|---|---|
| iOS は章跨ぎ操作を許可、web/Android は章内 clamp | `docs/ios-app.md` + 各実装コメント | `ChapterPolicy.crossChapterTurns` 入力 |
| iOS は sibling autoload の depth gate を落とす | `ReaderWork.kt` のコメント「iOS drops that gate. Web wins.」 | `ChapterPolicy.siblingAutoloadMinDepth` 入力 |
| Android の `beyondViewportPageCount` は A/B 調整値 | `docs/android-app.md` | カーネル対象外（UI） |
| iOS のスワイプ/キー章跨ぎ | `docs/ios-app.md` | `ChapterPolicy` 入力 |

## 7. 進捗表（Phase 0 時点: すべて未移行）

| port | カーネル | 移行済み規則 / ports | 適合テストの実行環境 |
|---|---|---|---|
| rust | `rust/bookq-domain`（未作成） | 0 / 12 | `cargo test -p bookq-domain`（未作成） |
| js | `web/static/domain/`（未作成） | 0 / 16 | `node:vm` harness（未作成） |
| swift | `ios/Domain`（未作成） | 0 / 15 | `swift test` on macOS（未作成） |
| kotlin | `android/core`（既存・整理のみ） | 0 / 16（物理的には `:core` に存在） | `:core:test`（既存） |

更新ルール: 規則をカーネルへ移した PR は、**この表を同じ PR で更新する**（§2 の該当行には移設先のシンボルを追記）。
表が更新されていない移動はレビューで差し戻す（進捗の唯一の可視化であるため）。
