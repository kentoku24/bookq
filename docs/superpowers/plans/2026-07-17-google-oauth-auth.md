# Google OAuth 認証機構 + FastAPI 移行 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Google OAuth 2.0 認証を追加し、許可リストのユーザーのみ画像ビューアにアクセス可能にする。同時に `http.server` から FastAPI に移行する。

**Architecture:** FastAPI + uvicorn でシングルコンテナの Web サーバー。authlib で Google OAuth、itsdangerous で署名付きセッション Cookie（7日間有効）。DB 不要のステートレス設計。既存コンテナ構成（viewer + tailscale）は維持。

**Tech Stack:** FastAPI, uvicorn, authlib, itsdangerous, httpx, Jinja2, Python 3.13

---

### Task 1: プロジェクト基盤 — requirements.txt と app パッケージ

**Files:**
- Create: `requirements.txt`
- Create: `app/__init__.py`

- [ ] **Step 1: `requirements.txt` を作成**

```txt
fastapi>=0.115
uvicorn[standard]>=0.34
authlib>=1.3
itsdangerous>=2.2
httpx>=0.28
jinja2>=3.1
```

- [ ] **Step 2: `app/__init__.py` を作成（空ファイル）**

```bash
touch app/__init__.py
```

- [ ] **Step 3: 依存パッケージをインストールして検証**

```bash
pip install -r requirements.txt
python -c "import fastapi, uvicorn, authlib, itsdangerous, httpx, jinja2; print('OK')"
```
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add requirements.txt app/__init__.py
git commit -m "chore: add Python dependencies and app package"
```

---

### Task 2: scanner.py — 既存コードの抽出

**Files:**
- Create: `app/scanner.py`
- Modify: `viewer.py` — 削除は後続タスクで（この段階では scanner 関数を viewer.py に残したままにする）

- [ ] **Step 1: `app/scanner.py` を作成し viewer.py から関数をコピー**

`viewer.py` の以下の部分を `app/scanner.py` に抽出する：

```python
"""Folder scanning utilities for the image viewer."""

import re
from pathlib import Path

_NUMERIC_RE = re.compile(r"(\d+)")

