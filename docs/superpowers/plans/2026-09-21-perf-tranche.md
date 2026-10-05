# 性能トランシェ（PR 1〜3）実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** トップ表示の律速である `/api/folders`（3.8MB 非圧縮 × 2 回）を「gzip 1 回 + 条件付き再検証」にし、静的アセットの Cache-Control を明示し、以降の PR がデグレを機械的に検知できる予算テストと計測スクリプトをリポジトリに入れる。

**Architecture:** サーバ側は Starlette `GZipMiddleware` をパス限定（`/api/`, `/static/`, `/sw.js`）で被せる薄いサブクラスと、`Cache-Control: no-cache` を付ける `StaticFiles` サブクラスの 2 点。クライアント側は再検証フェッチの `cache:"no-store"` を `"no-cache"` に変えて既存の ETag / 304 を活かす。デグレ検知は「ms に依存しない決定的指標」（リクエスト回数・転送バイト比・long task 数）を Playwright で CI に入れ、ms 指標は `scripts/perf/measure.py` で example-host 実データに対して手動計測する。

**Tech Stack:** FastAPI / Starlette middleware、Playwright (Python, chromium)、pytest、httpx（既存依存）。ビルドステップ・フレームワークは導入しない（決定 Q13）。

**Spec:** `output/2026-09-21-uiux-audit/decisions.md`（決定事項）、`output/2026-09-21-uiux-audit/report.md`（監査）、`output/2026-09-21-uiux-audit/baseline.md`（現状値）。

## Global Constraints

- 画像（`/image/*`, `/thumb/*`）は**圧縮しない・リサイズしない**（決定 Q10 b、および JPEG は非圧縮性）。
- `/api/folders` の JSON ボディは byte-identical を維持（既存の `_folders_response` の ETag 計算・オフライン IDB スナップショットが依存）。変えるのはエンコーディングとヘッダのみ。
- SW の `SHELL_ASSETS` と `index.html` のスクリプト読み込み順は触らない。
- KPI（iPhone プロファイル、example-host、3 run 中央値）: 初タイル ≤300ms、トップ転送 ≤1MB、開く→初画像 ≤150ms、ページ送り long task 0。先読み転送量は観測のみ。
- 各 PR は単独で revert 可能。PR 1 → 2 → 3 の順に main へマージし、PR 3 の予算テストは PR 1 マージ後の挙動を前提にする。
- コミットは署名する（`git commit -S`。`commit.gpgsign=true` なら `-S` 省略可。実行前に `git config --get commit.gpgsign` で確認）。コミット末尾に `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`。
- テスト実行コマンド（毎回これ）:
  `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest <path> -q`
- example-host への反映は人間が行う: ブランチを push したら「`git pull` して `./dev.sh up --lan-allow --local` お願いします（ブランチ: X）」と 1 行で依頼する。計測は反映後に `scripts/perf/measure.py http://example-host.local:8080`（ブラウザペインは同ホスト名がブロックされるため IP `192.0.2.1` でも可）。

---

## PR 1: `/api/folders` gzip + 条件付き再検証

ブランチ: `perf/api-gzip`（main から作成）。

### Task 1: パス限定 gzip ミドルウェア

**Files:**
- Create: `app/compression.py`
- Modify: `app/main.py`（`create_app` 内、SessionMiddleware 追加の直後）
- Test: `tests/test_compression.py`

**Interfaces:**
- Produces: `app.compression.ApiGZipMiddleware(app, minimum_size=1024, compresslevel=6)` — Starlette `GZipMiddleware` のサブクラス。`scope["path"]` が `COMPRESS_PREFIXES = ("/api/", "/static/", "/sw.js")` のいずれかで始まる HTTP リクエストだけ親クラスに委譲し、それ以外は素通し。
- Consumes: 既存 `tests/conftest.py` の `server` フィクスチャ（uvicorn を test mode で起動、base URL を返す）。

- [ ] **Step 1: 失敗するテストを書く**

