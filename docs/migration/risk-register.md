# risk-register.md — Rust 移植リスクレジスタ（Phase 0 成果物 6/6）

> Phase 0 調査成果物 6/6（issue #163）。技術・互換性・運用・切戻しのリスクを、発見した構造的事実と対応付けて列挙。
> 評価基準: 基準コミット `d024ef0`、実測値は performance-baseline.md。

## 凡例

- 影響: H（機能破壊・互換違反）/ M（部分機能・性能回帰）/ L（局所・一時）
- 発生確度: 高 / 中 / 低
- 状態: Open（未処置・Phase 1+ で対応）/ Mitigated（計画で対策済み）/ Accepted（受容判断）

## A. 技術リスク

| # | リスク | 影響 | 確度 | 根拠（構造的事実） | 対応 |
|---|---|---|---|---|---|
| A1 | **`natural_key` の型混合タプル比較**を Rust で忠実再現できない | H | 中 | 返り値が `int|str` 混在 tuple の list（Python の tuple 比較規則に依存）。PyO3 で tuple を往復させると型変換の境界で差が出やすい | パイロットの core。Rust 側で比較規則を内部完結させ key を不変バイト列に落とす設計にし、byte 一致 corpus で全件検証 |
| A2 | **PyO3 × Python 3.14 の互換性** | H | 中 | `python:3.14-slim` 前提。PyO3 の 3.14 対応版・abi3 wheel・maturin 生成物が 3.14 で安定するか未検証 | Phase 1 最初に toolchain 雛形で検証。abi3 指定 + バージョンピン。不安定なら Python を拡張対応版まで固定する判断も視野 |
| A3 | **RAR 圧縮エントリの外部ツール依存**が残る | M | 高 | rarfile はリスティングのみ純 Python、圧縮読出は unrar/7z spawn/頁。Rust 化しても spawn 境界は消えない（unrar crate は C++ バインド） | W2 でリスティングのみ移植し展開経路は残留可と明示。「spawn 自体をプロセス内化するか」は別評価（効果・依存増の見合い） |
| A4 | **JPEG エンコーダ差でバイト非同一** | M | 確実 | Pillow(libjpeg-turbo) と Rust image-rs(jpeg-decoder/zune-jpeg 系) はエンコード出力が一致しない | W5 の前提条件：検証を「寸法+デコード画素近似+HTTP契約」に切替。byte 差分が必須なら W5 は棄却対象 |
| A5 | **GIL / 並行性の扱い** | M | 低 | 単一ワーカー・async 前提で共有状態は thread-safe 設計でない。PyO3 で GIL 解放し並列化すると意図しない共有状態競合を生む可能性 | 初期は GIL 保持のまま直列移植（最適化しない）が原則。並列化は構造を理解してから別判断 |
| A6 | **EPUB XML パース差異** | M | 中 | `ElementTree` 依存。OPF/spine 順・`%20` decode・SVG `<image>` 抽出のエッジを Rust XML クレートが一致させられるか | 列挙差分テストで spine 順を全件比較。不一致は棄却条件 |
| A7 | **pypdfium2 → Rust pdfium crate** | L | — | 両者とも同じ PDFium C++ を呼ぶだけ。Rust 化の伸び代ほぼなし | **対象外推奨**（baseline §3）。効果なしを記録して外す |
| A8 | **Unicode/ファイル名表現の差** | H | 中 | `os.listdir` の surrogateescape・NFC/NFD・`_is_within` の posixpath.normpath 契約を Rust の `PathBuf`/UTF-8 前提が破る可能性 | corpus に非ASCII・surrogate・`..`/正規化名を含める。`_is_within` 同等の正規化を Rust で保証する検証を必須化 |
| A9 | **tree_signature の d_type 最適化** | M | 低 | `stat()` を省く設計は `DirEntry.is_dir(follow_symlinks=False)` の d_type 利用。Rust `read_dir` は同等だが FS 実装差で挙動が変わり得る | digest 値固定テストが既存なので terms の同一性を機械検証。挙動差はテストが捕捉 |

## B. 互換性リスク（外部観測可能な振る舞い）

| # | リスク | 影響 | 確度 | 根拠 | 対応 |
|---|---|---|---|---|---|
| B1 | **ソート順の byte 同一性崩壊** | H | 中 | 漢数字正規化・`_sort_key`・natural_key がツリー/アーカイブ/metadata の全表示順を支配。1件でも順が変われば UI 差分 | W1 の合格線を「golden corpus 全件一致 + live 回帰で表示順同一」に固定 |
| B2 | **ETag/304/Range の微細差** | H | 低 | loose/archive/PDF で ETag 生成式・304・206 が個別に実装済み。配信層を移植するとキャッシュ・Range 意味論が壊れやすい | W6 まで配信層に触れない。触れる場合は契約テストで全パターン網羅 |
| B3 | **Service Worker / 静的資産契約** | H | 低 | SHELL_ASSETS のロード順・`no-cache`・`Service-Worker-Allowed`・gzip 範囲が SW オフラインの前提 | static/template 周辺は移植対象外に維持。触れる場合は test_sw_offline_contract 系を先に緑にする |
| B4 | **WS プロトコル語彙・状態機械** | H | 中 | protocol v1・6桁コード・epoch・dedup・manifest digest が多層で実装済み | W7 を最終盤に据え、`reader_sync_cases.json` を言語非依存の差分事例集として先に整備 |
| B5 | **202/インデックス構築の意味論** | M | 低 | 202 + Retry-After・stale-serve・generation ゲートは UI が直接依存。Rust 側キャッシュに移すとタイミング差が出る | index/tree キャッシュ機構は Python 側に留める設計（Rust はデータ生成のみ）で回避 |
| B6 | **folder_visibility SQLite 契約** | M | 低 | WAL・revision・HMAC owner・スキーマが確立済み | SQLite 層は移植対象外に維持。読み出しは SQL 契約のまま |

