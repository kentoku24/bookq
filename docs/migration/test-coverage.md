# test-coverage.md — 既存テストの範囲と不足（Rust 移植 Phase 0）

> Phase 0 調査成果物 3/6（issue #163）。`tests/` 107 ファイル + `tests/live/` 3 ファイルを機能面で分類（基準 `d024ef0`）。
> 実行: `BOOKQ_TEST_MODE=true uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/ -q -n auto`（CI `workflow-smoke.yml` と同一）。

## 1. カバレッジ地図（移植対象レイヤ別）

| レイヤ | 主なテストファイル | 性質 | 移植時の固定力 |
|---|---|---|---|
| L0 ソート/漢数字 | `test_scanner.py`（natural_key・_kanji_run_to_int・並び・tie-break） | pytest 単体 | ★★★ 仕様を直接固定。漢数字の保護名詞ケース（千尋・六本木・全角数字非変換）あり |
| L0 archive 純粋関数 | `test_archive.py`（_is_safe_entry/_is_image_entry/_finalize/build_archive_tree/split_archive_entries） | pytest 単体 | ★★★ 木形状・危険パス・cap・dedup まで固定 |
| L1 archive リーダ | `test_archive.py`/`test_archive_nested.py`/`test_archive_split.py`/`test_filetype_matrix.py`（zip/rar5手書き/epub/pdf） | pytest 単体〜API | ★★★ 形式別列挙・spine 順・壊れた書庫の fail-open・501 を固定 |
| L1 warmup_cache | `test_warmup_persistent_cache.py`/`test_warmup_cache_mirror.py`/`test_warmup_cache_race.py` | pytest 単体 | ★★★ キー形式・TTL・eviction・競合まで固定 |
| L1 folder_visibility | `test_folder_visibility_store.py`/`_unit.py`/`_api.py`/`_ui.py`/`_navigation.py`/`_pair.py` | pytest 単体〜E2E | ★★★ SQLite 契約・owner 検証・fail-closed |
| L2 scan_tree | `test_scanner.py`/`test_folders_shallow.py`/`test_mixed_folder.py`/`test_scan_phase_timings.py`/`test_scanner_perf_todo6.py`/`test_shallow_warmup.py`/`test_background_fill.py` | pytest 単体〜API | ★★★ flatten・thumb/thumbs・symlink・opaque 形状・phase 計時を固定 |
| L2 tree_signature | `test_tree_signature.py`（digest 値固定・stat 回数決定性） | pytest 単体 | ★★★ digest 値ピン＝terms 同一性の強い検証手段が既存 |
| L2 metadata | `test_metadata.py`/`test_filters_e2e.py`/`test_search_e2e.py`/`test_index_gate.py`/`test_index_building_202.py`/`test_ensure_index.py`/`test_stale_serve.py`/`test_refresh_endpoint.py` | pytest 単体〜API | ★★☆ ComicMetadata 形状・スコア・202/stale 意味論を固定 |
| L3 thumbs/view/sprite | `test_thumbs.py`/`test_thumb_cache_hit_no_open.py`/`test_thumb_sprite.py`/`test_view_endpoint.py`/`test_cover_warmup.py`/`test_thumbnail_measurement.py`/`test_thumbnail_simulator_mode.py` | pytest 単体〜E2E | ★★☆ 幅・キャッシュ・status を固定。**JPEG バイト値は固定していない**（decode/pixel 等価は別検証が要） |
| L4 HTTP/配信 | `test_image_revalidate.py`/`test_traversal.py`/`test_compression.py`/`test_static_headers.py`/`test_ui*.py`/`test_browse_*.py`/`test_offline*.py`/`test_sw_offline_contract.py`/`test_pwa_icons.py`/`test_reading_position.py`/`test_url_state.py`/`test_wipe_all.py`/`test_harness_prod.py`/`test_test_mode_fail_closed.py`/`test_compose_config.py` | API〜Playwright | ★★☆ ETag/304/Range/403/404/501/gzip 範囲・SW 契約を固定 |
| L5 reader_sync | `test_reader_*.py` 14 本 + `tests/fixtures/reader_sync_cases.json` + `reader_sync_tree.py`（pairing/state/model/ws/transport/manifest/pairUI/統合） | pytest 単体〜WS | ★★☆ フレーム語彙・状態遷移・manifest digest を固定 |
| 横断 perf 予算 | `test_perf_budget.py`（転送量・プリフェッチ数・long task）/`test_frontend_perf_lifecycle.py`/`test_top_display_perf.py`/`test_warmup_*.py`/`test_perf_toggle.py` | CI 決定的 | ★★☆ 性能回帰の早期検知（ms 指標でなく回数/bytes/long-task 系） |
| 実データ回帰 | `tests/live/test_live_api.py`/`test_live_images.py`/`test_live_viewer.py`（`BOOKQ_LIVE_ROOT` 時のみ収集） | live | ★★★ 実ライブラリで API/画像/ビューアを検証。秘匿ルール（hash アドレッシング・before/after 同一性検査）付き |