IMAGE_EXTENSIONS = {".avif", ".webp", ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg"}


def _sort_key(name: str) -> tuple[int, ...]:
    """Extract numeric parts for natural sort (01.avif < 02.avif < 10.avif)."""
    parts = _NUMERIC_RE.split(name)
    key: list[int] = []
    for p in parts:
        if p.isdigit():
            key.append(int(p))
    return tuple(key)


def _find_first_image(nodes: list[dict]) -> str | None:
    """Return the relative path of the first image leaf in the tree."""
    for node in nodes:
        if node["type"] == "image":
            return node["name"]
        if node.get("children"):
            result = _find_first_image(node["children"])
            if result is not None:
                return node["name"] + "/" + result
    return None


def scan_tree(base: Path) -> list[dict]:
    """Return recursive folder tree with image-leaves mixed in.

    Each node is a dict:
      {"name": str, "type": "folder"|"image", "children": [...] | None}

    Folder nodes also include:
      "thumb": the relative path of the first image in the subtree, or None.
      "thumbs": up to 9 thumb paths from immediate sub-folders (only if 2+ exist).

    Rules:
    - Directories become "folder" nodes with sorted children.
    - If a folder contains ONLY other folders (no images), its single-item children
      are flattened: the subfolder's children are appended directly to the parent
      instead of creating a nested folder node.
    """
    if not base.exists():
        return []
    entries: list[dict] = []

    files: list[str] = []
    dirs: list[str] = []
    for entry in base.iterdir():
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            dirs.append(entry.name)
        elif entry.is_file() and entry.suffix.lower() in IMAGE_EXTENSIONS:
            files.append(entry.name)

    dirs.sort()
    files.sort(key=_sort_key)

    if files:
        for fname in files:
            entries.append({"name": fname, "type": "image", "children": None})

    for dname in dirs:
        child = scan_tree(base / dname)
        if not files and len(dirs) == 1:
            for c in child:
                c["name"] = dname + "/" + c["name"]
                if c.get("thumb") is not None:
                    c["thumb"] = dname + "/" + c["thumb"]
                if c.get("thumbs"):
                    c["thumbs"] = [dname + "/" + t for t in c["thumbs"]]
            entries.extend(child)
        else:
            thumb = _find_first_image(child)
            sub_thumbs: list[str] = []
            for c in child:
                if c["type"] == "folder" and c.get("thumb"):
                    sub_thumbs.append(c["name"] + "/" + c["thumb"])
                if len(sub_thumbs) >= 9:
                    break
            entry: dict = {"name": dname, "type": "folder", "children": child,
                            "thumb": thumb}
            if len(sub_thumbs) >= 2:
                entry["thumbs"] = sub_thumbs
            entries.append(entry)

    return entries


def list_folders(base: Path) -> list[str]:
    """Return sorted directory names under base, skipping hidden / non-dirs."""
    if not base.exists():
        return []
    dirs = []
    for entry in base.iterdir():
        if entry.is_dir() and not entry.name.startswith("."):
            dirs.append(entry.name)
    dirs.sort()
    return dirs


def list_images(folder_path: Path) -> list[str]:
    """Return image files sorted naturally by numeric prefix."""
    files = [
        f.name
        for f in folder_path.iterdir()
        if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS and not f.name.startswith(".")
    ]
    files.sort(key=_sort_key)
    return files
```

- [ ] **Step 2: scanner.py が単体で import できることを確認**

```bash
python -c "from app.scanner import scan_tree, list_images, list_folders; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add app/scanner.py
git commit -m "refactor: extract scanner module from viewer.py"
```

---

### Task 3: config.py — 環境変数管理

**Files:**
- Create: `app/config.py`
- Modify: `.env.example`

- [ ] **Step 1: `app/config.py` を作成**

```python
"""Application settings loaded from environment variables."""

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    google_client_id: str = ""
    google_client_secret: str = ""
    secret_key: str = ""
    allowed_emails: list[str] = field(default_factory=list)
    downloads_dir: str = ""
    bind_host: str = "0.0.0.0"
    port: int = 8080

    @property
    def downloads_path(self) -> Path:
        path = Path(self.downloads_dir) if self.downloads_dir else (Path(__file__).parent.parent / "downloads")
        return path.resolve()

    @property
    def allowed_emails_set(self) -> set[str]:
        return {e.strip().lower() for e in self.allowed_emails if e.strip()}


def load_settings() -> Settings:
    """Load settings from environment variables. .env is read by docker compose env_file."""
    allowed_raw = os.getenv("ALLOWED_EMAILS", "")
    return Settings(
        google_client_id=os.getenv("GOOGLE_CLIENT_ID", ""),
        google_client_secret=os.getenv("GOOGLE_CLIENT_SECRET", ""),
        secret_key=os.getenv("SECRET_KEY", ""),
        allowed_emails=[e.strip() for e in allowed_raw.split(",") if e.strip()],
        downloads_dir=os.getenv("DOWNLOADS_DIR", ""),
        bind_host=os.getenv("BIND_HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8080")),
    )
```

- [ ] **Step 2: `.env.example` を更新**

```bash
# bookq — Environment variables
#
# Google OAuth 2.0 credentials
# Create at: https://console.cloud.google.com/apis/credentials
GOOGLE_CLIENT_ID=xxxxxxxx.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-xxxxxxxx

# Session signing key — generate with: openssl rand -hex 32
SECRET_KEY=change-me-to-a-random-64-char-string

# Comma-separated list of allowed Google account emails
ALLOWED_EMAILS=your.email@gmail.com

