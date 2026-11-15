# 意味論の統合設計 — サーバ解決 (ADR 0002) + 端末側カーネル (ADR 0003)

Status: **Proposal**（コード変更なし）· Baseline: `f4d8ff7` · 2026-11-15
関連: `docs/adr/0002-server-resolved-work-model.md`（PR #508 / #507）· `docs/adr/0003-ondevice-semantics-kernel.md`
台帳: `docs/architecture/semantics-inventory.md`

---

## 要旨

bookq の長期コストは「1 つの規則を 4 つのランタイムで実装していること」にある。
解き方は 2 層に分かれる。**実行の所在**（ADR 0002: サーバに寄せる）と、
**仕様の所在**（ADR 0003: 契約に寄せる）である。片方だけでは足りない。

| | 規則の実行 | 規則の定義・検証 |
|---|---|---|
| **ADR 0002** サーバ解決 | viewer-rs のみ。クライアントは投影 | OpenAPI（加算的変更） |
| **ADR 0003** 端末カーネル | 各ランタイム（残差のみ） | `api/domain/v1/` + ポートマニフェスト + CI |
| 対象 | サーバ側に入力が揃う規則（**32 実装**） | 端末状態に依存する規則（**27 実装**） |

統合の要点は 3 つ。

1. **ADR 0002 を主提案として採択する。** 削除できる重複は削除する（移動ではない）。
2. **ADR 0003 は残差だけを扱う。** ただし 0002 の Phase 0 が要求する判定機構と、
   共存期間に残るコピーの統治は 0003 が提供する。
3. **順序が重要。** 0003 のカーネル移設を 0002 より先にやると、**0002 が後で削除するコードを
   わざわざ移動する**ことになる。契約台帳（Phase A0）→ 0002（Phase B）→ 端末カーネル（Phase C）。

---

## 1. なぜ 2 層に分けるのか

`/api/v1/tree` はファイルシステムの投影（名前の木）を返す。そのため「作品・章・ページ順・見開き・
表紙・可視性」という**本の形**がワイヤに出ておらず、4 ランタイムがそれぞれ再構築している。
ADR 0002 はここを直す。これは正しい。ただし 2 つの限界がある。

**限界 1: 入力がサーバに無い規則は移せない。**
`layout.page-mode` は viewport の縦横、`layout.viewport-fit` は画面比、
`state.position-merge` はオフライン中の先書き、`sync.*` は受信チャンクの検証に依存する。
サーバはこれらを決定できない（決定できたとしても、通信できない場所で読めなくなる）。

**限界 2: 移行そのものが契約を要求する。**
ADR 0002 §2.4 は「ペアの `build_spread_plan` と単独読書の見開きが一致するか」を
**未検証の仮説**として Go/No-Go に置いている。判定するには、ベクタに `rust` を足して
`build_spread_plan` を全ケースで回す必要がある。しかし現在の `api/vectors/*.json` には
「どの関数を、どの向きで呼ぶか」を書く場所が無い（`format: 1`）。JS 側 harness は
`app.js` から関数をブレースマッチで切り出して実行している。

つまり **0002 の最初の一歩が、0003 の契約機構を必要としている。**

さらに 0002 §5.3 は「旧 iOS バイナリ・旧 PWA シェルは今の規則で動き続ける」と述べる。
新クライアント × 旧サーバ（`/api/v1/work` が 404）も同じで、**1 リリース分はクライアント側に
規則のコピーが残る**。そのコピーを「目で揃える」のをやめるのが 0003 である。

---

## 2. 規則の割り当て（19 規則 / 59 実装）

`移管先` はマニフェストの `resolution` フィールドに入る。**文書ではなくデータ**である点が重要で、
0002 の Phase が進むたびに `device → server` へ書き換え、ポートを削除する。

