# inventory.md — bookq 機能・モジュール棚卸し（Rust 移植 Phase 0）

> Phase 0 調査成果物 1/6（issue #163）。調査基準コミット: `d024ef0`（origin/main, 2026-10-29）。
> 本書は「外部から観測可能な振る舞い＝互換性契約」を特定することが主目的。

## 1. スタック

| 項目 | 値 | 根拠 |
|---|---|---|
| 言語 | Python 3.14（`python:3.14-slim`、GIL 有効ビルド） | Dockerfile:1 |
| Web フレームワーク | FastAPI + Starlette + Jinja2（テンプレ1枚） | requirements.txt |
| ASGI サーバ | 本番 hypercorn 単一ワーカー / テスト uvicorn | Dockerfile CMD, requirements-dev.txt |
| フロントエンド | ビルドなし素 JS：app.js 3,252 行 + モジュール ~20 個 + vendor 2 個 | app/static/, CLAUDE.md |
| 配布 | Docker イメージ（multi-stage: venv コピー + app/） | Dockerfile |
| 到達経路 | tailscale sidecar `tailscale serve`（HTTPS :443 → viewer:8080）。viewer はホストポート非公開 | docker-compose.yml, CLAUDE.md |
| 認証 | Google OAuth（authlib）→ SessionMiddleware 署名クッキー、email allowlist | app/auth.py, app/main.py |
| 対応 OS/端末 | サーバ Linux（Debian slim）。クライアントは iOS Safari（PWA）+ 低スペック Android が重点対象 | docs/, .claude/skills/ |
| 永続化 | SQLite（folder-visibility）+ JSON キャッシュ 3 系統（後述）+ インメモリ状態 | 下記 §5 |
| 外部バイナリ | Debian `7zip` + `unrar`（non-free）— RAR 圧縮エントリ展開のみ | Dockerfile apt-get |
| 重いネイティブ依存 | pypdfium2（PDFium C++ バイナリ）、Pillow | requirements.txt |

## 2. エントリポイントと起動系列

- `ENTRYPOINT ["hypercorn", "app.main:app"]` → `app/main.py:241` `app = create_app()`
- `create_app()`（main.py:188）: ロギング → `load_settings` → SessionMiddleware（https_only, same_site=lax, 7日）→ ApiGZipMiddleware → `init_oauth` → `/static` マウント（NoCacheStaticFiles）→ 3 ルータ登録 → `app.state` 初期化（folder_visibility, reader_sync, reading_positions）→ `/ws/reader-sync` WS ルート
- `lifespan`（main.py:110）: `warmup_metadata_index`（shallow scan + index + signature）と重い import（pypdfium2/rarfile/PIL）を `WARMUP_TIMEOUT_SECONDS=20` で並列 gather → `reader_sync.start()` → `起動完了` ログ → `_background_fill`（不透明 archive ノード逐次実体化）+ `_warm_covers`（カバー thumb 事前生成）を fire-and-forget
- テスト: `BOOKQ_TEST_MODE=true` が import 時一度だけ読まれ OAuth をバイパス（auth.py:22）。`BOOKQ_ENV=production` との併用は起動拒否（フェイルクローズ）

## 3. モジュール一覧（`app/`、実測行数）