```python
# tests/test_compression.py
"""gzip はテキスト系（/api, /static, /sw.js）だけ。画像は絶対に圧縮しない。"""
import gzip
import json

import httpx


def _get(server: str, path: str, accept_encoding: str | None = "gzip"):
    headers = {}
    if accept_encoding is not None:
        headers["Accept-Encoding"] = accept_encoding
    # httpx は既定で自動デコードするので raw を見るため stream を使う
    with httpx.Client(base_url=server, timeout=10) as c:
        with c.stream("GET", path, headers=headers) as r:
            raw = b"".join(r.iter_raw())
            return r, raw


def test_api_folders_is_gzipped_when_accepted(server):
    r, raw = _get(server, "/api/folders")
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"
    body = json.loads(gzip.decompress(raw))
    assert isinstance(body, list) and len(body) > 0
    # 圧縮が効いている（少なくとも 3 倍）
    assert len(raw) * 3 < len(json.dumps(body).encode("utf-8"))


def test_api_folders_identity_without_accept_encoding(server):
    r, raw = _get(server, "/api/folders", accept_encoding="identity")
    assert r.status_code == 200
    assert "content-encoding" not in r.headers
    assert isinstance(json.loads(raw), list)


def test_api_folders_etag_survives_gzip(server):
    r1, _ = _get(server, "/api/folders")
    etag = r1.headers.get("etag")
    assert etag
    with httpx.Client(base_url=server, timeout=10) as c:
        r2 = c.get("/api/folders", headers={"Accept-Encoding": "gzip", "If-None-Match": etag})
    assert r2.status_code == 304


def test_static_js_is_gzipped(server):
    r, raw = _get(server, "/static/app.js")
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"


def test_images_are_never_gzipped(server):
    # conftest の fixture ツリーに simple_manga/1.png がある
    r, raw = _get(server, "/image/simple_manga/1.png")
    assert r.status_code == 200
    assert "content-encoding" not in r.headers
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    r2, _ = _get(server, "/thumb/simple_manga/1.png?w=200")
    assert r2.status_code == 200
    assert "content-encoding" not in r2.headers
```

- [ ] **Step 2: 失敗を確認する**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_compression.py -q`
Expected: `test_api_folders_is_gzipped_when_accepted` / `test_static_js_is_gzipped` が `assert None == 'gzip'` で FAIL。他 3 本は PASS（現状も圧縮していないので identity・画像・ETag は満たす）。

- [ ] **Step 3: ミドルウェアを実装する**

```python
# app/compression.py
"""Path-scoped gzip.

Starlette's GZipMiddleware compresses every response regardless of type; that
would waste CPU on JPEG/PNG under /image and /thumb (already compressed) and
interfere with Range (206) responses there. So compression is opted in by path:
API JSON (3.8 MB trees shrink ~10x) and the JS/CSS shell. Everything else is
passed through untouched.
"""
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

COMPRESS_PREFIXES = ("/api/", "/static/", "/sw.js")


class ApiGZipMiddleware(GZipMiddleware):
    def __init__(self, app: ASGIApp, minimum_size: int = 1024, compresslevel: int = 6) -> None:
        super().__init__(app, minimum_size=minimum_size, compresslevel=compresslevel)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path", "").startswith(COMPRESS_PREFIXES):
            await super().__call__(scope, receive, send)
            return
        await self.app(scope, receive, send)
```

`app/main.py` の `create_app` で、`app.add_middleware(SessionMiddleware, ...)` の直後に追加:

```python
    from app.compression import ApiGZipMiddleware  # 先頭 import 群に移してよい
    # API JSON と JS/CSS だけ gzip。画像は素通し（app/compression.py 参照）。
    app.add_middleware(ApiGZipMiddleware, minimum_size=1024, compresslevel=6)
```

（`add_middleware` は後に追加したものが外側になる。gzip が SessionMiddleware の外側でも内側でも動作は変わらない。）

- [ ] **Step 4: テストが通ることを確認する**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_compression.py -q`
Expected: 5 passed。

- [ ] **Step 5: 既存テストへの影響を確認する**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_offline.py tests/test_sw_offline_contract.py tests/test_traversal.py tests/test_top_display_perf.py -q`
Expected: 全 PASS（SW の `caches.put` は fetch がデコード済みボディを返すので影響なし。Range 206 は `/image` `/thumb` 配下で圧縮対象外）。

- [ ] **Step 6: コミット**

```bash
git add app/compression.py app/main.py tests/test_compression.py
git commit -S -m "perf(api): gzip /api /static /sw.js のみ圧縮（画像は素通し）

/api/folders 3.8MB JSON を約 1/10 に。GZipMiddleware をパス限定で被せる
サブクラスで、/image /thumb の JPEG/PNG と Range 応答には触れない。

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 2: 2 回目の `/api/folders` を条件付き再検証にする

