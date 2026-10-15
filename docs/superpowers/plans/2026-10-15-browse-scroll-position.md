# フォルダ一覧の表示位置（スクロールアンカー）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** サイドバーのフォルダ一覧（`#sidebar-scroll` 内の `#tile-grid`）のスクロール位置を、①ビューア復帰では表示していた章、②新規オープンでは直下の最大既読エピソード、③該当なしでは上端、と決定的に決める。

**Architecture:** 決定（どこに合わせるか）と適用（幾何）を新モジュール `app/static/browse/scroll.js`（`window.BrowseScroll`）に集約し、`renderBrowse({scrollAnchor})` が `innerHTML` 置換と同一タスク内で同期適用する。ビューア復帰は `restoreBrowse` が2回走る（`returnToBrowse` と `pushBrowseUrl` の `history.go(-1)` 後の `hashchange`）ため、章パスは teardown 前の live 状態から `noteViewerReturn` でマーカー化し、level 一致の間は冪等に同じアンカーを返す。

**Tech Stack:** 素の JS（ビルドなし・global script・defer ロード）、FastAPI 静的配信、pytest + Playwright（Chromium/WebKit エミュレーション）。

**Spec:** `docs/superpowers/specs/2026-10-15-browse-scroll-position-design.md`（決定ルール・実測値・スコープ外はこちらが正）

---

## Global Constraints

- 作業ディレクトリ: `/Users/kentokumatsunami/Documents/GitHub/bookq/.worktrees/browse-scroll-position`（branch `ai/browse-scroll-position`）。**リポジトリ直下（main）では編集・commit しない**（AGENTS.md §8）。
- テスト実行（必須の形。`--with-requirements requirements-dev.txt` を省略しない）:
  - `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/ -q`
  - 単一ファイル: 末尾に `tests/test_browse_scroll_target.py -q -s`
  - Chromium ブラウザが無い環境では先に `uv run --with playwright playwright install chromium`
- `BOOKQ_TEST_MODE=true` は `tests/conftest.py` が環境に入れる（手動で付ける必要なし）。
- JS は **ビルドなし・再起動不要**。`app/templates/index.html` のスクリプトは `defer` の**ロード順が契約**: `offline-core` → `thumbs/resolveCover` → `browse/hash` → `browse/render` → **`browse/scroll`（新規）** → `search/facets` → `offline/storeAdapter` → `thumbs/prefetch` → `viewer/spread` → `viewer/gesture` → `viewer/zoom` → `viewer/overview` → `app.js`。
- スタイル: `app/static/browse/*.js` と `viewer/*.js` は `var` + IIFE + `window.X = {...}`。`app/static/app.js` は `const/let` + アロー。既存の try/catch + `console.warn(e)` の防御スタイルを踏襲する。
- `window.BrowseScroll` は `app.js` から見て**ハード依存**（`window.Overview` と同じ扱い。`index.html` の defer 順で必ず先に読み込まれる）。ただし `browse/render.js` から呼ぶ適用だけは `window.BrowseScroll` の有無をガードする（描画を絶対に壊さない）。
- SW 契約: `app/static/sw.js` の `SHELL_ASSETS` は `tests/test_sw_offline_contract.py::test_shell_precache_v2_count` が件数を pin（19 → 20 に更新する）。`SHELL_CACHE` のバージョンは上げない（sw.js の内容が変われば install が再実行され、同じ cache 名に新アセットが入る）。
- コミットは conventional commits + `(ai)` スコープ（AGENTS.md §2/§4）。小さく積む。`main` への push はしない。
- live 実データのフォルダ名は、コード・コミットメッセージ・ログ・レポートに出さない（AGENTS.md §4 / `tests/live/README.md`）。
- `app/static/app.js` は他 worktree と同時編集しない（AGENTS.md §8）。編集前に `git worktree list` と `docker ps --format '{{.Names}}\t{{.Labels}}' | grep com.bookq.name` で他作業を確認する。
- dev 環境スロット: この worktree の名前は `browse-scroll-position`（Task 0 で確保、マージ時に `./dev.sh down browse-scroll-position`）。

## File Structure

| ファイル | 責務 | 変更 |
| --- | --- | --- |
| `app/static/browse/scroll.js` | 一覧スクロール位置の**決定**（`decide`）と**適用**（`apply`）、ビューア復帰マーカー | 新規 |
| `app/static/browse/render.js` | `renderBrowse` が `scrollAnchor` を受けて `innerHTML` 置換直後に適用 | 変更 |
| `app/static/browse/hash.js` | `restoreBrowse` で章の捕捉・`decide`・`via:'saved'` 分岐、`resetToTop` でマーカー破棄＋上端 | 変更 |
| `app/static/app.js` | `renderView({scrollAnchor})` の受け渡し、`navigateToFolder` でアンカー計算、`loadImages` でマーカー破棄 | 変更 |
| `app/templates/index.html` | `browse/scroll.js` の defer ロード | 変更 |
| `app/static/sw.js` | `SHELL_ASSETS` に `/static/browse/scroll.js` | 変更 |
| `tests/test_sw_offline_contract.py` | 件数 19 → 20 | 変更 |
| `tests/test_browse_scroll_target.py` | 契約テスト（新規 10 本） | 新規 |
| `docs/core-features.md`, `CLAUDE.md`, `AGENTS.md` | 仕様とモジュール一覧 | 変更 |

---

### Task 0: 作業環境の確保

**Files:** なし（環境のみ）

- [ ] **Step 1: worktree とブランチを確認**

Run:
```bash
cd /Users/kentokumatsunami/Documents/GitHub/bookq/.worktrees/browse-scroll-position
git branch --show-current && git log --oneline -2
```
Expected: `ai/browse-scroll-position` と `docs(ai): フォルダ一覧の表示位置（スクロールアンカー）設計を追加` が見える。

- [ ] **Step 2: dev 環境スロットを確保（AGENTS.md §8）**

Run:
```bash
cd /Users/kentokumatsunami/Documents/GitHub/bookq/.worktrees/browse-scroll-position
./dev.sh up browse-scroll-position --local --port 8090
./dev.sh url browse-scroll-position
```
Expected: `http://127.0.0.1:8090` が出力され、`docker ps` に `com.bookq.name=browse-scroll-position` が現れる。ポート 8080 は他環境（dev-02）が使用中のため 8090 を使う。既に 8090 が埋まっていれば `--port 8091` に読み替える。

- [ ] **Step 3: Playwright ブラウザの存在確認**

Run:
```bash
cd /Users/kentokumatsunami/Documents/GitHub/bookq/.worktrees/browse-scroll-position
uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_sidebar_scroll.py -q
```
Expected: `2 passed`（既存のベースライン。ここで赤ければ環境要因なので先に解決する。`BrowserType.launch: Executable doesn't exist` が出たら `uv run --with playwright playwright install chromium`）。

- [ ] **Step 4: コミットは無し**（環境のみ）

---

### Task 1: RED テスト（契約を先に固定する）

**Files:**
- Create: `tests/test_browse_scroll_target.py`

- [ ] **Step 1: テストファイル全体を書く**

`tests/test_browse_scroll_target.py`:

