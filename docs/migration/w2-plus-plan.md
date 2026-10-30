# w2-plus-plan.md — サーバサイド全置き換えの順次計画（W2〜W7 + 収束形）

> Phase 1 追補文書（issue #163）。`roadmap.md` §3 の W2 以降を、実測 hotspot と依存グラフに基づき
> 「どの関数を・どのクレートで・どう検証するか」まで具体化した実行計画。
> 作成時点の前進: W1（ソートキー中核の PyO3 化）= PR #172 で検証基盤が稼働中。

## 0. 確立済み基盤（W1 で構築済み・全 Wave が再利用）

- `rust/bookq_rs` crate（pyo3 0.29 / abi3-py314 wheel / Cargo.lock 管理 / MSRV 1.85）
- maturin ビルド導線: Docker `rsbuilder` stage・`scripts/build-bookq-rs.sh`・CI の uv-gate `--with <wheel>` 注入・`--python 3.14` 固定
- 切替・切戻しの型: `BOOKQ_RUST=0` env + `ImportError` + 呼出単位フォールバック + `_py_*` 正本保持
- parity harness パターン: `tests/migration/`（edge + seeded rand + `BOOKQ_LIVE_ROOT` live corpus、失敗は hash prefix 報告で秘匿準拠）
- batch API の型: `sorted(zip(keys, names))` が key-lambda と要素単位で同一であることを利用

以降の Wave はこの型の上に積む。新しい構造（別プロセス・非同期橋渡し等）は原則導入しない。

## 1. Wave 順序（ROI 順、実測 hotspot に準拠）

```
W1 ✅ → W2 archive → W4 metadata → W3 scan/signature → W5 thumbs
      → W6 判定（ハイブリッド定住 or axum）→ W7 必要部分のみ
```

W3 を W4 より後に置く理由: index rebuild の FS walk と scan_tree の walk を**同一 Rust walker で共有**できる。先に metadata の walk を Rust 化してから scan へ横展開するか、W3+W4 の合体 wave にするかは W4 着手時に判断。

## 2. W2 — archive リーダ（最大 hotspot）

**対象**: `app/archive.py`（408 行）の読み取り系全般

| 関数 | Rust 移植先 | 備考 |
|---|---|---|
| `_zip_names` / zip エントリ読出 | `zip` crate | ⚠️ zipfile の cp437 フォールバック規則の一致が必須 |
| `_epub_entries` | `zip` + `quick-xml`/`roxmltree` | OPF spine 順・`%20` decode・`<img>`/svg `<image>` 抽出・壊れた EPUB の自然順フォールバック |
| `_rar_names` | 純 Rust RAR3/RAR5 ヘッダパーサ | rarfile の純 Python ヘッダ解析を移植。外部ツール不要域を維持 |
| `read_archive_entry` | zip/epub エントリ bytes | RAR **圧縮**エントリは unrar 子プロセスのまま残留（unrar crate = C++ 依存は別評価） |
| `_finalize`/`build_archive_tree`/`split_archive_entries` | 純粋ツリー構築 | W1 の `natural_key` に依存済み |

**parity 検証**: 実ライブラリ全書庫（rar 252 / epub 54 / pdf 1）で新旧リスティングを列挙差分 — エントリ名・順序・件数・失敗時 `[]` まで一致。`tests/fixtures/rar5.py` の手書き RAR 生成器はそのまま差分テストに転用可

**主リスク**:
- **zipfile のファイル名デコード**（UTF-8 フラグ無し → cp437）と `zip` crate の差 → 非 ASCII 名で不一致リスク最大。corpus に実ライブラリ書庫の全名を入れて焼き切る
- EPUB XML パース差（ElementTree vs quick-xml の名前空間・属性扱い）
- RAR ヘッダのエッジ（マルチボリューム・暗号化フラグ・壊れた書庫の fail-open `[]`）

**期待効果**: cold listing rar 中央 317ms / epub 277ms（Phase 0 実測）→ 1 桁台見込み。cold scan 54s → 数秒台、`/api/refresh` 中央 15.7s → 大幅短縮

**見積根拠**: 3 形式パーサ + EPUB XML + live 書庫差分ハーネス。W1 の ~4 倍の実装面だが検証は機械化可。**中規模**

## 3. W4 — metadata.index（rebuild 12s cold）

**対象**: `app/metadata/` 全体（index 270 + models 117 + facets 89 + search 74）

- metadata.toml パース → `toml` crate（tomllib 意味論一致。datetime 型差・`[comic]`/`[tags]`/`[source]` スキーマ・`|` 区切り artist の平坦化に注意）
- `rebuild`（FS walk）/ `rebuild_from_tree` / search relevance scoring / `facets_for` / `subtree_facets`

**parity**: comics 全件 JSON 一致 + search 結果順序一致 + facets/subtree_facets 集計一致

**期待効果**: `rebuild_from_tree` 2.6s（cold）/ 240ms（warm）→ CPU 部の削減でサブ秒化見込み。walk は W3 walker と共有で効率化

