# ADR 0002: リーダー意味論カーネル (reader semantics kernel)

- **Status:** Proposed
- **Date:** 2026-11-15
- **Deciders:** bookq maintainer
- **Baseline:** `f4d8ff7` (origin/main, 2026-11-15)
- **Related:** ADR 0001, `api/openapi.v1.yaml`, `api/v1-semantics.md`, `api/domain/v1/` (new), `docs/architecture/reader-semantics-kernel.md`, `docs/architecture/semantics-inventory.md`, issues #280 / #441 / #459 / #460

## 要旨

bookq のリーダー意味論（章の切り方・見開きの組み方・パス符号化・読書位置の LWW・隠しルート判定）は
Rust / ブラウザ JS / Swift / Kotlin の 4 実装に分散し、**正本は `web/static/app.js` の巨大 IIFE の中**にある。
本 ADR は、これを**言語非依存の契約 (`api/domain/v1/`) + 各ランタイムの純粋カーネルモジュール**に昇格させ、
外側の層（HTTP ハンドラ・DOM・SwiftUI/Compose・同期ソケット）をカーネルに依存させることを提案する。

## Context

1. **仕様の所在が実装の内側にある。** iOS の `ReaderModel.swift` は冒頭で
   「`web/static/app.js` の `loadImages` と `viewer/spread.js` に byte-faithful」と宣言し、
   Android の `ReaderWork.kt` は「iOS `ReaderWork` に byte-faithful（差がある場合は web が勝つ）」と宣言する。
   ソース中の他クライアント実装への言及は iOS 26 行 / Android 85 行にのぼる。
   定数 `SIBLING_AUTOLOAD_MIN_DEPTH = 2` は 4 言語すべてに独立して存在する。
2. **契約の強制が模倣で行われている。** 「parity」「must match」「keep in sync」系のコメントは
   4 ランタイム合計 151 箇所。JS 適合テストは `app.js` から関数を**ブレースマッチで切り出して**実行し、
   Rust 適合テストは本番の章解決器を呼ばず**テスト内で再実装**している。
3. **意味論の置き場所が事故で決まっている。** ツリー述語と章選択は
   `rust/viewer-rs/src/reader_sync/catalog.rs`（WebSocket ペアリング機能のモジュール）にあり、
   JS の `findNodeByPath` / `getParentPath` / `findParentOf` / `getSiblingLeafFolders` は
   `app.js` のレンダリング関数の間に挟まっている。パス符号化は `offline-core.js`（SW 用ユーティリティ）が持つ。
4. **契約は 2 層に分かれ、上層だけが機械可読。** wire shape は `openapi.v1.yaml` + codegen drift gate +
   Schemathesis + breaking check で強く守られている。一方その下の「形に表れない意味論」は
   `api/v1-semantics.md`（散文）と `api/vectors/*.json`（6 本の非形式的ケース）だけで、
   バージョン・所有者・変更手順を持たない。宣言の正本は `tests/test_vectors_declaration.py` の Python dict。
5. **テストが純粋ロジックのためにブラウザとサーバを要求する。** pytest 1,027 テスト中 511 がブラウザ fixture を取り、
   PR あたり Safari 8 shard + chromium 2 lane。一方 `android/core`（3,875 行、Android SDK 非依存の純 Kotlin）は
   同じ規則を JVM 単体テストで検証しており、**カーネル化が成立することを既に証明している**。

放置した場合のコストは線形ではない。クライアントが 1 つ増えるたびに、正本が `app.js` である規則の
「移植」がもう 1 本増え、その正しさは他実装のソース読解とベクタの追記で担保される。

## Decision

**すべてのリーダー意味論を、バージョン付きの言語非依存契約と、各ランタイム 1 つの純粋カーネルモジュールに移す。**

1. **契約のホーム**: `api/domain/v1/` に正準マニフェスト（ルール ID → ファイル・バージョン・op・ポート）を置く。
   既存 `api/vectors/*.json` は移動せず、まず登録する（Phase 0 は挙動不変）。
2. **カーネルのホーム**: ランタイムごとに 1 つ。
   Rust `rust/bookq-domain`（新クレート、依存は serde のみ） /
   Web `web/static/domain/*.js`（`window.BookqDomain`） /
   Swift `ios/Domain`（ローカル SwiftPM パッケージ） /
   Kotlin `android/core`（既存。パッケージを `domain/` へ整理）。