**Files:**
- Modify: `app/static/app.js` — `fetchLatestFolders`（`async function fetchLatestFolders(deps){` の行、現在 196 行付近。行番号は関数名で再特定する）
- Test: `tests/test_perf_budget.py`（新規。PR 3 でも同ファイルに追記する）

**Interfaces:**
- Consumes: サーバの `/api/folders` が `ETag` + `Cache-Control: private, max-age=60` を返し、`If-None-Match` 一致で 304（既存 `app/routes.py:_folders_response`）。
- Produces: `fetchLatestFolders` は `cache:"no-cache"`（＝必ずサーバに再検証しに行くが、304 なら HTTP キャッシュのボディを使う）。`revalidateFoldersForOnline` の他の挙動は不変。

- [ ] **Step 1: 失敗するテストを書く**

```python
# tests/test_perf_budget.py
"""性能予算（ms に依存しない決定的指標）。

ms の KPI は scripts/perf/measure.py で example-host 実データに対して測る。ここでは
CI の合成フィクスチャでも安定する「回数・バイト比・long task 数」だけを守る。
decisions.md（2026-09-21）の KPI/PR 順序に対応。
"""
import pytest

FOLDERS_ENTRIES_JS = """
() => performance.getEntriesByType('resource')
  .filter(e => e.name.includes('/api/folders'))
  .map(e => ({name: e.name, transfer: e.transferSize, encoded: e.encodedBodySize, decoded: e.decodedBodySize}))
"""


def _wait_for_folders_entries(page, n: int, timeout_ms: int = 10_000):
    page.wait_for_function(
        "(n) => performance.getEntriesByType('resource').filter(e => e.name.includes('/api/folders')).length >= n",
        arg=n,
        timeout=timeout_ms,
    )


def test_top_page_fetches_folders_body_once(server, browser):
    """初回ロードで /api/folders の 200 ボディ転送は 1 回だけ。

    tryAutoSync → revalidateFoldersForOnline の 2 回目は If-None-Match 付きで
    304 になり、転送量はヘッダ分のみ（< 2KB）でなければならない。
    """
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    try:
        page.goto(server + "/")
        page.wait_for_selector("#tile-grid .tile")
        _wait_for_folders_entries(page, 2)
        entries = page.evaluate(FOLDERS_ENTRIES_JS)
        print(f"[perf-budget] folders_entries={entries}")
        assert len(entries) >= 2
        first, rest = entries[0], entries[1:]
        # gzip が効いている: 転送は展開後の 1/3 未満
        assert first["encoded"] * 3 < first["decoded"], first
        # 2 回目以降はボディを運ばない
        for e in rest:
            assert e["transfer"] < 2048, e
    finally:
        context.close()
```

- [ ] **Step 2: 失敗を確認する**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_perf_budget.py -q -s`
Expected: `assert e["transfer"] < 2048` で FAIL（2 回目が `no-store` で全量取得しているため transfer が数十 KB 以上）。Task 1 済みなので gzip の assert は通る。

- [ ] **Step 3: クライアントを直す**

`app/static/app.js` の `fetchLatestFolders`:

```js
  async function fetchLatestFolders(deps){
    const _d = _resolveDeps(deps);
    // "no-cache" = 必ずサーバに再検証（If-None-Match）するが、304 なら HTTP
    // キャッシュのボディを使う。"no-store" だと 3.8MB を毎回まるごと取り直す。
    const res = await _d.fetch("/api/folders",{cache:"no-cache", credentials:"same-origin"});
    if(!res.ok) return;
    const data = await res.json();
    if(!Array.isArray(data)) return;
    return data;
  }
```

- [ ] **Step 4: テストが通ることを確認する**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_perf_budget.py -q -s`
Expected: 1 passed。ログの 2 番目エントリの `transfer` が数百バイト。

- [ ] **Step 5: 再検証の既存テストが壊れていないことを確認する**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_offline_full_browse.py tests/test_offline.py tests/test_search_e2e.py -q`
Expected: 全 PASS。`revalidateFoldersForOnline` を直接呼ぶテスト（`window.__BOOKQ_DEBUG.revalidateFoldersForOnline`）は fetch のモックを通すので `cache` オプションの値には依存しない。FAIL したら、該当テストが `no-store` を文字列で検査していないか確認し、検査していれば `no-cache` に更新する。

- [ ] **Step 6: コミット**

```bash
git add app/static/app.js tests/test_perf_budget.py
git commit -S -m "perf(browse): /api/folders の再検証を no-cache にして 304 を活かす

