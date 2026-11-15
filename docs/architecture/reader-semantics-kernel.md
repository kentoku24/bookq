# リーダー意味論カーネル — 設計提案 (reader semantics kernel)

Status: **Proposal** (implementation 未着手 / code change なし) · Baseline: `f4d8ff7` · 2026-11-15
関連: `docs/adr/0002-reader-semantics-kernel.md` · `docs/architecture/semantics-inventory.md`

---

## 要旨

bookq の難しさは、画像を配ることも、ページを捲ることも、ましてや UI を組むことでもない。
**ライブラリの木をリーダーの状態に変換する規則**——章の切り方、見開きの組み方、パスの符号化、
読書位置の LWW、隠しルートの判定——が難しい。そしてこの規則だけが、いま**どこにも属していない**。

4 つのランタイム（Rust / ブラウザ JS / Swift / Kotlin）が同じ規則をそれぞれ実装し、
その正本は `web/static/app.js` の中（5,206 行の IIFE の、レンダリング関数の合間）にある。
他の 3 言語はそれを名指しで参照し、151 個の「parity」コメントで追跡している。

本提案は、この規則群を**バージョン付きの言語非依存契約**と**ランタイムごとに 1 つの純粋カーネルモジュール**に移し、
HTTP 層・DOM 層・UI 層・同期層をカーネルに依存させる。契約は散文ではなく**実行可能なケース**で定義し、
CI が「全ポートが同じ規則を、本番の入口で、同じ期待値で実装している」ことを検証する。

これは新しい抽象ではなく、**既に 2 箇所で成功しているパターンの一般化**である:

- `api/vectors/*.json` + 4 言語の適合テスト（#280 / #441 / #459 / #460）— 既に drift を捕まえている。
- `android/core`（3,875 行、Android SDK 非依存の純 Kotlin JVM、JUnit で完結）— カーネル化が成立する証明。

---

## 1. 現状の構造

| 層 | 実体 | 規模 |
|---|---|---|
| サーバ | `rust/viewer-rs`（axum）+ `rust/bookq-core`（archive/pdf/meta/scans） | 21,787 行 |
| Web クライアント | `web/static/*.js` ビルドなし classic script、`app.js` が composition root | 19,447 行（vendor 除く js/css/html）+ `app.js` 単体 5,206 行 |
| iOS | `ios/Bookq`（SwiftUI, 依存ゼロ, Xcode 3 target） | 13,768 行（テスト込み .swift） |
| Android | `:core`（純 JVM）/ `:data` / `:app` | 20,853 行（テスト込み .kt） |
| 契約 | `api/openapi.v1.yaml` + `api/asyncapi.yaml` + `api/v1-semantics.md` + `api/vectors/*` | — |
| テスト | pytest 1,027 関数（ブラウザ fixture 511）/ 85 の `test_*.py` モジュール | — |

依存の実態:

```
  API wire shape  ─── openapi.v1.yaml ──┬─→ api-types.d.ts (web, generated)
        (強い)                          ├─→ SpecConformanceTests (iOS, test-only)
                                        ├─→ SpecConformanceTest (Android, test-only)
                                        └─→ Schemathesis + breaking-check (rust)

  意味論 (弱い)   ─── ？？？ ─────────────┬─→ rust: tree.rs / reader_sync::catalog / state.rs
                                        ├─→ js:   app.js ← ← ← ここが事実上の正本
                                        ├─→ swift: ReaderModel.swift  ("byte-faithful to app.js")
                                        └─→ kotlin: ReaderWork.kt    ("byte-faithful to iOS … Web wins")
```

wire shape の統治は模範的である（生成・drift gate・契約テスト・breaking check）。
その 1 段下——**形に表れない規則**——には、所有者も版も、機械可読な正本も無い。

---

## 2. 診断: 意味論が漏れ出す 5 つの仕方

**F1. 仕様の所在がプレゼンテーション層の中。**
`ios/Bookq/Reader/ReaderModel.swift` の冒頭は「`web/static/app.js` の `loadImages` と
`web/static/viewer/spread.js` に byte-faithful」と宣言する。`SIBLING_AUTOLOAD_MIN_DEPTH = 2` は
`app.js` / `reader_sync/catalog.rs` / `ReaderModel.swift` / `ReaderWork.kt` の 4 箇所に独立して存在する。
規則を変えるには、まず `app.js` を読み、次に 3 言語の移植先を grep で探す。

