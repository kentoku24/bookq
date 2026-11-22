# /admin ユーザー管理 UI — 設計 (#613)

## 目的

`/admin` から通常ユーザー（`member`）の追加・無効化・再有効化を行えるように
する。admin の昇降格は誤操作とロックアウトを避けるため、従来どおり
`viewer-rs users` CLI のみとする。

背景: #608 で `users` テーブルと `/admin` ゲート、#610 で `ALLOWED_EMAILS`
退役と CLI 一本化を実施済み。現状 `/admin` は空ページで、日常操作には
コンテナ内 CLI が必要。

## 用語

- **member**: `users.role='member'` のログイン可能ユーザー
- **admin**: `users.role='admin'` の管理ユーザー
- **無効化**: `users.disabled_at` を設定する soft delete（既存 CLI の
  `remove` と同じ）。行は残り、再有効化できる
- **再有効化**: 無効化済み member の `disabled_at` を `NULL` に戻す

## スコープ

### 含む

- `GET /admin` に全ユーザーの一覧（email / role / status）を表示
- member の追加フォーム（role は常に `member`）
- active member の「ログイン停止」、disabled member の「再有効化」
- admin 行は読み取り専用（操作ボタンを出さない）
- POST の認可・CSRF・XSS・入力検証
- Rust / Python のテストと運用ドキュメント更新

### 含まない

- admin の追加・降格・無効化 UI（CLI のみ）
- 物理削除、note / created_by の編集、検索・ページング
- 監査ログの新設、メール招待、パスワード管理
- DB スキーマ変更

## 画面仕様

### 一覧

`users` テーブルの全行を email 昇順で表示する。列は次の 4 つ。

| 列 | 内容 |
|---|---|
| email | 正規化済み email（HTML エスケープして表示） |
| role | `admin` / `member` |
| status | `active` / `disabled` |
| 操作 | member の停止 / 再有効化、admin は `CLI で管理` の注記 |

行ごとの操作:

- active member → 「ログイン停止」ボタン（POST `/admin/users/disable`）
- disabled member → 「再有効化」ボタン（POST `/admin/users/enable`）
- admin（active / disabled とも）→ 操作なし。注記として
  「admin の変更は CLI で行います」を表示する

### 追加フォーム

- email 入力（`type="email"`、`autocomplete="off"`、必須）
- 送信ボタン「追加」
- role 選択は置かない（常に member）
- 同一ページ内の一覧の上に配置する

### フィードバック

POST 後は 303 で `/admin?status=<code>` に戻す。email は URL に載せない。
画面には固定文言だけを表示する。

| status | 表示 |
|---|---|
| `added` | ユーザーを追加しました |
| `reactivated` | ユーザーを再有効化しました |
| `exists` | そのユーザーは既に登録されています |
| `disabled` | ログインを停止しました |
| `already-disabled` | そのユーザーは既に停止されています |
| `not-found` | ユーザーが見つかりません |
| `admin-readonly` | admin はこの画面から変更できません。CLI を使ってください |
| `invalid` | email の形式が正しくありません |

`enable` は `add_member` を再利用するため、未知 email への crafted
`POST /admin/users/enable` は member 追加として扱われ `added` を返す。通常の
UI では disabled member 行にのみ「再有効化」が出るため、この経路は管理操作
からのみ到達する。

未知の `status` はメッセージを出さない。メッセージはすべて固定文字列で、
クエリ値をそのまま HTML に出さない。

## ルーティングと HTTP 契約

| Method | Path | 動作 |
|---|---|---|
| GET/HEAD | `/admin`, `/admin/` | 一覧 + フォーム + flash |
| POST | `/admin/users/add` | member 追加 / 再有効化 |
| POST | `/admin/users/disable` | member 無効化 |
| POST | `/admin/users/enable` | disabled member 再有効化 |

- POST 以外は `405` + `Allow: POST`
- 成功・入力エラー・競合はすべて `303 Location: /admin?status=...`
- 未認証は `303 Location: /`
- 非 admin は `404`（admin 画面の存在を隠す既存挙動を維持）
- CSRF 検証失敗は `403`
- store エラーは `500`（詳細はログのみ）
- リクエスト本文は `application/x-www-form-urlencoded`、`email` のみ。
  本文サイズ上限は 8KiB

## 認可と CSRF

### 認可

各 POST はページ gate を流用せず、次の順で毎回検証する。

1. `corrupt_session` → 500
2. `caller()` で session / Bearer の identity を取得。なければ 303 `/`
3. `admin_check(st, email)` で `users.role='admin' AND disabled_at IS NULL`
   を DB に問い合わせる。失敗・非 admin は 404