```python
"""フォルダ一覧の表示位置（スクロールアンカー）の契約。

決定ルール（優先順）:
  1. ビューア復帰 → ビューアで表示していた章のタイルを上端に
  2. その階層に保存位置がある restore → 従来どおり保存位置を復元
  3. それ以外の新規オープン → 直下の既読エピソードのうち一覧順で最後を上端に
  4. 該当なし → 上端

390x844 では fixture のほとんどの階層がスクロール不能（実測: group は
scrollHeight == clientHeight == 728）で空振りするため、このファイルは専用
ビューポート 390x430（tile モードで 3 件以上の階層が確実にスクロールする高さ）
で測る。各テストは冒頭で scrollHeight > clientHeight を前提として assert し、
空振りを構造的に排除する。

fixture（tests/conftest.py）:
  zoom_series = zoom_series/series/ep-1..3（scanner の flatten で zoom_series の直下）
  group = deep_series / episodes / fullwidth_episodes / other_work
"""
import os

import pytest

W, H = 390, 430
SERIES = "zoom_series"
EP2 = "zoom_series/series/ep-2"
EP3 = "zoom_series/series/ep-3"


def _browser(request):
    if os.environ.get("BROWSER") == "webkit":
        return request.getfixturevalue("webkit_browser")
    return request.getfixturevalue("browser")


@pytest.fixture
def small(request, server):
    """390x430 の専用コンテキスト（フルツリー到着まで待つ）。"""
    b = _browser(request)
    context = b.new_context(viewport={"width": W, "height": H})
    page = context.new_page()
    page.goto(server)
    page.wait_for_selector(".tile")
    page.wait_for_function("() => window.treeLoaded === true")  # shallow → full の到着待ち
    yield page
    context.close()


def _scroll(page):
    return page.evaluate("""() => {
        const ss = document.getElementById('sidebar-scroll');
        return {top: ss.scrollTop, max: ss.scrollHeight - ss.clientHeight};
    }""")


def _assert_scrollable(page):
    s = _scroll(page)
    assert s["max"] > 0, (
        f"前提: 一覧がスクロール可能であること (max={s['max']})。"
        "fixture か viewport が変わった可能性"
    )
    return s


def _tile_top(page, path):
    """一覧先頭からのタイル上端オフセット（アンカー適用時の期待値）。"""
    return page.evaluate("""(p) => {
        const ss = document.getElementById('sidebar-scroll');
        const el = [...document.querySelectorAll('#tile-grid [data-path]')]
            .find(e => e.dataset.path === p);
        return el ? Math.round(el.offsetTop - ss.offsetTop) : null;
    }""", path)


def _open(page, path):
    """タイルをタップ（画像だけのフォルダならビューア、それ以外はドリルダウン）。

    どちらに転んだか（currentFolder / currentBrowsePath）を待ってから返す。
    """
    page.locator(f'#tile-grid [data-path="{path}"]').first.click()
    page.wait_for_function(
        "(p) => window.currentFolder === p || window.currentBrowsePath === p", path, timeout=5000
    )
    page.wait_for_timeout(150)


def _wait_browse(page, path):
    """ビューアが閉じて一覧に戻るまで（☰ / Back）。"""
    page.wait_for_function(
        "(p) => window.currentFolder === null && window.currentBrowsePath === p", path, timeout=5000
    )
    page.wait_for_timeout(400)


def _seed_progress(page, entries):
    page.evaluate("(o) => localStorage.setItem('bookq_progress', JSON.stringify(o))", entries)
    page.reload()
    page.wait_for_selector(".tile")
    page.wait_for_function("() => window.treeLoaded === true")


def _chapter(page):
    return page.evaluate("() => (window.currentViewerState() || {}).folder || null")


def test_fresh_open_without_progress_starts_at_top(small):
    """上端: 親をスクロールしていても、新しく開いた階層は上端から始まる。"""
    small.evaluate("() => { document.getElementById('sidebar-scroll').scrollTop = 500; }")
    assert _scroll(small)["top"] == 500, "前提: ルートは 500px スクロールできる"
    _open(small, SERIES)
    _assert_scrollable(small)
    assert _scroll(small)["top"] == 0


def test_fresh_open_anchors_last_read_episode(small):
    """最大既読: 一覧順で最後の既読エピソード（ts の新旧ではない）。"""
    _seed_progress(small, {
        EP2: {"page": 0, "total": 4, "ts": 2_000_000_000_000},  # ts はこちらが新しい
        EP3: {"page": 0, "total": 7, "ts": 1},
    })
    _open(small, SERIES)
    _assert_scrollable(small)
    ep2_top, ep3_top = _tile_top(small, EP2), _tile_top(small, EP3)
    assert ep3_top is not None and ep2_top is not None, "前提: ep-2 / ep-3 のタイルが並んでいる"
    assert ep3_top > ep2_top, "前提: ep-3 は ep-2 より下の行にある"
    top = _scroll(small)["top"]
    assert abs(top - ep3_top) <= 2, f"最大既読 ep-3 が上端 (top={top} expected={ep3_top})"


def test_viewer_return_anchors_displayed_episode_beats_progress(small):
    """ビューア復帰 > 最大既読: 表示していた ep-2 に合う（既読の ep-3 ではない）。"""
    _seed_progress(small, {EP3: {"page": 0, "total": 7, "ts": 1}})
    _open(small, SERIES)
    _assert_scrollable(small)
    _open(small, EP2)
    assert _chapter(small) == EP2, "前提: ep-2 を表示している"
    small.keyboard.press("ArrowLeft")   # 1 ページ送り（章は ep-2 のまま）
    small.wait_for_timeout(300)
    small.locator("#menu-btn").click()  # ☰ で一覧へ復帰
    _wait_browse(small, SERIES)
    ep2_top, ep3_top = _tile_top(small, EP2), _tile_top(small, EP3)
    top = _scroll(small)["top"]
    assert abs(top - ep2_top) <= 2, (
        f"表示していた ep-2 が上端 (top={top} ep-2={ep2_top} ep-3={ep3_top})"
    )


def test_viewer_return_is_idempotent_across_double_restore(small):
    """☰ 復帰は restoreBrowse が 2 回走る（returnToBrowse + history.go 後の hashchange）。

    2 回目の描画で位置が変わらないこと（＝マーカーが level 一致で冪等に効くこと）。
    """
    _open(small, SERIES)
    _open(small, EP3)
    small.evaluate(
        "() => { window.__rv = 0; const orig = window.renderView;"
        " window.renderView = function(){ window.__rv++; return orig.apply(this, arguments); }; }"
    )
    small.locator("#menu-btn").click()
    _wait_browse(small, SERIES)
    calls = small.evaluate("() => window.__rv")
    assert calls >= 2, f"前提: ☰ 復帰では renderView が 2 回走る (calls={calls})"
    ep3_top = _tile_top(small, EP3)
    top = _scroll(small)["top"]
    assert abs(top - ep3_top) <= 2, f"2 回目の描画で位置が変わらない (top={top} expected={ep3_top})"


def test_browser_back_from_viewer_anchors_displayed_episode(small):
    """ブラウザ Back でも表示していた章に合う。"""
    _open(small, SERIES)
    _open(small, EP3)
    assert _chapter(small) == EP3, "前提: ep-3 を表示している"
    small.keyboard.press("ArrowLeft")
    small.wait_for_timeout(300)
    small.go_back()
    _wait_browse(small, SERIES)
    ep3_top = _tile_top(small, EP3)
    top = _scroll(small)["top"]
    assert abs(top - ep3_top) <= 2, f"Back でも ep-3 が上端 (top={top} expected={ep3_top})"


def test_breadcrumb_back_keeps_saved_scroll(small):
    """回帰 pin: ↩ で親に戻るときの保存位置復元は従来どおり（アンカーは働かない）。"""
    small.evaluate("() => { document.getElementById('sidebar-scroll').scrollTop = 500; }")
    _open(small, "group")
    _open(small, "group/deep_series")
    small.locator("#browse-header").click()   # ↩
    small.wait_for_timeout(600)
    top = _scroll(small)["top"]
    assert abs(top - 500) <= 2, f"親の保存位置 (500) が復元される (top={top})"


def test_viewer_return_beats_saved_scroll(small):
    """ビューア復帰 > 保存位置: 保存位置が残っている階層でも表示していた章に合う。"""
    _open(small, "group")
    _open(small, "group/deep_series")            # ← ここで group の保存位置が入る
    small.locator("#browse-header").click()      # ↩ で group に戻る（保存位置を消費）
    small.wait_for_timeout(600)
    saved = small.evaluate("() => window.scrollPositions[window.getScrollKey('group')]")
    assert saved is not None, "前提: group に保存位置がある"
    small.evaluate("() => { document.getElementById('sidebar-scroll').scrollTop = 0; }")
    _open(small, "group/other_work")             # leaf → ビューア
    assert _chapter(small) == "group/other_work", "前提: other_work を表示している"
    small.locator("#menu-btn").click()
    _wait_browse(small, "group")
    ow_top = _tile_top(small, "group/other_work")
    assert ow_top is not None and ow_top > 0, "前提: other_work は 2 行目にある"
    top = _scroll(small)["top"]
    assert abs(top - ow_top) <= 2, f"保存位置({saved})ではなく other_work({ow_top}) (top={top})"


def test_root_fresh_open_is_always_top(small):
    """ルート（作品一覧）はアンカーを使わない: ビューアから戻っても上端。"""
    _seed_progress(small, {"simple_manga": {"page": 0, "total": 5, "ts": 1}})
    small.evaluate("() => { document.getElementById('sidebar-scroll').scrollTop = 500; }")
    _open(small, "simple_manga")
    assert _chapter(small) == "simple_manga", "前提: root 直下の leaf を開いた"
    small.go_back()                              # bare root → resetToTop
    small.wait_for_function(
        "() => window.currentFolder === null && window.currentBrowsePath === null", timeout=5000
    )
    small.wait_for_timeout(400)
    assert _scroll(small)["top"] == 0


def test_decide_exposes_decision_table(small):
    """decide() の決定表（DOM 計測なし）とマーカーの寿命。"""
    _open(small, SERIES)
    _assert_scrollable(small)
    d = lambda o: small.evaluate("(o) => window.BrowseScroll.decide(o)", o)  # noqa: E731
    assert d({"level": None, "cause": "restore", "hasSaved": False}) == {"via": "top", "anchor": None}
    assert d({"level": None, "cause": "restore", "hasSaved": True}) == {"via": "saved", "anchor": None}
    assert d({"level": SERIES, "cause": "restore", "hasSaved": True}) == {"via": "saved", "anchor": None}
    small.evaluate(
        "(e) => localStorage.setItem('bookq_progress', JSON.stringify(e))",
        {EP3: {"page": 0, "total": 7, "ts": 1}},
    )
    assert d({"level": SERIES, "cause": "open", "hasSaved": False}) == {"via": "progress", "anchor": EP3}
    # マーカー: level 一致なら viewer（保存位置より優先）
    small.evaluate("(p) => window.BrowseScroll.noteViewerReturn(p.level, p.chapter)",
                   {"level": SERIES, "chapter": EP2})
    assert d({"level": SERIES, "cause": "restore", "hasSaved": True}) == {"via": "viewer", "anchor": EP2}
    # level 不一致 → 破棄
    d({"level": "group", "cause": "restore", "hasSaved": False})
    assert small.evaluate("() => window.BrowseScroll.peekViewerReturn()") is None
    # cause==='open' → 破棄
    small.evaluate("(p) => window.BrowseScroll.noteViewerReturn(p.level, p.chapter)",
                   {"level": SERIES, "chapter": EP2})
    d({"level": SERIES, "cause": "open", "hasSaved": False})
    assert small.evaluate("() => window.BrowseScroll.peekViewerReturn()") is None


@pytest.mark.parametrize("mode,clicks", [("list", 1), ("card", 2)])
def test_apply_geometry_in_list_and_card(small, mode, clicks):
    """list/card でも適用先は同じスクローラで、タイル上端＝scrollTop になる。"""
    for _ in range(clicks):
        small.locator("#footer-refresh-btn").click()
        small.wait_for_timeout(200)
    assert small.evaluate("() => window.viewMode") == mode
    _assert_scrollable(small)
    target = small.evaluate("""() => {
        const ss = document.getElementById('sidebar-scroll');
        const max = ss.scrollHeight - ss.clientHeight;
        const els = [...document.querySelectorAll('#tile-grid [data-path]')];
        const el = els.find(e => e.dataset.path && Math.round(e.offsetTop - ss.offsetTop) <= max);
        return el ? el.dataset.path : null;
    }""")
    assert target, "前提: スクロール範囲内にタイルがある"
    small.evaluate("(p) => window.BrowseScroll.apply(p)", target)
    top = _scroll(small)["top"]
    assert abs(top - _tile_top(small, target)) <= 2, f"{mode}: apply がタイル上端に合わせる (top={top})"
    small.evaluate("() => window.BrowseScroll.apply(null)")
    assert _scroll(small)["top"] == 0, f"{mode}: apply(null) は上端"
```