# TS_AUTHKEY: Tailscale auth key (reusable).
# Generate at: https://login.tailscale.com/admin/settings/keys
TS_AUTHKEY=tskey-auth-xxxxxxxx
```

- [ ] **Step 3: インポート検証**

```bash
python -c "from app.config import load_settings; s = load_settings(); print(f'downloads={s.downloads_path}')"
```
Expected: downloads パスが表示される（環境変数未設定時はデフォルト値）

- [ ] **Step 4: Commit**

```bash
git add app/config.py .env.example
git commit -m "feat: add configuration module with Google OAuth env vars"
```

---

### Task 4: auth.py — OAuth クライアントとセッション管理

**Files:**
- Create: `app/auth.py`

- [ ] **Step 1: `app/auth.py` を作成**

```python
"""Google OAuth 2.0 authentication and session management."""

from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import RedirectResponse
from authlib.integrations.starlette_client import OAuth, OAuthError
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from app.config import Settings

SESSION_COOKIE_NAME = "session"
SESSION_MAX_AGE = 604800  # 7 days in seconds

router = APIRouter(prefix="/auth", tags=["auth"])

oauth = OAuth()


def init_oauth(settings: Settings):
    """Register Google OAuth client. Call once at app startup."""
    oauth.register(
        name="google",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )


def _get_serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt="session")


def create_session_cookie(settings: Settings, user: dict) -> str:
    """Create a signed session cookie value."""
    serializer = _get_serializer(settings)
    return serializer.dumps(user)