初回ロード直後の tryAutoSync が no-store で 3.8MB を取り直していた。
ETag/304 は既にサーバにあるので cache:'no-cache' で条件付きにする。
予算テスト tests/test_perf_budget.py を追加（2 回目転送 < 2KB、gzip 比 1/3）。

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 3: PR 1 の検証・計測・PR 作成

**Files:** なし（運用）

- [ ] **Step 1: 全テスト**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/ -q -x -n 4`
Expected: 全 PASS。

- [ ] **Step 2: 自分の dev 環境で目視**

```bash
./dev.sh up perf-gzip --fixture
```
ブラウザで開き、DevTools Network で `/api/folders` が `content-encoding: gzip`、2 本目が 304 であることを確認。確認後 `./dev.sh down perf-gzip`。

- [ ] **Step 3: push して反映依頼**

```bash
git push -u origin perf/api-gzip
```
ユーザーへ: 「`perf/api-gzip` を `git pull` して `./dev.sh up --lan-allow --local` お願いします」

- [ ] **Step 4: example-host で before/after 計測**

反映連絡後:
```bash
uv run --with playwright --with-requirements requirements-dev.txt python output/2026-09-21-uiux-audit/measure.py http://example-host.local:8080
```
（PR 3 でスクリプトは `scripts/perf/measure.py` に移る。PR 1 時点では監査ディレクトリのものを使う。）
`baseline.md` の値と比較し、初タイル ms・トップ転送量・`/api/folders` 回数/転送を PR 本文に表で載せる。目標: トップ転送 ≤1MB、初タイル ≤300ms（未達なら「PR 1 では 500ms 前後、浅いツリー化 (b) を Issue 化」と明記）。

- [ ] **Step 5: PR 作成**

```bash
gh pr create --repo kentoku24/bookq --base main --head perf/api-gzip \
  --title "perf: /api/folders を gzip + 条件付き再検証にしてトップ転送量を 1/10 に" \
  --body "$(cat <<'EOF'
## 変更
- /api, /static, /sw.js のみ gzip（app/compression.py）。画像は素通し。
- 初回ロード直後の再検証フェッチを cache:'no-cache' にして ETag/304 を活かす。
- 予算テスト tests/test_perf_budget.py（2 回目転送 < 2KB、gzip 比 1/3）と tests/test_compression.py。

## 計測（example-host、iPhone プロファイル、3 run 中央値）
| 指標 | before | after |
|---|---|---|
| 初タイル描画 | 826ms | (記入) |
| トップ転送量 | 8.3MB | (記入) |
| /api/folders 転送 | 3.8MB × 2 | (記入) |

監査・決定: output/2026-09-21-uiux-audit/（決定 Q9 (a)）

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## PR 2: 静的アセットの Cache-Control を明示

ブランチ: `perf/static-cache-control`（main から作成。PR 1 とは独立）。

背景（監査 X3 の再評価）: 現状 Starlette `StaticFiles` は ETag のみで `Cache-Control` を出さない。ブラウザは**ヒューリスティックキャッシュ**（Last-Modified からの経過時間の 10%）で再検証を省くことがあり、デプロイ直後に古い `app.js` が使われうる。一方 `max-age` を長くすると SW の `staticSWR` の裏側 `fetch(req)` も HTTP キャッシュに当たり、更新を取りに行けなくなる。ファイル名にハッシュを持たない現行構成では **`Cache-Control: no-cache`（毎回 ETag 再検証、304 は数百バイト）が正解**で、性能は中立、正しさが改善する。PR 本文にこの理由を書く。

### Task 4: `NoCacheStaticFiles`

**Files:**
- Modify: `app/main.py`（StaticFiles mount）
- Create: `app/static_files.py`
- Test: `tests/test_static_headers.py`

**Interfaces:**
- Produces: `app.static_files.NoCacheStaticFiles(StaticFiles)` — `file_response()` の戻りに `Cache-Control: no-cache` を付ける。ETag/Last-Modified/304 は親クラスのまま。

- [ ] **Step 1: 失敗するテストを書く**