- [ ] **Step 2: RED を確認する**

Run:
```bash
cd /Users/kentokumatsunami/Documents/GitHub/bookq/.worktrees/browse-scroll-position
uv run --with pytest --with playwright --with-requirements requirements-dev.txt \
  pytest tests/test_browse_scroll_target.py -q 2>&1 | tail -20
```
Expected: **10 failed, 1 passed**（テストは 11 本 = 10 関数 + list/card の parametrize 2 本）。パスするのは `test_breadcrumb_back_keeps_saved_scroll`（既存挙動の回帰 pin）。代表的な失敗理由:
- `test_fresh_open_without_progress_starts_at_top`: `assert 373 == 0`（親の持ち越し）
- `test_fresh_open_anchors_last_read_episode`: `top=0 expected≈370`
- `test_decide_exposes_decision_table` / `test_apply_geometry_in_list_and_card`: `window.BrowseScroll is undefined`

- [ ] **Step 3: コミット**

```bash
git add tests/test_browse_scroll_target.py
git commit -m "test(ai): フォルダ一覧の表示位置（アンカー）のREDテストを追加"
```

> **実装時の実測メモ（Task 1 完了時の状態）**
> 実装（commit `e273a77` + `d33905f` + `75a703f`）で、上のコードブロックから次の点が変わった。理由はすべて「テストが空振りする」または「Playwright のAPI差」で、契約の意味は同じ:
> - `page.wait_for_function(expr, path, timeout=...)` → `arg=path` をキーワードで渡す（Python 版 Playwright のシグネチャ）。`_open` / `_wait_browse` の 2 箇所。
> - **ビューア復帰系 3 本は復帰操作の直前に `document.getElementById('sidebar-scroll').scrollTop = 0` を入れる**。Playwright の `click()` がクリック前にタイルをビューポートへスクロールさせるため、リセットが無いと未実装コードでも `|top - 期待値| <= 2` に収まって空振り pass する（実測で反証済み）。
> - `test_breadcrumb_back_keeps_saved_scroll` は期待値 500 が誤り（保存されるのは子階層へドリルした時点の親の位置）。`window.scrollPositions[window.getScrollKey('group')]` を読んでから 0 に壊し、復元値と比較する形に変更（現状コードで GREEN の回帰 pin）。
> - `decide` の決定表テストに「保存位置 > 最大既読」の反例と「直下の子のみ（孫の既読は効かない）」＋その対照（`group/deep_series` では `progress`）を追加。
> - 実測の RED 内訳: `assert 373 == 0` / `top=437 expected=370` / 復帰系 3 本 `top=0 expected=370` / `保存位置(53)ではなく other_work(357)` / `assert 1569 == 0` / `window.BrowseScroll is undefined` ×3（テストは 11 本、10 failed / 1 passed）。