## 4. W3 — scan_tree / tree_signature / list_images（FS 走査層）

**対象**: `scanner.py` の走査部 + `tree_signature.py`（193 行）

**設計上の核心問題**: ツリーは ~205k の Python dict ノード — FFI で dict ツリーを組むと構築コストが移るだけになり得る。選択肢:
- (a) Rust がフラット列挙（path/type/mtime のタプル列）を返し Python が dict 化
- (b) Rust 側 compact 表現 + 最小変換層
- (c) scan_tree は Python のまま、iterdir/classification の hot 部のみ Rust
→ **着手時に micro-bench で (a)(b)(c) を実測選択**。これが W3 の主判断事項

- `compute_signature`: sha256 terms + 8 ワーカー BFS + d_type 最適化 → `sha2` + `walkdir`/自前 walk。**digest 値がテスト固定済み**（terms 同一性の検証手段が既存）
- `list_images`: `_sort_file` 経路に載るだけで追加移植は小さい

**parity**: ツリー JSON 全差分（live）+ digest 値一致 + symlink 循環/壊れケース

## 5. W5 — thumbs / sprite / view（CPU 変換層）

**対象**: `thumbs.py`（993 行）の decode→resize→JPEG encode + sprite 合成 + cache eviction

**合格条件の切替が前提（重大 caveat）**: **JPEG 出力バイトは実装非同一**（libjpeg-turbo vs `zune-jpeg`/`image` crate）。検証を「寸法 + フォーマット + decode 画素の近似閾値（PSNR/SSIM 等）+ HTTP 契約（ETag/cache/304/status）一致」に変更する。**byte 差分が必須なら W5 は棄却対象**
- disk cache のキー形式は維持（既存キャッシュとの互換性）
- `archive_pdf`（pypdfium2）は**対象外** — PDFium C++ を両側で呼ぶだけで伸び代なし

## 6. W6 — HTTP/配信層：全面置換の分岐点

**対象**: `routes.py` 1,736 + `folder_visibility_routes` 166 + `auth` 116 + `main` 241 + `compression`/`static_files`

ここで「PyO3 定住 vs Rust サーバ本体化」を再判定（issue 設問4 への最終回答）:

| 選択 | 内容 | 判断 |
|---|---|---|
| **A. ハイブリッド定住（推奨）** | FastAPI 殻は残す。重い処理は W1-W5 で Rust 化済み。ETag/304/Range/202/gzip/session/OAuth/Jinja/WS の意味論再実装は避ける | 配信層の Rust 化は薄いグルーの写し直しで効果薄・互換リスク高 |
| B. axum 全面置換 | 全エンドポイント + ミドルウェア + セッション + WS + テンプレートを axum 再実装 | 「全部 Rust」が目的なら選択可だが、契約テスト整備が本質工数で効果主目的の正当化は困難 |

**事前条件（B を選ぶ場合のみ）**: ETag/304/Range/gzip/202/セッション/OAuth/SW 契約の網羅的契約テストが先に必要（現状は局所テストの集合であり、契約スイートとしての整理が要）

## 7. W7 — 有状態・対称制御層

**対象**: `reader_sync/` ~2.5k 行（protocol/model/state/sessions/catalog/transport）+ `folder_visibility` 217（SQLite）+ `reading_positions`（インメモリ）

- `catalog` の manifest/digest 部は純粋処理で移植可
- WS/セッション/heartbeat/dedup は**単一ワーカー前提の共有状態** — Rust 化には構成変更を伴い、性能動機が薄い
- `folder_visibility` は rusqlite で技術的には移植可だが運用メリット薄
- **判断**: 必要部分のみ移植 or 残置が有力な結論。「全て Rust」が目的なら選択するが、効果主目的では対象外が妥当

## 8. 各 Wave 共通の合格条件（W1 型を踏襲）

1. **差分検証緑**: live corpus or digest/JSON 一致の機械検証
2. **ベンチ記録**: 同一環境・同一データで新旧並走（非回帰 or 改善）
3. **切替・切戻し**: `BOOKQ_RUST_*` env + `ImportError` + フォールバック実演（wave ごとのフラグまたは統一 `BOOKQ_RUST`）
4. **live 回帰**: `BOOKQ_LIVE_ROOT=... ./scripts/live-regress.sh` 実施
5. **効果判定**: 効果なしなら「Rust 化しない」決定として記録して戻る（Go/No-Go ゲート）

## 9. 収束形の見通し（正直な予測）

- **Rust に移る部分（~80% の CPU 純粋処理）**: sort core ✅ / archive listing / index / scan core / thumbs / signature — 実測改善が見込める領域
- **Python が残る部分（~20%）**: HTTP 殻（routes/main/auth/middleware）・WS reader_sync・SQLite visibility・キャッシュ機構 — 経済的に合理的な終着点
- **「全て Rust」は W6 の判定事項** — issue の「Rust 化を目的化しない」原則上、効果主目的では A（ハイブリッド定住）が推奨結論。B（axum 全面置換）は「Rust 化そのものを目的とする場合」のみ採用