**F2. 移植の連鎖。**
`android/core/sync/PairModels.kt`: 「`ios/Bookq/Sync/PairModels.swift` の移植（それは
`app/reader_sync/{protocol,model}.py` + `viewer/pairModel.js` の移植）」。
参照元が既に消えた Python を経由した 4 段の写経になり、各段で「byte-faithful」が主張される。

**F3. 置き場所が事故で決まる。**
- Rust: ツリー述語（`has_only_images` / `has_images`）と章選択（`chapter_pages` / `sibling_leaf_folders`）が
  `viewer-rs/src/reader_sync/catalog.rs` = **WebSocket ペアリング機能のモジュール**にある。
  単独ビューアも同じ規則を使うが、置き場所はペアリング機能の都合で決まった。
- JS: `findNodeByPath` / `getParentPath` / `findParentOf` / `getSiblingLeafFolders` が
  タイル描画関数の合間（`app.js`）に埋まっている。パス符号化は `offline-core.js`（SW 用）が持つ。

**F4. 契約の強制が模倣で行われる（3 つの症状）。**

| 症状 | 場所 | 内容 |
|---|---|---|
| ソースからの関数抽出 | `tests/test_conformance_vectors_js.py` | `app.js` をブレースマッチで切り出して `eval`。失敗メッセージ自身が「`catalog/rules.js` に移したなら harness を直せ」と書く |
| テスト内での再実装 | `rust/viewer-rs/src/conformance_vectors.rs` | 本番の章解決器を呼ばず、`resolve_chapters` / `chapter_pages` をテスト側に再実装 |
| 私的な縮小 fixture | `ios/BookqTests/PairSyncTests.swift` | 正準 `api/reader-sync-cases.json`（31 KB, step 駆動）ではなく `tests/fixtures/reader_sync_cases.json`（15 KB, 関数別）を読む。メタテストの統治外 |

さらに `tests/test_vectors_declaration.py` は「どの言語がどのベクタを読むか」を **Python の dict** で保持する。
言語を 1 つ足すには Python テストを編集する必要があり、Rust 側は `include_str!` でパスを直書きしている。

**F5. 純粋規則のテストがブラウザ・サーバ・シミュレータを要求する。**
pytest 1,027 関数中 511 がブラウザ fixture を取る。PR あたり Safari 8 shard（60 分 timeout × 8 = 最大 480 runner-min）+
chromium 2 lane。`solo-spreads` / `chapter-padding` / `encode-path` は**純粋関数**だが、
検証には Playwright が要る。Android は同じ規則を JUnit で 1 秒未満で検証している。

**帰結**: 規則の変更コストが「4 実装 + 1 dict + ベクタ + ブラウザ検証」に比例し、
正しさの根拠が「他言語のソースを読んだ」になる。クライアントが増えるほどこれは悪化する。

---

## 3. 目標アーキテクチャ

### 3.1 層と依存方向

```
                    ┌──────────────────────────────────────────────────┐
   outer rings      │  axum handlers │ DOM/views │ SwiftUI │ Compose     │
   (I/O, 状態機械)   │  HTTP, SW, WS, ジェスチャ, アニメーション          │
                    └───────────────┬──────────────────────────────────┘
                                    │ 下にだけ依存する
                    ┌───────────────▼──────────────────────────────────┐
   kernel           │  rust/bookq-domain │ web/static/domain           │
   (純粋・同期・総関数) │  ios/Domain        │ android/core               │
                    └───────────────┬──────────────────────────────────┘
                                    │ 入力型だけを知る
                    ┌───────────────▼──────────────────────────────────┐
   contract         │  api/domain/v1: 正準マニフェスト + ルール + ケース │
   (言語非依存の正本)  │  + 各ポートの domain.manifest.json               │
                    └──────────────────────────────────────────────────┘
```

**依存規則（レビューで守る唯一の規則）**: カーネルは外側を知らない。外側はカーネルを知ってよい。
カーネル同士は互いを知らない（共有は契約のみ）。

### 3.2 契約 — `api/domain/v1/`

```
api/domain/
  README.md                 # 契約の統治: ID 規約・版・変更手順・所有者
  CHANGELOG.md              # ルールごとの版履歴（1 行/版）
  v1/
    manifest.json           # 正準: rule id → {file, version, op, ports, owner, status}
    rules/
      work.chapters.json
      layout.spread-plan.json
      path.encode.json
      ...                   # Phase 0 では api/vectors/*.json を path 参照で登録（移動しない）
```