---

### Task 2: `browse/scroll.js`（決定と適用）＋ shell 登録

**Files:**
- Create: `app/static/browse/scroll.js`
- Modify: `app/templates/index.html`（`browse/render.js` の直後）
- Modify: `app/static/sw.js`（`SHELL_ASSETS`）
- Modify: `tests/test_sw_offline_contract.py`（件数 19 → 20）

- [ ] **Step 1: 新モジュールを書く**

`app/static/browse/scroll.js`:

```js
/* bookq browse/scroll — フォルダ一覧（#tile-grid）のスクロール位置の決定と適用。
 * Plain global script. index.html で browse/render.js の後、search/facets.js の前に読む。
 * 決定（decide）は DOM 計測をしない（単体テスト可能）。適用（apply）だけが幾何を触る。
 *
 * 決定表（上から順に評価）:
 *   cause==='top'                        -> top（上端）
 *   level===null（ルートの作品一覧）      -> hasSaved ? saved : top
 *   cause==='restore' && マーカーが一致   -> viewer（章がこの階層の表示子にあるときだけ）
 *   cause==='restore' && hasSaved         -> saved（呼び出し側の従来の rAF 復元に任せる）
 *   直下に既読がある                       -> progress（一覧順で最後＝最大番号）
 *   それ以外                              -> top
 *
 * ビューア復帰マーカー: ☰/Back の復帰は restoreBrowse が 2 回走る（returnToBrowse と、
 * pushBrowseUrl の history.go(-1) が起こす hashchange）。2 回目にはビューアは teardown
 * 済みで章パスを live 状態から取れないため、1 回目の teardown 前に確定した値をここに
 * 保持し、level が一致する間は冪等に同じアンカーを返す（一覧が跳ねない）。
 * URL の p= は 200ms デバウンスで古いので章の特定には使わない。
 */
(function () {
  var viewerReturn = null; // {level: string, chapterPath: string}

  function noteViewerReturn(level, chapterPath) {
    // ルート（level が空）はビューア復帰の対象外（作品一覧は #resume-btn の役割）
    if (!level || !chapterPath) return;
    viewerReturn = { level: level, chapterPath: chapterPath };
  }
  function peekViewerReturn() { return viewerReturn; }
  function clearViewerReturn() { viewerReturn = null; }

  // 表示中の階層の子（＝renderTiles が描くフォルダタイル）のパス一覧。
  // 絞り込み適用後なので「一覧に無いものはアンカーにしない」判定に使える。
  function _visibleChildPaths() {
    var out = [];
    try {
      var kids = window.getBrowseChildren ? window.getBrowseChildren() : [];
      for (var i = 0; i < kids.length; i++) out.push(kids[i]._fullPath);
    } catch (e) { console.warn(e); }
    return out;
  }

  function _has(list, v) { return list.indexOf(v) !== -1; }

  // 一覧順（＝サーバの自然順ソート）で最後の既読子。ReadingProgress は 1 回だけ
  // parse する（タイルごとに get() しない、browse/render.js と同じ方針）。
  function _lastReadChild(paths) {
    var all = {};
    try {
      var RP = window.ReadingProgress;
      if (RP && typeof RP.getAllSafe === 'function') all = RP.getAllSafe() || {};
      else if (RP && typeof RP.getAll === 'function') all = RP.getAll() || {};
    } catch (e) { console.warn(e); }
    var found = null;
    for (var i = 0; i < paths.length; i++) if (all[paths[i]]) found = paths[i];
    return found;
  }

  function decide(opts) {
    opts = opts || {};
    var level = (opts.level === undefined) ? null : opts.level;
    var cause = opts.cause || 'restore';
    var hasSaved = !!opts.hasSaved;

    if (cause === 'top') { clearViewerReturn(); return { via: 'top', anchor: null }; }
    // 別 level の判定に来た時点でマーカーは期限切れ
    if (viewerReturn && viewerReturn.level !== level) clearViewerReturn();
    if (!level) return hasSaved ? { via: 'saved', anchor: null } : { via: 'top', anchor: null };
    // 明示的にフォルダを開いた＝ビューア復帰の文脈は切れた
    if (cause === 'open') clearViewerReturn();

    var paths = _visibleChildPaths();
    if (viewerReturn && viewerReturn.level === level && _has(paths, viewerReturn.chapterPath)) {
      return { via: 'viewer', anchor: viewerReturn.chapterPath };
    }
    if (hasSaved) return { via: 'saved', anchor: null };
    var last = _lastReadChild(paths);
    if (last) return { via: 'progress', anchor: last };
    return { via: 'top', anchor: null };
  }

  // 実際にスクロールする要素。実測では #sidebar-scroll（#tile-grid は
  // scrollHeight === clientHeight で自分では動かない）。CSS が変わった場合の保険として
  // スクロール可能な方を選ぶ。
  function scroller() {
    var ss = document.getElementById('sidebar-scroll');
    if (ss && ss.scrollHeight > ss.clientHeight) return ss;
    var tg = document.getElementById('tile-grid');
    if (tg && tg.scrollHeight > tg.clientHeight) return tg;
    return ss || tg;
  }

  function _tileFor(path) {
    var grid = document.getElementById('tile-grid');
    if (!grid) return null;
    var els = grid.querySelectorAll('[data-path]');
    for (var i = 0; i < els.length; i++) {
      // セレクタ文字列にパスを埋めない（引用符・空白を含む名前のため）
      if (els[i].dataset.path === path) return els[i];
    }
    return null;
  }

  // innerHTML 置換と同一タスク内で呼ぶ（同期）。anchorPath が null/'' なら上端。
  function apply(anchorPath) {
    var sc = scroller();
    if (!sc) return;
    var target = 0;
    if (anchorPath) {
      var tile = _tileFor(anchorPath);
      // 一覧に無い（絞り込み・空リスト）→ 上端のまま
      if (tile) target = Math.max(0, Math.round(tile.offsetTop - sc.offsetTop));
    }
    sc.scrollTop = target;
  }

  window.BrowseScroll = {
    decide: decide,
    noteViewerReturn: noteViewerReturn,
    peekViewerReturn: peekViewerReturn,
    clearViewerReturn: clearViewerReturn,
    scroller: scroller,
    apply: apply,
  };
})();
```