| # | rule id | 実装 ports | 移管先 | 根拠 / 消えるもの |
|---|---|---|---|---|
| 1 | `tree.flatten` | 1 | server（既に） | `tree.rs` のみ。クライアントは `name` に `/` が入る契約を消費 |
| 2 | `tree.lookup` | 4 | **server** (0002) | ノードの `path` をサーバが返す → `findNodeByPath` / `BrowseRules.nodeAt` / `node(at:)` が消える |
| 3 | `tree.siblings` | 4 | **server** (0002) | `Work.chapters` に吸収 |
| 4 | `work.chapters` | 4 | **server** (0002) | 同上。`ReaderWork.chapters` / `siblingLeaves` が消える |
| 5 | `work.page-order` | 1 | server（既に） | `bookq-core::scans::sort_file_cmp` |
| 6 | `path.encode` | 4 | **server** (0002) | サーバが URL を発行。`encodePath` の一致保証自体が不要になる |
| 7 | `layout.spread-plan` | 3 | **server**（条件付き） | 0002 §2.4 が Go なら `spreadTables` に吸収（`rebuildSpreads` ×3 が消える） |
| 8 | `layout.chapter-padding` | 3 | **server**（条件付き） | 同上（`padChapterSpacers` / `assemble` ×2 が消える） |
| 9 | `layout.page-mode` | 3 | device | viewport の縦横に依存。サーバは決定できない |
| 10 | `layout.spread-anchor` | 3 | device | スプレッド番号 ↔ 表示位置の写像。spreadTable があっても必要 |
| 11 | `layout.viewport-fit` | 3 | device | 画面比・外部ディスプレイ。端末ごとに異なる |
| 12 | `state.position-merge` | 4 | device | オフライン先書きの LWW は送信前に決まる必要がある |
| 13 | `state.visibility-filter` | 4 | **server** (0002) | `visibility=apply\|annotate`。適用箇所の非対称（search はサーバ、tree はクライアント、iOS は二重）も同時に解消 |
| 14 | `offline.leaf-paths` | 3 | **server** (0002) | Work 1 回の取得で保存対象が決まる |
| 15 | `search.facet-filter` | 2 | device | オフライン閲覧時のローカル絞り込み（`FacetRules` / `facets.js`） |
| 16 | `sync.canonical-json` | 4 | device | MANIFEST チャンクの digest 検証は端末で走る（0002 も残す） |
| 17 | `sync.manifest-digest` | 4 | device | 同上 |
| 18 | `sync.frame-validate` | 4 | device | 同上 |
| 19 | `search.facet-aggregate` | 1 | server（既に） | `search.rs::facets_for` |

集計:

| 指標 | 現在 | 0002 完了後（§2.4 = Go） | §2.4 = No-Go |
|---|---|---|---|
| 規則 × ランタイムの実装数 | **59** | **34** | **40** |
| うちクライアント側 | 47 | **23** | 29 |
| うちサーバ側 | 12 | 11 | 11 |
| クロス言語で読まれるベクタ（規則数） | 6 | 0（`["rust"]` のみ） | 2（規則 7・8） |

**No-Go でも計画は壊れない。** 規則 7・8 の `resolution` を `device` のままにするだけで、
そのとき 2 規則は端末カーネルの担当になる（+6 実装）。文書の書き直しは発生しない。

---

## 3. 共通の契約機構 — `api/domain/v1/`

両 ADR が使う土台。0002 は「規則をサーバに寄せてよいか」の判定に、0003 は「残差の移植」に使う。

```
api/domain/
  README.md                 # 統治: ID 規約・resolution・版・変更手順・所有者
  CHANGELOG.md              # ルールごとの版履歴（1 行/版）
  v1/
    manifest.json           # 正準: rule id → {file, version, op, resolution, ports, owner, casesDigest}
    rules/                  # 新形式の規則。Phase A0 では api/vectors/*.json をパス参照で登録
```

規則ファイル（`format: 2`）:

```jsonc
{
  "rule": "layout.chapter-padding",
  "version": 1,
  "format": 2,
  "status": "current",
  "resolution": "server",            // server | device  ← 0002 の進行に応じて書き換える
  "title": "Pad a chapter boundary so a spread never straddles two chapters",
  "spec": "docs/architecture/rules/layout.chapter-padding.md",
  "owner": "reader",
  "op": {
    "name": "padChapterSpacers",     // ← これが無いのが現状（三者三様の解釈の原因）
    "inputs": [
      {"name": "images", "type": "PageRef[]"},
      {"name": "boundaries", "type": "int[]"},
      {"name": "pageMode", "type": "'single' | 'spread'"},
      {"name": "spreadOffset", "type": "0 | 1"}
    ],
    "output": "PageRef[]"
  },
  "ports": ["rust", "js", "swift", "kotlin"],
  "cases": [{"name": "odd chapter, offset 0", "input": {"...": "..."}, "expect": {"...": "..."}}]
}
```

ポートマニフェスト（各クライアントツリーに 1 枚。以下は `web/domain.manifest.json`）:

```jsonc
{
  "port": "js",
  "contract": "api/domain/v1",
  "module": "web/static/domain",
  "harness": "tests/domain/test_domain_js.py",
  "rules": {
    "layout.page-mode": {"version": 1, "symbol": "BookqDomain.pageMode"},
    "layout.viewport-fit": {"version": 1, "symbol": "BookqDomain.spreadFit"},
    "state.position-merge": {"version": 1, "symbol": "BookqDomain.mergePosition"}
  }
}
```

これで「宣言の正本」が `tests/test_vectors_declaration.py` の Python dict からデータに移り、
言語追加はマニフェスト 1 枚、**規則の移管は `resolution` 1 行**になる。

---

## 4. 端末側カーネル（残差 27 実装）

| ポート | ホーム | 移すもの | 純粋性の強制 |
|---|---|---|---|
| rust | `viewer-rs/src/domain/`（**0002 Phase 1 が作る置き場に相乗り**） | `state.rs` の LWW 判断、`reader_sync` の canonical / digest / frame 検証 | `clippy::disallowed_paths`（`std::fs`/`net`/`time`/`process`、`axum`/`tokio`）+ import 走査。新クレートは作らない |
| js | `web/static/domain/*.js` (`window.BookqDomain`) | `viewer/spread.js` の fit、`app.js` の page-mode inline 部と anchor、`progress/sync.js` の判断、`pairClient.js` の canonical/digest、`search/facets.js` の絞り込み | `node:vm` の空 context（`window`/`document`/`fetch`/`localStorage` なし）で本番モジュールを実行 |
| swift | `ios/Domain`（ローカル SwiftPM パッケージ） | `ReaderWork` の spread 数学・`spreadFit`、`ProgressSync` の判断、`CanonicalJSON` | Foundation のみ import。`swift test` を macOS destination で（シミュレータ不要） |
| kotlin | `android/core`（既存） | `ReaderWork` / `ExtDisplay` / `ProgressBook` / `CanonicalJson` / `FacetRules` | `:core` は `:app`/`:data` に依存しない（Gradle で固定）。JUnit のみ |

**0003 の当初案からの変更点**: Rust は新クレート `bookq-domain` を作らない。
純粋性を依存グラフで**証明**する強さは失うが、0002 Phase 1 が `viewer-rs/src/domain/` を作るので、
そこへ相乗りする方が構成要素もレビュー対象も少ない。残差の Rust 規則は
`position-merge` と `sync.*` の 4 つだけであり、クレート 1 つを正当化する規模ではない。

---

## 5. 意図的な差異は入力にする

クライアント差はコメントで宣言されている（例: 「iOS はユーザ要望で章跨ぎ操作を許可するが、
web/Android は章内 clamp」「iOS は sibling autoload の depth gate を落とす」）。
カーネル化でこれを `if (platform)` にすると最悪の設計になる。代わりに**op の入力**にする。

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

iOS は `{siblingAutoloadMinDepth: 0, crossChapterTurns: true}`、web/Android は `{2, false}`。
差が**データ**になるので、ベクタで両方の policy を固定できる。
なお規則 3・4 が 0002 でサーバ解決になった後も、この policy は Work 取得クエリの入力として残る。