ルールファイル（`format: 2`。既存 `format: 1` は Phase 2 まで有効）:

```json
{
  "rule": "layout.chapter-padding",
  "version": 1,
  "format": 2,
  "status": "current",
  "title": "Pad a chapter boundary with spacer slots so a spread never straddles two chapters",
  "spec": "docs/architecture/rules/layout.chapter-padding.md",
  "owner": "reader",
  "op": {
    "name": "padChapterSpacers",
    "inputs": [
      {"name": "images", "type": "PageRef[]"},
      {"name": "boundaries", "type": "int[]"},
      {"name": "pageMode", "type": "'single' | 'spread'"},
      {"name": "spreadOffset", "type": "0 | 1"}
    ],
    "output": "PageRef[]"
  },
  "ports": ["js", "swift", "kotlin"],
  "cases": [
    {"name": "odd chapter, offset 0", "input": {"...": "..."}, "expect": {"...": "..."}}
  ]
}
```

ポートマニフェスト（各クライアントツリーに 1 枚。以下は `web/domain.manifest.json` の例）:

```jsonc
{
  "port": "js",
  "contract": "api/domain/v1",
  "module": "web/static/domain",
  "harness": "tests/domain/test_domain_js.py",
  "rules": {
    "layout.chapter-padding": {"version": 1, "symbol": "BookqDomain.padChapterSpacers"},
    "path.encode":           {"version": 1, "symbol": "BookqDomain.encodePath"},
    "work.chapters":         {"version": 1, "symbol": "BookqDomain.chapters"}
  }
}
```

これで「宣言の正本」が Python dict からデータに移り、言語追加は**マニフェスト 1 枚**になる。
`tests/test_vectors_declaration.py` は言語非依存の汎用チェッカ（`scripts/domain_check.py`）へ置き換える。

### 3.3 規則のカタログ（18 規則 / 60 実装）

棚卸しの全体は `docs/architecture/semantics-inventory.md`。名前空間は 8 つ。

| namespace | 規則 | 例 |
|---|---|---|
| `tree.*` | flatten / lookup / parent / siblings | `tree.lookup`: path → node |
| `work.*` | chapters / page-order | `work.chapters`: folder → 章列 |
| `path.*` | encode | セグメント単位 percent-encode |
| `layout.*` | spread-plan / chapter-padding / page-mode / spread-anchor / viewport-fit | 見開きの組み方 |
| `state.*` | position-merge / visibility-filter | LWW + tombstone / 隠しルート |
| `offline.*` | leaf-paths | 作品の葉フォルダ列挙 |
| `search.*` | facet-filter | クライアント側の絞り込み |
| `sync.*` | canonical-json / manifest-digest / frame-validate | ペアリング |

**カーネルに入れないもの**（重要）: `offline.cache-name`（Cache Storage / ファイルシステムの名前付けは
プラットフォーム識別子であり、規則ではなく実装詳細）、HTTP/リトライ、DOM 座標、ジェスチャ閾値、
アニメーション時間。これらは外側に残す。

### 3.4 各ランタイムのカーネル

| ポート | ホーム | 移すもの | 純粋性の強制 |
|---|---|---|---|
| rust | `rust/bookq-domain`（新クレート） | `tree.rs` の述語・探索、`reader_sync::catalog` のツリー述語と章選択、`thumbs.rs` の path 復号 / `serve.rs` の containment 判定、`state.rs` の LWW 判断、`sync` の canonical JSON / digest | 依存グラフ = `serde` のみ。`std::fs`/`net`/`time`/`process` を clippy `disallowed_paths` で禁止 |
| js | `web/static/domain/*.js` (`window.BookqDomain`) | `offline-core.js` の `encodePath`、`viewer/spread.js` の組み方、`app.js` の lookup/parent/sibling/章選択、`progress/sync.js` の LWW 判断、`browse/visibility.js` の `isHidden`、`pairClient.js` の canonical JSON / sha256 | 適合 harness は `node:vm` の**まっさらな context** で実行（`window`/`document`/`fetch`/`localStorage` なし）。動けば純粋、動かなければ設計ミス |
| swift | `ios/Domain`（ローカル SwiftPM パッケージ） | `ReaderModel` の `ReaderWork` 数学、`ImageURL`、`VisibilityStore.isHidden`、`ProgressSync` の判断、`CanonicalJSON` | パッケージは Foundation のみ import。`swift test` を macOS destination で実行（シミュレータ不要） |
| kotlin | `android/core`（既存） | 既に `:core` にある（`ReaderWork`, `ImagePath`, `VisibilityRules`, `ProgressBook`, `CanonicalJson`, `OfflineRules`）。`domain/` パッケージへ整理し、rule id と対応づける | `:core` は `:app`/`:data` に依存しない（Gradle で固定）。JUnit のみ |