## C. 運用・プロセスリスク

| # | リスク | 影響 | 確度 | 対応 |
|---|---|---|---|---|
| C1 | **Docker ビルド複雑化** | M | 高 | Rust toolchain + maturin を builder stage に追加でビルド時間・イメージ管理が増える | multi-stage で toolchain を builder に隔離。イメージサイズが増えないことを計測対象に |
| C2 | **二実装 drift** | M | 中 | Rust 移植後も Python 実装が正本。片方だけ修正して挙動がずれる | フラグ切替 CI で両系統を毎回走査（新旧同テスト）。drift 検知を自動化 |
| C3 | **レビュー負荷・実装と検証の非対称** | M | 中 | issue が「実装と検証を同時進行・別コンテキストレビュー」を要求。検証工数が実装より重い構造になりやすい | タスクテンプレ（roadmap §4）で検証項目を事前固定し、レビューは差分 corpus・ベンチ・切戻し証跡に絞る |
| C4 | **CI 時間・依存管理の増大** | L | 中 | `cargo build`・crates 依存・rust-toolchain 管理が CI に加わる | crates は最小限（PyO3 + 必要 crate のみ）。lock 管理・供給元レビューをタスクテンプレの「新依存」項目で強制 |
| C5 | **パフォーマンス目標の未達** | M | 中 | baseline §3 で「Rust 化しても効果が薄い」領域（PDF/配信/walk I/O 律速）が存在 | 各 Wave の Go/No-Go で「効果なしなら Rust 化しない」と明示的に記録して戻る運用。効果が出ない領域は素直に対象外化 |
| C6 | **実ライブラリへの依存計測の秘匿** | L | 中 | 実データ差分が最有力な検証だが、フォルダ名をコード/ログ/レポートに出せない制約 | hash 化 corpus・集計値のみ記録・秘匿ルールを差分ハーネスの設計要件化（tests/live ルール流用） |

## D. 切戻しリスク

| # | リスク | 影響 | 確度 | 対応 |
|---|---|---|---|---|
| D1 | **切戻し手段が実装されていない** | H | 中 | env フラグ/import fallback を「後で足す」設計だと Rust 障害時に戻せない | 各 Wave の統合時点で fallback を必須実装。切戻し実演を PR の合格条件化 |
| D2 | **共有状態を Rust に移し過ぎて戻せなくなる** | H | 低 | app.state の index/reader_sync/positions を Rust 側に移すと fallback が効かなくなる | 設計原則として「共有状態は Python が正本を握り続け、Rust は純粋関数/決定的 I/O に限定」を維持（roadmap §6） |
| D3 | **ディスクキャッシュ形式の非互換** | M | 低 | warmup/thumb/PDF キャッシュのキー・形式を Rust 側が変えると、切戻し後にキャッシュが無効化・肥大化 | キャッシュ層は Python が保持し、Rust は「キーを受け取り結果を返す」だけに。形式変更はしない設計を徹底 |
| D4 | **全面置換の不可逆化** | H | 低 | PyO3 → Rust サーバ本体化した後に Python 版が保守されず戻れない | W6 の再判定まで全面置換を凍結。仮に進んでも旧イメージ SHA ロールバック手順を維持 |

## E. 未知事項・Phase 0 の発見（要裁定）

| # | 事項 | 内容 | 対応 |
|---|---|---|---|
| E1 | **「must match JS byte-for-byte」対応物なし** | `scanner.py:45` のコメントに対応する JS 側ソート/正規化実装が `app/static/` に存在しない（`natural_key` docstring は "sole source of sort truth" と明記 → コメントが陳腐化の可能性） | パイロット仕様固定時に確認し、誤記なら Python コメント側を別小 PR で是正。「JS との三方一致」は要求仕様から外す |
| E2 | **free-threaded Python 3.14t** | 将来 `python3.14t`（free-threaded）を採用した場合、PyO3 拡張のスレッド安全性要件が変わる | 現行は通常ビルド前提。free-threaded 移行は別計画として記録のみ |
| E3 | **PyO3 の Python バージョン追従コスト** | Python メジャー更新ごとに PyO3/maturin 更新が追従コストとして発生 | toolchain pin + 更新手順をタスクテンプレの運用項目に |
| E4 | **unrar crate の依存形態** | RAR 展開をプロセス内化する場合、unrar の Rust バインドは C++ unrar への依存で pure Rust ではない | 「外部ツールのまま」が現行設計と同等で最も低リスク。プロセス内化は効果計測後に判断 |
| E5 | **フロント KPI との接続** | 今回の実測はサーバ側のみ。UI 体感への寄与は未測定 | W2 以降で「サーバ改善 → トップ表示/本を開く KPI」の対応付けを perf-measurement skill で取得 |

## F. レジスタ運用

- 状態更新は各 Wave 完了時。新規リスクは発見次第追記し ID は振り直さない
- 棄却した Wave は「効果なし/検証不能」を理由に Accepted にして記録 — 同じ領域を再検討する際の根拠とする
- H リスクが Open のままの Wave は Go/No-Go を通さない