def verify_session(settings: Settings, cookie_value: str) -> dict | None:
    """Verify a session cookie. Returns user dict or None."""
    serializer = _get_serializer(settings)
    try:
        return serializer.loads(cookie_value, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


async def get_current_user(request: Request) -> dict:
    """FastAPI dependency: extract and verify the current user from session cookie.
    Raises HTTPException(401) if not authenticated.
    """
    settings: Settings = request.app.state.settings
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = verify_session(settings, cookie)
    if user is None:
        raise HTTPException(status_code=401, detail="Session expired or invalid")
    return user


# ── Auth routes ────────────────────────────────────────────────────────


@router.get("/login")
async def login(request: Request):
    """Redirect to Google OAuth consent screen."""
    redirect_uri = str(request.url_for("auth_callback"))
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/callback", name="auth_callback")
async def auth_callback(request: Request):
    """Handle Google OAuth callback: exchange code for token, verify user, set session."""
    settings: Settings = request.app.state.settings

    try:
        token = await oauth.google.authorize_access_token(request)
    except OAuthError:
        return RedirectResponse(url="/?error=access_denied", status_code=303)

    userinfo = token.get("userinfo")
    if not userinfo:
        raise HTTPException(status_code=502, detail="Auth provider error: no userinfo")

    email = (userinfo.get("email") or "").lower()
    if email not in settings.allowed_emails_set:
        raise HTTPException(status_code=403, detail="Email not allowed")

    session_data = {
        "email": email,
        "name": userinfo.get("name", email),
        "picture": userinfo.get("picture", ""),
    }

    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=create_session_cookie(settings, session_data),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return response


@router.get("/logout")
async def logout():
    """Clear session cookie and redirect to home."""
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response
```

- [ ] **Step 2: インポート検証**

```bash
python -c "from app.auth import router, get_current_user, init_oauth; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add app/auth.py
git commit -m "feat: add Google OAuth client and session management"
```

---

### Task 5: routes.py — 全エンドポイント

**Files:**
- Create: `app/routes.py`

- [ ] **Step 1: `app/routes.py` を作成**

```python
"""Application routes: viewer page, API, and image serving."""

import mimetypes
from pathlib import Path

from fastapi import APIRouter, Depends, Request, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from app.auth import get_current_user
from app.scanner import scan_tree, list_images

api_router = APIRouter()

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@api_router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Viewer home page. Shows login prompt if unauthenticated, full viewer if authenticated."""
    user = None
    try:
        user = await get_current_user(request)
    except HTTPException:
        pass

    return templates.TemplateResponse("index.html", {
        "request": request,
        "user": user,
    })


@api_router.get("/api/health")
async def health():
    """Health check — no auth required."""
    return {"status": "ok"}


@api_router.get("/api/folders")
async def api_folders(request: Request, _user: dict = Depends(get_current_user)):
    """List all folders as a recursive tree. Requires authentication."""
    downloads_dir = request.app.state.settings.downloads_path
    tree = scan_tree(downloads_dir)
    return tree


@api_router.get("/api/images")
async def api_images(
    request: Request,
    folder: str = Query(..., description="Folder path relative to downloads dir"),
    _user: dict = Depends(get_current_user),
):
    """List images in a folder. Requires authentication."""
    downloads_dir = request.app.state.settings.downloads_path
    folder_path = (downloads_dir / folder).resolve()

    if not str(folder_path).startswith(str(downloads_dir)):
        raise HTTPException(status_code=403, detail="Forbidden")

    if not folder_path.is_dir():
        raise HTTPException(status_code=404, detail=f"Folder not found: {folder}")

    images = list_images(folder_path)
    return images


@api_router.get("/image/{path:path}")
async def serve_image(
    path: str,
    request: Request,
    _user: dict = Depends(get_current_user),
):
    """Serve an image file. Requires authentication. Path is folder/subfolder/.../filename."""
    downloads_dir = request.app.state.settings.downloads_path

    if "/" not in path:
        raise HTTPException(status_code=400, detail="Invalid image path")

    slash_idx = path.rindex("/")
    folder_name = path[:slash_idx]
    filename = path[slash_idx + 1:]

    if not folder_name or not filename:
        raise HTTPException(status_code=400, detail="Invalid image path")

    file_path = (downloads_dir / folder_name / filename).resolve()

    if not str(file_path).startswith(str(downloads_dir)):
        raise HTTPException(status_code=403, detail="Forbidden")

    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    ctype, _ = mimetypes.guess_type(str(file_path))
    return FileResponse(
        path=str(file_path),
        media_type=ctype or "application/octet-stream",
        headers={"Cache-Control": "public, max-age=3600"},
    )
```

- [ ] **Step 2: インポート検証**

```bash
python -c "from app.routes import api_router; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add app/routes.py
git commit -m "feat: add FastAPI routes with auth-gated endpoints"
```

---

### Task 6: main.py — FastAPI アプリケーション

**Files:**
- Create: `app/main.py`

- [ ] **Step 1: `app/main.py` を作成**

```python
"""bookq — FastAPI image viewer with Google OAuth authentication."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import load_settings
from app.auth import router as auth_router, init_oauth
from app.routes import api_router


def create_app() -> FastAPI:
    settings = load_settings()

    app = FastAPI(title="bookq", version="2.0.0")
    app.state.settings = settings

    init_oauth(settings)

    app.include_router(auth_router)
    app.include_router(api_router)

    @app.exception_handler(401)
    async def unauthorized_handler(request: Request, exc: Exception):
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})

    return app