| ファイル | 行 | 責務 |
|---|---|---|
| `main.py` | 241 | アプリ生成・lifespan（warmup 起動系列） |
| `config.py` | 57 | env → Settings dataclass（SECRET_KEY 未設定は production で起動拒否） |
| `auth.py` | 116 | Google OAuth 3 エンドポイント、`_TEST_MODE` バイパス、`user_from_connection`（HTTP/WS 共用） |
| `routes.py` | 1,736 | HTTP 面のほぼ全域：テンプレ、index/tree キャッシュ機構、folders/images/search/facets/refresh、画像・アーカイブ・PDF・thumb・sprite 配信、reading position、`_background_fill`/`_warm_covers` |
| `scanner.py` | 392 | FS 走査 `scan_tree`（単一子 flatten・thumb/thumbs・symlink 循環防止・phase 計時）、`list_images`、natural sort・漢数字正規化 |
| `archive.py` | 408 | ZIP/CBZ/RAR/CBR/EPUB を仮想フォルダ化：リスティング（`_finalize`/`build_archive_tree`/`split_archive_entries`）、単一エントリ読出、危険パスガード |
| `archive_pdf.py` | 160 | PDF ページ数（memo + disk cache + regex フォールバック）、pypdfium2 レンダ、`pNNN.png` 名変換 |
| `thumbs.py` | 993 | `/thumb`（幅 200/400/800、JPEG q70、disk cache cap 5000）、`/view`、スプライト（9 セル/2D グリッド）、cache eviction・warm |
| `tree_signature.py` | 193 | FS 署名（d_type 活用・8ワーカーBFS・sha256）— TTL 期限後の変更検出ゲート |
| `metadata/index.py` | 270 | MetadataIndex：walk/ツリー由来の再構築、検索、facets |
| `metadata/models.py` | 117 | ComicMetadata、metadata.toml パース（tomllib）、タグ平坦化 |
| `metadata/facets.py` | 89 | facets 集計・subtree facets・世代メモ化 |
| `metadata/search.py` | 74 | relevance スコア・フィルタマッチ |
| `reader_sync/protocol.py` | 219 | WS フレーム語彙・定数（version 1、64KiB/32MiB 上限、ERR_*） |
| `reader_sync/model.py` | 176 | 見開き Spread/PageRef 純粋モデル |
| `reader_sync/state.py` | 280 | セッション状態遷移・epoch 検証・順序付き op 適用 |
| `reader_sync/sessions.py` | 1,299 | SessionRegistry：6桁コードペアリング、heartbeat、dedup、load スケジュール |
| `reader_sync/catalog.py` | 493 | 共有マニフェスト解決（chapters/pageRefs/spreadTable/sha256 digest、分割転送） |
| `reader_sync/transport.py` | 152 | starlette WebSocket ⇔ PeerLink 適合層 |
| `warmup_cache.py` | 348 | 再起跨ぎ JSON キャッシュ（archive entries・pdf count、`mtime_ns+size` キー、cap 5000、原子的 replace + メモリミラー） |
| `folder_visibility.py` | 217 | SQLite 隠しルート store（WAL、per-user revision、HMAC owner トークン） |
| `folder_visibility_routes.py` | 166 | GET/PUT `/api/folder-visibility`（owner ヘッダ検証・ツリー所属検証・fail-closed） |
| `compression.py` | 23 | パス限定 gzip（`/api/`・`/static/`・`/sw.js` のみ。画像素通し） |
| `static_files.py` | 18 | `Cache-Control: no-cache` の StaticFiles 拡張 |

**合計約 8.1k 行の製品 Python**。フロントエンドは `app/templates/index.html` ~230 行 + `app/static/` の JS/CSS（ビルドなし、`sw.js` SHELL_ASSETS がロード順契約を固定）。

## 4. 外部から観測可能な振る舞い（互換性契約）

### 4.1 HTTP エンドポイント

| パス | 認証 | 契約上重要な振る舞い |
|---|---|---|
| `GET /` | 要 | ログイン/ビューア分岐の HTML shell（Jinja、`{% if not user %}`）。未認証は 302/ログイン面 |
| `GET /sw.js`, `/static/*` | — | `Service-Worker-Allowed: /`、static は `Cache-Control: no-cache`（ETag/Last-Modified 再検証） |
| `GET /api/folders` | 要 | ツリー JSON。`?depth=1` shallow（`children:[]`・metadata なし・opaque archive）/ full（metadata+subtree_facets 付与）。`?since=<gen>` で世代一致 304。`Retry-After: 2` 付き **202**（index 構築中）。ETag `sha1[:24]-g<gen>`、`Cache-Control: private, max-age=60`、If-None-Match 304 |
| `GET /api/images?folder=` | 要 | フォルダ直下の画像名リスト（非再帰）。アーカイブ/PDF/アーカイブ内部 prefix も同一契約。403/404 区別 |
| `GET /image/{path}` | 要 | loose=FileResponse（ETag `mtime-size`、304、Range 未実装）/ archive entry=bytes（ETag `mtime-size-sha1(inner)[:16]`、304、**Range 206 対応**、Accept-Ranges）/ PDF ページ=PNG。`_is_within` 違反 403、不正 400、無し 404、展開ツール欠落 501 |
| `GET /thumb/{path}?w=` | 要 | 幅 {200,400,800} のみ・JPEG q70・disk cache。`/image` と同じ解決・条件付き応答 |
| `GET /view/{path}` | 要 | 幅上限の閲覧用 JPEG（thumb 系 disk cache の別 namespace） |
| `GET /api/thumb-sprite?entries=&rows=` | 要 | ≤9 セル・200px セルのコンタクトシート JPEG。`rows` で 2D グリッド（幅クラス別寸法） |
| `GET /api/search?q=&artist=&tag=` | 要 | relevance 検索。hidden roots はサーバ側除外（`include_hidden` で本人のみ opt-out）。202 ゲート |
| `GET /api/metadata/facets?path=` | 要 | `{artists, tags}`。同上の hidden 除外・202 ゲート |
| `POST /api/refresh` | 要 | index lock 下で同期 full rebuild → `{status, folders}` |
| `GET/PUT/DELETE /api/reading-position` | 要 | ユーザー1件・ts 最大勝ち・DELETE は tombstone・`?hard=1` は test mode のみ・未来 ts 拒否・インメモリのみ |
| `GET/PUT /api/folder-visibility` | 要 | `{owner, revision, hiddenRoots}`。PUT は `x-bookq-visibility-owner` HMAC 照合 + `{"root","hidden"}` 厳格スキーマ + ツリー top-level ノード所属検証（hide のみ） |
| `GET /health`, `/api/version` | 不要 | liveness と version JSON |
| `GET /auth/login|callback|logout` | — | Google OAuth。callback は allowlist 外 403、`/` へ 303 |
| `WS /ws/reader-sync` | 要（セッションクッキー） | versioned JSON フレーム（64KiB/フレーム、manifest 総量 32MiB） |