---

## 6. CI ゲート

| gate | 保証する内容 | 実装 | コスト |
|---|---|---|---|
| **G1 domain-contract** | 全規則が正準マニフェストに載る / `resolution` と `ports` が整合 / 全ポートが同版を宣言 / ケースが版を跨いで書き換えられていない | `scripts/domain_check.py`（純 Python、ブラウザ・サーバ不要） | 数秒 |
| **G2 domain-purity** | カーネルが IO を知らない | rust: clippy + import 走査 / js: `node:vm` 空 context / swift: import 走査 + `swift test` / kotlin: Gradle 依存固定 | 数十秒 |
| **G3 domain-anchor** | 適合は**本番の入口**を呼ぶ（ソース抽出・再実装・逆算の禁止） | js harness は `web/static/domain/**` 以外を load できない。`tests/` が `web/static/**` を読むことを lint で禁止（SHELL_ASSETS 検査のみ whitelist） | 数秒 |
| **G4 domain-single-copy** | 移した規則が外側に再宣言されない | マニフェストの `symbol` を外側で grep し、`domain/` 外の定義を fail | 数秒 |
| **G5 resolution-drift** | `resolution: server` の規則にクライアント実装が残っていない / `device` の規則にサーバ実装が無い | マニフェスト × ツリーの突き合わせ | 数秒 |

`casesDigest`（ケース JSON の正準化 sha256）をマニフェストに記録する。
これが「リファクタ中に期待値を動かして緑にする」を機械的に封じる。
G5 は 0002 の進捗を強制する新しいゲートで、**移管し忘れたクライアント実装を PR 時に落とす**。

---

## 7. 統合移行計画

各 Phase は単独でマージ可能・単独で切り戻し可能。**挙動変更は 0**。

### Phase A0 — 契約台帳（0002 より先）

- `api/domain/README.md` / `v1/manifest.json`（19 規則、`resolution` 付き）を追加。
- 4 ポートに `domain.manifest.json` を追加（現時点の実装シンボルを正直に列挙）。
- `scripts/domain_check.py` + `.github/workflows/domain-contract.yml`（G1/G5）。
- `tests/test_vectors_declaration.py` を `domain_check.py` の薄い呼び出しに置換。
- `api/vectors/*.json` に `rule` / `version` / `op` を追記（**ケース配列は 1 バイトも変えない**。digest で証明）。

**Exit gate**: 既存テストが緑、`domain-contract` 緑、`git diff --stat` に `.rs/.js/.swift/.kt` が無い。

### Phase A1 — §2.4 判定（0002 Phase 0 と共同）

- `solo-spreads` / `chapter-padding` の `ports` に `rust` を足し、`build_spread_plan` で全ケースを実行。
- 結果を `resolution` に反映（Go: `server` / No-Go: `device` + 不一致ケースを `status: draft` で固定）。

**Exit gate**: 判定が 1 コマンドで再現できる。No-Go なら規則 7・8 が 0003 の担当になる。

### Phase B — 0002 本体（サーバ解決、Phase 0–4）

0002 の計画をそのまま実行する。本提案はここに手を出さない。
各 Phase の完了時に `resolution` を `server` に書き換え、`ports` から該当ポートを削除する
（G5 が「削除し忘れ」を検出する）。実装削除は 0002 側の PR が行う。

### Phase C1 — 端末カーネル: web（0002 Phase 2 の後）

- `web/static/domain/{layout,state,sync,search}.js` を新設。classic script + `window.BookqDomain`。
- 既存ファイルは委譲シムに変える（`app.js` が既に `SpreadView` に対してやっている形）。
- `tests/domain/` に `node:vm` harness を新設し、`test_conformance_vectors_js.py` のブラウザテストを置換。
- `sw.js` の `SHELL_ASSETS` と `index.html` の defer 順に追加（36 件の手書きリスト。Phase D で自動化）。

**Exit gate**: JS 適合テストが**ブラウザ 0・サーバ 0**で緑。

