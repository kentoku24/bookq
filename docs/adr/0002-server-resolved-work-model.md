# ADR 0002: ドメイン解決をサーバに一元化する (Server-resolved Work model)

- **Status:** Proposed
- **Date:** 2026-11-15
- **Deciders:** bookq-maintainers
- **Context:** #507 / 詳細設計: [`docs/architecture/server-resolved-work-model.md`](../architecture/server-resolved-work-model.md)

## Context

`/api/v1/tree` はファイルシステムの投影 (名前の木) を返す。「作品・章・ページ順・見開き・表紙・
hidden-root 可視性・オフライン集約」というドメイン概念は wire に出ておらず、viewer-rs
(reader-sync の manifest 用)、web、iOS、Android の 4 ランタイムが木からそれぞれ再構築している。

- 9 つの規則ファミリのうち 8 つが 3〜4 ランタイムに実装されている。`api/vectors/` で契約固定されているのは 6 つで、うち 5 つはクライアント言語を含む。
- drift は実際に起きている。章連結の深さゲートが iOS にない (`ReaderWork.kt` の「Web wins」注記)、`f38c31d` (iOS の章クランプ)、可視性フィルタの適用場所がそろっていない (search はサーバ、tree は 3 クライアント、iOS は search を二重にフィルタ)。
- サーバは既に `reader_sync/catalog.rs` の `build_manifest_core` で、章構成 + pageRefs + spreadTable + digest を作っている。ただしこれを使うのはペア読書だけで、solo 読書とオフライン DL はクライアント側で同じものを作り直している。

conformance vectors は drift の**検知**には有効だが、コストは規則数 × ランタイム数で増え続ける。

## Decision

**ドメイン解決は viewer-rs だけが行い、クライアントは解決済みモデルを描画する projection にする。**

1. `build_manifest_core` を `viewer-rs/src/domain/work.rs` に移す。WS の MANIFEST と新しい REST `GET /api/v1/work?folder=` (ETag = 同じ digest) の両方がこれを使う。
2. `/api/v1/tree` のノードに `path` / `coverUrl(s)` / `kind` / `work` を optional で足す。可視性は opt-in の `visibility=apply|annotate` で扱い、fail-closed をサーバ側で成立させる。
3. クライアントは path を合成・分解・エンコードせず、サーバが返す `path` / URL / `digest` をそのまま使う。
4. wire の変更はすべて v1 への加算的変更とする (`api-breaking` を通す)。v2 は切らない。
5. 見開き表 (`spreadTables`) を Work に含めるのは、solo の見開き vectors が `build_spread_plan` と全件一致した場合に限る (現時点では未検証の仮説)。

移行は Phase 0 (契約) → 1 (サーバ domain 層) → 2 (web) → 3 (iOS/Android) → 4 (解決済みツリー + 可視性) → 5 (ペア reducer の扱いを判断) の順で進める。各 Phase は単独でマージ・切り戻しできる。

## Alternatives Considered

| # | 案 | 棄却理由 |
|---|---|---|
| A | 現状維持 + vectors 拡充 | コストの増え方が変わらない。vectors は書いたケースしか守らない |
| B | Rust 共有クライアントコア (UniFFI + wasm) | web の「ビルドステップなし」と SW precache の契約を崩す。FFI ツールチェーンが 3 系統増える。重複の大半はリアクティブ状態に絡んでいて純粋関数ではない。誤った境界がそのまま残る。Phase 5 で reader-sync reducer に限って再評価する |
| C | `app.js` の ESM 化 / 分割を先に行う | web だけの改善になる。Phase 2 の後に行うほうが対象が小さい |
| D | viewer-rs の層整理だけを行う | 単独では効果が小さい。Phase 1 に含める |
| E | 安定コンテンツ ID の導入 | 永続的な ID 対応表と読書位置の移行が要る。path を opaque に扱う原則で将来に備える |

## Consequences

### Positive

- ドメイン規則の変更が 1 ランタイム (Rust) で完結する。クロスランタイムの vectors は 5 本から 0〜1 本に減る。
- solo / オフライン / ペアが同じ digest の同じ Work を使うので、「solo とペアで見開きが違う」種類の不整合が構造的に起きなくなる。
- web の shallow tree モードで full tree を待たずに作品を開ける。オフライン DL が leaf ごとの列挙から Work 1 回の取得になる。
- 契約固定のなかった表紙・可視性・オフライン集約の規則が、サーバの単体テストで固定される。

### Negative

- 作品を開く時に直列の 1 RTT が増える (現在 web は tree からリクエスト 0 回で組み立てている)。1 ページ目の並行取得と viewport 内の Work 先読みで緩和し、`perf-measurement` で非悪化を Phase 2 の Go 条件にする。
- ダウンロード済み作品の Work JSON を、各クライアントのオフラインストアに明示的に保存する必要がある。
- 旧クライアント (App Store に残る iOS バイナリ、キャッシュ済み PWA shell) のために、tree の既定挙動と `/api/v1/images` を当面残す。

### Neutral

- reader-sync のクライアント状態機械 (3 実装) は本 ADR の範囲外で、Phase 5 で別の ADR として判断する。