```python
# tests/test_static_headers.py
"""静的アセットは ETag 再検証前提の no-cache。ヒューリスティックキャッシュで
古い app.js が残ることを防ぐ（ハッシュ付きファイル名を持たない現行構成での正解）。"""
import httpx

SHELL_FILES = [
    "/static/app.js",
    "/static/style.css",
    "/static/offline-core.js",
    "/static/viewer/spread.js",
    "/static/manifest.json",
]


def test_static_assets_send_no_cache_and_etag(server):
    with httpx.Client(base_url=server, timeout=10) as c:
        for path in SHELL_FILES:
            r = c.get(path)
            assert r.status_code == 200, path
            assert r.headers.get("cache-control") == "no-cache", (path, dict(r.headers))
            assert r.headers.get("etag"), path


def test_static_asset_conditional_304(server):
    with httpx.Client(base_url=server, timeout=10) as c:
        r1 = c.get("/static/app.js")
        r2 = c.get("/static/app.js", headers={"If-None-Match": r1.headers["etag"]})
    assert r2.status_code == 304
    assert r2.headers.get("cache-control") == "no-cache"


def test_sw_js_keeps_no_cache_and_images_keep_public_max_age(server):
    """既存の方針が崩れていないこと（回帰防止）。"""
    with httpx.Client(base_url=server, timeout=10) as c:
        sw = c.get("/sw.js")
        img = c.get("/image/simple_manga/1.png")
        thumb = c.get("/thumb/simple_manga/1.png?w=200")
    assert sw.headers.get("cache-control") == "no-cache"
    assert img.headers.get("cache-control") == "public, max-age=3600"
    assert thumb.headers.get("cache-control", "").startswith("public, max-age=86400")
```

- [ ] **Step 2: 失敗を確認する**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_static_headers.py -q`
Expected: 最初の 2 本が `cache-control` None で FAIL。3 本目は PASS（`/thumb` の値が `public, max-age=86400` で始まらなければ、`app/routes.py:serve_thumb` の実際の値を確認して期待値を合わせる）。

- [ ] **Step 3: 実装**

```python
# app/static_files.py
"""StaticFiles with an explicit Cache-Control.

Starlette's StaticFiles sends ETag/Last-Modified but no Cache-Control, so
browsers fall back to heuristic freshness and may serve a stale app.js after a
deploy. Shell files have no content hash in their names, so a long max-age is
unsafe (the service worker's stale-while-revalidate fetch would hit the HTTP
cache too). `no-cache` = always revalidate with If-None-Match; a 304 costs a
few hundred bytes.
"""
from starlette.staticfiles import StaticFiles
from starlette.responses import Response


class NoCacheStaticFiles(StaticFiles):
    def file_response(self, *args, **kwargs) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response
```

`app/main.py`:

```python
from app.static_files import NoCacheStaticFiles
...
    if static_dir.exists():
        app.mount("/static", NoCacheStaticFiles(directory=static_dir), name="static")
```

（`from fastapi.staticfiles import StaticFiles` の import は不要になれば削除。）

- [ ] **Step 4: テストが通ることを確認する**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_static_headers.py tests/test_offline.py tests/test_sw_offline_contract.py tests/test_pwa_icons.py -q`
Expected: 全 PASS。

- [ ] **Step 5: コミット・push・PR**

```bash
git add app/static_files.py app/main.py tests/test_static_headers.py
git commit -S -m "fix(static): 静的アセットに Cache-Control: no-cache を明示

ヒューリスティックキャッシュでデプロイ後も古い app.js が残りうるのを防ぐ。
ハッシュ無しファイル名なので長い max-age は SW の SWR とも相性が悪く、
ETag 再検証（304）が正解。性能は中立。

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
git push -u origin perf/static-cache-control
gh pr create --repo kentoku24/bookq --base main --head perf/static-cache-control \
  --title "fix(static): 静的アセットに Cache-Control: no-cache を明示" \
  --body "$(cat <<'EOF'
監査 X3「静的アセットに Cache-Control なし」の対応。長い max-age ではなく no-cache にした理由:
- ファイル名にハッシュが無いので max-age はデプロイ後の古い JS を固定してしまう
- SW の staticSWR の裏側 fetch も HTTP キャッシュに当たるため更新を取れなくなる
- 現状のヒューリスティックキャッシュは再検証を省くことがあり、こちらの方が危険
性能は中立（304 は数百バイト、PWA では SW キャッシュから即描画）。正しさの改善。

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## PR 3: 性能予算テスト + 計測スクリプトのリポジトリ取り込み

ブランチ: `perf/budget-tests`（**PR 1 マージ後の main** から作成。Task 2 のテストが PR 1 の挙動を前提にするため）。

### Task 5: ページ送り予算（long task 0、先読みリクエスト数）

**Files:**
- Modify: `tests/test_perf_budget.py`（Task 2 のファイルに追記）

**Interfaces:**
- Consumes: `app/static/app.js` の `PREFETCH_AHEAD = 8`（`window.PREFETCH_AHEAD` として公開済み）、フィクスチャ `long/`（30 ページ）、ビューアの `#counter` テキスト `"N / 30"`、`←` キー = 次ページ。