- [ ] **Step 2: index.html にロード順を足す**

`app/templates/index.html` の `<script src="/static/browse/render.js" defer></script>`（149 行目付近）の直後に挿入:

```html
<script src="/static/browse/scroll.js" defer></script>
```

- [ ] **Step 3: sw.js の SHELL_ASSETS に足す**

`app/static/sw.js` の `SHELL_ASSETS` 配列内、`"/static/browse/render.js",` の直後に挿入:

```js
"/static/browse/scroll.js",
```

（`SHELL_CACHE` のバージョンは据え置き。sw.js のバイト列が変わるので install が再実行され、同じ cache 名に新アセットが入る。）

- [ ] **Step 4: SW 契約テストの件数を更新**

`tests/test_sw_offline_contract.py` の `test_shell_precache_v2_count`:

```python
    assert len(entries) == 20, f"expected 20 SHELL_ASSETS, got {len(entries)}: {entries}"
```

- [ ] **Step 5: 実行して確認**

Run:
```bash
cd /Users/kentokumatsunami/Documents/GitHub/bookq/.worktrees/browse-scroll-position
uv run --with pytest --with playwright --with-requirements requirements-dev.txt \
  pytest tests/test_sw_offline_contract.py tests/test_browse_scroll_target.py -q 2>&1 | tail -15
```
Expected: SW 契約は全パス。`test_decide_exposes_decision_table` と `test_apply_geometry_in_list_and_card`（2 本）がパスに変わる（計 4 passed / 7 failed。残り 7 本はまだ赤）。

- [ ] **Step 6: コミット**

```bash
git add app/static/browse/scroll.js app/templates/index.html app/static/sw.js tests/test_sw_offline_contract.py
git commit -m "feat(ai): browse/scroll モジュール（一覧アンカーの決定と適用）を追加"
```

---

### Task 3: 描画側の受け渡し（`renderView` → `renderBrowse` → `apply`）

**Files:**
- Modify: `app/static/browse/render.js:486-493`（`renderBrowse`）
- Modify: `app/static/app.js:1541-1551`（`renderView`）

この時点ではまだ誰もアンカーを渡さないので、既存テストが緑のままであることが確認対象。

- [ ] **Step 1: render.js に適用ヘルパと `scrollAnchor` を足す**

`app/static/browse/render.js` の `renderBrowse` の直前に追加:

```js
  // scrollAnchor が渡された描画だけが位置を決める（undefined = 触らない）。
  // innerHTML 置換と同一タスクで書くので、誤った位置が 1 フレーム見えない。
  function _applyScrollAnchor(anchorPath){
    try { if (window.BrowseScroll) window.BrowseScroll.apply(anchorPath); } catch(e){ console.warn(e); }
  }
```

`renderBrowse` を次のように置き換える:

```js
  function renderBrowse(opts){
    var mode=opts&&opts.mode?opts.mode:(window.viewMode||'tile');
    var results=opts&&'results' in opts?opts.results:(window.searchMode?window.searchResults:null);
    var isSearch=Array.isArray(results);
    if(mode==='tile'){ if(isSearch) { if(window.renderSearchTiles) window.renderSearchTiles(); else renderTiles(); } else renderTiles(); }
    else if(mode==='card'){ if(isSearch) { if(window.renderSearchCards) window.renderSearchCards(); else renderCards(); } else renderCards(); }
    else { if(isSearch) { if(window.renderSearchList) window.renderSearchList(); else renderList(); } else renderList(); }
    // 検索結果の描画には適用しない。undefined は「位置を決めない」の合図。
    if(!isSearch && opts && opts.scrollAnchor !== undefined) _applyScrollAnchor(opts.scrollAnchor);
  }
```

- [ ] **Step 2: app.js の renderView に受け渡しを足す**

`app/static/app.js` の `renderView`（1541 行目付近）を置き換える:

```js
  // opts.scrollAnchor: undefined = 位置を決めない（再描画・モード切替・検索）、
  // null = 上端、path = そのタイルを上端に。指定できるのは
  // navigateToFolder / restoreBrowse / resetToTop の 3 経路だけ（設計 §3.1）。
  function renderView(opts){
    tileGrid.classList.remove("list-mode", "card-mode");
    sidebar.classList.remove("tile-full");
    viewer.classList.remove("tile-full");
    sidebar.classList.toggle("tile-full", viewMode !== "list");
    viewer.classList.toggle("tile-full", viewMode !== "list");
    tileGrid.classList.toggle("list-mode", viewMode === "list");
    tileGrid.classList.toggle("card-mode", viewMode === "card");
    window.BrowseRender.renderBrowse({
      mode: viewMode,
      results: searchMode ? searchResults : null,
      scrollAnchor: (opts && 'scrollAnchor' in opts) ? opts.scrollAnchor : undefined,
    });
    updateResumeButton();
  }
```

- [ ] **Step 3: 既存スイートで無回帰を確認**

Run:
```bash
cd /Users/kentokumatsunami/Documents/GitHub/bookq/.worktrees/browse-scroll-position
uv run --with pytest --with playwright --with-requirements requirements-dev.txt \
  pytest tests/test_browse_url_state.py tests/test_sidebar_scroll.py tests/test_ui.py tests/test_traversal.py -q 2>&1 | tail -10
```
Expected: 全パス（アンカーを渡す呼び出し元がまだ無いので従来どおりの挙動）。

- [ ] **Step 4: コミット**

```bash
git add app/static/browse/render.js app/static/app.js
git commit -m "feat(ai): renderView にスクロールアンカーを受け渡す口を追加"
```

---

### Task 4: フォルダを開いたときのアンカー（`navigateToFolder`）

**Files:**
- Modify: `app/static/app.js:1111-1129`（`navigateToFolder`）

- [ ] **Step 1: navigateToFolder を置き換える**

`app/static/app.js` の `navigateToFolder` を置き換える:

```js
  function navigateToFolder(path){
    if (_shallowMode){
      // Shallow window: target children==[] — queue the drill-down in the URL
      // (pushState fires no hashchange; applyUrlState runs once post-full) and
      // show the existing waiting style until the full tree lands.
      _pendingBrowsePath = path;
      try { writeBrowseStateWith(path); } catch(e) { console.warn(e); }
      const tg = document.getElementById("tile-grid");
      if (tg) tg.innerHTML = '<div style="color:#555;text-align:center;padding:20px;font-size:13px">読込中…</div>';
      return;
    }
    const key = getScrollKey(currentBrowsePath);
    scrollPositions[key] = sidebarScroll.scrollTop;
    currentBrowsePath = path;
    updateBrowseHeader();
    // 開いた直後の位置: 直下に既読エピソードがあればその最大番号、無ければ上端。
    // window.BrowseScroll は index.html の defer 順で app.js より先に読み込まれる
    // ハード依存（window.Overview と同じ扱い）。
    const decision = window.BrowseScroll.decide({ level: path, cause: 'open', hasSaved: false });
    renderView({ scrollAnchor: decision.anchor });
    writeBrowseState();
  }
```