3. **依存方向**: 外側 → カーネル。カーネルは IO・時計・DOM・HTTP・フレームワーク型を一切知らない。
   時刻・ユーザー・ポリシーはすべて引数。
4. **適合の定義**: ベクタは**本番のカーネル入口**を呼ぶ。ソースからの関数抽出・テスト内再実装・
   逆算による代替は禁止。各言語のマニフェストが「ルール ID → 実装シンボル」を宣言し、CI が突き合わせる。
5. **意図的な差異は入力にする。** クライアントごとの仕様差（例: iOS は章跨ぎ操作を許可、web/Android は章内 clamp）は
   実装の fork ではなく、`policy` 入力として契約に明記する。

### Consequences

**Positive**

- 規則の正本が 1 箇所（契約ファイル）になり、4 実装は「契約に適合する移植」になる。差分は CI で落ちる。
- 純粋規則のテストがブラウザ・サーバ・シミュレータ不要になる（node `vm` / `cargo test` / `swift test` / JUnit）。
  ブラウザ側の flake 面と CI 時間が、規則の数に比例しなくなる。
- `app.js`（5,206 行）・`main.rs`（1,949 行）・`ReaderView.swift`（1,081 行）から規則が抜け、
  それぞれの層が「表示」「ルーティング」「状態機械」に責務を絞れる。
- 新しいクライアント（将来の Web 版再実装・デスクトップ等）は、カーネル 1 つとマニフェスト 1 枚で参加できる。
- 意味論の変更がレビュー可能になる。「この差分はどの規則のどのバージョンを動かすか」が PR に現れる。

**Negative / コスト**

- Phase 0–3 で合計 15–25 PR 規模の機械的移動が発生する。1 PR あたりは小さく保てるが、総量は大きい。
- カーネル境界の維持に規律が要る（IO を足したくなる圧力）。これを機械的に弾く仕組み（下記 G2）が必須。
- Rust は 3 クレート目、iOS はローカルパッケージ追加によりビルド構成が 1 段増える。
- ベクタが「仕様」になってしまう危険（実装の現在挙動がそのまま法になる）。散文の規則定義を必須にすることで抑える。

**Neutral**

- wire shape の統治（OpenAPI + codegen）はそのまま。本 ADR はその下の層だけを扱う。
- Web にビルドステップは導入しない。カーネルは Phase 1 では classic script + 名前空間のまま。

## Alternatives Considered

| # | 案 | 棄却理由 |
|---|---|---|
| A | 現状維持 + ベクタを増やす | 正本が `app.js` である限り、ベクタは「移植の写経」を検証するだけ。規則の変更時に 4 実装を探す作業は残る。 |
| B | 共有ランタイム（WASM に 1 実装、全クライアントが呼ぶ） | 起動コスト・デバッグ性・iOS/Android のビルド鎖が重い。純粋関数 18 規則に対して過剰。カーネル化の後に再検討可能。 |
| C | DTO/型だけ codegen（Swift/Kotlin も OpenAPI から生成） | wire shape の重複は減るが、**アルゴリズム**の重複（本 ADR の主題）には効かない。相補的であり、別 ADR で扱う。 |
| D | 1 言語に全面書き換え（例: Rust に統一しクライアントは薄い表示層） | ネイティブ体験（オフライン・ジェスチャ・外部ディスプレイ）を失う。移行コストも最大。 |
| E | 規則ごとに「正本」を宣言するだけ（例: 章選択は Rust が正） | クライアントは結局オフラインで同じ判断をする必要があり、実装は残る。宣言だけでは drift を止められない。 |

## Follow-ups

- `docs/architecture/reader-semantics-kernel.md` — 設計・契約スキーマ・移行 Phase・ゲート・メトリクス。
- `docs/architecture/semantics-inventory.md` — 規則 × ランタイムの棚卸し（移行の進捗表を兼ねる）。
- 別 ADR（予定）: strangler  seam（`Upstream` / `Native` gate / `_or_proxy`）の撤去。カーネル化で
  ハンドラが `parse → kernel → serialize` になると、この層は独立に削除できる。