**Rust の型に関する注意（唯一の難しい点）。** `viewer-rs` の `tree::Node` は
メモリ最適化のため interner + `Arc<str>` を持つ。カーネルに移すと `viewer-rs` のキャッシュ構造まで動く。
現実的な解は **2 段構え**:

1. Phase 2a: カーネルは**借用ビュー**（`TreeView<'a>`, `FolderView<'a>`）を受け取る純粋関数として定義し、
   `viewer-rs` の `Node` から `TreeView` への変換を `viewer-rs` 側に置く（規則だけ移す）。
2. Phase 2b（任意）: 型そのものをカーネルへ移し、`viewer-rs` は再エクスポートする。
   2b は性能検証つきの別 PR とし、必須にしない。

この「型は残す / 規則だけ移す」分割により、カーネル化の便益（規則の単一化）を
性能リスク（キャッシュ構造の改変）と切り離せる。

### 3.5 強制ゲート（CI）

| gate | 何を保証するか | 実装 | コスト |
|---|---|---|---|
| **G1 domain-contract** | 全ルールが正準マニフェストに載り、全ポートが同版を宣言し、ケースが版を跨いで書き換えられていない | `scripts/domain_check.py`（純 Python、ブラウザ・サーバ不要） | 数秒 |
| **G2 domain-purity** | カーネルが IO を知らない | rust: `cargo tree` + clippy / js: `node:vm` の空 context / swift: import 走査 + `swift test` / kotlin: Gradle 依存固定 | 数十秒 |
| **G3 domain-anchor** | 適合は**本番の入口**を呼ぶ（ソース抽出・再実装・逆算の禁止） | js harness は `web/static/domain/**` 以外を load できない。`tests/` が `web/static/**` を読むことを lint で禁止（SHELL_ASSETS 検査のみ whitelist） | 数秒 |
| **G4 domain-single-copy** | 移した規則が外側に再宣言されない | マニフェストの `symbol` を外側で grep し、`domain/` 外の定義を fail | 数秒 |
| **G5 domain-cost** | コストの可視化 | PR コメント: kernel LOC / ルールごとのポート充足率 / ブラウザテスト数 | 数秒 |

G1 のケース改変検知は `casesDigest`（ケース JSON の正準化 sha256）を正準マニフェストに記録して行う。
**この 1 つが「リファクタ中に期待値を動かして緑にする」を機械的に封じる**ので、
Phase 1–3 の安全網として最初に入れる。

### 3.6 意図的な差異は「入力」にする

現状、クライアント差はコメントで宣言されている（例: 「iOS はユーザ要望で章跨ぎ操作を許可するが、
web/Android は章内 clamp」「iOS は sibling autoload の depth gate を落とす」）。
カーネル化でこれを吸収しようとすると、規則に `if (platform)` が入り最悪の設計になる。

代わりに、**差異を op の入力にする**:

```jsonc
"op": {
  "name": "chapters",
  "inputs": [
    {"name": "tree", "type": "TreeView"},
    {"name": "folder", "type": "string"},
    {"name": "policy", "type": "ChapterPolicy"}   // { siblingAutoloadMinDepth: int, crossChapterTurns: bool }
  ],
  "output": "ChapterList"
}
```

- iOS は `{siblingAutoloadMinDepth: 0, crossChapterTurns: true}`、web/Android は `{2, false}` を渡す。
- 差は**データ**になり、ベクタで両方の policy を固定できる（`cases[].input.policy`）。
- 差を増やすときは「policy のフィールド追加」であり、実装 fork ではない。

---

## 4. 移行計画

各 Phase は独立にマージ可能で、終了時にシステムは常に緑。**挙動変更は 0**。

### Phase 0 — 登録（挙動不変・コード移動なし）

