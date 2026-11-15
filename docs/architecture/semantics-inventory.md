# 意味論インベントリ — 規則 × ランタイム棚卸し（統合版）

Baseline: `f4d8ff7` (origin/main, 2026-11-15) ·
関連: `docs/architecture/semantics-contract.md` · `docs/adr/0002-server-resolved-work-model.md` · `docs/adr/0003-ondevice-semantics-kernel.md`

本書は**唯一の台帳**である。規則がサーバ解決（ADR 0002）に移ったか、端末カーネル（ADR 0003）に
入ったかは、ここの `移管先` 列だけで判断できる。進捗の可視化もここで行う。

## 1. 計測方法

- 対象はランタイム（`rust/viewer-rs` + `rust/bookq-core` / `web/static` / `ios/Bookq` / `android/core`+`:data`+`:app`）。
- 「ports」= **その規則（同じ入力 → 同じ出力の op）を独立に実装しているランタイム数**（1〜4）。
  同一ランタイム内の複数関数は 1 と数える。
- 「合計 59」= 規則 × ランタイムの実装数（`Σ ports`）。
- 「移管先」= `server`（ADR 0002 が吸収）/ `device`（ADR 0003 のカーネル）/ `既にサーバのみ`。
- 「ケース」= `api/vectors/*.json` または `api/reader-sync-cases.json` に既に固定されているか。
- 行番号は書かない（ドリフトするため）。`file` + シンボル名で追跡する。
- 再計測: `grep -rn "<symbol>" rust web/static ios android` と本書の表を突き合わせる。

移管は実装の**総数を減らす**（サーバ解決）か、**場所を変える**（カーネル）かのどちらかである。
前者は 0002、後者は 0003 が所有する。

## 2. 棚卸し（19 規則 / 59 実装）

| # | rule id | rust | js | swift | kotlin | ports | 移管先 | 既存ケース |
|---|---|---|---|---|---|---|---|---|
| 1 | `tree.flatten`（単一子フォルダの親への hoist） | `viewer-rs/src/tree.rs`（scan/assemble） | —（`name` に `/` が現れる契約を消費） | — | — | 1 | 既にサーバのみ | — |
| 2 | `tree.lookup`（path → node） | `tree.rs::find_tree_node` / `catalog.rs::find_container` | `app.js::findNodeByPath` / `findParentOf` | `Browse/BrowseModel.swift::node(at:in:)` / `LibraryStore::node(at:)` | `browse/BrowseRules.kt::nodeAt` | 4 | **server** (0002) | `path-lookup` ✓4 |
| 3 | `tree.siblings`（兄弟葉フォルダ列挙） | `catalog.rs::sibling_leaf_folders` | `app.js::getSiblingLeafFolders` | `ReaderModel.swift::siblingLeaves` | `ReaderWork.kt::siblingLeaves` | 4 | **server** (0002) | `work-chapters` ✓4 |
| 4 | `work.chapters`（folder → 章列の選択） | `catalog.rs::chapter_pages`（+ `conformance_vectors.rs` に再実装） | `app.js` `loadImages` 内 | `ReaderModel.swift::chapters(for:in:)` | `ReaderWork.kt::chapters` | 4 | **server** (0002) | `work-chapters` ✓4 |
| 5 | `work.page-order`（自然順 + cover 先頭） | `bookq-core/src/scans.rs::natural_cmp` / `sort_file_cmp` | —（サーバ順を信頼） | — | — | 1 | 既にサーバのみ | — |
| 6 | `path.encode`（セグメント単位 percent-encode） | `thumbs.rs::unquote`（復号側） | `offline-core.js::encodePath` | `API/ImageURL.swift::encodePath` | `api/ImagePath.kt::encodePath` | 4 | **server** (0002) | `encode-path` ✓4 |
| 7 | `layout.spread-plan`（ページ → 見開き） | —（クライアント専用） | `viewer/spread.js::rebuildSpreads` | `ReaderModel.swift::rebuildSpreads` | `ReaderWork.kt::rebuildSpreads` | 3 | **server**（§2.4 が Go のとき） | `solo-spreads` ✓3 |
| 8 | `layout.chapter-padding`（章境界のスペーサ挿入） | — | `viewer/spread.js::padChapterSpacers` | `ReaderModel.swift::assemble`（pad 式） | `ReaderWork.kt::assemble` | 3 | **server**（§2.4 が Go のとき） | `chapter-padding` ✓3 |
| 9 | `layout.page-mode`（viewport → single/spread） | — | `app.js` 内に**関数名なしで inline** | `ReaderModel.swift::pageMode(for:landscapeSpread:)` | `ReaderWork.kt::pageMode` | 3 | device | — |
| 10 | `layout.spread-anchor`（章内レンジ・スライダー写像） | — | `app.js::getChapterSpreadRange` | `ReaderModel.swift::chapterSpreadRange` / `chapterSlider*` | `ReaderWork.kt::chapterSpreadRange` / `chapterStartSpread` | 3 | device | — |
| 11 | `layout.viewport-fit`（見開きの寸法決定） | — | `viewer/spread.js::spreadMetrics` | `ReaderModel.swift::spreadFit` | `ReaderWork.kt::spreadFit` + `reader/ExtDisplay.kt` | 3 | device | — |
| 12 | `state.position-merge`（LWW + tombstone + 未来 ts 拒否） | `state.rs::Slot`（store から分離済み） | `progress/sync.js` の判断部 | `Reader/ProgressSync.swift` + `Library/ReadingProgressStore.swift` | `progress/ProgressBook.kt` | 4 | device | `positions-cases`（rust のみ） |
| 13 | `state.visibility-filter`（hidden roots の被覆判定） | `state.rs::covered_by_roots` | `browse/visibility.js::isHidden` / `hiddenRootFor` | `Visibility/VisibilityStore.swift::isHidden` | `visibility/VisibilityRules.kt::isHidden` | 4 | **server** (0002) | — |
| 14 | `offline.leaf-paths`（作品 → 葉フォルダ列挙） | — | `offline/storeAdapter.js`（`collectLeafPaths`） | `Offline/OfflineStore.swift` | `offline/OfflineRules.kt::leafPaths` / `leafMost` | 3 | **server** (0002) | — |
| 15 | `search.facet-filter`（クライアント側の絞り込み） | —（集計は #19） | `search/facets.js::_visibleResults` | —（サーバ結果を表示） | `search/FacetRules.kt::nodeMatches` | 2 | device | — |
| 16 | `sync.canonical-json`（キー整列・区切り固定） | `reader_sync/catalog.rs::canonical` | `viewer/pairClient.js::canonicalJson` | `Sync/CanonicalJSON.swift` | `sync/CanonicalJson.kt` | 4 | device | `reader-sync-cases`（部分） |
| 17 | `sync.manifest-digest`（content keys の sha256） | `catalog.rs::manifest_digest` | `pairClient.js::manifestDigestHex` | `CanonicalJSON.swift::manifestDigest` | `CanonicalJson.kt` | 4 | device | `reader-sync-cases`（部分） |
| 18 | `sync.frame-validate`（frame 検証・エラーコード） | `reader_sync/protocol.rs` | `pairModel.js` / `pairClient.js` | `Sync/PairModels.swift` | `sync/PairModels.kt` + `PairProto` | 4 | device | `reader-sync-cases`（部分） |
| 19 | `search.facet-aggregate`（ファセット索引の集計） | `search.rs::facets_for` | — | — | — | 1 | 既にサーバのみ | — |
| | **合計** | | | | | **59** | server 32 / device 27 | 10/19 規則 |

