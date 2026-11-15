# ADR 0003: 端末側に残る意味論のカーネル — サーバ解決 (ADR 0002) の補完

- **Status:** Proposed（ADR 0002 の採択を前提とする）
- **Date:** 2026-11-15
- **Deciders:** bookq maintainer
- **Baseline:** `f4d8ff7` (origin/main, 2026-11-15)
- **関連:**
  - **ADR 0002: サーバ解決型ドメインモデル**（`docs/adr/0002-server-resolved-work-model.md` / PR #508 / issue #507）— 本 ADR の主提案
  - 本 ADR の詳細設計: `docs/architecture/semantics-contract.md`
  - 棚卸し: `docs/architecture/semantics-inventory.md`
  - `api/openapi.v1.yaml`, `api/v1-semantics.md`, issues #280 / #441 / #459 / #460

## 要旨

ADR 0002 は「ドメイン解決を viewer-rs に一元化し、クライアントを投影にする」。
これは**サーバ側に入力が揃っている規則**には最善で、クライアント実装を削除できる。

しかし 19 規則・59 実装のうち、端末の状態（viewport・画面比・オフライン先書き・受信データの検証）
に依存する 27 実装はサーバに移せない。加えて 0002 には 3 つの付帯条件がある —
(a) §2.4 の見開き等価性判定、(b) 旧クライアントとの共存、(c) 新クライアント × 旧サーバの
フォールバック。いずれもクライアント側に規則が残る期間を生む。

本 ADR は、その**残差を正しく保つ仕組み**だけを決める:
言語非依存の契約 `api/domain/v1/` と、ポー トごとに 1 つの純粋カーネルモジュール、
そして「全ポートが本番の入口で同じケースを通る」ことを強制する CI ゲート。

2 本の ADR の役割は直交する。**0002 = 規則をどこで実行するか。0003 = 規則をどう定義し検証するか。**

## Context

1. **0002 が吸収するのは 32 実装ぶんの規則**（そのうちクライアント側 23 実装が削除され、
   残り 9 は既にサーバにある）。内訳は `docs/architecture/semantics-inventory.md`。
   残る 27 は端末状態に依存する（`layout.page-mode` / `layout.viewport-fit` /
   `layout.spread-anchor` / `state.position-merge` / `sync.*` 3 規則 / `search.facet-filter`）。
2. **0002 の Phase 0 自体が契約機構を必要とする。** §2.4 は「`solo-spreads` / `chapter-padding` に
   `rust` を足し、`build_spread_plan` が全件一致するか」を Go/No-Go 条件にしている。
   現状このベクタには `op`（どの関数をどの向きで呼ぶか）が無く、JS 側 harness は
   `app.js` から関数をブレースマッチで切り出す。判定を機械的に回す土台が無い。
3. **0002 の共存期間は「クライアント側規則の維持」を要求する。**
   旧 iOS バイナリ・旧 PWA シェルは今の規則で動き続ける（0002 §5.3）。
   さらに新クライアント × 旧サーバ（`/api/v1/work` が 404）でも動く必要がある。
   この 1 リリース分の残置コピーを「目で揃える」のは、まさに 0002 が解消しようとしている問題である。
4. **残差側の統治は現在ゼロ。** parity 系コメント 151 箇所、宣言の正本は
   `tests/test_vectors_declaration.py` の Python dict、Rust 適合テストは本番解決器を呼ばず再実装、
   iOS は正準でない私的 fixture（15 KB）を読む。0002 はここを扱わない。
5. **0002 の効果はデータで表現できる。** 各規則を `resolution: server | device` として
   マニフェストに持てば、§2.4 が No-Go に転んでも「2 規則の resolution を device に変える」で済む。
   文書の書き直しも、計画の作り直しも要らない。

## Decision

1. **ADR 0002 を採択する**（サーバ解決）。本 ADR はそれを置き換えず、残差だけを扱う。
2. **残差 27 実装を、契約 `api/domain/v1/` + ランタイムごとの純粋カーネルに移す。**
   カーネルは IO・時計・DOM・HTTP・フレームワーク型を知らない。時刻・ユーザー・ポリシーは引数。
3. **契約機構を 0002 の Phase 0 より先に入れる。**
   正準マニフェスト（rule → version / op / resolution / ports / owner）と
   ポートマニフェスト（rule → 実装シンボル）、`casesDigest`（ケース改変検知）、
   CI ゲート `domain-contract`。0002 の §2.4 判定はこの機構の 1 コマンドになる。
