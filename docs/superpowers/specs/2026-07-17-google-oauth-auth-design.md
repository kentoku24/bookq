# Google OAuth 認証機構 設計書

**作成日**: 2026-07-17
**ステータス**: 設計完了（実装待ち）

---

## 1. 背景と目的

bookq は現在 Tailscale tailnet 内であれば誰でもアクセス可能な状態。Google OAuth による認証を追加し、許可された Google アカウントのみが画像ビューアを利用できるようにする。

## 2. 要件

- Google OAuth 2.0 でログインする
- `.env` の `ALLOWED_EMAILS` に指定されたメールアドレスのみアクセスを許可する
- セッションは署名付き Cookie で管理し、DB は使わない（ステートレス）
- 既存の全機能（フォルダ表示、画像配信、ビューア）は認証後にのみ利用可能
- `/api/health` のみ認証不要（Docker ヘルスチェック用）

## 3. 技術スタック

| 要素 | 選択 | 理由 |
|---|---|---|
| Web フレームワーク | FastAPI | 軽量、非同期、DI で認証が簡潔 |
| ASGI サーバー | uvicorn | FastAPI の標準 |
| OAuth ライブラリ | authlib | Google OAuth 2.0 に特化した高水準 API |
| セッション管理 | itsdangerous | 署名付き Cookie、改ざん防止、有効期限付き |
| HTTP クライアント | httpx | authlib が非同期リクエストに使用 |
| テンプレート | Jinja2 | FastAPI デフォルト、認証状態の埋め込みが容易 |

### 依存パッケージ (`requirements.txt`)

```
fastapi>=0.115
uvicorn[standard]>=0.34
authlib>=1.3
itsdangerous>=2.2
httpx>=0.28
jinja2>=3.1
```

## 4. プロジェクト構成

```
bookq/
├── app/
│   ├── __init__.py
│   ├── main.py           # FastAPI アプリ生成、router 登録、例外ハンドラ
│   ├── config.py         # .env 読み込み、Settings dataclass
│   ├── auth.py           # Google OAuth クライアント + get_current_user Dependency
│   ├── routes.py         # 全エンドポイント (/ 〜 /api/* 〜 /image/*)
│   ├── scanner.py        # scan_tree(), list_images(), list_folders()
│   └── templates/
│       └── index.html    # Jinja2 テンプレート（ビューア UI）
├── Dockerfile            # uvicorn 起動に変更
├── docker-compose.yml    # GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET 環境変数追加
├── requirements.txt      # 新規
├── .env.example          # Google OAuth 用変数追加
└── prd.sh              # 変更なし
```

### ファイルごとの責務

| ファイル | 責務 | 想定行数 |
|---|---|---|
| `main.py` | FastAPI() 生成、router include、static mount、HTTPException ハンドラ | ~30行 |
| `config.py` | `python-dotenv` でも可、環境変数を dataclass に読み込み | ~20行 |
| `auth.py` | OAuth クライアント登録、`get_current_user` 依存関数、ログイン/コールバック/ログアウトのルート | ~80行 |
| `routes.py` | `/`, `/api/health`, `/api/folders`, `/api/images`, `/image/*` | ~120行 |
| `scanner.py` | `scan_tree()`, `list_images()`, `list_folders()`, `_sort_key()` | ~180行 |
| `templates/index.html` | ビューア HTML（Jinja2 化、ログイン状態の条件分岐追加） | ~900行 |

## 5. 認証フロー

```
[未認証] ── GET / ──▶ ログインボタン表示
    │
    └──クリック──▶ GET /auth/login ──▶ Google 認証画面へリダイレクト
                        │
                        └──承認──▶ GET /auth/callback?code=...
                                        │
                                        ├── code → token 交換 (Google API)
                                        ├── token → userinfo 取得 (Google API)
                                        ├── email が ALLOWED_EMAILS に含まれるか検証
                                        ├── セッション Cookie 発行 (itsdangerous 署名)
                                        └──▶ GET / へリダイレクト
                                              │
                                        [認証済み] ── 通常のビューア表示
```

### セッション Cookie