- `api/domain/README.md` / `v1/manifest.json` を追加。既存 6 ベクタを**パス参照で登録**（移動しない）。
- 4 ポートに `domain.manifest.json` を追加（現時点の実装シンボルを正直に列挙）。
- `scripts/domain_check.py` + `domain-contract` workflow を追加。
- `tests/test_vectors_declaration.py` を `domain_check.py` の薄い呼び出しに置換。
- `api/vectors/*.json` に `rule` と `version` を追記（**ケース配列は 1 バイトも変えない**。`casesDigest` で証明）。

**Exit gate**: 既存テストが全て緑、`domain-contract` 緑、`git diff --stat` に `.rs/.js/.swift/.kt` が無い。
**Rollback**: 追加ファイルを戻すだけで完了（既存コードは未変更）。

### Phase 1 — Web カーネル（4 PR、規則ファミリ単位）

- `web/static/domain/{tree,path,work,layout,state}.js` を新設。classic script + `window.BookqDomain`（ビルドなし・defer 順は既存のまま）。
- 既存ファイルは**委譲シム**に変える（`app.js` が既に `SpreadView` に対してやっているのと同じ形）。
- `tests/domain/` に `node:vm` harness を新設し、`test_conformance_vectors_js.py` の 5 ブラウザテストを置換。
- `sw.js` の `SHELL_ASSETS` と `index.html` の defer 順に新ファイルを追加（現状 36 件の手書きリスト。Phase 4 で自動化）。

**Exit gate**: JS 適合テストが**ブラウザ 0・サーバ 0**で緑。`app.js` から規則が抜け、
移動行数を PR に記録する（見積り −400〜900 行、実測で確定）。`domain-single-copy` が
外側の重複定義を検出しない。

### Phase 2 — Rust カーネル（3 PR）

- 2a: `rust/bookq-domain` 新設（`serde` のみ）。借用ビュー + tree predicates + chapter selection + path + sync canonical/digest。
  `catalog.rs` から規則を抜き、`viewer-rs` は変換アダプタを持つ。
- 2b: `conformance_vectors.rs` を本番解決器呼び出しに書き換え、テスト内 `resolve_chapters` を削除。
- 2c: `state.rs` の LWW 判断を `PositionBook`（純粋）へ分離（`v1-semantics.md` が既にこの分離を記述している）。

**Exit gate**: `cargo tree -p bookq-domain` が `serde` 系のみ。`reader_sync/catalog.rs` から tree 述語が消える。
`cargo test -p bookq-domain` がベクタを実行（サーバ起動なし）。

### Phase 3 — モバイル・カーネル（3 PR）

- 3a: Android — `:core` を `domain/` パッケージへ整理し、rule id と対応づけ（挙動不変、移動のみ）。
- 3b: iOS — `ios/Domain`（ローカル SwiftPM パッケージ、Foundation のみ）を追加し、
  `ReaderWork` 数学 / `ImageURL` / `isHidden` / `CanonicalJSON` を移設。`Bookq` と `BookqTests` が依存。
  CI は `swift test`（macOS destination）でベクタを実行。
- 3c: iOS の私的 fixture（`tests/fixtures/reader_sync_cases.json`）を正準 `api/reader-sync-cases.json` に統合
  （差分が意味を持つなら「正準側へ case を移す」）。

**Exit gate**: 3 ランタイムの適合テストがシミュレータ・実機なしで走る。iOS `ReaderModel.swift` から
`pad = (startIdx - spreadOff + 2) % 2` が消え、`Domain.padChapterSpacers` を呼ぶ。

### Phase 4 — 外側を薄くする（カーネル化の元を取る）

- サーバ: ハンドラを `parse → kernel → serialize` に統一。`_or_proxy` / `native.enabled` gate /
  FastAPI のエラー形状パリティは**独立した ADR（0003）で撤去**する（カーネル化とは分離して実施）。
- Web: `SHELL_ASSETS` を `index.html` から生成（現在 36 件の手書きリストと script タグ 29 件が手で同期されており、
  片方にしか無いものが 1 件ずつある。いずれも意図的だが、その意図はコードで表明されていない）。
- テスト: 「DOM とソーステキスト」への assertion を規則レベルの assertion に置換。ブラウザテスト数の削減目標を設定。

### Phase 5 — 定常状態