- [ ] **Step 2: RED が GREEN になることを確認**

Run:
```bash
cd /Users/kentokumatsunami/Documents/GitHub/bookq/.worktrees/browse-scroll-position
uv run --with pytest --with playwright --with-requirements requirements-dev.txt \
  pytest tests/test_browse_scroll_target.py -q 2>&1 | tail -12
```
Expected: `test_fresh_open_without_progress_starts_at_top` と `test_fresh_open_anchors_last_read_episode` がパス。他の 5 本（ビューア復帰系・ルート）はまだ赤。

- [ ] **Step 3: コミット**

```bash
git add app/static/app.js
git commit -m "feat(ai): フォルダを開いたときの位置を最大既読エピソード／上端に決める"
```

---

### Task 5: ビューア復帰と保存位置の一元化（`restoreBrowse`）

**Files:**
- Modify: `app/static/browse/hash.js:320-357`（`restoreBrowse`）

- [ ] **Step 1: restoreBrowse を置き換える**

`app/static/browse/hash.js` の `restoreBrowse` を**関数全体**次のように置き換える（`_teardownViewer()` より前に章を捕捉するのが要）:

```js
  function restoreBrowse(path) {
    // ビューア復帰の章は teardown 前の live 状態から確定する（URL の p= は 200ms
    // デバウンスで古いので使わない）。☰ 復帰はこの関数が 2 回走る
    // （returnToBrowse と、pushBrowseUrl の history.go(-1) 後の hashchange）ため、
    // BrowseScroll 側にマーカーとして残し、2 回目も同じ位置を返せるようにする。
    var BS = window.BrowseScroll;
    if (BS && path) {
      try {
        var vst = currentViewerState();
        if (vst && vst.folder) BS.noteViewerReturn(path, vst.folder);
      } catch(e) { console.warn(e); }
    }
    // Bump token first so in-flight loadImages cannot overwrite
    try { ++window.urlRestoreGen; window.urlRestoring = false; } catch(e) { console.warn(e); }
    _teardownViewer();
    var node = path ? _getNodeAt(path) : null;
    var dead = !!path && !node; // a path was requested but doesn't resolve — dead/nonexistent folder
    try { window.currentBrowsePath = (path && node) ? path : null; } catch(e) { console.warn(e); }
    if (typeof window.updateBrowseHeader === 'function') try { window.updateBrowseHeader(); } catch(e) { console.warn(e); }
    // Clear search
    try { window.searchMode = false; } catch(e) { console.warn(e); }
    var si = document.getElementById("search-input");
    if (si) si.value = "";
    // 位置は BrowseScroll.decide が一元管理する: 保存位置（従来の rAF 復元）か
    // アンカー（ビューア復帰 / 最大既読 / 上端）のどちらか一方だけを書く。
    var savedTop;
    try {
      var getScrollKey = window.getScrollKey;
      var scrollPositions = window.scrollPositions;
      if (getScrollKey && scrollPositions) {
        var key = getScrollKey(window.currentBrowsePath);
        if (scrollPositions[key] !== undefined) savedTop = scrollPositions[key];
      }
    } catch(e) { console.warn(e); }
    var decision = null;
    if (BS) {
      try {
        decision = BS.decide({
          level: window.currentBrowsePath || null,
          cause: 'restore',
          hasSaved: savedTop !== undefined,
        });
      } catch(e) { console.warn(e); }
    }
    if (typeof window.renderView === 'function') {
      try {
        window.renderView(
          (decision && decision.via !== 'saved') ? { scrollAnchor: decision.anchor } : undefined
        );
      } catch(e) { console.warn(e); }
    }
    if (savedTop !== undefined && (!decision || decision.via === 'saved')) {
      requestAnimationFrame(function () {
        var ss = document.getElementById("sidebar-scroll");
        if (ss) ss.scrollTop = savedTop;
      });
    }
    // Clear the hash back to canonical root when: (a) a dead/nonexistent
    // folder was requested — CLAUDE.md "a dead or empty folder clears the
    // hash" is unconditional, view mode notwithstanding (a bogus
    // "?v=list" must not survive); or (b) we're genuinely at root in tile
    // mode, where the bare URL IS the canonical form. Root in list/card mode
    // keeps its "#/browse/?v=..." hash — that's not dead, it's the real URL.
    if (!window.currentBrowsePath && location.hash.startsWith(BROWSE_HASH_PREFIX) &&
        (dead || window.viewMode === "tile")) {
      history.replaceState(null, "", location.pathname + location.search);
      urlStackReplace(location.pathname + location.search);
    }
  }
```

- [ ] **Step 2: ビューア復帰系の RED が GREEN になることを確認**

Run:
```bash
cd /Users/kentokumatsunami/Documents/GitHub/bookq/.worktrees/browse-scroll-position
uv run --with pytest --with playwright --with-requirements requirements-dev.txt \
  pytest tests/test_browse_scroll_target.py -q 2>&1 | tail -12
```
Expected: `test_viewer_return_anchors_displayed_episode_beats_progress` / `test_viewer_return_is_idempotent_across_double_restore` / `test_browser_back_from_viewer_anchors_displayed_episode` / `test_viewer_return_beats_saved_scroll` がパス。残る赤は `test_root_fresh_open_is_always_top`（Task 6）のみ。

- [ ] **Step 3: 既存の復元・履歴テストの無回帰を確認**

Run:
```bash
cd /Users/kentokumatsunami/Documents/GitHub/bookq/.worktrees/browse-scroll-position
uv run --with pytest --with playwright --with-requirements requirements-dev.txt \
  pytest tests/test_sidebar_scroll.py tests/test_browse_url_state.py tests/test_url_state.py tests/test_url_layout_params.py tests/test_offline_full_browse.py -q 2>&1 | tail -10
```
Expected: 全パス。

- [ ] **Step 4: コミット**

```bash
git add app/static/browse/hash.js
git commit -m "feat(ai): ビューア復帰で表示していたエピソードに合わせる（保存位置とは一元化）"
```

---

### Task 6: ルート復帰とビューア開放でのマーカー破棄

**Files:**
- Modify: `app/static/browse/hash.js:310-318`（`resetToTop`）
- Modify: `app/static/app.js:1661-1671`（`loadImages` の先頭）

- [ ] **Step 1: resetToTop を置き換える**

`app/static/browse/hash.js` の `resetToTop` を置き換える:

```js
  function resetToTop() {
    // ルート（作品一覧）はアンカーを使わない: 常に上端。ビューア復帰の文脈も切る。
    try { if (window.BrowseScroll) window.BrowseScroll.clearViewerReturn(); } catch(e) { console.warn(e); }
    _teardownViewer();
    var empty = document.getElementById("empty");
    if (empty) { empty.innerHTML = "<p>\uD83D\uDCC2 サイドバーからフォルダを選んでください</p>"; empty.style.display = "flex"; }
    try { window.currentBrowsePath = null; } catch(e) { console.warn(e); }
    if (typeof window.updateBrowseHeader === 'function') try { window.updateBrowseHeader(); } catch(e) { console.warn(e); }
    if (typeof window.renderView === 'function') try { window.renderView({ scrollAnchor: null }); } catch(e) { console.warn(e); }
    clearViewerState();
  }
```