### 4.2 配信・キャッシュの意味論

- **ETag/304**: loose `"{mtime:.0f}-{size}"`、archive `"{mtime}-{size}-{sha1(inner)[:16]}"`、PDF `"...-pdf{idx}"`、folders `"{sha1[:24]}-g{gen}"`。If-Modified-Since も評価（秒精度切捨て）
- **Range**: archive/PDF エントリのみ 206（`bytes=start-end`、suffix range 対応）。loose FileResponse は Starlette 既定
- **gzip**: `/api/`・`/static/`・`/sw.js` のみ（`ApiGZipMiddleware`、minimum 1024B）。画像・Range は素通し
- **ツリーキャッシュ**: `metadata_index_cache`（tree+index+signature+generation、TTL 300s）。期限切れは stale-serve + 署名ゲート付き単一 flight の背景 refresh。cold-miss は `index_build_task` 共有 + `_INDEX_BUILD_AWAIT_SEC=0.8` 超過で 202
- **`folders_body_cache`**: generation+path+depth キーで JSON body+ETag をメモ化（世代変更で破棄）
- **warmup disk cache**: `BOOKQ_CACHE_DIR` or tmpdir `/bookq-cache/warmup-cache-v1.json`（archive listings・pdf counts、`r:<relpath>\0mtime\0size` キー、7日 TTL、cap 5000、tmp+replace 原子書込、メモリミラー、`_STORE_LOCK` で直列）
- **thumb/sprite/view disk cache**: `BOOKQ_CACHE_DIR` or tmpdir 系、`thumbs/<w>/`・`sprites/<w>/` 各 cap 5000、遅延 eviction（oldest mtime）
- **PDF count memo**: `(path,mtime_ns,size)` キー 300s プロセス内 memo
- **reader_sync**: `app.state` 内のみ（単一ワーカー前提、再起動で消失）。heartbeat PING 5s / 無応答 15s / code・resume TTL 各 5min / match 窓 2min / load タイムアウト 120s
- **reading_positions**: `app.state` インメモリのみ（再起動で消失 — 許容済み設計）
- **folder_visibility**: `BOOKQ_STATE_DIR/folder-visibility.sqlite3`（WAL、user-state volume で永続）

### 4.3 外部フォーマット

- **ZIP/CBZ**: stdlib zipfile。エントリ=画像拡張子のみ、危険パス除去、cap 2000、natural sort
- **RAR/CBR**: rarfile。リスティングは純 Python ヘッダ解析（ツール不要）、store エントリはツール不要で読出、**圧縮エントリは外部 unrar/7z を1ページごとに spawn**（solid RAR は逐次展開で遅い既知特性）。ツール欠落時 501
- **EPUB**: zipfile + XML。OPF spine 順（`<img>`/SVG `<image>`、`%20` decode）、不可読は natural sort フォールバック
- **PDF**: pypdfium2 ページ数+`scale=2` PNG レンダ。ページ名 `pNNN.png`（1-based 3桁）
- **metadata.toml**: tomllib（`[comic]`/`[tags]`/`[source]`、artist `|` 区切り、tags カテゴリ平坦化）