- [ ] **Step 1: 失敗しうるテストを書く（現状は PASS 見込みの回帰ガード）**

```python
# tests/test_perf_budget.py に追記

def _open_long_book(page, server):
    page.goto(server + "/#/view/long?p=0")
    page.wait_for_selector("#img-track img")
    page.wait_for_function(
        "() => { const i = document.querySelector('#img-track img'); return i && i.complete && i.naturalWidth > 0; }"
    )


def test_open_book_prefetch_request_budget(server, browser):
    """本を開いた直後の /image/ リクエストは PREFETCH_AHEAD + 1 枚（現在 9）。

    定数を変えるならこのテストと test_ui.py の DOM セル数テストを同時に更新する。
    """
    context = browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    try:
        _open_long_book(page, server)
        expected = page.evaluate("window.PREFETCH_AHEAD + 1")
        page.wait_for_function(
            "(n) => performance.getEntriesByType('resource').filter(e => e.name.includes('/image/')).length >= n",
            arg=expected,
            timeout=10_000,
        )
        page.wait_for_timeout(500)  # 余分に走っていないことを見るための猶予
        count = page.evaluate(
            "() => performance.getEntriesByType('resource').filter(e => e.name.includes('/image/')).length"
        )
        print(f"[perf-budget] open_image_requests={count} expected={expected}")
        assert count == expected
    finally:
        context.close()


def test_page_turns_have_no_long_tasks(server, browser):
    """10 回のページ送りで 50ms 超の long task が 0（baseline: CPU 4x でも 0）。"""
    context = browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    try:
        _open_long_book(page, server)
        page.evaluate(
            """() => {
              window.__ccLongTasks = 0;
              new PerformanceObserver((list) => { window.__ccLongTasks += list.getEntries().length; })
                .observe({type: 'longtask', buffered: false});
            }"""
        )
        for i in range(10):
            page.keyboard.press("ArrowLeft")
            page.wait_for_function(
                "(n) => document.getElementById('counter').textContent.trim().startsWith(n + ' /')",
                arg=i + 2,
                timeout=5_000,
            )
            page.wait_for_timeout(350)  # スライドアニメーション（300ms）を待つ
        long_tasks = page.evaluate("window.__ccLongTasks")
        print(f"[perf-budget] page_turn_long_tasks={long_tasks}")
        assert long_tasks == 0
    finally:
        context.close()
```

- [ ] **Step 2: 実行して現状値を確認する**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_perf_budget.py -q -s`
Expected: 3 passed。`open_image_requests=9 expected=9`、`page_turn_long_tasks=0`。もし `open_image_requests` が 9 でなければ、`#/view/long?p=0` の直リンク復元で `PREFETCH_BEHIND` 側が走っているかを `performance` エントリの URL 一覧で確認し、期待値を `PREFETCH_AHEAD + 1`（先頭ページなので behind は 0）で説明できる数に修正する。説明できない差分はバグとして報告する。

- [ ] **Step 3: 3 回連続で安定することを確認する**

Run: `for i in 1 2 3; do uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_perf_budget.py -q || break; done`
Expected: 3 回とも passed。long task が不安定なら閾値を `<= 1` にせず、原因（初回 JIT・フォント読み込み等）を `page.wait_for_timeout(500)` を `_open_long_book` 末尾に足して排除する。

- [ ] **Step 4: コミット**