## 2. 移植に使える既存の検証資産

- **決定的ユニット**: scanner/archive/metadata/tree_signature の純粋関数は pytest で直接固定済み — Rust 実装を同じ入力で呼ぶ差分ハーネスにそのまま転用可能
- **digest ピン**: `test_tree_signature.py` は sha256 値を固定 → W3 で「同一ツリー同一 digest」を機械検証できる
- **手書き RAR5 フィクスチャ**: `tests/fixtures/rar5.py` で unrar 無しに RAR 生成可能 — Rust 側 RAR リーダの差分テストにそのまま使える
- **WS プロトコル事例集**: `reader_sync_cases.json` + fake-clock 設計（sessions.py）は言語非依存の状態機械テストに転用可能
- **HTTP 契約**: `_is_within`/ETag/Range/202 の各テストは、配信層を移植しても「同じ応答」を黒箱で検証できる
- **live 回帰**: 実ライブラリに対しフォルダ名を出さずに差分検証できる唯一の層 — Phase 1 以降の新旧比較で主力

## 3. 不足・ギャップ（Phase 1 の検証基盤整備で埋める対象）

| ギャップ | 影響 | 対応案 |
|---|---|---|
| **新旧実装の直接差分ハーネスがない** | 関数単位の golden-diff が未整備 | corpus（実ライブラリ由来の名前集合＋合成エッジケース）を両実装に通す比較器を Phase 1 で構築。名前 corpus はライブラリ由来だが hash/インデックス化して秘匿 |
| **JPEG 出力の等価性検証がない** | W5 thumbs でバイト差分不可（エンコーダ差は必然） | 寸法・フォーマット・デコード画素の近似（例: 許容 PSNR/SSIM 閾値）＋HTTP 契約比較に切替。採用基準は Phase 1 で合意が必要 |
| **ベンチマークの CI 化なし** | 性能改善の再現検証が手動 | criterion/pytest-benchmark 等の micro-bench を `cargo bench`＋差分レポートで運用。実測値比較は performance-baseline.md の手順で |
| **WS 長時間・再接続ストーム系の不足** | reader_sync 移植時の耐久検証 | fake-clock ロジックは移植可能だが実ソケットの負荷試験は要新設 |
| **メモリ/RSS の回帰検知なし** | メモリ削減目標の定量的評価 | docker stats / psutil サンプリングを測定スクリプト化（baseline 収集済み、Phase 1 で閾値検討） |
| **epub/RAR 大容量・壊れ系の網羅** | 現フィクスチャは小規模正常系中心 | 実ライブラリ由来の列挙差分（live 回帰）でカバー＋合成の極端ケース追加 |
| **「must match JS byte-for-byte」対応物不在** | scanner.py:45 コメントに対応する JS 実装が `app/static/` に見当たらない（natural_key が "sole source of sort truth" と明記する以上、陳腐化の可能性） | パイロット仕様固定時にコメントの出処・現行 JS 契約を確認し、誤記なら Python コメント側を是正するタスクとして処理（本 issue 外の小修正扱いで切り出し） |

## 4. テスト運用ルール（移植で踏むべきでない制約）

- 実装側が既存テストを削除・弱体化しない（issue 基本方針）。期待値変更は「実際の仕様変更の有無」を明記してレビュー対象
- `BOOKQ_TEST_MODE`・`BOOKQ_STATE_DIR` sandbox・`DOWNLOADS_DIR` 差し替えは conftest 管理 — Rust 側も同 env 契約を維持
- `tests/live/` の秘匿ルール（実フォルダ名をコード/ログ/レポートに出さない）は差分ハーネス・ベンチマークにも適用
- CI ゲート: app/tests/** 変更で workflow-smoke がフルスイート実行。docs 単独は対象外