### Phase C2 — 端末カーネル: rust 残差

- `viewer-rs/src/domain/` に `state::position` / `sync::{canonical,digest,protocol}` を移す。
- `state.rs` は store（SQLite）と HTTP だけを残す。
- `conformance_vectors.rs` の再実装を削除し、本番関数を呼ぶ。

**Exit gate**: `state.rs` から LWW 判断が消える。`cargo test` がベクタを実行（サーバ起動なし）。

### Phase C3 — 端末カーネル: iOS / Android

- Android: `:core` を `domain/` パッケージへ整理し rule id と対応づけ（移動のみ）。
- iOS: `ios/Domain`（SwiftPM、Foundation のみ）を追加し、`ReaderWork` の spread 数学 /
  `spreadFit` / `ProgressSync` / `CanonicalJSON` を移設。CI は `swift test` でベクタを実行。
- iOS の私的 fixture（`tests/fixtures/reader_sync_cases.json`）を正準 `api/reader-sync-cases.json` に統合。

**Exit gate**: 残差の適合テストがシミュレータ・実機なしで走る。

### Phase D — 締め

- G3/G4 を厳格化。`SHELL_ASSETS` を `index.html` から生成（36 件手書き vs script タグ 29 件が手で同期されている）。
- `app.js` / `main.rs` の残りを薄くする（0002 Phase 1・2 と重複しない範囲）。
- 四半期ごとに `semantics-inventory.md` を剪定し、`resolution` の充足を確認。

---

## 8. メトリクス

| 指標 | 現在 | Phase B 完了 | Phase C 完了 | 計測方法 |
|---|---|---|---|---|
| 規則 × ランタイムの実装数 | 59 | 34（Go）/ 40（No-Go） | 同左 | `semantics-inventory.md` |
| クライアント側の実装数 | 47 | 23 / 29 | 同左（すべてカーネル内） | 同上 |
| クロス言語で読まれるベクタ（規則数） | 6 | 0（server 規則は `["rust"]` のみ） | 8（device 規則のみ。意図的） | `manifest.json` |
| 純粋規則の検証にブラウザ/サーバ/シミュレータを要する数 | JS 5（Playwright）+ iOS シミュレータ | 同左 | 0 | CI job の構成 |
| ブラウザ fixture を取る pytest 関数 | 511 | 同左 | ≤ 400（見積り） | `domain_check.py --report` |
| parity 系コメント | 151 | 減少（0002 が理由を消す） | ≤ 40 | grep |
| 規則追加コスト | 4 実装 + Python dict + ベクタ + ブラウザ検証 | 契約 1 ファイル + サーバ 1 実装 | 契約 1 ファイル + 該当ポートのみ | 手順の差分 |

数値は方向と検証可能性を示すためのもので、達成を約束しない。Phase ごとに実測して台帳を更新する。

---

## 9. リスク

| # | リスク | 対策 |
|---|---|---|
| R1 | 0002 が §2.4 で No-Go になり、見開きが端末に残る | `resolution` を `device` にするだけ（計画の書き直し不要）。継続条件は A1 の判定 |
| R2 | 0002 と 0003 で同じファイル（`app.js`, `ReaderModel.swift`）を触って衝突 | 順序を固定（A0 → B → C）。C1 は 0002 Phase 2 のマージ後に着手 |
| R3 | 0002 より先にカーネル移設をやって工数が無駄になる | **Phase B より前に C を着手しない**（本統合の中心的な制約） |
| R4 | 共存期間（旧クライアント / 新クライアント × 旧サーバ）のコピーが drift | G5 + 契約。新クライアントは `/api/v1/work` 404 時にカーネルへフォールバックする（1 リリース） |
| R5 | カーネルへの IO 混入 | G2 を「動けば通る」ではなく「動かなければ落ちる」方向で設計。時計・ユーザーは引数 |
| R6 | ベクタが仕様化する（現状挙動が法になる） | 規則ごとに散文定義（inputs / outputs / invariants / why）を必須にし、ケースはユーザーに見える筋で正当化 |
| R7 | 契約の官僚化 | 規則数を上限 20 に保つ。`status: "draft"` はポート義務なし。マニフェストは 1 画面に収める |
| R8 | Rust の純粋性証明が弱くなる（新クレートをやめた代償） | clippy `disallowed_paths` + import 走査 + `domain/` 外からの参照禁止。将来必要ならクレート化は独立 PR |
| R9 | iOS のターゲット追加で pbxproj が荒れる | 既にフォルダ自動同期（`PBXFileSystemSynchronizedRootGroup`）。ローカル SwiftPM パッケージならリンク追加 1 回 |
| R10 | Web の defer 順・SW precache の破壊 | Phase C1 は classic script + 名前空間で据え置き。`SHELL_ASSETS` 追加漏れは既存テストが検出する |