```bash
git add tests/test_perf_budget.py
git commit -S -m "test(perf): ページ送り long task 0 と先読みリクエスト数の予算テスト

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 6: `measure.py` をリポジトリに入れ、ドキュメント化

**Files:**
- Create: `scripts/perf/measure.py`（`output/2026-09-21-uiux-audit/measure.py` を移動・調整）
- Create: `docs/performance.md`
- Modify: `CLAUDE.md`（Testing approach 節の末尾に 3 行追記）

**Interfaces:**
- Produces: `python scripts/perf/measure.py <base_url> [--out DIR]`。既定 base_url は `http://example-host.local:8080`。環境変数 `ONLY=A|B|A_cpu4x` で 1 プロファイルのみ。出力 `DIR/baseline.json` と `DIR/baseline.md`（既定 `DIR=output/perf-<YYYYMMDD-HHMM>/`）。

- [ ] **Step 1: 移動と引数の整理**

```bash
mkdir -p scripts/perf
git mv output/2026-09-21-uiux-audit/measure.py scripts/perf/measure.py 2>/dev/null || cp output/2026-09-21-uiux-audit/measure.py scripts/perf/measure.py
```
（`output/` は git 管理外なので `cp` になる。監査ディレクトリ側の複製は残してよい。）

`scripts/perf/measure.py` 冒頭の `BASE = sys.argv[1] if len(sys.argv) > 1 else "http://192.0.2.1:8080"` を次に置き換える:

```python
import argparse, datetime, os

parser = argparse.ArgumentParser(description="bookq perf measurement (Playwright chromium)")
parser.add_argument("base_url", nargs="?", default="http://example-host.local:8080")
parser.add_argument("--out", default=None, help="output dir (default output/perf-<timestamp>/)")
parser.add_argument("--runs", type=int, default=3)
_args = parser.parse_args()
BASE = _args.base_url.rstrip("/")
OUT_DIR = Path(_args.out or f"output/perf-{datetime.datetime.now():%Y%m%d-%H%M}")
OUT_DIR.mkdir(parents=True, exist_ok=True)
RUNS = _args.runs
```

続けて、スクリプト内で結果を書いている箇所（`baseline.json` / `baseline.md` を書く `Path(...)` 呼び出し）を `OUT_DIR / "baseline.json"`、`OUT_DIR / "baseline.md"` に変更し、run 回数の定数（`for _ in range(3)` 等）を `RUNS` に置き換える。`grep -n "baseline\|range(3)" scripts/perf/measure.py` で全箇所を洗う。

- [ ] **Step 2: 動作確認（ローカル test-mode サーバに対して）**

別ターミナルで:
```bash
BOOKQ_TEST_MODE=true DOWNLOADS_DIR=/tmp/bookq-fixture-perf/downloads uv run --with-requirements requirements-dev.txt uvicorn app.main:app --port 8099
```
（フィクスチャは `uv run --with-requirements requirements-dev.txt python -c "from tests.fixtures.uiux_tree import *; import sys; print('see dev.sh --fixture for generation')"` ではなく、`./dev.sh up perf-measure --fixture --local --port 8099` で用意するのが簡単。その場合 uvicorn 起動は不要。）

```bash
uv run --with playwright --with-requirements requirements-dev.txt python scripts/perf/measure.py http://127.0.0.1:8099 --runs 1 --out /tmp/perf-check
test -s /tmp/perf-check/baseline.md && head -20 /tmp/perf-check/baseline.md
```
Expected: 表が出力される。フィクスチャの `sample_library` 階層は無いので、クリック連鎖が失敗する場合は `run_once` のフォルダ選択を「`#tile-grid .tile[data-type=folder]` の先頭を、リーフに着くまで最大 4 回クリック」に一般化する（`data-path` を読み、`#img-track img` が出たら停止）。

- [ ] **Step 3: docs/performance.md を書く**

```markdown
# 性能 KPI と計測

## KPI（決定 2026-09-21、iPhone プロファイル、example-host 実データ、3 run 中央値）

| 指標 | ベースライン (2026-09-21) | 目標 |
|---|---|---|
| トップ初タイル描画（cold） | 826ms | ≤300ms |
| トップ転送量 | 8.3MB | ≤1MB |
| 本を開く→初画像 | 55ms | ≤150ms 維持 |
| ページ送り中 long task | 0 | 0 維持 |
| 開く時の先読み転送量 | 2.9MB | 観測のみ（画像は原寸配信を維持する決定） |

## 2 層のデグレ検知

1. **CI（決定的指標）** `tests/test_perf_budget.py` — `/api/folders` ボディ転送 1 回・gzip 比、
   本を開いた直後の `/image/` リクエスト数 = `PREFETCH_AHEAD + 1`、ページ送り 10 回で long task 0。
   `tests/` 一括実行に含まれる。
2. **手動（ms 指標）** `scripts/perf/measure.py` — 実データのある example-host に対して PR ごとに before/after。

## 手動計測の手順

1. ブランチを push し、example-host 側で `git pull` → `./dev.sh up --lan-allow --local`（人間が実行）。
2. `uv run --with playwright --with-requirements requirements-dev.txt python scripts/perf/measure.py http://example-host.local:8080`
   （`ONLY=A` で iPhone のみ、`--runs 1` で素早く）
