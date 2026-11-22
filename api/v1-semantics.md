# v1 契約の意味論 (semantics)

`openapi.v1.yaml` が wire shape (パス・パラメータ・スキーマ) の正本であるのに対し、
この文書は**形に表れない振る舞い**の正本。クライアント・サーバーのどちらの実装者も
ここだけで振る舞いを再現できることが目的。実装ソースの対応箇所を併記する
(参照は便宜上のもので、矛盾があればこの文書が正)。

## 目次

1. [GET /api/v1/tree — 世代と条件付き取得](#1-tree)
2. [reading-positions — per-folder LWW と tombstone](#2-reading-positions)
3. [folder-visibility — owner トークンと revision](#3-folder-visibility)
4. [認証 — PKCE フローと token のライフサイクル](#4-認証)
5. [gzip / 圧縮対象](#5-gzip)
6. [エラー応答の形](#6-エラー応答)
7. [media decode — thumb/view/sprite の対応形式契約](#7-media-decode)
8. [media ETag / Range — 条件付き取得の書式](#8-media-etag--range)
9. [library edit — アップロード (単発/分割) と削除](#9-library-edit)

## 1. tree

`GET /api/v1/tree` は `{kind, nodes, generation}` envelope を返す
(参照: `app/routes.py::_folder_tree`)。

- **`kind`**: `"tree"` = フルツリー、`"children"` = `depth`/`path` 指定の部分木。
- **`generation`**: 1 始まりの整数。ツリー内容が検証済みで変化した時に +1
  (signature-gated のバックグラウンド走査か `POST /api/v1/index/refresh`)。
  変化なしの index 再構築では進まない。世代番号は **opaque な単調増加値**として
  扱うこと (内容のハッシュではない)。
- **`since=<gen>`**: `since == 現在の generation` → **304**。古い世代 →
  **200 + 現在のフルツリー** (`{kind:"tree", nodes, generation}` — 差分ではない)。
  この応答には ETag を付けない。
- **`ETag`**: `"<sha1-24hex>-g<generation>-v<revision>"`。sha1 は
  `key_ns:path:depth:visibility:body`。`-g` 接尾辞により、本文 digest が同一でも
  background-fill で世代が進めば revalidation が起きる。`-v<revision>` は
  folder-visibility の `revision` (§3) で、隠しルートの変更で 304 が誤って
  返らないようにする。パラメータなしの既定応答にも `-v` を含める。
  `If-None-Match` 完全一致 → **304**。
- **`visibility`** (T03・加算的、省略可): 既定 (パラメータなし) の挙動は
  **不変** — 隠しルートを除外しない従来どおりの未フィルタ木を返す
  (旧クライアント互換のため)。
  - `visibility=apply`: 隠しルート配下をサーバ側で除外する
    (先頭セグメント一致、書庫も対象、§3 と同一述語)。
    visibility 状態を読めない時は未フィルタの木を返さず **503**
    (fail-closed。`GET /api/v1/search` と同じく、フィルタなし一覧は返さない)。
  - `visibility=annotate`: 除外せず、隠しルート配下のノードに
    `hidden: true` を付ける (隠しモード・再表示 UI 用)。
    `hidden` はこのモードでのみ現れ、他のモードではキーを付けない
    (不在 = 可視ではなく「注釈なし」として扱うこと)。
  - 不正値 (上記 2 つ以外) → **422**。`since` との併用時は `since` 判定を優先する。
- **解決済みフィールド** (T03・加算的、optional): ノードの `path` は
  flatten 後の合成済みフル path (`parent/child` を含む。不透明値として扱い、
  分解・再合成しない)。`coverUrl`/`coverUrls[]` は `/thumb/…` 始まりの絶対 URL
  (クライアントは `?w=` のみ付与し、`/image/<fullPath>/<thumb>` の自前解決はしない)。
  `kind` は `work` (タップで開く作品) / `shelf` (階層のみ) /
  `mixed` (両方を含む)。`work` はタップ時に開く Work の `folder`
  (将来の `GET /api/v1/work?folder=` にそのまま渡す)。旧クライアントは
  未知フィールドを無視するため互換は保たれる。
- **`Cache-Control`**: `private, max-age=60`。
- **index 構築中**: **202** `{"detail":"metadata index building"}` +
  `Retry-After: 2` + `Cache-Control: no-store`。クライアントは Retry-After 秒後に
  再試行 (SW api キャッシュにも入らない)。
- **`depth`**: `<1` → 400、非整数 → 422。`depth=1` (shallow) は直下の
  `type=="folder"` 子のみで各ノードの `children` は `[]` (null ではない)、
  `metadata`/`subtree_facets` キーなし — **フルツリーではないので offline
  永続化に使わないこと**。
- **`name`**: single-child flatten で `parent/child` を含む (そのまま path として使う)。
- **v0 `/api/folders` との ETag 分離**: `key_ns` で v0/v1 の cache key を分離
  (同一世代でも ETag 値は別)。

## 2. reading-positions

`GET/PUT/DELETE /api/v1/reading-positions` (参照: `app/api_v1.py`)。

- **user key**: `user_key_for_email(email)` = `strip().lower()` — ユーザー毎に
  独立した slot。email が取れない呼び出しは **403** (共有 "unknown" バケットへは
  フォールバックしない — `app/reading_positions.py::position_user_key`)。
- **GET**: `{positions: [{folder,page,total,ts}]}` — **ts 降順 (newest first)**。
  `?folder=` で単一 folder に絞れる (tombstone/不在時は空配列)。
- **PUT**: `?folder=` 必須 (省略 → 422)、body `{page,total,ts}`。
  - **未来 ts ガード**: `ts > now + 300_000 ms` → **400** (クランプではなく拒否 —
    異常時計の端末が永久に LWW を支配するのを防ぐ設計上の意図)。
  - **per-folder last-writer-wins**: 同一 folder で `ts <= 既存` は受理されるが
    `{status:"stale"}` (書き込みは行わない)。
  - **delete-all tombstone との競合**: `ts <= tombstone_ts` → `{status:"stale"}`。
  - 正常 → `{status:"ok"}`。2xx 系は受理扱いでよい (`ok`/`stale` は best-effort)。
  - **上限**: user 100 (`POSITION_MAX_USERS`, LRU 追い出し)、folder 512。
- **DELETE**: `?folder=` → その folder に tombstone を記録 (in-flight な古い PUT が
  復活させない)。`?folder` なし → delete-all tombstone_ts を記録。
- **永続化** (#375): **user-state SQLite に永続** — `BOOKQ_STATE_DIR/
  folder-visibility.sqlite3` の `position_users`/`position_folders`
  (schema v3、`api/user-state-schema.sql` が DDL 正本)。
  viewer-rs (`rust/viewer-rs/src/state.rs`) が同じ DB を読み書きするため、
  再起動・コンテナ再起動でも位置は失われない。
  規則実装は `PositionBook` (純粋 LWW/検証) と store に分離され、
  `tests/fixtures/positions_cases.json` が py/rs 共通の conformance fixture。
  v0 `/api/reading-position` は同ストアの「ユーザー最新1件」互換ビュー。

## 3. folder-visibility

`GET/PUT /api/v1/folder-visibility` (参照: `app/folder_visibility.py`)。

- **hiddenRoots はライブラリ共有**: 全認証ユーザーに同一のセットが効く。
  DB 上は sentinel `user_key = "*"` (`GLOBAL_USER_KEY`) 配下に保存される。
- **owner トークン** (書き込み者の stale タブ対策。データのスコープではない):
  `hex(HMAC-SHA256(key=SECRET_KEY, msg="folder-visibility:v1:" + user_key))`
  - `user_key` = `email.strip().lower()`
  - `X-Bookq-Visibility-Owner` ヘッダで提示する。クライアントは自身の email と
    SECRET_KEY から計算できる (server が GET で返す `owner` 値と一致するはず)。
- **GET** → `{owner, revision, hiddenRoots}` (`hiddenRoots` はソート済み)。
- **PUT** body `{root, hidden: bool}`:
  - owner ヘッダなし → **403**、値が一致しない → **409**
  - Content-Type が JSON でない → **415**
  - JSON 不正・`root`/`hidden` 型不正 → **400**
  - `hidden=true` で未知 root → **404** (unhide は既知でなくても受理)
  - store 障害 → **503**
- **`revision`**: 実際に hiddenRoots が変化した時のみ +1 (no-op PUT では進まない)。
  クライアントは GET の `revision` を保持して差分検出に使える。
- **root の意味**: パスの**先頭セグメント** (ネストした folder も含むトップレベル)。

## 4. 認証

### ブラウザ (web)

Google OAuth → 署名付き session cookie。**API 契約上は内部詳細**で、
spec の `security` は cookie OR Bearer のどちらか。

### native (iOS / 他クライアント) — RFC 8252 PKCE

参照: `app/auth.py::_native_login_params`, `app/auth_token.py`。

- **`GET /auth/login?client=native&code_challenge=<b64url(sha256(verifier))>`**
  **`&code_challenge_method=S256&state=<csrf>`**
  - `code_challenge` は 43–128 文字の unreserved 文字、`S256` 必須 (`plain` → 400)
  - `state` は callback でそのまま返る → 照合必須
- **callback**: 303 → `bookq://auth?code=<one-time>&state=<echo>`
  (`state` 省略時は param 自体なし)
- **`POST /api/v1/auth/token`** (`security: []` — bootstrap 自体なので非認証):
  - `grant_type=authorization_code` + `code` + `code_verifier` →
    `{access_token, token_type:"Bearer", expires_in:3600, refresh_token}`
    - code は **one-time・120s TTL**・S256 challenge と照合。verifier 不一致でも
      **consume される** (再提示は失敗)
    - `code`/`code_verifier`/`grant_type` 欠落・未知値 → **400/401**
  - `grant_type=refresh_token` + `refresh_token` → **新 pair**
    - rotation: 提示 refresh は **成否に関わらず consume** (並行リクエストでの
      2 重使用防止のため single-flight 前提)
  - **レート制限 (#559 AUTH-01)**: 失敗交換 (401) はクライアント IP 毎に
    60 秒窓 20 件まで。超過で **429 + `Retry-After`**。成功交換は
    カウントしないため通常のリトライで誤発動しない。`GET /auth/login`
    起動も別枠で 60 秒 120 件。識別は sidecar が付与する proxy ヘッダ
    (`CF-Connecting-IP` / XFF 末尾) 由来。
- **access token**: HS256 JWT、claims `{sub,name,picture,iat,exp}`、`exp = iat+3600`。
  - identity の取得は claims をローカル decode する (`/api/me` 等は存在しない)
  - server 側の検証: exp 必須・`sub` を email として `users` テーブルの active membership
    で再確認 — **allowlist が空なら全拒否 (fail-closed)**、email を allowlist から
    落とせば発行済み token も即無効
- **`BOOKQ_TEST_MODE`**: `?client=native` はバイパス**されない** (Google OAuth を
  通る)。dev 検証はクライアント側の token 注入機構を使う。

## 5. gzip

`ApiGZipMiddleware` が prefix 単位で圧縮 (参照: `app/compression.py`):

- **圧縮対象**: `/api/`、`/static/`、`/sw.js` — 本文 ≥1024B、level 6。
- **除外**: `/image`・`/thumb`・`/view` — 既圧縮メディア + Range (206) との
  干渉を避けるため素通し。
- クライアントは `Accept-Encoding: gzip` でよい (強制ではない)。

## 6. エラー応答

- 全 API エラーは `{"detail": <string | FastAPI validation array>}`。
- パス/型バリデーション由来は 422 (FastAPI 自動) — spec 上も各 op に記載済み。
- **`text/plain` の例外**: `/image` の Range 構文エラーは Starlette の
  `FileResponse` 内部由来で `text/plain` になることがある (spec にも記載)。
- **意味的拒否は schema で表現不可**: 未来 ts ガード (§2)、visibility owner
  (§3)、traversal 403 等は schema-valid でも拒否される —
  `schemathesis.toml` で expected-statuses を op 単位で許容している。

## 7. media decode

decode を要する endpoint (`GET /thumb/{path}`、`GET /view/{path}` の
normalize 分岐、`GET /api/v1/thumb-sprite` のラスタセル) の形式契約
(#406、#563 で `.avif` を native 化。正本:
`rust/viewer-rs/src/thumbs.rs::NATIVE_DECODE`)。

- **viewer-rs がネイティブ decode する共通集合**: `.jpg` `.jpeg` `.png`
  `.gif` `.webp` `.bmp` `.avif` (#563 — `avif-native` = mp4parse +
  dav1d の decode-only feature、encoder 系依存は持ち込まない)。
  正常時は **200 + `image/jpeg`**。
- **`.svg` は decode しない**: 元バイトを `image/svg+xml`
  で pass-through (`image` は SVG decoder を持たない)。
  CSP `default-src 'none'; sandbox` を付与。
- **decode 不可の形式 (代表: `.tif`/`.tiff`)**: `NATIVE_DECODE` 外の
  拡張子は payload 読み込み前に **501** (`{detail:"not implemented"}`)。
  `image` crate に feature を持たない形式 (`.tga` `.pcx` `.dds` `.qoi`
  `.ico` `.sgi` `.sun` `.xbm` `.xpm` `.ppm` `.psd` 等) も同じく 501。
  ただし tree に載るのは `serve.rs::IMAGE_EXTENSIONS` の 8 形式だけなので、
  `.tiff`/`.heic`/`.tga` 等は tree 経由では見えず direct URL でのみ
  到達する点に注意。
- **decode 失敗**: `.heic`/`.heif` `.jxl`、破損バイト等 — 拡張子ゲート
  (`NATIVE_DECODE` 外 → 501) または decode 失敗 (`make_thumb`/`make_view`
  が `None` → 501)。いずれも 500 ではない。
- **`/image/{path}` は decode 対象外だが配信は画像限定** (#62):
  生バイト配布で decode はしないが、loose file / アーカイブ entry
  とも `IMAGE_EXTENSIONS` (`.avif .webp .jpg .jpeg .png .gif .bmp
  .svg`) のみ 200 — それ以外は 404 (存在を秘匿)。レスポンスには
  `nosniff` が付き、SVG は sandbox CSP (`default-src 'none';
  sandbox`) を伴う。loose file の判定は realpath 後のパスで行う
  ため `*.png` 名の symlink 経由の型偽装は不可。
- **将来の対応形式追加時の規則**: `image` crate に feature を足す
  (例: `tiff`) 場合は、必ず本節の集合と
  `rust/viewer-rs/src/thumbs.rs` の定数と
  `tests/test_media_decode_contract.py` + `thumbs.rs::tests` を同時更新
  すること (#563 の `.avif` 追加がこの手順の実例)。

## 8. media ETag / Range

`/image` `/thumb` `/view` `/api/v1/thumb-sprite` の条件付き取得契約
(#277 — それまで実装だけに存在した ETag 書式の正本化)。
mtime/size は `fmt_mtime` (`{:.0}`) で整数表記される
(`/image` loose/archive の size は u64 そのまま — 同じ整数値)。
**ETag 文字列を変えてはいけない** — SW の再検証とオフラインキャッシュの
ヒット率に直結する (#277 の注意書きどおり)。

| endpoint | ETag 形式 | digest の素 |
|---|---|---|
| `GET /image/{path}` loose file | `"<mtime>-<size>"` | — |
| `GET /image/{path}` archive entry | `"<mtime>-<size>-<sha1(inner)[..16]>"` | inner パスの SHA1 |
| `GET /image/{path}` PDF page (`pNNN.png`) | `"<mtime>-<size>-pdf<idx>"` | idx は 0-based ページ番号 |
| `GET /thumb/{path}` | `"<mtime>-<size>-<width>-<digest[..16]>"` | ソース cache-key の SHA1 |
| `GET /view/{path}` passthrough 分岐 | `"<mtime>-<size>-view-<digest[..16]>"` | ソース cache-key の SHA1 |
| `GET /view/{path}` normalize 分岐 | `"<mtime>-<size>-view-<wtag>-<digest[..16]>"` | `?w=` 有りは cache-key に `|w<w>` が乗る。`wtag` は `"full"` または clamp 後の幅 |
| `GET /api/v1/thumb-sprite` | `"<mtime>-<size>-<cell>-s<digest[..16]>"` | セル内容 key の SHA1 |

- **スプライト ETag の入力**: archive 指定時は archive ファイル自体の
  mtime/size。通常フォルダ時は `size` = 要求セル数 (**欠損
  `miss:`/不正 `bad:` セルも含む**)、`mtime` = stat できたセルの
  最新 mtime (全滅時 0)。digest にはフォルダパス・セル名一覧・
  セル毎の stat key (欠損は `miss:<name>`/`bad:<name>` marker)・
  セルサイズ・行レイアウトが入る — **セルの stat key (path/mtime/
  size) が変われば ETag が変わる**。画像バイト列は読まないため、
  mtime/size を維持したまま内容だけ差し替えたセルは検出できない
  (archive 指定時はセル毎ではなく archive ファイルの mtime/size のみ)。
- **`If-None-Match` / `If-Modified-Since`**: どちらか一方でも条件を
  満たせば **304** (body 無し)。ETag 不一致でも IMS が新しければ 304 に
  なり得る (両者は独立判定 — SW は両ヘッダを送る)。archive 系は
  `int(mtime)` 切り捨てで比較 (`archive_not_modified`)、`/image` loose
  file は float mtime をそのまま比較 (`file_not_modified`) — サブ秒の
  差で 304 が揺れ得る非対称は既知のまま (ETag 側が主経路)。
- **304 応答ヘッダ**: `X-Content-Type-Options: nosniff` のみ —
  ETag/Last-Modified は付かない。
- **`Last-Modified`**: 200/206 の media 応答に付与 (HTTP-date)。304 には付かない。
- **`Range: bytes=`**: `/image` loose file のみ単一 Range に対応
  (206 + `Content-Range`)。archive entry / PDF page は
  `parse_archive_range` で**不正・充足不能な Range を無視して 200 フル**
  を返す (/thumb `/view` `/api/v1/thumb-sprite` は Range 対象外 — 生成物)。
  loose file 側の詳細: 複数 Range・構文エラーは契約外 (複数 Range は
  200 フル、構文エラーは 400 `text/plain` になり得る — §6 参照)、
  **end が EOF を超える場合は 416 ではなく末尾までの 206**、
  416 は `start >= size` の場合のみ。
- **archive inner path の字句ガード**: `/image/{archive}/{inner}` と
  `/thumb` `/view` の archive 分岐は `serve.rs::valid_archive_inner`
  (空・先頭 `/`・`..` セグメントを拒否 → 400) を共有する (#277)。
  ただし適用は `resolve_archive` の正規化**後** — 空・`.` セグメントは
  先に除去され、`..` を含む URL は archive 分岐に入らず plain-file
  経路へ落ちる (受信 URL への字句適用ではない点に注意)。

## 9. library edit

`/api/v1/library/*` — ライブラリへの書き込み面 (参照:
`rust/viewer-rs/src/library.rs`)。読み取り面と同じくダウンロードルート
(`LIBRARY_DIR`) 相対パスで指定し、realpath 後の containment を全 op で検証する。

- **対応形式**: `.zip .cbz .rar .cbr .epub .pdf` のみ
  (`serve.rs::ARCHIVE_EXTENSIONS` + `PDF_EXTENSIONS`)。拡張子は小文字化して
  判定。それ以外は **415** — ただし multipart (`/library/files`) では
  per-file `unsupported_type` として `errors[]` に列挙され 200 を返し得る。
- **uploads 無効化**: 起動時に `<library>/.bookq-uploads/` を作成できない
  デプロイ (read-only マウント等) では、upload 系・delete ともに
  **503** `{detail:"uploads disabled"}`。読み取り面は従来どおり動作する。
- **`dir` の検証**: 空 = ルート。realpath 後 `is_within(library)`、
  実在 `is_dir()`。書庫ファイルを dir に指定した場合・`.bookq-uploads`
  配下は **400**、不存在は **404**。
- **`name` の sanitize**: 空・制御文字・`/`・`\`・`.`/`..`・先頭 `.`・
  末尾空白/`.`・255B 超は **400** (`invalid_name`)。`/api/v1/library/files`
  (multipart) ではファイル単位の拒否なので `errors[]` に列挙され
  リクエスト自体は 200 を返し得る。
- **衝突の自動リネーム**: 同名 (ファイルでもディレクトリでも) が既に
  存在する場合、`name (1).ext` `name (2).ext` … を 999 まで試す。
  尽きた場合は `conflict` — multipart (`/library/files`) では per-file
  `errors[]` として 200 に列挙、分割アップロードの finalize では
  **409** を返す。採用名は応答の `name`/`path` で返る。
- **書き込みの atomicity**: ファイルは必ず tmp (multipart は対象 dir 内の
  `.bq-tmp-*`、session は `.bookq-uploads/<id>/blob`) に書き、fsync 後に
  rename する。途中失敗・切断で未完成の本体がライブラリ名で残ることはない
  (tmp 残骸のみ。`.bookq-uploads` と `.` 始まりの名は scanner が拾わない)。
- **分割アップロードの順序契約**: `PUT …/uploads/{id}` は
  `Upload-Offset == 現在 offset` の時のみ append。不一致は
  **409 `{offset: <現在値>}`** — 重複・順序外・再送はすべてこれで検出し、
  クライアントは返された offset から再開する。`offset+len > size` は **400**。
- **finalize**: `offset == size` の時のみ。rename 後にセッション dir を
  削除するので finalize は冪等ではない (2 回目は **404**)。
- **セッション寿命**: `<library>/.bookq-uploads/<id>/{meta.json,blob}` に
  永続 (再起動をまたぐ)。`BOOKQ_UPLOAD_SESSION_TTL_SECS` (既定 7 日) を
  超えたものは起動時・init 時に sweep される。
- **index 反映**: 確定・削除の成功後にバックグラウンド refresh が走り、
  signature 変化を検証して generation が進む。同期で確実に反映したい
  クライアントは `POST /api/v1/index/refresh` を使う。
- **delete**: `path` はファイル (単体) またはディレクトリ (再帰)。
  ルート・`.bookq-uploads` 配下は **400**、不存在は **404**。
  reading positions・visibility・クライアント側オフライン cache は
  削除しない (orphan 化するが無害; 同名再アップロードで positions は
  自然に復活する)。
