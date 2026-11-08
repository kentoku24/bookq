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
- **`ETag`**: `"<sha1-24hex>-g<generation>"`。sha1 は `key_ns:path:depth:body`。
  `-g` 接尾辞により、本文 digest が同一でも background-fill で世代が進めば
  revalidation が起きる。`If-None-Match` 完全一致 → **304**。
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

- **user key**: `str(email or "unknown").lower()` — ユーザー毎に独立した slot。
- **GET**: `{positions: [{folder,page,total,ts}]}` — **ts 降順 (newest first)**。
  `?folder=` で単一 folder に絞れる (tombstone/不在時は空配列)。
- **PUT**: `?folder=` 必須 (省略 → 422)、body `{page,total,ts}`。
  - **未来 ts ガード**: `ts > now + 300_000 ms` → **400** (クランプではなく拒否 —
    異常時計の端末が永久に LWW を支配するのを防ぐ設計上の意図)。
  - **per-folder last-writer-wins**: 同一 folder で `ts <= 既存` は受理されるが
    `{status:"stale"}` (書き込みは行わない)。
  - **delete-all tombstone との競合**: `ts <= tombstone_ts` → `{status:"stale"}`。
  - 正常 → `{status:"ok"}`。2xx 系は受理扱いでよい (`ok`/`stale` は best-effort)。
  - **上限**: user 100 (`_POSITION_MAX_USERS`, LRU 追い出し)、folder 512。
- **DELETE**: `?folder=` → その folder に tombstone を記録 (in-flight な古い PUT が
  復活させない)。`?folder` なし → delete-all tombstone_ts を記録。
- **揮発性**: **per-process in-memory** — 再起動・backend 切替 (strangler flip) で
  全消失する設計。永続性を期待しないこと。

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
- **access token**: HS256 JWT、claims `{sub,name,picture,iat,exp}`、`exp = iat+3600`。
  - identity の取得は claims をローカル decode する (`/api/me` 等は存在しない)
  - server 側の検証: exp 必須・`sub` を email として allowlist (`ALLOWED_EMAILS`)
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