### 4.4 ソート規則（byte 同一が要求される領域）

- `natural_key`: `[0-9]+` で分割 → 数字片は int・他は str の tuple + 生名 tie-break。**ASCII 数字のみ**（`\d` 不使用は意図的：全角数字を数値化しない）
- `_sort_key`（ファイル用）: 数字片のみの int tuple（cover.png が先の回帰）
- 漢数字正規化：`_KANJI_PRE`（`第【之の・ー- ` + 空白）直後かつ `_KANJI_POST`（`部話章巻回集編幕期頁号節月】` or 端）直前の run のみ変換。`万億兆` 含有 run は全体不変換（千尋・六本木など固有名詞保護）
- 単一子 flatten：画像0・子 dir1 のフォルダは `name` を `parent/child` に書換えて親にホイスト（thumb/thumbs は書換えない）
- folder cover：`thumb`=最初の画像（深さ優先先頭）、`thumbs`=直下サブフォルダの thumb 最大9・2 件未満ならキー自体を出さない
- ⚠️ **調査メモ**: `scanner.py:45` の `# must match JS byte-for-byte` コメントに対応する JS 側ソート実装は `app/static/` に存在しない（`natural_key` docstring 自身が "sole source of sort truth" と明記）。コメントは陳腐化の可能性 — パイロット仕様固定時に要裁定（risk-register 参照）

### 4.5 フロントエンド／Service Worker 契約

- `sw.js` SHELL_ASSETS = defer 順の JS/CSS 一覧（**追加・改名時に同期必須**）。navigate NetworkFirst、`/static/*` SWR、`bookq-work-*` CacheFirst、API NetworkFirst、offline.html fallback
- URL ハッシュ状態：`#/view/<folder>?p=<page>`、`#/browse/<path>`（`hashchange` 単一リスナ）
- IndexedDB `bookq-offline.works`（folder キー、cacheName/pageCount/cachedCount/bytes/thumb/addedAt/lastReadAt）
- WS プロトコル v1：`CREATE/JOIN/APPROVE/REJECT/RESUME/LEAVE/MATCH/TURN/GOTO/SET_OFFSET/SWAP_SIDES/OPEN_WORK/SET_ACTIVITY/READY/PING/APPLIED/SIGNAL/TRANSPORT_STATE/UI_SYNC`

### 4.6 環境変数

`SECRET_KEY`（必須・production では placeholder 拒否）、`GOOGLE_CLIENT_ID/SECRET`、`BASE_URL`、`ALLOWED_EMAILS`、`DOWNLOADS_DIR`、`BOOKQ_STATE_DIR`、`BOOKQ_CACHE_DIR`、`BOOKQ_TEST_MODE`、`BOOKQ_ENV`、`BOOKQ_LOG_LEVEL`、`BOOKQ_GIT_SHA`、`BOOKQ_BUILD_DATE`、`TS_AUTHKEY`（deploy 側）

## 5. データフローの要点

- **`/api/folders` 1 リクエスト = 1 FS スキャン**（TTL 300s の tree+index キャッシュが間に挟まる）。1 回の scan_tree が folders 応答と index rebuild の双方を供給
- ライブラリ実測（Phase 0 baseline）: **3,177 folder nodes・205,891 image leaves・307 archive nodes（252 rar / 54 epub / 1 pdf）・full body 12.3MB**
- ツリーは「全画像リーフを含む完全構造」— サイズがページング不可能な契約上の制約
- 単一ワーカー前提の共有状態：`metadata_index_cache`、`folders_body_cache`、reader_sync、reading_positions、`_MEM_CACHE`（warmup ミラー）、PDF memo、facets memo

## 6. 非対象（Rust 移植スコープ外の初期判断）

- **フロントエンド JS/CSS/HTML**：issue の「UI・プラットフォーム固有コードを安易に全面移植対象に含めない」に従い初期スコープ外。WASM 化は別途判断事項として risk-register に記録
- **deploy 系シェル**（dev.sh/prd.sh/rebuild.sh/tailscale/）：移植対象ではなく境界の外側
- **テスト/CI 基盤**：Phase 1 の検証基盤としては扱うが製品コード移植ではない