3. 出力 `output/perf-<timestamp>/baseline.md` の表を PR 本文に貼る。

`/image` `/thumb` は gzip 対象外・リサイズ無し。`/api` `/static` `/sw.js` は gzip（`app/compression.py`）。
静的アセットは `Cache-Control: no-cache` + ETag（`app/static_files.py`）。
```

- [ ] **Step 4: CLAUDE.md の Testing approach 節末尾に追記**

```markdown
Performance budgets live in `tests/test_perf_budget.py` (deterministic: request counts, gzip ratio, long tasks — run with the rest of `tests/`). Millisecond KPIs are measured manually against the real-library dev host with `scripts/perf/measure.py`; targets and procedure are in `docs/performance.md`. `/api`, `/static`, `/sw.js` are gzipped by `app/compression.py`; `/image` and `/thumb` are never compressed or resized.
```

- [ ] **Step 5: 全テストとコミット**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/ -q -x -n 4`
Expected: 全 PASS。

```bash
git add scripts/perf/measure.py docs/performance.md CLAUDE.md
git commit -S -m "docs(perf): KPI と計測手順を docs/performance.md に、measure.py を scripts/perf に

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
git push -u origin perf/budget-tests
gh pr create --repo kentoku24/bookq --base main --head perf/budget-tests \
  --title "test(perf): 性能予算テストと measure.py の取り込み" \
  --body "$(cat <<'EOF'
決定 Q12（CI の決定的予算 + 手動 ms 計測）の実装。
- tests/test_perf_budget.py: /api/folders 転送 1 回・gzip 比、先読みリクエスト数、ページ送り long task 0
- scripts/perf/measure.py: example-host 向け before/after 計測（監査で使ったものを整理）
- docs/performance.md: KPI 表と手順。CLAUDE.md に 3 行。

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

### Task 7: 残課題の Issue 化（PR 3 のあとに 1 回）

**Files:** なし（GitHub）

- [ ] **Step 1: PR 1 の計測結果で初タイル 300ms 未達なら Issue を立てる**

```bash
gh issue create --repo kentoku24/bookq --title "perf(browse): /api/folders を浅いツリー化してトップ初タイル 300ms を狙う" \
  --body "PR 1（gzip + 条件付き再検証）後も初タイル (記入)ms。ツリーが全リーフの画像名を含む（3.8MB）構造が残るため、depth=1 API を初回に使い、リーフの画像一覧は本を開くときに /api/images で取る構造変更が必要。flatten・getLeafPathsFor・検索 enumeration の書き換えを伴う。決定 Q9 (b)。根拠: output/2026-09-21-uiux-audit/"
```

- [ ] **Step 2: 監査で外した項目の Issue（PR 8 で行うが、性能に関係するものはここで）**

（PR 8 のスコープ。ここでは作らない。）

---

## Self-Review

- **Spec coverage**: 決定 Q9 (a) → Task 1–2、Q12 (a)(b) → Task 2・5・6、X3 → Task 4、KPI 計測フロー Q16/Q19 → Task 3・6、(b) の条件付き Issue → Task 7。PR 4〜8（URL 設定、ビューア/本棚磨き込み、日本語統一、Issue 化）は本計画の対象外で、第 2 計画 `2026-09-2x-polish-tranche.md` に書く。
- **Placeholder scan**: 「(記入)」は計測後に人が埋める PR 本文の値のみ。実装・テストコードに TBD なし。
- **Type consistency**: `ApiGZipMiddleware` / `COMPRESS_PREFIXES` / `NoCacheStaticFiles` / `_open_long_book` / `FOLDERS_ENTRIES_JS` / `_wait_for_folders_entries` の名前は各 Task で一致。`measure.py` の CLI は `base_url [--out] [--runs]` + `ONLY` 環境変数で Task 6 と docs が一致。