- [ ] **Step 2: loadImages の先頭でマーカーを破棄する**

`app/static/app.js` の `loadImages` の shallow 早期 return の直後（`currentFolder = fullPath;` の前）に追加:

```js
    // 別の本を開いた＝ビューア復帰の文脈は切れた（一覧のアンカーには使わない）
    try { if (window.BrowseScroll) window.BrowseScroll.clearViewerReturn(); } catch(e) { console.warn(e); }
```

- [ ] **Step 3: 全 10 本が GREEN になることを確認**

Run:
```bash
cd /Users/kentokumatsunami/Documents/GitHub/bookq/.worktrees/browse-scroll-position
uv run --with pytest --with playwright --with-requirements requirements-dev.txt \
  pytest tests/test_browse_scroll_target.py -q 2>&1 | tail -8
```
Expected: `11 passed`（10 関数 + list/card の parametrize 2 本）。

- [ ] **Step 4: コミット**

```bash
git add app/static/browse/hash.js app/static/app.js
git commit -m "feat(ai): ルート復帰・ビューア開放でアンカー文脈を破棄する"
```

---

### Task 7: WebKit とフルスイートでの横断確認

**Files:** なし（検証のみ。落ちた場合のみ最小修正）

- [ ] **Step 1: WebKit（iOS エミュレーション）で新テストを走らせる**

Run:
```bash
cd /Users/kentokumatsunami/Documents/GitHub/bookq/.worktrees/browse-scroll-position
BROWSER=webkit uv run --with pytest --with playwright --with-requirements requirements-dev.txt \
  pytest tests/test_browse_scroll_target.py -q 2>&1 | tail -10
```
Expected: 全パス。`Executable doesn't exist` が出たら `uv run --with playwright playwright install webkit` を実行してから再実行（このステップは環境が無ければスキップ可。Chromium の緑が必須条件）。

- [ ] **Step 2: フルスイート**

Run:
```bash
cd /Users/kentokumatsunami/Documents/GitHub/bookq/.worktrees/browse-scroll-position
uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/ -q 2>&1 | tail -15
```
Expected: 全パス（既知の flaky を除く）。失敗したテストは「本変更が scrollTop を書く経路（フォルダを開く / ビューア復帰 / 保存位置なし restore / ルート復帰）」に依存していないか確認する。もし `#sidebar-scroll` の位置を前提にしているテストが落ちたら、そのテストの前提を（アンカーが働く場面かどうかを切り分けて）修正する。

- [ ] **Step 3: コミットは無し**（差分が出た場合のみ、その修正に応じたメッセージで commit）

---

### Task 8: ドキュメント更新

**Files:**
- Modify: `docs/core-features.md`（サイドバーのスクロールの項）
- Modify: `CLAUDE.md`（モジュール一覧・一覧スクロールの契約）
- Modify: `AGENTS.md`（モジュール一覧。gesture/zoom/overview が抜けて古いので同時に直す）

- [ ] **Step 1: core-features.md に決定ルールを追記**

`docs/core-features.md` の「階層を戻ったときのために、パスごとにスクロール位置を保存・復元する」の直後に追加:

```md
- フォルダを開いたときの一覧の位置は次の順で決まる: ①ビューアから戻ったときはビューアで表示していたページのあるエピソードを上端に、②その階層の保存位置がある復帰（`↩`/Back）は保存位置を復元、③それ以外の新規オープンは直下の読了済みでない既読エピソードのうち一覧順で最後（＝最大番号）を上端に、④該当なしは上端。ルート（作品一覧）は①③を使わない（「続きから」ボタンの役割）。判定は `app/static/browse/scroll.js`（`window.BrowseScroll.decide`）が一元管理し、描画側は `renderBrowse({scrollAnchor})` で `#tile-grid` の `innerHTML` 置換と同一タスクで適用する
```

- [ ] **Step 2: CLAUDE.md のモジュール一覧と契約を更新**

`CLAUDE.md` の「plus eleven global-script modules」を **twelve** に変え、リストの `browse/render` の直後に `browse/scroll` を挿入する。あわせて URL 状態の項（`## URL state lives in the hash` の段落の近く）に次を追記:

```md
- **Browse-list scroll position is decided, not carried over.** `app/static/browse/scroll.js` (`window.BrowseScroll`) owns the decision: viewer-return (the chapter the reader was on, captured before `_teardownViewer` and kept as a level-scoped one-shot marker, because `restoreBrowse` runs twice per ☰ return), else a saved position, else the last read direct child in list order, else the top. `renderBrowse({scrollAnchor})` applies it in the same task as the `innerHTML` swap; `undefined` means "do not touch" (re-renders must never move the list). `#sidebar-scroll` is the real scroller (`#tile-grid` never scrolls).
```

- [ ] **Step 3: AGENTS.md のモジュール一覧を現在の構成に直す**

`AGENTS.md` の「`app/static/app.js` 3279L composition root + 7 modules (...)」を、実際の構成（`app.js` + 12 modules: `offline-core.js`, `thumbs/resolveCover.js`, `browse/hash.js`, `browse/render.js`, `browse/scroll.js`, `search/facets.js`, `offline/storeAdapter.js`, `thumbs/prefetch.js`, `viewer/spread.js`, `viewer/gesture.js`, `viewer/zoom.js`, `viewer/overview.js`）に更新する（行数表記は実測値に合わせる必要はないが、モジュール名は網羅する）。

- [ ] **Step 4: 整合確認**

Run:
```bash
cd /Users/kentokumatsunami/Documents/GitHub/bookq/.worktrees/browse-scroll-position
git diff --stat
grep -n "browse/scroll" CLAUDE.md AGENTS.md docs/core-features.md app/templates/index.html app/static/sw.js
```
Expected: 5 ファイルすべてに `browse/scroll` の言及がある。

- [ ] **Step 5: コミット**

```bash
git add docs/core-features.md CLAUDE.md AGENTS.md
git commit -m "docs(ai): 一覧スクロールアンカーの仕様とモジュール一覧を更新"
```

---

### Task 9: dev 環境での目視確認と最終検証

**Files:** なし（検証のみ）

- [ ] **Step 1: dev 環境を開いて 3 つの挙動を目視する**

Run（Task 0 で起動済みのはず。落ちていれば再実行）:
```bash
cd /Users/kentokumatsunami/Documents/GitHub/bookq/.worktrees/browse-scroll-position
./dev.sh url browse-scroll-position
```
Expected: `http://127.0.0.1:8090`。

ハーネスの browser ツール（`tools.browser.tabs.open`）でその URL を開き、ビューポートを **390x430 相当**（`tools["chrome-devtools"].resize_page` があればそれ、無ければ端末幅を狭める）にして次を確認する:

1. エピソードを複数持つ作品のフォルダを開く → 一覧が**上端**から始まる（親で下までスクロールしていても）
2. エピソードを1つ開いて数ページ読み、`☰`（またはフォルダ名タップ）で戻る → **そのエピソードが上端**に来る
3. そのエピソードより後ろの番号のエピソードも既読にしてから、別のエピソードを開いて戻る → **表示していた方**に合う（最大既読に引っ張られない）
4. `↩`（パンくず）で親に戻る → 従来どおり**保存位置**が復元される

