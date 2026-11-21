# ADR 0003: クロスランタイム契約の範囲を「ワイヤとプロトコル」に限定する

- **Status:** Proposed（ADR 0002 の採択を前提とする）
- **Date:** 2026-11-16
- **Deciders:** bookq maintainer
- **Baseline:** `3ff4a07`（origin/main, 2026-11-16）
- **関連:**
  - ADR 0002: サーバ解決型ドメインモデル（`docs/adr/0002-server-resolved-work-model.md` / PR #508 / issue #507、実装 issue #518–#529）
  - 台帳: `docs/architecture/semantics-inventory.md`
  - `api/vectors/`（#280）、`api/reader-sync-cases.json`、`api/asyncapi.yaml`、`api/v1-semantics.md`
  - iOS `ios/Packages/BookqCore`（#534）、Android `android/core`

## 要旨

bookq の長期的な変更コストは「1 つのドメイン規則を 4 ランタイムで実装していること」で決まる。
ADR 0002 はこれを**削除**で解く（規則をサーバに寄せ、クライアントを投影にする）。

本 ADR が決めるのは、その後に**それでも複数ランタイムに残るもの**の扱いである。結論は単純で、

> **クロスランタイムで一致を保証する対象は「ワイヤ上でバイト一致が要るもの」だけにする。
> それ以外は、サーバに寄せるか、端末ごとの責務として切り離す。中間（4 実装を契約で縛り続ける）は作らない。**

契約の仕組みは新設しない。既存の `api/vectors/` と reader-sync golden をそのまま使い、
「本番の入口を呼ぶ」規律と「規則の置き場の決め方」だけを定める。

## Context

### 1. 残差は思ったより小さい

ADR 0002 適用後に複数ランタイムに残る規則を、入力の所在で分類し直した（詳細は台帳）。

| 分類 | 規則 | 一致が必要な理由 |
|---|---|---|
| **protocol** | `sync.canonical-json` / `sync.manifest-digest` / `sync.frame-validate` | サーバと全クライアントがワイヤ上で同じバイト・同じ digest を出さないとペア読書が成立しない |
| **server-authoritative** | `state.position-merge` | 正はサーバの LWW + tombstone + 未来 ts 拒否（`positions-cases`）。クライアントは「ローカルより新しければ採用」という自明な比較とローカルキャッシュの管理だけで、stale な PUT はサーバが `stale` で弾く（`web/static/progress/sync.js` の冒頭コメント） |
| **platform-local** | `layout.page-mode` / `layout.viewport-fit` / `layout.spread-anchor` | 画面サイズ・向き・外部ディスプレイ・UI 部品に依存するビュー方針。ADR 0002 §2.5 も「ページモードの選択はビュー状態」として非目標にしている |
| 未決 | `search.facet-filter`（web + Android の 2 実装） | 入力 `subtree_facets` はサーバ由来。0002 Phase 4 の解決済みツリーで吸収できるかを判断する |

**一致を契約で保証すべきなのは protocol の 3 規則だけ**で、これは既に golden がある領域である。

### 2. 実際の穴は「新しい仕組みが無いこと」ではなく「既存の golden が端末を通っていないこと」

- `api/reader-sync-cases.json`（step 駆動）は rust + python + `scripts/api/ws_mock.py` が読む。
- iOS は別物の `tests/fixtures/reader_sync_cases.json`（関数別）を `BookqCoreTests` から読む。
- Android は digest golden を `PairSyncTest.kt` に**定数で直書き**している。
- web の `pairClient.js` `canonicalJson` / `manifestDigestHex` を golden で叩くテストは無い。

つまり protocol 規則は 4 ポートに実装がありながら、**同じ golden を全ポートが読む状態になっていない**。

### 3. 移行期のコピーは「凍結」すれば統治は要らない

ADR 0002 の共存期間に残るクライアント側規則は 2 種類しかない。