`admin_check` は既存の per-request 判定をそのまま使う。無効化・降格された
admin は次の POST から即時拒否される。

### CSRF

session cookie は `SameSite=Lax; HttpOnly; Secure`。加えて POST では
Fetch Metadata / Origin を次のように検証する。

- `Sec-Fetch-Site` が存在する場合: `same-origin` または `none` のみ許可
- `Origin` が存在する場合: host が `Host` と一致すること
  （`Origin: null` は拒否）
- 両方欠落は拒否（POST はブラウザ由来なら少なくとも一方が付く）
- 両方存在する場合は **両方** が条件を満たすこと

`cross-site` / `same-site`、Origin 不一致、`Origin: null`、両方欠落は
403。既存の `oauth::same_site_initiation` は欠落を許容するため、POST 用に
「シグナル必須・存在するものはすべて検証」の薄い wrapper を `admin_ui`
側に置く。

CSP は `script-src 'none'` を維持し、`form-action 'none'` を
`form-action 'self'` にのみ変更する。`default-src 'self'`、
`frame-ancestors 'none'`、`base-uri 'none'`、`object-src 'none'` は維持。

## Store 層

`users.rs` に UI 専用の操作を追加する。既存の `add` / `disable` は CLI の
role 変更・無効化用として変更しない。

```rust
pub(crate) enum Change {
    // 既存
    Added, Updated, AlreadyPresent, Activated, Disabled, AlreadyDisabled, NotFound,
    // 追加
    AdminProtected,
}

impl UserStore {
    /// member の追加 / 再有効化。既存 admin は AdminProtected。
    pub(crate) fn add_member(&self, email: &str, created_by: &str) -> Result<Change, UserError>;

    /// member の無効化。既存 admin は AdminProtected。
    pub(crate) fn disable_member(&self, email: &str) -> Result<Change, UserError>;
}
```

### `add_member`

トランザクション内で既存行を確認する。

- 行なし → `member` / active で INSERT（`created_by` は操作 admin、
  `note='admin-ui'`）→ `Added`
- active member → 変更なし → `AlreadyPresent`
- disabled member → `disabled_at=NULL`、role は `member` のまま → `Activated`
- admin（active / disabled とも）→ 変更なし → `AdminProtected`

### `disable_member`

- 行なし → `NotFound`
- active member → `disabled_at=now` → `Disabled`
- disabled member → 変更なし → `AlreadyDisabled`
- admin（active / disabled とも）→ 変更なし → `AdminProtected`

`enable` は `add_member` を再利用する（disabled member は `Activated` →
`reactivated`、active member は `AlreadyPresent` → `exists`）。UI 上は別
エンドポイントだが store の意味は同じ。admin 行は `AdminProtected` →
`admin-readonly` になる。

admin 行の保護を UI の事前チェックだけにしないのは、CLI や別リクエストとの
競合で admin を降格・無効化しないため。

`users_cli.rs` は `Ok(other)` の catch-all を持つため、`Change` への
variant 追加に伴う変更は不要（CLI の `add` / `disable` 経路では発生しない）。

## レンダリング

- `web/templates/admin.html` を shell として残し、次のプレースホルダを
  サーバーで置換する
  - `{{ADMIN_FLASH}}`: flash メッセージ（空なら空文字）
  - `{{ADMIN_USERS}}`: `<tbody>` の行 HTML
- 新モジュール `rust/viewer-rs/src/admin_ui.rs`
  - `escape_html(&str) -> String`
  - `render(template: &str, users: &[UserRow], flash: Option<&str>) -> String`
  - flash code → 固定文言のマッピング
  - `same_origin_post(headers) -> bool`
- `main.rs` の `admin_page` は `users.list()` を `spawn_blocking` で取得し、
  `admin_ui::render` に渡す。テンプレート読み込み失敗は既存どおり 500
- POST ハンドラは form をパースし、store 操作を `spawn_blocking` で実行、
  `Change` を status code に写像して 303 を返す
- `style.css` に管理テーブル、フォーム、ボタン、status バッジを追加。
  dark theme / 44px タップ領域 / 横スクロール可能なテーブル

### エスケープ

`normalize_email` は空白と制御文字しか拒否しないため、`<` や `"` を
含む email が保存され得る。`escape_html` は最低限 `& < > " '` を実体参照へ
変換し、一覧・フォーム・flash のすべての動的値を通す。URL の `status` は
既知コードのみを採用し、未知値は無視する。

