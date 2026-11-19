# performance-baseline.md — ベースライン測定計画と実測結果（Rust 移植 Phase 0）

> Phase 0 調査成果物 4/6（issue #163）。サーバ側 hotspot の実測 + 既存フロント KPI の引用。
> **注意**: 値は「同一条件での相対比較」用のベースライン。絶対性能の主張ではない。実ライブラリ名は秘匿ルールにより出さない。

## 1. 測定環境・方法

| 項目 | 値 |
|---|---|
| 実施日 | 2026-10-29 |
| 対象コード | `d024ef0`（origin/main = worktree HEAD、コード差なし） |
| 環境 A（合成） | `./dev.sh up rust-m0-syn --local --port 8080 --fixture` → `http://127.0.0.1:8080`（tests/fixtures/uiux_tree.py 生成、100 folder/146 image、書庫なし） |
| 環境 B（実データ） | `DOWNLOADS_HOST_DIR=<live> ./dev.sh up rust-m0-live --local --port 8099` → `http://127.0.0.1:8099`（prod と同一ソースを **ro マウント**、5 トップレベル） |
| 環境 C（書庫合成） | /tmp 生成ツリー（zip12/cbz6/rar6/epub6/pdf6・30頁各 + 平ディレクトリ。`make_archive_tree.py`） |
| プローブ | ホスト側 `probe_scan.py`（app を直接 import、scan/index/reader を計時）・`probe_http.py`（urllib wall-time + docker stats）— evidence: `.omo/evidence/rust-migration-phase0/` |
| 実行形態 | シリアル（単一ワーカーのため並列計測は汚染する）。各値は 3–5 run の中央値 |
| データ規模（実測） | **folder nodes 3,177 / image leaves 205,891 / archive nodes 307（rar 252・epub 54・pdf 1・zip 0）** |

## 2. 実測結果

### 2.1 プロセス内 hotspot（probe_scan.py、host 直 import）

| 計測 | 合成(fixture) | 書庫合成 | **実データ** |
|---|---|---|---|
| scan_tree shallow（書庫 opaque） | 3.2ms | 0.8ms | **1,219ms** |
| scan_tree full | 2.7ms | cold 22.5ms→warm 3.3ms | **cold 54.2s** → warm 1,199ms |
| ├ walk_ms 中央 | 2.3ms | 0.7ms | 1,067ms |
| ├ archive_ms 中央（CPU span 和） | — | 6.5ms | 247ms |
| tree_signature | 4.9ms | 0.8ms | **828ms** |
| MetadataIndex rebuild（FS walk） | 3.0ms | 0.8ms | **11,960ms** |
| MetadataIndex rebuild_from_tree | 0.8ms | 0.7ms | **2,575ms** |
| **archive listing cold**（1書庫あたり中央値） | — | zip 0.6ms/epub 1.1ms/rar(store) 0.9ms/pdf 0.7ms | **rar 317ms（p95 994ms, max 5.5s）・epub 277ms（p95 1.0s）・pdf 299ms** |
| archive listing warm（disk cache hit） | — | ~0.01ms | ~0.1ms |

### 2.2 HTTP/コンテナ（probe_http.py + docker）

| 計測 | fixture | **実データ** |
|---|---|---|
| `/api/folders` full warm 中央 | 21.8ms（18.6KB） | **1,019ms（12.29MB body）** |
| `/api/folders?depth=1` | 26.9ms | 24.1ms（247B） |
| `POST /api/refresh`（同期 full rebuild） | 290ms | **中央 15.7s**（runs: 41.1/31.6/15.7/6.4/6.1s — キャッシュが温まるにつれ低下） |
| `/image/` loose | 22.9ms | 1.7ms |
| `/image/` archive entry（rar） | — | 14.2ms |
| `/image/` PDF page（pdfium render） | — | **778ms/ページ** |
| `/thumb?w=400`（cache hit） | 16.6ms | 0.9ms |
| 起動→起動完了 | ~1.3s | **~17.7s**（shallow warmup 16.0s） |
| RSS（docker stats） | 102–104MiB | 373–399MiB（prod viewer 186MiB idle） |

### 2.3 既存フロント KPI（引用: docs/performance.md）

| 指標 | ベースライン(9/21) | after PR#192 | 目標 |
|---|---|---|---|
| トップ初タイル cold | 826ms | 506ms | ≤300ms |
| トップ転送量 | 8.3MB | 576KB | ≤1MB |
| 本を開く→初画像 | 55ms | 54ms | ≤150ms 維持 |
| ページ送り long task | 0 | 0 | 0 維持 |