- **旧クライアント × 新サーバ**（App Store に残る旧 iOS、SW が保持する旧 PWA シェル）:
  0002 の変更はすべて v1 への加算なので、旧クライアントは旧規則のまま動く。
  そのコードは**もう変更されない**ため、drift の心配も、契約で縛る必要も無い。
- **新クライアント × 旧サーバ**: web シェルはサーバ自身が配信するので発生しない。
  ネイティブはデプロイ主体が 1 人なので「サーバを先にデプロイする」で消せる。
  クライアント側に旧規則のフォールバックを残す必要は無い。

### 4. 現状の基盤は既に十分に近い

- `api/vectors/*.json` は `languages` を自己宣言し、`tests/test_vectors_declaration.py` が
  各言語の reader がそのファイルを参照していることを検査している。
- iOS の純粋ドメイン規則は `BookqCore`（SwiftPM、`swift test` でシミュレータ不要）に切り出し済み（#534）。
  Android は `:core`（純 Kotlin、JUnit）が同じ役割を既に持つ。
- 欠けている規律は 2 つだけ: Rust の `conformance_vectors.rs` が `chapter_pages` を**テスト内で再実装**していること、
  JS の harness が `app.js` から関数を**ブレースマッチで切り出して**いること。
  どちらも ADR 0002 の Phase 1・2 で該当コードの居場所が変わる時点で直すのが最も安い。

## Decision

1. **ADR 0002 を主提案として採択し、その進行を本 ADR の前提条件にしない。**
   0002 の Phase 0（#518 の §2.4 判定）は本 ADR の成果物を必要としない。
   `solo-spreads` / `chapter-padding` の `languages` に `rust` を足し、
   `conformance_vectors.rs` から `reader_sync::model::build_spread_plan` を呼ぶだけで判定できる。

2. **クロスランタイム契約（全ポートが同じケースを読む義務）は protocol 規則だけに課す。**
   protocol の golden は 1 ファイルに統合し、rust / js / swift / kotlin の全ポートが同じファイルを読む。
   移行期の vectors（`work-chapters` / `path-lookup` / `encode-path` / `solo-spreads` / `chapter-padding`）は
   0002 の各 Phase がクライアント実装を削除する PR で、同時に `languages` から該当言語を外す。

3. **新しい契約インフラは作らない。** 置き場は既存の `api/vectors/` と `api/reader-sync-cases.json`、
   宣言検査は既存の `tests/test_vectors_declaration.py`。マニフェスト・ポート別宣言・専用 CI ジョブは追加しない。

4. **適合テストは本番の入口を呼ぶ。** テスト内の再実装、ソースからの関数切り出し、出力からの逆算は、
   既存箇所を解消したうえで新規には認めない。既存の 2 箇所は以下で解消する:
   - Rust `conformance_vectors.rs::chapter_pages` → 0002 Phase 1（#521）の `domain::work` を直接呼ぶ。
   - JS の `app.js` 切り出し → 0002 Phase 2（#523）で対象関数自体が削除される。それまでは現状維持。

5. **platform-local 規則はクロスランタイム契約にしない。** 各ランタイムの単体テスト（`BookqCoreTests` / `:core:test` / JS）で守る。
   プラットフォーム間の意図的な差は、コード内の分岐ではなく関数の入力（例: `ChapterPolicy`）として表現する。

6. **デプロイ順序で共存を解く。** サーバ機能を使う新ネイティブクライアントは、そのサーバ機能が本番に出てからリリースする。
   クライアントに旧規則のフォールバック実装は残さない。旧サーバ（`/api/v1/work` が 404 等）を検知したら、
   黙って旧経路に戻らず「サーバの更新が必要」を明示する。

7. **規則を追加・変更するときの置き場は次の順で決める**（台帳とレビューでこれを適用する）:
   1. 入力がサーバに揃うか → **server**（viewer-rs に 1 実装。クライアントは結果を描画）
   2. ワイヤ上でバイト一致が必要か → **protocol**（golden に追加し、全ポートが読む）
   3. 端末状態にしか依存しないか → **platform-local**（各ランタイムの単体テスト）
   4. どれでもないのに 2 ランタイム以上で同じ規則を書く必要がある → 設計を見直す。やむを得ず残すなら
      vector を同じ PR で追加し、台帳に「解消予定」として載せる