## データフロー

### GET

1. session / admin gate
2. `UserStore::list()` → `Vec<UserRow>`
3. `admin.html` 読み込み
4. `admin_ui::render` で行と flash を埋め込み
5. `no-store`, `Vary: Cookie`, `nosniff`, `referrer-policy`,
   `x-frame-options: DENY`, `x-robots-tag: noindex`, CSP を付与

一覧は DB を直接読むため、追加・無効化は次の GET で即時反映される。
認証側の in-memory directory は最大 1 秒で追従する（既存仕様）。

### POST

1. method / corrupt session / caller / admin gate
2. same-origin 検証
3. form パース（失敗は 400）
4. `add_member` / `disable_member` を実行
5. `Change` → `status` code
6. `303 /admin?status=...`（`no-store` 等の private headers 付き）

## エラーハンドリング

- 入力 email 不正: store の `InvalidEmail` → `?status=invalid`
- 本文パース失敗: 400、サイズ超過: 413（画面に戻さない）
- store error: 500 + `tracing::warn!`。ユーザーには詳細を出さない
- `AdminProtected`: `?status=admin-readonly`
- `NotFound`（disable 対象なし）: `?status=not-found`
- 二重送信: 冪等。`AlreadyPresent` / `AlreadyDisabled` をそれぞれの
  status に写像する

## テスト

### Rust

- `users.rs`
  - `add_member`: 新規 Added、active member AlreadyPresent、disabled member
    Activated、admin（active / disabled）AdminProtected
  - `disable_member`: active member Disabled、disabled member
    AlreadyDisabled、admin AdminProtected、不在 NotFound
  - 既存 `add` / `disable` の挙動が変わらないこと
- `admin_ui.rs`
  - `escape_html` の `& < > " '` と二重エスケープ防止
  - admin 行にボタンが出ない / member 行に適切なボタンが出る
  - flash code のマッピングと未知コードの無視
  - `same_origin_post` の matrix（same-origin / none / cross-site /
    Origin 不一致 / 両方欠落）

### Python `tests/test_admin.py`

test-mode サーバー（admin 画面と form）と real-auth サーバー
（membership の実挙動）を使い分ける。

- GET `/admin`: 追加フォーム、一覧、member の操作ボタン、admin 行の
  操作なしを確認
- POST add: 303 → 一覧に反映、`users check` が member
- 重複 add: `exists`、role 不変
- admin email の add: `admin-readonly`、role が admin のまま
- disable: 303、`users check` が disabled、real-auth API が 401
- enable: 303、API が 200 に戻る
- crafted disable admin: `admin-readonly`、admin は active のまま
- 不正 email: `invalid`、行が増えない
- XSS: `<img@example.com>` のような email を追加しても生タグが出ない
- 認可: 非 admin POST 404、未認証 POST 303、GET action 405
- CSRF: cross-site / `Sec-Fetch-Site: same-site` / 両方欠落 403、
  same-origin は成功
- ヘッダ: `Cache-Control: no-store`、CSP が `form-action 'self'` で
  `script-src 'none'` のまま

### CI

既存の Rust / pytest / browser / iOS / Android ジョブをそのまま通す。
新しい JS は追加しないため browser テストの追加は不要。form の実操作は
HTTP クライアントテストで担保する。

### live 実データ回帰

認可判定と `users` テーブルの読み書きが変わるため、可能なら
`BOOKQ_LIVE_ROOT=... ./scripts/live-regress.sh` を実施する。folder 名は
レポートに出さない（`tests/live/README.md`）。

## ドキュメント

- `docs/deployment.md`「ユーザーと admin の管理」
  - UI でできること（member の追加・無効化・再有効化）
  - admin は UI から変更できないこと（CLI のみ）
  - 無効化は最大 1 秒で認証 API に反映されること
  - CSRF / XSS の防御と、`/admin` をキャッシュしないこと
- `AGENTS.md` の admin 節に「UI は member のみ」を 1 行追記
- `README.md` のユーザー管理節に管理画面の導線を追記

## ロールアウト / ロールバック

- DB スキーマ変更なし。デプロイは viewer-rs の差し替えのみ
- 既存の `users` 行・role・disabled 状態はそのまま
- 問題時は `./prd.sh up --ref <previous-ref>` で戻す。UI から加えた
  member 行は DB に残るが、旧バイナリは `/admin` が空ページに戻るだけで
  認可には影響しない
- 追加した member を消したい場合は CLI の `users remove` を使う

## 未決事項

なし。
