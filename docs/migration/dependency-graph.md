# dependency-graph.md — モジュール依存と移植順序（Rust 移植 Phase 0）

> Phase 0 調査成果物 2/6（issue #163）。依存関係は `app/**/*.py` の import 文を全件検証した実測値（基準 `d024ef0`）。

## 1. 検証済み import グラフ

```
main ─→ auth ─→ config ─→ folder_visibility (DB_FILENAME のみ)
    ─→ compression（starlette のみ）
    ─→ folder_visibility_routes ─→ auth, folder_visibility, routes(_ensure_tree_and_index)
    ─→ routes
    ─→ reader_sync.sessions → reader_sync.transport
    ─→ static_files

routes ─→ auth, archive, archive_pdf, folder_visibility,
        ─→ folder_visibility_routes (lazy: hidden_roots_for_request)
        ─→ metadata, scanner, thumbs, tree_signature, jinja2

scanner ─→ archive   (lazy: ARCHIVE_EXTENSIONS/EPUB_EXTENSIONS/build_archive_tree/list_archive_entries)
        ─→ archive_pdf (lazy: pdf_page_count/pdf_page_name)
archive ─→ scanner   (IMAGE_EXTENSIONS/folder_cover_fields/natural_key), warmup_cache
archive_pdf ─→ warmup_cache
tree_signature ─→ archive (ARCHIVE_EXTENSIONS), scanner (PDF_EXTENSIONS)

metadata.index ─→ archive (ARCHIVE_EXTENSIONS), folder_visibility, scanner (natural_key/PDF_EXTENSIONS)
metadata.facets ─→ folder_visibility (covered_by_roots)
metadata.search ─→ scanner (natural_key)
metadata.models ─→ (tomllib のみ)

thumbs ─→ archive, archive_pdf, scanner (is_pdf_file), PIL
       ─→ routes   (lazy: _is_within/_not_modified/_resolve_archive)

reader_sync.protocol ─→ (定数のみ)
reader_sync.model ─→ (dataclasses のみ)
reader_sync.state ─→ model, protocol
reader_sync.sessions ─→ protocol, state, catalog (lazy: ManifestError/split_manifest)
reader_sync.catalog ─→ archive, model, protocol, scanner,
                     ─→ routes (_ensure_tree_and_index/_find_tree_node/_is_within/_list_folder_images/_resolve_archive)
reader_sync.transport ─→ auth (user_from_connection), sessions, catalog (lazy)

warmup_cache ─→ (stdlib のみ)
folder_visibility ─→ (sqlite3/hmac のみ)
```

## 2. 循環依存（lazy import で回避しているもの）

| 循環 | 構造 | 移植への示唆 |
|---|---|---|
| `scanner ⇄ archive` | scanner が lazy import で archive の reader を呼び、archive は scanner の `natural_key`/`folder_cover_fields`/`IMAGE_EXTENSIONS` を使う | **実質の最小単位は scanner の文字列関数群**。ここを先に切り出すと両側が Rust 実装を共有できる |
| `thumbs → routes` | thumbs が routes の `_is_within`/`_not_modified`/`_resolve_archive` を lazy import（routes は thumbs の handle_* を呼ぶ） | 配信系は後段。thumbs を先に移植するならこれら 3 関数を先に移す必要あり |
| `routes ⇄ folder_visibility_routes` | routes が lazy import で `hidden_roots_for_request` を呼び、同 routes の `_ensure_tree_and_index` を同モジュールが使う | 小さいが相互依存。同じ wave で扱う |
| `catalog → routes` | reader_sync.catalog が routes の tree/index キャッシュと画像列挙コアを共有（"no second enumeration implementation" が明示の設計意図） | **共有状態への依存が最深**。reader_sync は最終盤 |

## 3. レイヤリング（移植順序の基礎）