- キー名: `session`
- 内容: `{"email": "user@gmail.com", "name": "Display Name", "picture": "https://..."}`
- 署名: `itsdangerous.URLSafeTimedSerializer` + `SECRET_KEY`
- 有効期限: 7日間 (`max_age=604800`)
- HttpOnly: Yes（JavaScript からアクセス不可）
- Secure: Tailscale が HTTPS 終端するため `True`

### 保護対象と認証要件

| エンドポイント | 認証 |
|---|---|
| `GET /` | 要認証。未認証時はログインボタン付きの最小限ページを表示 |
| `GET /api/health` | **不要** |
| `GET /api/folders` | 要認証 → 未認証は 401 |
| `GET /api/images?folder=...` | 要認証 → 未認証は 401 |
| `GET /image/*` | 要認証 → 未認証は 401 |
| `GET /auth/login` | 不要 |
| `GET /auth/callback` | 不要 |
| `GET /auth/logout` | 不要 |

### ログアウト

`GET /auth/logout` → セッション Cookie を削除（max_age=0 で上書き）→ `/` にリダイレクト。サーバー側の状態は持たない。

## 6. 環境変数

```bash
# .env
GOOGLE_CLIENT_ID=xxxxxxxx.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-xxxxxxxx
SECRET_KEY=random-64-char-string        # itsdangerous の署名鍵（openssl rand -hex 32 で生成）
ALLOWED_EMAILS=your.email@gmail.com     # カンマ区切りで複数指定可能
TS_AUTHKEY=tskey-auth-xxxxxxxx          # 既存
```

## 7. Google Cloud Console 設定

以下の設定が事前に必要：

1. Google Cloud Console → APIs & Services → Credentials
2. OAuth 2.0 Client ID を作成（Web application タイプ）
3. Authorized redirect URIs に `https://<tailscale-hostname>/auth/callback` を追加
   - 例: `https://bookq.<your-tailnet>.ts.net/auth/callback`
4. 発行された Client ID / Client Secret を `.env` に設定

## 8. エラー処理

| ケース | HTTP ステータス | 動作 |
|---|---|---|
| 未認証で API アクセス | 401 | `{"error": "Not authenticated"}` |
| 許可リスト外のユーザー | 403 | `{"error": "Email not allowed"}` → Cookie 削除してリダイレクト |
| Google OAuth 拒否 | — | `/?error=access_denied` にリダイレクト |
| Google API エラー | 502 | `{"error": "Auth provider error"}` |
| セッション期限切れ | — | 未認証と同様に扱い、再ログインを促す |
| 画像/フォルダ不在 | 404 | `{"error": "Not found"}` |
| パストラバーサル試行 | 403 | `{"error": "Forbidden"}` |

## 9. コンテナ構成

```
ブラウザ ── HTTPS ──▶ tailscale ── HTTP ──▶ viewer (:8080)
 (Tailnet内)        (TLS終端 + Serve)         FastAPI + uvicorn
```

コンテナ数・構成は変更なし。`Dockerfile` の起動コマンドのみ `python3 viewer.py` → `uvicorn app.main:app --host 0.0.0.0 --port 8080` に変更。

## 10. テスト計画

実装後に確認する項目：

1. **未認証アクセス**: `/api/folders`, `/api/images`, `/image/*` が 401 を返す
2. **認証済みアクセス**: 有効なセッション Cookie 付きで API が正常に動作する
3. **許可リスト**: `ALLOWED_EMAILS` に含まれないユーザーが 403 になる
4. **画像配信**: 認証済みで画像が正しい Content-Type と共に配信される
5. **ログアウト**: Cookie が削除され、再アクセスで未認証状態になる
6. **リグレッション**: フォルダ一覧の自然ソート、ツリー構造、画像のページめくりが引き続き動作する

## 11. 移行手順

1. `requirements.txt` を作成し `pip install -r requirements.txt`
2. `viewer.py` から `scanner.py` に関数を抽出
3. `config.py`, `auth.py`, `routes.py`, `main.py` を新規作成
4. `templates/index.html` に Jinja2 版のビューア HTML を作成（認証状態の埋め込みを追加）
5. `Dockerfile` を uvicorn 起動に変更
6. `docker-compose.yml` に環境変数を追加
7. Google Cloud Console で OAuth 2.0 クライアントを作成
8. `.env` に認証情報を設定
9. `docker compose up -d --build` で起動確認
10. テスト項目の全確認