## Consequences

**Positive**

- ADR 0002 の進行を何も遅らせない。#518 は今すぐ着手できる。
- 長期的にクロスランタイムで維持するテスト資産が「protocol golden 1 本 + Rust 単一実装の単体テスト」に収束する。
  4 実装を契約で縛り続ける構造を作らないので、規則の追加コストが恒久的に下がる。
- platform-local をはっきり切り離すことで、iPhone / Android タブレット / web それぞれのビュー調整が
  他プラットフォームとの一致に縛られない。
- 追加の仕組みが無いので、メンテナンス対象（スクリプト・CI ジョブ・マニフェスト）が増えない。

**Negative / コスト**

- platform-local 規則（見開き寸法・ページモード等）は、プラットフォーム間で**意図せず**ずれても CI では検出されない。
  ずれがユーザーに見える問題になった時点で、それは server か protocol に昇格させる判断材料として扱う。
- 「サーバを先にデプロイする」運用規律が 1 つ増える（ネイティブのリリース手順に明記する）。
- 0002 の §2.4 が No-Go の場合、見開き（規則 7・8）が 3 クライアントに残る。この場合は platform-local ではなく
  「解消予定の重複」として vectors を維持し、どちらを正とするかを先に決める（0002 §2.4 の方針どおり）。

**Neutral**

- wire shape の統治（OpenAPI + `api-breaking` + codegen）は変わらない。
- web にビルドステップは導入しない。

## Alternatives Considered

| # | 案 | 棄却理由 |
|---|---|---|
| A | 0002 のみ（残差は放置） | protocol 規則の golden が全ポートを通っていない穴が残る |
| B | 端末側カーネル + 契約機構（本 PR の前版: `api/domain/v1/` マニフェスト、ポート別宣言、`casesDigest`、CI ゲート G1–G5、`node:vm` harness、`ios/Domain`） | 守る対象の大半（ページモード・寸法・アンカー）は本来一致させるべきでない platform-local 規則で、protocol 3 規則のためには既存の golden で足りる。0002 Phase 0 の前提に置くと主提案を遅らせる。自己申告のシンボル宣言を grep で照合するゲートは、守っている感覚に対して保証が弱い。`ios/Domain` は #534 の `BookqCore` で既に実現済み |
| C | 共有クライアントコア（UniFFI / wasm） | ADR 0002 の代替案 B と同じ理由。protocol 規則に限った再評価は 0002 Phase 5（#529）で行う |
| D | 移行期のクライアント側フォールバックを契約で縛る | 旧クライアントのコードは凍結されるので drift しない。新クライアント × 旧サーバはデプロイ順で消せる |

## Follow-ups

1. **protocol golden の統合**（独立して着手可能・小）: `tests/fixtures/reader_sync_cases.json` の関数別ケースと
   Android の直書き digest を `api/reader-sync-cases.json` に統合し、rust / js / swift / kotlin の全ポートが同じファイルを読む。
   `test_vectors_declaration.py` と同等の宣言検査をこのファイルにも適用する。
2. **#518 の完了条件に追記**: 判定結果（Go / No-Go と不一致ケース）を台帳の規則 7・8 に反映する。
3. **#521 の完了条件に追記**: `conformance_vectors.rs` の `chapter_pages` 再実装を削除し、`domain::work` を呼ぶ。
4. **#523–#528 の完了条件に追記**: クライアント実装を削除する PR で、対応する vector の `languages` から該当言語を外し、台帳を更新する。
5. **ネイティブのリリース手順**（`docs/ios-app.md` / `docs/android-app.md`）に「依存するサーバ機能が本番に出ていること」を追記する。
6. 0002 Phase 4 の後に台帳を見直し、`search.facet-filter` の置き場と Phase 5（#529）を判断する。