---

## 10. 未決事項

| # | 論点 | 推奨 |
|---|---|---|
| Q1 | 契約のホーム | `api/domain/`（`api/` は既に契約の家） |
| Q2 | 契約機構の実装言語 | Python（`scripts/domain_check.py`、既存テスト群と同じ実行環境） |
| Q3 | iOS は SwiftPM か framework target か | SwiftPM（`swift test` がシミュレータ不要） |
| Q4 | Web は classic script 継続か ESM 化か | Phase C1 は classic script。ESM は SW/defer 順の再設計を伴うため別判断 |
| Q5 | 規則 owner | `reader`（layout/state）/ `browse`（tree/search/offline）/ `sync`（sync.*） |
| Q6 | 最初の 1 規則 | `layout.page-mode`（端末専用・小さい・既に Swift/Kotlin に名前がある・JS は inline で名前すら無い） |

---

## 付録 A: PR の順序（具体）

1. **A0** 契約台帳 + `domain-contract` CI（本提案の次。0002 の前提）
2. **A1** `solo-spreads` / `chapter-padding` に `rust` を追加し §2.4 を判定（0002 と共同）
3. **B** ADR 0002 の Phase 0–4（サーバ解決。所有は 0002）
4. **C1** `web/static/domain/page-mode.js` + `node:vm` harness（最初の 1 規則）
5. **C2** `viewer-rs/src/domain/state.rs`（LWW）+ 適合テストを本番関数へ
6. **C3** iOS `Domain` パッケージ / Android `:core` 整理
7. **D** ゲート厳格化 + `SHELL_ASSETS` 自動化

各 PR は「1 規則・挙動不変」で、レビューは次の 2 点に集中できる:
**ケースが変わっていないこと（`casesDigest`）**、**外側に重複定義が無いこと（G4）**。

## 付録 B: この 2 本が噛み合っていることの証明

ADR 0002 の成功指標は「`languages` が `["rust"]` 以外のベクタ: 5 / 6 → 0〜1」である。
本統合でも Phase B 完了時点ではその状態になる（サーバ解決された 10 規則のベクタは `["rust"]` のみ）。

ただし **Phase C で意図的に戻る**。残差 8 規則は端末で走るので、js / swift / kotlin が
同じケースを読む形に戻す必要がある。ここで 0002 の指標をそのまま当てると「後退」に見えるが、
そうではない。**クロス言語ベクタの対象が「全規則」から「端末で走る規則だけ」に変わる**のであって、
コストは `規則数 × ランタイム数` から `device 規則数 × 1` に下がる。

この区別は文書では曖昧になりやすいが、`manifest.json` の `resolution` 列があれば機械的に検査できる
（ゲート G5）。つまり両 ADR の指標は、**`resolution` を挟むことで矛盾せずに接続する**。

| 時点 | クロス言語ベクタを持つ規則 | 理由 |
|---|---|---|
| 現在 | 6 | 規則の所在が曖昧なため、全ポートが全ベクタを読んでいる |
| Phase B 完了 | 0 | サーバ解決された規則は 1 実装しかない |
| Phase C 完了 | 8 | 端末に残る規則だけが複数ポートを持つ（意図的・上限あり） |