`test_perf_budget.py` 等が CI で転送量・プリフェッチ数・long task を決定的にゲート（ms 閾値は手動計測）。

## 3. 分析 — 実測された hotspot と Rust 化の効果見通し

| 順位 | hotspot | 実測 | Rust 化の見通し |
|---|---|---|---|
| 1 | **RAR/EPUB リスティング**（cold） | rar 中央 317ms・epub 277ms × 307 件 → cold scan 54s、refresh 15.7s の主因。rarfile の純 Python ヘッダ解析 + zipfile+ElementTree が律速 | **効果大**。書庫ヘッダ解析は CPU 純粋処理で Rust が最も効く領域。warmup disk cache があるため「cache ミス時」のみだが、初回/無効化時の体感を直接支配 |
| 2 | **full tree の JSON 応答サイズ** | 12.3MB body で warm 1.0s（serialize は世代キャッシュ済み → 実コストは転送+gzip+client 側 parse） | Rust 化で改善しにくい（帯域・フロント側コスト）。body 削減（差分/snapshot 圧縮）はアーキテクチャ側の別論題 |
| 3 | **MetadataIndex rebuild** | FS walk 12.0s / from_tree 2.6s（live） | walk 自体は FS I/O 律速だが、現在 refresh は `_scan_and_rebuild`（1 walk 共有）を通るため scan と一体改善。from_tree 2.6s は parse/facets の CPU で Rust 化余地あり |
| 4 | **scan_tree walk** | live 1.07s（walk_ms） | iterdir+stat の I/O 律速が主。Rust で syscall 経路は変わらず効果は限定的 — flatten/sort/文字列処理の一部は改善余地 |
| 5 | **PDF page レンダ** | 778ms/頁 | pypdfium2（C++ PDFium）が既にネイティブ — Rust pdfium crate も同一エンジン呼出で**効果ほぼなし**。対象外推奨 |
| 6 | **archive entry 配信** | rar entry 14ms/頁（圧縮は外部 unrar spawn/頁） | spawn 境界は残る。プロセス内ライブラリ化できれば効くが unrar crate は C++ バインドで導入判断が要 |
| 7 | **thumb 生成（Pillow）** | cache hit 0.9ms / miss は画像 decode+resize+JPEG encode | Rust image-rs は高速だが**JPEG 出力バイト非同一**（検証方式を要変更）。decode/resize 高速化余地あり、優先度中 |
| 8 | **tree_signature** | 828ms（live） | scandir+sha256。Rust 化で hash 計算は同等、d_type walk は同じ OS 境界 → 効果は限定的（CPU は小さい） |
| 9 | **起動** | live ~17.7s（warmup 16s = shallow scan+index+signature が主） | 上記 1/3/4/8 の改善がそのまま効く。import 自体は ~50ms で小さい |

**総括**: 最大の CPU 律速は **書庫リスティング（RAR/EPUB のコンテナ解析）**。第2は metadata index の walk/parse。配信層・PDF は既にネイティブ/外部依存で Rust 化の伸び代が小さい。→ 移植順は dependency-graph.md §4 の W1→W2 を支持。

## 4. 未測定・既知の限界

- **書庫 1 件あたりの分散**: rar max 5.5s・epub p95 1.0s — 大書庫の外れ値が主コスト。個別ファイルのサイズ分布は記録していない（秘匿ルールで個別名を出せないため、suffix×件数の集計のみ）
- **solid RAR の逐ページ展開コスト**: 設計上「n 頁目で 0..n 展開」の既知特性（CLAUDE.md）だが実測は未取得 — W2/W6 で計測タスク化
- **WS/reader_sync の負荷**: フレーム頻度・遅延の実測なし（Phase 0 範囲外・L5 移植前に取得）
- **gzip 圧縮 CPU**: 12.3MB body の /api/ gzip コストは観測したが分離計測していない
- **実データの再現性**: ライブラリ内容は時刻で変わり得る。本表は d024ef0+当日状態の一点観測 — 比較は「同一データ・同一環境」内のみ有効
- **フロント KPI の再測定は未実施**（ユーザー選択: docs/performance.md 引用に留める）。Phase 1 以降の before/after で必要に応じて perf-measurement skill を使用