4. **`resolution` をデータにする。** 規則がサーバ解決か端末解決かはマニフェストの 1 フィールド。
   0002 の Phase が進むたびに `device → server` へ書き換え、そのときポートは削除される。
   逆方向（No-Go）も 1 行で表現できる。
5. **Rust のカーネルは新しいクレートを作らない。** 0002 の Phase 1 が作る
   `viewer-rs/src/domain/` を共通の置き場とし、残差規則もそこに置く。
   純粋性は依存グラフではなくモジュール境界（`clippy::disallowed_paths` + import 走査）で守る。
   — クレートを 1 つ増やすコストと、証明の強さを天秤にかけた結果、0002 との統合を優先する。
6. **適合は本番の入口で行う。** ソースからの関数抽出・テスト内再実装・逆算は禁止。
   JS は `node:vm` の空 context（`window`/`document`/`fetch` なし）で本番モジュールを実行する。
7. **意図的なクライアント差は入力にする。** 例: iOS は章跨ぎ操作を許可し、web/Android は章内 clamp。
   これは `ChapterPolicy` の入力であり、実装 fork ではない。

## Consequences

**Positive**

- 0002 の §2.4 が「測定可能な Go/No-Go」になる。現在は未検証の仮説であり、
  No-Go 時の計画（見開きが 3 実装で残る）が白紙だった。
- 0002 の共存期間に残すコピーが、契約で縛られたカーネルになる。移行中の drift を CI が止める。
- 残差 27 実装の変更が 1 箇所（ケース）から始まる。parity コメントを grep する作業が消える。
- 純粋規則のテストがブラウザ・サーバ・シミュレータ不要になる（`node:vm` / `cargo test` / `swift test` / JUnit）。
- サーバ解決への移行が「データの書き換え + ポート削除」になり、段階移行の各ステップが小さくなる。

**Negative / コスト**

- 0002 と 0003 の 2 本立てになり、規則の所在を 2 つの文書で確認する必要がある
  （対策: `semantics-inventory.md` を唯一の台帳とし、`resolution` 列で両方を表現する）。
- 契約機構（マニフェスト + 検査スクリプト + CI ジョブ）の初期コスト。
- Rust の純粋性が「依存グラフで証明」から「境界の規律 + lint」に弱まる。
- ベクタが仕様化する危険（実装の現在挙動が法になる）。散文の規則定義を必須にして抑える。

**Neutral**

- wire shape の統治（OpenAPI + codegen）は変わらない。
- Web にビルドステップは導入しない。カーネルは classic script + `window.BookqDomain`。
- ベクタは消えない。増え方が `規則 × ランタイム` から `規則 × 1` に変わる（0002 の指標と整合）。

## Alternatives Considered

| # | 案 | 棄却理由 |
|---|---|---|
| A | 0002 のみ（残差は現状維持 + ベクタ拡充） | 残る 27 実装の drift は検知できても縮まない。0002 の共存期間と No-Go 分岐が無防備になる |
| B | 0003 のみ（全規則をカーネル化、本 ADR の当初案） | サーバに寄せられる 32 実装を残す。**削除できる重複を削除しない**のは明確に劣る |
| C | 全規則をクライアント共有コア（UniFFI / wasm）に寄せる | 0002 の代替案 B と同じ理由で棄却（ビルド鎖 3 系統、SW precache 契約の破壊） |
| D | 2 本を 1 本の ADR に統合する | 意思決定の単位が混ざる（実行の所在 / 仕様の所在）。1 本が No-Go になったときに片方まで巻き戻る |
| E | 契約は OpenAPI に寄せる（`/api/v1/work` の schema だけ） | 形式は表現できるが、**アルゴリズム**（spread table の作り方、LWW の判定）は表現できない |

## Follow-ups

- 詳細設計（割り当て表・契約スキーマ・ゲート・統合移行計画・メトリクス）:
  `docs/architecture/semantics-contract.md`
- 台帳: `docs/architecture/semantics-inventory.md`（`resolution` 列が進捗の唯一の可視化）
- ADR 0002 の §2.4 判定結果を、本 ADR のマニフェストへ反映する PR を 1 本立てる。