app = create_app()
```

- [ ] **Step 2: アプリが起動することを確認**

```bash
cd /Users/<your-username>/Documents/GitHub/bookq && python -c "
from app.main import app
print(f'App created: {app.title}')
print(f'Routes: {len(app.routes)}')
"
```
Expected: `App created: bookq` とルート数が表示される

- [ ] **Step 3: Commit**

```bash
git add app/main.py
git commit -m "feat: add FastAPI application entry point"
```

---

### Task 7: templates/index.html — Jinja2 ビューアテンプレート

**Files:**
- Create: `app/templates/index.html`

- [ ] **Step 1: テンプレートディレクトリを作成**

```bash
mkdir -p app/templates
```

- [ ] **Step 2: `app/templates/index.html` を作成**

`viewer.py` の `INDEX_HTML` 文字列全体をコピーし、以下の変更を加える：

1. `<!DOCTYPE html>` の上に Jinja2 の認証チェックを追加
2. `</body>` の直前にログイン/ログアウトの分岐を追加

ファイルの先頭に追加する認証ブロック：
```html
{% if not user %}
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>bookq — Login</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{width:100%;height:100%;background:#0d0d0d;color:#ccc;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
body{display:flex;align-items:center;justify-content:center}
.login-box{text-align:center}
.login-box h1{font-size:24px;font-weight:300;color:#888;margin-bottom:24px}
.login-btn{display:inline-flex;align-items:center;gap:10px;padding:10px 24px;background:#1a1a1a;border:1px solid #333;border-radius:8px;color:#ccc;font-size:15px;cursor:pointer;text-decoration:none;transition:background .15s,border-color .15s}
.login-btn:hover{background:#222;border-color:#555}
.login-btn img{width:20px;height:20px}
</style>
</head>
<body>
<div class="login-box">
  <h1>bookq</h1>
  <a href="/auth/login" class="login-btn">
    <img src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath fill='%234285F4' d='M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 01-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1z'/%3E%3Cpath fill='%2334A853' d='M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z'/%3E%3Cpath fill='%23FBBC05' d='M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z'/%3E%3Cpath fill='%23EA4335' d='M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z'/%3E%3C/svg%3E" alt="Google">
    Sign in with Google
  </a>
</div>
</body>
</html>
{% else %}
```

ファイルの末尾に追加するブロック（`</html>` の直前）：
```html
{% endif %}
```

完全なファイルは、既存の `viewer.py` の `INDEX_HTML` 文字列を以下のように囲んだもの：
```
{% if not user %}
  [ログイン画面の HTML]
{% else %}
  [既存の INDEX_HTML 全文]
{% endif %}
```

**注意**: Jinja2 テンプレート内で `{{ }}` や `{% %}` が使われているが、既存の JavaScript には `{{` `}}` は含まれていないので競合しない。

- [ ] **Step 3: テンプレートが読み込めることを確認**

```bash
python -c "
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader('app/templates'))
tmpl = env.get_template('index.html')
print('Template loaded OK')
"
```
Expected: `Template loaded OK`

- [ ] **Step 4: Commit**

```bash
git add app/templates/index.html
git commit -m "feat: add Jinja2 viewer template with login gate"
```

---

### Task 8: Dockerfile — uvicorn 起動に変更

**Files:**
- Modify: `Dockerfile`

- [ ] **Step 1: `Dockerfile` を更新**

```dockerfile
FROM python:3.13-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/

# Create downloads mount point
RUN mkdir -p /app/downloads

EXPOSE 8080

ENTRYPOINT ["uvicorn", "app.main:app"]
CMD ["--host", "0.0.0.0", "--port", "8080"]
```

- [ ] **Step 2: Commit**

```bash
git add Dockerfile
git commit -m "feat: update Dockerfile for FastAPI + uvicorn"
```

---

### Task 9: docker-compose.yml — 環境変数追加

**Files:**
- Modify: `docker-compose.yml`

- [ ] **Step 1: `docker-compose.yml` を更新**

環境変数を viewer サービスに追加：

```yaml
services:
  viewer:
    build: .
    volumes:
      - /Users/<your-username>/Documents/GitHub/library/downloads:/app/downloads
    environment:
      - GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID}
      - GOOGLE_CLIENT_SECRET=${GOOGLE_CLIENT_SECRET}
      - SECRET_KEY=${SECRET_KEY}
      - ALLOWED_EMAILS=${ALLOWED_EMAILS}
    restart: unless-stopped

  tailscale:
    build: ./tailscale
    hostname: bookq
    cap_add:
      - NET_ADMIN
      - NET_RAW
    environment:
      - TS_AUTHKEY=${TS_AUTHKEY}
      - TS_HOSTNAME=bookq
    volumes:
      - tailscale-state:/var/lib/tailscale
    restart: unless-stopped
    depends_on:
      - viewer

volumes:
  tailscale-state:
```

- [ ] **Step 2: Commit**

```bash
git add docker-compose.yml
git commit -m "feat: add Google OAuth env vars to docker-compose"
```

---

### Task 10: クリーンアップ — viewer.py の削除

**Files:**
- Delete: `viewer.py`

- [ ] **Step 1: viewer.py を削除**

```bash
rm viewer.py
```

- [ ] **Step 2: Commit**

```bash
git add viewer.py
git commit -m "refactor: remove old viewer.py — replaced by FastAPI app"
```

---

### Task 11: 統合テストと起動確認

**Files:**
- No changes — verification only

- [ ] **Step 1: Docker イメージをビルド**

```bash
docker compose build
```
Expected: エラーなくビルド完了

- [ ] **Step 2: サービスを起動**

```bash
docker compose up -d
```

- [ ] **Step 3: ヘルスチェックを確認**

```bash
sleep 3 && curl -s http://localhost:8080/api/health
```
Expected: `{"status":"ok"}`

- [ ] **Step 4: 未認証での API アクセスを確認（401 が返ること）**

```bash
curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/api/folders
```
Expected: `401`

- [ ] **Step 5: ホームページにログインボタンが表示されることを確認**

```bash
curl -s http://localhost:8080/ | grep -c "Sign in with Google"
```
Expected: `1`

- [ ] **Step 6: サービスを停止**

```bash
docker compose down
```

- [ ] **Step 7: Commit（必要なし、検証のみ）**

---

### Task 12: 最終確認とドキュメント更新

**Files:**
- Modify: `README.md`

- [ ] **Step 1: README.md を更新**

FastAPI 移行と Google OAuth 認証の追加を反映：

`## 必要なもの` に以下を追加：
```markdown
- Google Cloud Console で OAuth 2.0 クライアントを作成
```

`## 構築手順` の前に以下を追加：
```markdown
### 0. Google OAuth の設定

1. [Google Cloud Console](https://console.cloud.google.com/apis/credentials) で OAuth 2.0 クライアント ID を作成
   - アプリケーションタイプ: **ウェブアプリケーション**
   - 承認済みのリダイレクト URI: `https://<your-tailscale-hostname>/auth/callback`
2. 発行されたクライアント ID とクライアントシークレットを `.env` に設定
3. セッション署名鍵を生成:
   ```bash
   openssl rand -hex 32  # → SECRET_KEY に設定
   ```
4. アクセスを許可する Google アカウントのメールアドレスを `ALLOWED_EMAILS` に設定
```

`## API` のテーブルを更新：
```markdown
| エンドポイント | 認証 | 説明 |
|---|---|---|
| `GET /` | 要 | 画像ビューア |
| `GET /api/health` | 不要 | ヘルスチェック |
| `GET /api/folders` | 要 | フォルダツリー一覧 |
| `GET /api/images?folder=...` | 要 | フォルダ内の画像一覧 |
| `GET /image/<path>` | 要 | 画像ファイルの配信 |
| `GET /auth/login` | 不要 | Google OAuth ログイン |
| `GET /auth/callback` | 不要 | OAuth コールバック |
| `GET /auth/logout` | 不要 | ログアウト |
```

`## ファイル構成` のテーブルを更新：
```markdown
| ファイル | 説明 |
|---|---|
| `app/main.py` | FastAPI アプリケーション |
| `app/config.py` | 環境変数設定 |
| `app/auth.py` | Google OAuth 認証 + セッション管理 |
| `app/routes.py` | 全エンドポイント |
| `app/scanner.py` | フォルダ/画像スキャン |
| `app/templates/index.html` | ビューア HTML テンプレート |
| `requirements.txt` | Python 依存パッケージ |
| `docker-compose.yml` | viewer + tailscale サイドカー |
| `Dockerfile` | viewer コンテナ |
| `prd.sh` | 起動スクリプト |
| `tailscale/` | Tailscale コンテナ |
| `.env.example` | 環境変数テンプレート |
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: update README for FastAPI + Google OAuth auth"
```
