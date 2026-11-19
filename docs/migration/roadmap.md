# roadmap.md — 段階的 Rust 移植ロードマップ（Phase 0 成果：統合方式比較・パイロット指名・進行順序）

> Phase 0 調査成果物 5/6（issue #163）。本書は「どこをどう Rust 化するか」の実行設計。
> 原則：Rust 化を目的化しない・既存実装を唯一の正本として残す・境界ごとに差分検証と切戻し手段を持つ。

## 1. パイロット選定

### 指名（1つ）

**W1「ソート/エントリモデル中核」**: `scanner.py` の `natural_key` / `_normalize_kanji`（漢数字正規化）/ `_sort_key` と、それに伴う archive 側の共有ソート呼出を PyO3 で Rust 化する。

### 理由（issue の選定基準に対応）

| 基準 | 適合状況 |
|---|---|
| 依存・責務が小さく分離 | ★ 最底辺の L0 純粋関数群（stdlib のみ・副作用なし）。scanner⇄archive 循環の「結び目」で、ここを Rust 化すると scanner・archive・metadata が同一実装を共有できる（依存グラフ §2 参照） |
| 既存テストで固定可能 | ★ `test_scanner.py`（並び・tie-break・漢数字の全ケース＝千尋/六本木/万億兆/全角数字）と `test_archive.py`（_finalize の順序）が byte 単位の期待値を既に固定 |
| 入出力を機械比較可能 | ★ 文字列→文字列/タプルの決定的関数。golden corpus 差分が組みやすい |
| 性能変化を測定しやすい | ★ 全ソート・リスティング・index 経路の内部関数で、micro-bench（pytest 上の新旧並走）と end-to-end（scan_tree 時間）の両面で測れる |
| 切戻しが容易 | ★ 関数 import の単一シームで `try/except ImportError` フォールバック or env フラグ切替が完結 |

### 検証条件（パイロット合格基準）

1. **byte 一致**: golden corpus（実ライブラリ由来の名前集合を hash 化して秘匿 + 合成エッジケース）で `natural_key`/`_sort_key`/`_normalize_kanji` の出力が Python 版と**全件完全一致**
2. **既存テスト緑**: `pytest tests/ -q -n auto` が新旧両実装で全通過（切替 env で両系統を CI で走査）
3. **性能**: micro-bench で中央値が**悪化しない**こと（改善は必須でなく「非回帰」が合格線 — パイロットは互換性検証が主目的）
4. **live 回帰**: `scripts/live-regress.sh` で表示・ソート順に差分なし
5. **切戻し証明**: env フラグ/拡張欠落で旧実装に即時復帰する手順を PR で実演

### リスクと見積根拠

- **主リスク**: `natural_key` の返り値が `int|str` 混合タプルの list（tuple 比較の型順序に依存）。Rust 側で「数字片は int、他は str」の比較順序を**自前で忠実実装**する必要（PyO3 で tuple を往復するより Rust 内部で完結させ key→bytes に落とす方が安定）
- **第二リスク**: `scanner.py:45` の "must match JS byte-for-byte" コメントに対応する JS 実装が `app/static/` に見当たらない（Phase 0 発見）。仕様固定時に「JS 契約は現在存在しない（natural_key が sole source）」ことを確認してから進める
- **見積根拠**: 対象は ~150 行の純粋関数 + PyO3 雛形（maturin）+ 差分ハーネス。規模は「数日規模」クラスだが、**maturin/PyO3 の CI・Docker 導線を最初に立てる工数が本体**。Rust コード自体より検証基盤の方が重い見積が正しい

### 棄却条件（パイロット中止の基準）

- PyO3/maturin を Linux slim + CI で安定導入できない（abi3・toolchain 管理の課題が収まらない）
- 差分 corpus で**解消不能な不一致**が出る（＝現行 Python 実装の仕様自体が未定義振る舞いを含むと判明）
- 検証工数が実装工数を大幅に上回り続ける