```
L0 純粋・定数/小関数層
   scanner の文字列関数群（natural_key/_normalize_kanji/_sort_key/IMAGE_EXTENSIONS）
   archive の純粋関数（_is_safe_entry/_is_image_entry/_finalize/split_archive_entries/build_archive_tree/folder_cover_fields 系）
   reader_sync.protocol 定数、model（Spread/PageRef 純粋関数）
   metadata.search/facets の純粋関数、covered_by_roots、validate_root

L1 ファイル形式・キャッシュ層（決定的 I/O）
   archive のリーダ（_zip_names/_rar_names/_epub_entries/read_archive_entry）
   archive_pdf（pypdfium2 経由ページ数/レンダ）
   warmup_cache（JSON キャッシュ）
   folder_visibility（SQLite）

L2 走査・集計層（FS I/O + 集計ロジック）
   scanner.scan_tree / list_images
   tree_signature.compute_signature
   metadata.index（rebuild/rebuild_from_tree/search/facets_for/subtree_facets）

L3 CPU 変換層
   thumbs（decode→resize→JPEG、sprite 合成）
   compression（gzip）はミドルウェア枠組み側

L4 HTTP/セッション層
   routes（全エンドポイント、_ensure_tree_and_index 等キャッシュ機構、_background_fill/_warm_covers）
   folder_visibility_routes、auth、static_files、main/lifespan

L5 有状態・対称制御層
   reader_sync.sessions/state/catalog/transport（WS、heartbeat、dedup、manifest）
   reading_positions、SessionMiddleware/OAuth
```

## 4. 移植順序案と根拠

原則（issue #163 の方針）：依存の少ない純粋ロジックから。副作用のある I/O は出力が決定的に差分可能なものに限る。最終順序はこのグラフで決める。

| Wave | 対象 | 選定根拠 | 差分検証の容易性 |
|---|---|---|---|
| W1 | **L0 ソート/エントリモデル中核**（natural_key 族 + archive 純粋関数） | 全消費者（scanner/archive/metadata/catalog）が共有する最底辺。scanner⇄archive 循環の「結び目」であり、ここを Rust 化すれば両側が同実装を参照する | ★★★ 文字列入出力を byte 比較できる |
| W2 | **archive リーダ**（list_archive_entries の zip/epub 系 + read_archive_entry） | 実測 hotspot（live: rar 中央 317ms・epub 277ms）。出力がエントリ名列挙=決定的。RAR 展開のみ外部ツール依存のまま残留可 | ★★★ 同じファイルに対する列挙差分 |
| W3 | **scan_tree / tree_signature / list_images** | 実測 walk 1.07s・signature 828ms（live）。署名値はテストが digest 固定済み（terms 同一性の検証手段が既存） | ★★☆ ツリー JSON・digest 値比較 |
| W4 | **metadata.index**（walk/parse/search/facets） | rebuild 実測 12.0s（live）。natural_key 依存 → W1 前提 | ★★☆ comics/facets JSON 差分 |
| W5 | **thumbs / sprite / view** | CPU 集約だが **JPEG 出力バイトが実装非同一**（エンコーダ差）→ 知覚・寸法比較に検証方法を変更する必要。Pillow→image-rs の品質検証が本質 | ★☆☆ バイト比較不可（後述） |
| W6 | **routes/配信層・auth・main** | HTTP 意味論（ETag/304/Range/gzip/202/セッション）の正確な再実装が要。FastAPI 上の部分移植は効果薄 → この wave で「全面置換かハイブリッド定住か」を再判定 | ★★☆ 契約テストで網羅可だが面が広い |
| W7 | **reader_sync + folder_visibility + reading_positions** | WS・対称制御・SQLite・app.state 共有。単一ワーカー前提の in-process 設計を Rust 側で等価にするには構成変更を伴う | ★★☆ プロトコル差分テスト |

**パイロット**: W1 のうち「ソート/エントリモデル中核」— roadmap.md §3 で詳述。

## 5. Python 側シーム（A/B 切替点）

各 wave で Rust 側と切替可能な自然なシーム：

- W1: `app/scanner.py` の関数単位（`natural_key` 等）— 呼び出し側で `try: from bookq_rs import natural_key` / `except ImportError: python` のフォールバック形、または `BOOKQ_RUST_SORT` env フラグ
- W2: `archive.list_archive_entries` / `read_archive_entry` の関数単位
- W3: `scanner.scan_tree` / `tree_signature.compute_signature` の関数単位
- W4: `MetadataIndex` のメソッド単位（walk/parse）
- W5: `thumbs.make_thumbnail`/`compose_sprite` 系（出力比較方式の変更が前提）
- W6/W7: エンドポイント単位または全面移行判定後に構造ごと

切替境界は「同一プロセス内関数呼出（PyO3）」を基本設計とし、W6 以降で別プロセス化を再検討する（roadmap.md §2）。