凡例: ✓4 = 4 ポートすべてが同じベクタを読む。✓3 = 3 ポート。`部分` = 段駆動 golden の一部のみ。

### 集計

- **19 規則 / 59 実装**（規則 × ランタイム）。1 規則あたり 1〜4 ポート。
- 移管先の内訳: **server 32**（うち 3 は既にサーバのみ）/ **device 27**。
- 実行可能なケースがあるのは **10/19 規則**。2 ポート以上が同じベクタを読む
  （＝クロス言語）のは **6 規則**（`path-lookup` / `work-chapters` / `encode-path` /
  `solo-spreads` / `chapter-padding` と、`work-chapters` を共有する 2 行）。
- **device 担当 8 規則のうち、ケースがあるのは 4 つだけ**（`positions-cases` は rust のみ、
  `reader-sync-cases` は rust + python で、**端末ポートを検証していない**）。
  残り 4 つ（`layout.page-mode` / `spread-anchor` / `viewport-fit` / `search.facet-filter`）は
  ケースが皆無で、目で揃えている。
- サーバに実装が無い（＝サーバの契約テストでは守れない）規則が 7 つ。

### 統合後の見通し

| 指標 | 現在 | 0002 完了（§2.4 = Go） | §2.4 = No-Go |
|---|---|---|---|
| 実装数（規則 × ランタイム） | 59 | **34** | **40** |
| うちクライアント側 | 47 | **23** | 29 |
| うちサーバ側 | 12 | 11 | 11 |
| 端末カーネル（0003）が担う実装 | 0 | 23 | 29 |
| 2 ポート以上が読むベクタを持つ規則 | 6 | **0** | 2（規則 7・8） |

No-Go のときは規則 7・8 の `移管先` を `device` に変えるだけで、計画の他の部分は変わらない。

## 3. カーネルに入れない規則（意図的除外）

| 規則 | 現状の実装 | 除外理由 |
|---|---|---|
| `offline.cache-name`（Cache Storage 名 / ディレクトリ名） | `offline-core.js::cacheNameFor` / `OfflineStore` / `ScopedFiles.segment` | プラットフォームのストレージ識別子。規則ではなく実装詳細。共有すると各プラットフォームの移行を縛る |
| HTTP リトライ / 202 Retry-After / 304 判断 | `api/v1.js::fetchWithBuildingRetry` / `BookqAPI` / `BookqApi.kt` | I/O とスケジューリング。wire 契約（`v1-semantics.md`）が既に統治 |
| ジェスチャ閾値・アニメーション時間 | `viewer/gesture.js` / `ReaderView.swift` `PageTurnMath` / `ZoomGestures.kt` | 知覚とデバイス特性の関数。共有すると体験の調整が不可能になる |
| タップゾーン比率 | `zoneActionAt`（js）/ `zoneAction`（swift/kotlin） | UI 契約。3 実装で一致しているが、カーネル化の便益より調整自由度が高い |