## 2. 統合方式の比較 → 推奨

| 方式 | 構造 | 長所 | 短所 | 本リポジトリ適合 |
|---|---|---|---|---|
| **A. PyO3 / maturin ネイティブ拡張** | `.so` を import、関数単位で置換 | 同一プロセス・データ共有・段階的置換が自然・app.state 共有状態に手を出さずに済む | PyO3×Python3.14 対応・GIL 周り・Docker/CI に Rust toolchain 追加 | **推奨**。最小シームで最も安全に始められる |
| B. Rust サイドカー（axum 等） | 別プロセス化、HTTP/Unix sock で IPC | プロセス分離・個別スケール | IPC/直列化コスト・認証セッション・単一ワーカー前提の共有状態（index/reader_sync/positions）を跨ぐ再設計が必須・運用複雑化 | 今回は不採用 — app.state 共有が多く分離境界を引くと設計変更が大きい |
| C. Rust subprocess/CLI | 子プロセス呼出 | 分離最強・障害封じ込め | 毎回 spawn コスト・結果の直列化・unrar spawn と同じ構造で高速化の狙いに逆行 | 書庫列挙のような呼出し頻度の高い処理には不向き |
| D. 全面置換 / 大規模 Rust app | 段階到達点としての Rust サーバ | 最終形なら統一 | 前倒し不可・検証不能なビッグバン | 到達点としてのみ残す。W6 で再判定 |

**推奨 = A（PyO3）**。理由: 共有状態を持つ既存設計に対し「同一プロセス内・関数単位」で切替可能な唯一の方式で、切戻しが import フォールバックで済む。B/C は現段階では境界を引くコストが効果を上回り、D は Phase 4 以降の到達点候補。

## 3. 段階ロードマップ（依存順）

| Wave | 対象 | 移植形態 | 検証 | 性能目的 | Go/No-Go |
|---|---|---|---|---|---|
| **W1** | ソート/エントリモデル中核（natural_key/漢数字/_sort_key + archive 共有純粋関数） | PyO3 拡張 | byte 一致 corpus + 既存 pytest + live 回帰 | 非回帰（互換性主目的） | 検証基盤確立が第一成果物 |
| **W2** | archive リーダ（zip/epub リスティング + read_archive_entry の純粋部） | PyO3 | 列挙差分・形式別テスト・cold 時間差分 | **cold scan 54s→短縮**（実測 hotspot #1） | rar 圧縮エントリは外部ツール残留可 |
| **W3** | scan_tree / tree_signature / list_images | PyO3 | ツリー JSON 差分 + **digest 値固定テスト** | walk 1.07s・signature 828ms の CPU 部 | FS I/O 律速部は伸び代限定的 → 効果測定後に進行判定 |
| **W4** | metadata.index（walk/parse/search/facets） | PyO3 | comics/facets JSON 差分 | rebuild 12.0s / from_tree 2.6s の削減 | W1 前提（natural_key 依存） |
| **W5** | thumbs / sprite / view（Pillow→image-rs） | PyO3 | 知覚比較（閾値）+ HTTP 契約 | decode/resize 高速化 | **JPEG バイト非同一 → 検証方式変更が前提条件**。効果見合いか要判断 |
| **W6** | routes/配信層（ETag/304/Range/gzip/202）・auth・main/lifespan | PyO3 or axum 再判定 | 契約テスト網羅 + live 回帰 | HTTP 面 CPU 削減 | **ここで「PyO3 定住 vs サイドカー/全面置換」を再判定**。FastAPI 上の部分移植では効果薄の見込み |
| **W7** | reader_sync + folder_visibility + reading_positions | 再検討 | WS プロトコル差分・SQLite 契約 | — | 共有状態・対称制御の再設計が要。効果主目的でなければ手を出さない判断もあり得る |