- `api/domain/README.md` に変更手順を明文化: ①規則の散文定義 ②ケース追加 ③版判定 ④全ポート同時更新 ⑤CHANGELOG。
- 規則ごとに owner（`reader` / `browse` / `sync`）を決め、PR テンプレートに「touch した rule id」欄を追加。
- 四半期ごとに棚卸し表を剪定（実装サイト数が減っていることを数値で確認）。

---

## 5. 通しの例: `work.chapters`（章の切り方）

現状（4 実装 + 参照関係）:

| port | 場所 | 備考 |
|---|---|---|
| js | `web/static/app.js` `loadImages` 内 + `getSiblingLeafFolders` | **正本とされている** |
| rust | `reader_sync/catalog.rs` `sibling_leaf_folders` / `chapter_pages` | WS ペアリング機能の下にある |
| swift | `ReaderModel.swift` `static func chapters(for:in:)` | 「app.js に byte-faithful」 |
| kotlin | `ReaderWork.kt` | 「iOS に byte-faithful、差があれば web が勝つ」 |

移行後:

```rust
// rust/bookq-domain/src/work.rs
pub fn chapters(tree: &TreeView<'_>, folder: &str, policy: ChapterPolicy) -> ChapterList
```
```js
// web/static/domain/work.js
BookqDomain.chapters = function (tree, folder, policy) { /* … */ };
```
```swift
// ios/Domain/Sources/BookqDomain/Work.swift
public func chapters(tree: TreeView, folder: String, policy: ChapterPolicy) -> ChapterList
```
```kotlin
// android/core/…/domain/Work.kt
fun chapters(tree: TreeView, folder: String, policy: ChapterPolicy): ChapterList
```

ケース（`api/domain/v1/rules/work.chapters.json`）に 1 行追加するだけで、
4 実装すべてが同じ入力で検証される。`policy.siblingAutoloadMinDepth` の差は
「iOS 用ケース」として明示的に 2 本持つ（現在はコメントでしか存在しない）。

---

## 6. メトリクス

ベースラインは `f4d8ff7` 実測。目標は Phase 3 完了時点の**見積り**。

| 指標 | 現在 | 目標 | 計測方法 |
|---|---|---|---|
| 規則 × ランタイムの実装数 | 60（18 規則、1 規則あたり 1〜4 ポート） | 60（ただし全部がカーネル内） | `semantics-inventory.md` の表 |
| 規則の正本 | `app.js` + 151 の parity コメント | `api/domain/v1/*`（parity コメントは 40 以下） | grep |
| 純粋規則の検証にブラウザ/サーバ/シミュレータを要する数 | JS 5 テスト（Playwright）+ iOS シミュレータ | 0 | CI job の構成 |
| `app.js` 行数 | 5,206 | 4,000 未満 | `wc -l` |
| `rust/viewer-rs/src/main.rs` 行数 | 1,949 | 1,200 未満（Phase 4 後） | `wc -l` |
| ブラウザ fixture を取る pytest 関数 | 511 | 400 以下 | `scripts/domain_check.py --report` |
| 新しい規則を 1 つ足すコスト | 4 実装 + Python dict + ベクタ + ブラウザ検証 | 契約 1 ファイル + 4 カーネル関数（CI が漏れを列挙） | 手順の差分 |
| `touch した rule id` が PR に現れるか | 無い | 必須 | PR テンプレート |

数値目標は達成を約束するものではなく、**方向と検証可能性**を示すためのもの。
Phase ごとに実測して `docs/architecture/semantics-inventory.md` を更新する。

---

## 7. リスクと対策