この 4 種を除いたものが対象である、という境界線自体を `api/domain/README.md` に明記する。

## 4. 契約の分断（現状の実測）

| 症状 | 実測 |
|---|---|
| 「宣言の正本」がテストコード | `tests/test_vectors_declaration.py` の `EXPECTED` / `READERS` dict。言語追加 = Python 編集。Rust は `include_str!` でパス直書き |
| 同じ golden が 2 つ | `api/reader-sync-cases.json`（31 KB, step 駆動; rust + python + `scripts/api/ws_mock.py`）と `tests/fixtures/reader_sync_cases.json`（15 KB, 関数別; iOS だけ）。**内容も形式も別物** |
| iOS にだけ無い drift guard | Android には `PairSpecTest.kt`（`api/asyncapi.yaml` と `PairProto` の照合）がある。iOS には asyncapi 参照が 0 件 |
| 部分的な言語宣言 | `positions-cases.json` は `["rust"]` のみ。クライアントは LWW 判断を持つ（#12）が検証されない |
| ベクタの op が未定義 | `format: 1` には「どの関数を、どの向きで呼ぶか」が無い。結果、JS は関数を抽出し、Rust は復号側で**逆算**し、Kotlin はモデルへ写像する — 三者三様の解釈 |

## 5. 「分離はされているが純粋ではない」例

| 例 | 内容 |
|---|---|
| `web/static/search/facets.js` | 独立ファイルだが、入力ではなく `window.activeFilters` / `window.facets` / `window.searchResults` を直接読む。関数として切り出されているのにテスト可能ではない |
| `web/static/offline-core.js` | Service Worker（`importScripts`）とページの両方から読まれる二重用途モジュール。SW 用のキャッシュ名生成と、純粋なパス符号化が同居する |
| `rust/bookq-core` | 「純粋」を意図しているが `image` / `lopdf` / `pdfium-render` / `unrar` に依存する。規則の純粋性を依存グラフで検証するには規則専用の境界が要る（本統合では `viewer-rs/src/domain/` + clippy で代替） |

## 6. 意図的な差異（コメントでしか存在しない仕様）

| 差異 | 現在の記録場所 | 統合後 |
|---|---|---|
| iOS は章跨ぎ操作を許可、web/Android は章内 clamp | `docs/ios-app.md` + 各実装コメント | `ChapterPolicy.crossChapterTurns` 入力 |
| iOS は sibling autoload の depth gate を落とす | `ReaderWork.kt` のコメント「iOS drops that gate. Web wins.」 | `ChapterPolicy.siblingAutoloadMinDepth` 入力 |
| Android の `beyondViewportPageCount` は A/B 調整値 | `docs/android-app.md` | 対象外（UI） |
| iOS のスワイプ/キー章跨ぎ | `docs/ios-app.md` | `ChapterPolicy` 入力 |

## 7. 進捗表

### 7.1 サーバ解決トラック（ADR 0002）

| 段階 | 対象規則 | 状態 | 出口 |
|---|---|---|---|
| A1 §2.4 判定 | 7, 8 | 未実施（仮説） | `solo-spreads` / `chapter-padding` に `rust` を足して全件一致を確認 |
| B-1 サーバドメイン層 | 2, 3, 4, 14 | 未着手 | `domain::work` / `domain::library` が MANIFEST と REST の共通経路になる |
| B-2 web | 2, 3, 4, 6, 14 | 未着手 | `loadImages` / `downloadWork` が Work 経由になる |
| B-3 iOS / Android | 2, 3, 4, 14 | 未着手 | `ReaderWork.chapters` / `siblingLeaves` が消える |
| B-4 解決済みツリー + 可視性 | 2, 6, 13 | 未着手 | path 合成・表紙解決・可視性述語がクライアントから消える |
| B-5 見開き（Go のときのみ） | 7, 8 | 未着手 | `rebuildSpreads` / `padChapterSpacers` / `assemble` が消える |

完了の定義: `manifest.json` の `resolution` が `server` に変わり、`ports` からクライアントが消え、
G5（resolution-drift）が緑。

### 7.2 端末カーネルトラック（ADR 0003）

| port | カーネル | 移行済み / device 担当 | 適合テストの実行環境 |
|---|---|---|---|
| rust | `viewer-rs/src/domain/`（0002 Phase 1 が作成） | 0 / 4 | `cargo test`（サーバ起動なし） |
| js | `web/static/domain/` | 0 / 8 | `node:vm` harness |
| swift | `ios/Domain`（SwiftPM） | 0 / 7 | `swift test` on macOS |
| kotlin | `android/core`（既存・整理のみ） | 0 / 8 | `:core:test`（既存） |

更新ルール: 規則を移した PR は**この表と §2 の該当行を同じ PR で更新する**。
表が更新されていない移管はレビューで差し戻す（進捗の唯一の可視化であるため）。