**ゲート原則**（issue より）：各 Wave 完了時に (a) 差分検証緑 (b) ベースラインからの非回帰 or 定量的改善 (c) 運用・保守コストの記録、を満たして初めて次へ進む。効果が出ない Wave は「その実装を Rust にしない」決定として記録して戻る。

## 4. タスクテンプレート（各 Wave の PR が含むべき項目）

- **目的と境界**: 何を Rust 化し何を残すか（関数単位で列挙）
- **期待値の根拠**: 既存テスト名 + golden corpus 由来 + 実測値
- **差分検証**: 新規ハーネス or 既存差分手段の実行結果
- **ベンチマーク**: 同一環境で新旧並走した before/after（ms・メモリ）
- **切戻し手順**: env フラグ名 / import fallback の動作確認手順
- **監視ポイント**: ログ・メトリクス・live 回帰で見る値
- **影響範囲の既存テスト**: 変更した契約が踏むテスト一覧（弱体化していないことの証跡）
- **秘匿・セキュリティ**: live 名非開示の確認 / 新依存の供給元・ライセンス
- **live 回帰実施有無**: `BOOKQ_LIVE_ROOT=... ./scripts/live-regress.sh` の実行の有無と結果

## 5. 状態遷移と役割・コスト記録

### 実装単位の状態機械

```
調査 → 仕様固定 → 実装(両実装並存) → 差分検証 → ベンチ → 統合(フラグ既定ON)
     → 監視観察期間(≥1 live回帰・運用) → [採用確定] or [棄却→ロールバック]
```

- **両実装並存期間**: 各 Wave の統合直後は「フラグで新旧切替可能」状態を最低 1 つの live 回帰サイクル維持。採用確定後も Python 実装は「正本」として残し、Rust 側が落ちた時の fallback を維持する（issue の「既存実装を唯一の正本」）
- **役割**: 実装者（Devin）と別コンテキストのレビュー（人間 or 別エージェント）を分離。実装者自身の検証だけで確定しない
- **コスト記録**: 各 Wave で「実装工数 / 検証工数 / 得た改善（ms・MB）/ 増えた依存・toolchain」を表にして蓄積 → 次 Wave の Go/No-Go と効果見合いの判断材料
- **停止条件**: パイロット棄却条件（§1）が発動、または「改善なしで保守コストだけ増えた」と客観記録が出た場合はその Wave で停止・既存実装へ復帰し、以降の移植は棚卸しから再評価

## 6. 既存実装の可用性とロールバック（issue 問7 対応）

- **移植中も現行は生き続ける**: 全 Wave が PyO3 関数置換なら、Rust 拡張は「import できなければ Python に落ちる」形を維持 → 拡張ビルドが壊れてもアプリは起動する
- **即時切戻し**: `BOOKQ_RUST_*` フラグ or 拡張の ImportError で旧実装へ。HTTP 層・共有状態（index/reader_sync/SQLite/キャッシュ）は Python が正本を握り続けるので、Rust 側を外しても振る舞いは不変
- **イメージ単位のロールバック**: Rust 導入後も Docker イメージ SHA で旧ビルドへ戻せる（toolchain は builder stage に隔離）
- **不可逆ポイントの先送り**: 「全面置換（PyO3 でなく Rust サーバが本体）」は W6 の再判定まで決めない。それまでは常に Python が生存可能な構成を維持

## 7. Phase 1 の最初の実行項目（承認後の着手順）

1. `Cargo.toml`/`rust-toolchain`/`maturin` 雛形 + Docker builder stage + CI ジョブを立てる（製品コードはまだ触らない）
2. 差分ハーネス: Python↔Rust を同入力で呼ぶ pytest フィクスチャ + golden corpus 生成器（live 名は hash 化）
3. W1 の Rust 実装（natural_key/_normalize_kanji/_sort_key + archive 共有部）
4. byte 一致 corpus + 既存 pytest 両実装 + micro-bench
5. live 回帰 + 切戻し実演 → Go/No-Go 記録