| # | リスク | 対策 |
|---|---|---|
| R1 | 大規模移動による回帰 | 1 PR = 1 規則ファミリ。委譲シムを先に入れ、移動と挙動変更を同一 PR に混ぜない。Phase 0 の `casesDigest` で期待値の無断変更を封じる |
| R2 | カーネルへの IO 混入 | G2（`node:vm` 空 context / crate 依存グラフ / import 走査）は「動けば通る」ではなく「動かなければ落ちる」方向で設計。時計は引数、ユーザーは引数 |
| R3 | ベクタが仕様化してしまう（現状挙動が法になる） | ルールごとに散文定義（inputs/outputs/invariants/why）を必須にし、ケースは「ユーザーに見える筋」で正当化。規則追加は定義なしでは受理しない |
| R4 | iOS のターゲット分割で pbxproj が荒れる | 既に `PBXFileSystemSynchronizedRootGroup`（フォルダ自動同期）を使っている。ローカル SwiftPM パッケージなら pbxproj 変更は 1 回のリンク追加のみ |
| R5 | Web の defer 順・SW precache の破壊 | Phase 1 は classic script + 名前空間で据え置き、ESM 化は別判断。`SHELL_ASSETS` 追加漏れは existing のテスト（`test_frontend_perf_lifecycle.py` の SHELL_ASSETS 検査）が既に検出する |
| R6 | カーネル関数呼び出しの性能劣化（ページ毎） | 規則は既にページ毎に呼ばれている。移動で呼び出し回数は増えない。Phase 1 で `test_top_display_perf.py` の表示完了時間を before/after で記録 |
| R7 | 契約の官僚化（小変更に 5 ファイル） | 規則数を上限 20 に保つ。`status: "draft"` はポート義務なし。マニフェストは 1 ファイル 1 画面に収める |
| R8 | Rust の型移動がキャッシュ性能を壊す | Phase 2a（規則のみ、借用ビュー）を既定とし、型移動は 2b の任意作業に分離 |

---

## 8. やらないこと

- **ビルドステップの導入**（bundler / tsc emit / Webpack）。カーネルは素の JS のまま。
- **ネイティブ DTO の OpenAPI codegen**。wire shape の重複は別問題であり、別 ADR で扱う（本提案と直交）。
- **共有ランタイム（WASM）化**。純粋関数 18 規則には過剰。カーネル化後に再評価可能。
- **UI フレームワーク・同期プロトコル・デプロイの変更**。
- **`app.js` の全面 ESM 化**。Phase 1 では触らない。

---

## 9. 判断が必要な点

| # | 論点 | 推奨 |
|---|---|---|
| Q1 | 契約のホームは `api/domain/` か `spec/domain/` か | `api/domain/`（`api/` は既に契約の家） |
| Q2 | Rust は新クレート `bookq-domain` か `bookq-core` 内モジュールか | 新クレート。依存グラフで純粋性を機械検証できることが最大の利点 |
| Q3 | iOS はローカル SwiftPM パッケージか framework target か | SwiftPM（`swift test` がシミュレータ不要、pbxproj 変更が最小） |
| Q4 | Web は classic script 継続か ESM 移行か | Phase 1 は classic script。ESM は SW/defer 順の再設計を伴うため別判断 |
| Q5 | 規則の owner をどう切るか | `reader`（work/layout/state）/ `browse`（tree/path/search/offline）/ `sync`（sync.*）の 3 つ |
| Q6 | 最初に移す規則 | `path.encode`（最小・全ポート存在・既にベクタあり）。次に `work.chapters`（最も価値が高い） |

---

## 付録 A: 最初の 3 PR の具体的な形

1. **PR 1（Phase 0）** — `api/domain/v1/manifest.json` + 4 ポートの `domain.manifest.json` +
   `scripts/domain_check.py` + `.github/workflows/domain-contract.yml`。
   差分はほぼ追加のみで、既存ファイルの変更は `test_vectors_declaration.py` の置換と
   `api/vectors/*.json` へのメタ追記（`rule` / `version`）だけ。
2. **PR 2（Phase 1a）** — `web/static/domain/path.js`（`encodePath`）+ `offline-core.js` の委譲化 +
   `tests/domain/test_domain_js.py`（`node:vm`）+ JS の `encode-path` ブラウザテスト削除。
3. **PR 3（Phase 2a の一部）** — `rust/bookq-domain` クレート作成、`path` のみ実装、
   `thumbs::unquote` を委譲化、`cargo test -p bookq-domain` で `encode-path` を実行。

各 PR は「1 規則・全ポート・挙動不変」で、レビューは
「ケースが変わっていないこと（digest）」「外側に重複定義が無いこと（G4）」の 2 点に集中できる。

## 付録 B: なぜ「テストの高速化」が主目的ではないのか

ブラウザテストの削減は**副産物**であり、主目的は**正本の所在地**である。
ただし副産物も大きい: PR あたり Safari 8 shard（最大 480 runner-min）のうち、
純粋規則に関する部分が不要になれば、CI 待ち時間と flake 面が規則数に比例しなくなる。
順序は「正本を移す → テストが自然に軽くなる」であり、逆ではない。