注意: 目視に live のフォルダ名を使った場合、その名前をコード・コミットメッセージ・レポートに書かない（AGENTS.md §4）。合成ツリーで確認したい場合は `./dev.sh down browse-scroll-position && ./dev.sh up browse-scroll-position --local --port 8090 --fixture` を使う。

- [ ] **Step 2: スクリーンショットを残す（任意）**

撮った場合は `.omo/evidence/` 配下に置く（コミットしない）。

- [ ] **Step 3: 最終フルスイート**

Run:
```bash
cd /Users/kentokumatsunami/Documents/GitHub/bookq/.worktrees/browse-scroll-position
uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/ -q 2>&1 | tail -10
```
Expected: 全パス。

- [ ] **Step 4: 差分の自己点検**

Run:
```bash
cd /Users/kentokumatsunami/Documents/GitHub/bookq/.worktrees/browse-scroll-position
git log --oneline origin/main..HEAD
git diff origin/main --stat
```
Expected: コミットが 7〜8 個（設計・RED テスト・モジュール・描画口・開く・復帰・ルート・docs）、差分は `app/static/browse/scroll.js`（新規）と `render.js` / `hash.js` / `app.js` の小さい追加、`index.html` / `sw.js` / 契約テスト / 新テスト / docs。

- [ ] **Step 5: 報告**

`finishing-a-development-branch` スキルに進む前に、次をユーザーに報告する:
- 実施したテスト（RED→GREEN の本数、フルスイート、WebKit の有無）
- live 実データ回帰（`BOOKQ_LIVE_ROOT=... ./scripts/live-regress.sh`）は**未実施**である旨（本変更はフロントのみでサーバ応答を変えないため。実施するなら dev 環境ではなく手元の live ルートで）
- スコープ外として残した既知事項（`tests/test_sidebar_scroll.py` のデスクトップ分岐が空振り / URL `p=` の 200ms デバウンス / `ReadingProgress` の flush 不足）
- dev 環境スロット `browse-scroll-position` はマージ時に `./dev.sh down browse-scroll-position` で解放する

---

## Self-Review

**1. Spec coverage（`docs/superpowers/specs/2026-10-15-browse-scroll-position-design.md`）**

| spec の節 | 対応するタスク |
| --- | --- |
| §1 決定ルール（ビューア復帰 > 保存位置 > 最大既読 > 上端、上端合わせ、直下の子のみ、ルート除外） | Task 2（`decide` の実装）＋ Task 4/5/6（呼び出し元）＋ Task 1（契約テスト 1,2,3,5,7,8,9） |
| §1 適用範囲（新しく開く＋ビューア復帰。`↩`/Back の保存位置は従来どおり） | Task 4（`navigateToFolder`）／Task 5（`via:'saved'` 分岐・↩ は無変更）／Task 1 の `test_breadcrumb_back_keeps_saved_scroll` |
| §3 `window.BrowseScroll` の API（`decide`/`noteViewerReturn`/`peekViewerReturn`/`scroller`/`apply`） | Task 2（`clearViewerReturn` も破棄条件の実装に必要なので追加エクスポート。Task 6 で使用） |
| §3.1 呼び出し元（`navigateToFolder` / `restoreBrowse` / `resetToTop` / `loadImages` / `renderBrowse` / `renderView`） | Task 3（描画口）・Task 4・Task 5・Task 6 |
| §3.2 マーカー（level 一致・one-shot・冪等・破棄条件） | Task 2（実装）＋ Task 1 の `test_viewer_return_is_idempotent_across_double_restore` / `test_decide_exposes_decision_table`（寿命） |
| §3.3 適用（実行時解決・`dataset.path` 比較・同期・`null` は計測なし） | Task 2 の `scroller()` / `_tileFor()` / `apply()` |
| §4 エッジケース（絞り込み・空・再描画は触らない・検索・resume・shallow・ルート loose images） | Task 2（`_visibleChildPaths` による存在判定・`undefined` の合図）／Task 3（`isSearch` 除外・`undefined` は触らない） |
| §5 テスト計画（小ビューポート・前提 assert・10 本） | Task 1 |
| §6 変更ファイル一覧 | File Structure 表 ＋ Task 2〜6・8 |
| §7 スコープ外 | Task 9 Step 5 で報告（コードは触らない） |
| §8 リスクと対策 | 二重描画=Task 2/5、保存位置との二重書き=Task 5 の `via` 分岐、章の取り違え=Task 5 の live 取得、空振り=Task 1 の前提 assert、再描画=Task 3 の `undefined`、計測コスト=Task 2 の `apply`（null は計測しない） |
| §9 未解決 | Task 7 Step 1（WebKit）と Task 1 の前提 assert（`scroller` の同定） |

**2. Placeholder scan:** `TBD` / `TODO` / 「同様に」「適切に」の類は無し。Task 7 の WebKit は環境依存のためスキップ条件を明記（Chromium の緑が必須条件）。Task 9 の目視は「何を見るか」を 1〜4 で列挙済み。

**3. Type consistency:** `decide({level, cause, hasSaved}) -> {via, anchor}`（`via` は `'viewer'|'saved'|'progress'|'top'`）を Task 2 で定義し、Task 4（`cause:'open'`）・Task 5（`cause:'restore'`、`via==='saved'` の分岐）・Task 6（`clearViewerReturn`）・Task 1（全 `via` の期待値）で同じ名前を使用。`renderView({scrollAnchor})` / `renderBrowse({scrollAnchor})` の三状態（`undefined`=触らない / `null`=上端 / path=そのタイル）は Task 3 で定義し Task 4/5/6/1 で一致。マーカーのフィールド名は `level` / `anchor` / `via`（intent 統一後。`noteViewerReturn(level, chapterPath)` は `{level, anchor: chapterPath, via: 'viewer'}` を記録し、`peekViewerReturn()` が返すオブジェクトのキーも同じ）。

---

## 実装完了時の状態（計画からの差分）

- テストは 16 ケース（`tests/test_browse_scroll_target.py`）。追加分: 表示子に無い章のフォールバック、保存位置の Back 経路、`cause:'open'` の pin、stale 保存位置の上書き防止、intent の寿命、アンカー未指定の再描画が位置を触らない契約。
- ビューポートは 390x400（390x430 では最終行タイルのアンカーが max に clamp され検証にならなかったため。到達可能性の上限は実測で約 423）。
- ビューア復帰マーカーは「level に紐づく intent」に統一した（新規オープンの決定も同じ仕組みで保持し、直後の `history.go(-1)` 経由の `restoreBrowse` が古い保存位置で上書きするのを防ぐ）。`peekViewerReturn()` は `via==='viewer'` のときだけ返す。
- 検証: Chromium 16 passed / WebKit 16 passed / フルスイート 732 passed, 6 skipped。live 実データ回帰は未実施。
- スコープ外として残した既知事項: `tests/test_sidebar_scroll.py` のデスクトップ分岐が空振り（`#tile-grid.scrollTop` は効かない）、URL の `p=` が 200ms デバウンスで古い、`ReadingProgress` が `teardownViewer` で flush されない。
