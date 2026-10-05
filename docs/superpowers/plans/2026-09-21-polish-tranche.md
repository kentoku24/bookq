# 磨き込みトランシェ（PR 4〜7）実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 2026-09-21 の監査で「入れる」と決めた UIUX 所見を、PR 4（URL 設定パラメータ）→ PR 5（ビューア）→ PR 6（本棚）→ PR 7（日本語統一）の順に、PR 単位で revert できる形で実装する。

**Architecture:** 既存の hash ルータ（`app/static/browse/hash.js`）にクエリパラメータ `s` / `o` / `v` を足し、レイアウト設定を localStorage ではなく URL に持たせる（ユーザー決定 Q11/Q17。CLAUDE.md の方針文を更新する）。ビューア・本棚の磨き込みは CSS とレンダラ（`render.js`）の局所変更で、既存の Playwright テストの座標・文言アサーションを同じ PR で更新する。日本語統一は文字列置換のみで i18n 機構は入れない。

**Tech Stack:** 素の JS / CSS（ビルド無し）、Jinja テンプレート 1 枚、Playwright (Python) テスト。性能予算テスト `tests/test_perf_budget.py`（PR 3）が各 PR のデグレ検知として走る。

**Spec:** `output/2026-09-21-uiux-audit/decisions.md`（決定事項、ローカル git 管理外）、`output/2026-09-21-uiux-audit/report.md`（監査所見 ID: V2/V3/V4/V5/B2/B3/B5/O2/X1）。GitHub Issue #202〜#205（本計画の PR 単位の Issue）。

## Global Constraints

- **URL 設定の仕様（決定 Q11/Q17）**: ビューア `#/view/<folder>?p=N[&s=1|2][&o=1]`、本棚 `#/browse/<path>[?v=list|card]`。既定値（`s=2` 横持ち時 2P、`o=0`、`v=tile`）はパラメータを**書かない**。設定変更は `history.replaceState`。`s` も `o` も**本をまたいで引き継がない**（タイルから本を開くたび既定値、URL にあれば URL 優先）。localStorage には保存しない。
- 既存の URL テストは末尾一致（`\?p=0$` / `#/browse/x$`）で検証している。既定値では従来と同一の URL になるので、これらのテストは**そのまま通る**ことが要件（壊れたら実装が既定値を書いている）。
- タップゾーン比率は 40% / 20% / 40%（決定 Q21）。`tests/test_ios.py:112-116` の 0.45 アサーションを 0.40 に更新する。
- ツールバーはモバイル縦（幅 390）で高さ **64px 以下**（`min-height` 56px + safe-area）。スライダーの**当たり判定**は 44px、見た目のトラックは 8px のまま。
- タッチ目標 44px の対象に `#filter-btn` / `#offline-status` / `#harness-link` を追加し、`tests/test_ui_visual.py::test_touch_targets_min_44px` のセレクタ配列に加える。
- ストレージ表示は本棚上部の `#storage-bar` を廃止し、フッタに `#storage-text` を移してテキストのみ **`💾 <使用量> / 空き <quota−usage>`**（quota 不明時は `💾 <使用量>`）。`#storage-text` の id と `fmtBytes` 出力（`GB`/`MB` の単位文字）は既存テストが部分一致で見るので維持する。
- タイル名は 2 行: `.tile-name-text`（末尾セグメント、11px）+ `.tile-name-parent`（親セグメント、9px、薄色）。`/` を含まない名前は親行を出さない。
- 「続きから」ピル文言は `📖 続きから: <本名> p.N`、本名は中央省略（先頭 12 文字 + `…` + 末尾 8 文字、24 文字超のとき）。ピルが見えているときは `#tile-grid` の下に 72px の余白。
- UI 文言は日本語に統一。**`#folder-count` は `"N 件"`** に変え、`tests/conftest.py::folder_count_text` と literal を持つテストを同 PR で更新する。`#empty` の 2 文言も同 PR でテスト更新。
- ビルドステップ・フレームワーク・i18n 機構は導入しない。SW の `SHELL_ASSETS` とスクリプト読み込み順は触らない。
- 各 PR は main から切り、1 PR = 1 revert 単位。コミットは署名（`git commit -S`）、末尾に `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`。
- テスト実行: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest <path> -q`。PR ごとに全スイート（`tests/ -q -x -n 4`）を通す。見た目・タッチに関わる PR 5/6 は `./dev.sh up <name> --fixture --local --port 80xx` で 390x844 と 1920x1200 のスクリーンショット before/after を PR 本文に貼り、ユーザーの実機確認を依頼する（低頻度、この 2 PR のみ）。
- 行番号は目安。関数名・セレクタで再特定する。

---

## PR 4: レイアウト設定を URL に載せる（V2 の代替、Issue #202）

ブランチ: `ux/url-layout-params`（main から）。

### Task 1: ビューア `s` / `o` パラメータの読み書き

**Files:**
- Modify: `app/static/browse/hash.js` — `parseViewerHash`, `viewerHash`, `currentViewerState`, `pushViewerState`, `replaceViewerState`
- Modify: `app/static/app.js` — `spreadShift` / `spreadToggle` の click ハンドラ（`spreadShift.addEventListener("click"` で検索）
- Test: `tests/test_url_layout_params.py`（新規）

**Interfaces:**
- Produces: `parseViewerHash()` → `{folder, page, s: 1|2|null, o: 0|1|null}`（無ければ `null`）。`viewerHash(folder, page, layout)` で `layout = {landscapeSpread: bool, spreadOffset: 0|1}`；`landscapeSpread === false` のとき `&s=1`、`spreadOffset === 1` のとき `&o=1` を付ける。`currentViewerState()` は `{folder, page, landscapeSpread, spreadOffset}` を返す。
- Consumes: `window.landscapeSpread` / `window.spreadOffset`（app.js が `Object.defineProperty` で公開済み）。

- [ ] **Step 1: 失敗するテストを書く**

```python
# tests/test_url_layout_params.py
"""レイアウト設定（1P/2P, 見開きずらし, 表示モード）は URL に載る（決定 Q11/Q17）。
既定値はパラメータを書かない。本をまたいで引き継がない。localStorage には保存しない。"""
import re

from playwright.sync_api import expect


def _open_long(page, server):
    page.goto(server + "/#/view/long?p=0")
    page.wait_for_selector("#img-track img")


def test_default_layout_writes_no_params(landscape_page, server):
    _open_long(landscape_page, server)
    landscape_page.keyboard.press("ArrowLeft")
    expect(landscape_page).to_have_url(re.compile(r"#/view/long\?p=1$"))


def test_spread_toggle_and_shift_write_s_and_o(landscape_page, server):
    _open_long(landscape_page, server)
    landscape_page.locator("#spread-shift").click()          # o=1
    expect(landscape_page).to_have_url(re.compile(r"#/view/long\?p=0&o=1$"))
    landscape_page.locator("#spread-toggle").click()         # 2P → 1P: s=1（o は 1P では無意味なので落ちる）
    expect(landscape_page).to_have_url(re.compile(r"#/view/long\?p=0&s=1$"))
    landscape_page.locator("#spread-toggle").click()         # 1P → 2P: 既定に戻る
    expect(landscape_page).to_have_url(re.compile(r"#/view/long\?p=0$"))


def test_layout_params_restore_from_url(landscape_page, server):
    landscape_page.goto(server + "/#/view/long?p=2&s=1")
    landscape_page.wait_for_selector("#img-track img")
    assert landscape_page.evaluate("window.pageMode") == "single"
    assert landscape_page.evaluate("window.landscapeSpread") is False
    landscape_page.goto(server + "/#/view/long?p=2&o=1")
    landscape_page.wait_for_selector("#img-track img")
    assert landscape_page.evaluate("window.spreadOffset") == 1


def test_layout_change_uses_replace_state_not_push(landscape_page, server):
    _open_long(landscape_page, server)
    before = landscape_page.evaluate("history.length")
    landscape_page.locator("#spread-shift").click()
    expect(landscape_page).to_have_url(re.compile(r"&o=1$"))
    assert landscape_page.evaluate("history.length") == before


def test_layout_not_carried_to_next_book(landscape_page, server):
    landscape_page.goto(server + "/#/view/long?p=0&o=1")
    landscape_page.wait_for_selector("#img-track img")
    landscape_page.keyboard.press("f")                       # サイドバーを開く
    landscape_page.locator('#tile-grid .tile[data-path="simple_manga"]').click()
    landscape_page.wait_for_url(re.compile(r"#/view/simple_manga\?p=0$"))
    assert landscape_page.evaluate("window.spreadOffset") == 0


def test_no_localstorage_for_layout(landscape_page, server):
    _open_long(landscape_page, server)
    landscape_page.locator("#spread-shift").click()
    keys = landscape_page.evaluate("Object.keys(localStorage)")
    assert all(k in ("bookq_progress", "bookq_persist_toast") for k in keys), keys
```

- [ ] **Step 2: 失敗を確認する**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_url_layout_params.py -q`
Expected: `test_spread_toggle_and_shift_write_s_and_o` / `test_layout_params_restore_from_url` / `test_layout_not_carried_to_next_book` が FAIL（URL に `o=`/`s=` が出ない、復元されない）。`test_default_layout_writes_no_params` と `test_no_localstorage_for_layout` は現状でも PASS。

- [ ] **Step 3: hash.js を実装する**

```js
  // hash.js — parseViewerHash: s / o を読む（無ければ null）
  function parseViewerHash() {
    var raw = location.hash;
    if (!raw.startsWith(VIEWER_HASH_PREFIX)) return null;
    var rest = raw.slice(VIEWER_HASH_PREFIX.length);
    var q = rest.indexOf("?");
    var rawPath = q === -1 ? rest : rest.slice(0, q);
    var folder;
    try { folder = decodeURIComponent(rawPath); } catch { return null; }
    var query = q === -1 ? "" : rest.slice(q + 1);
    var m = query.match(/(?:^|&)p=(\d+)/);
    var ms = query.match(/(?:^|&)s=([12])/);
    var mo = query.match(/(?:^|&)o=([01])/);
    return {
      folder,
      page: m ? parseInt(m[1], 10) : 0,
      s: ms ? parseInt(ms[1], 10) : null,
      o: mo ? parseInt(mo[1], 10) : null,
    };
  }

  // 既定値（横持ち 2P、ずらし無し）は書かない。1P では o は無意味なので落とす。
  function viewerHash(folder, page, layout) {
    var enc = folder.split("/").map(encodeURIComponent).join("/");
    var h = VIEWER_HASH_PREFIX + enc + "?p=" + page;
    if (layout) {
      if (layout.landscapeSpread === false) h += "&s=1";
      else if (layout.spreadOffset === 1) h += "&o=1";
    }
    return h;
  }

  function currentViewerState() {
    var currentFolder = _getCurrentFolder();
    var images = _getImages();
    var chapterList = _getChapterList();
    if (!currentFolder || !images.length || !chapterList.length) return null;
    var currentIdx = (typeof window.currentIdx !== 'undefined') ? window.currentIdx : 0;
    var info = _getCurrentChapterInfo(currentIdx);
    var chapter = chapterList[info.chapterIdx];
    if (!chapter) return null;
    return {
      folder: chapter.fullPath, page: info.localIdx,
      landscapeSpread: window.landscapeSpread !== false,
      spreadOffset: window.spreadOffset === 1 ? 1 : 0,
    };
  }
```

`pushViewerState` / `replaceViewerState` の `viewerHash(st.folder, st.page)` を `viewerHash(st.folder, st.page, st)` に変える（2 箇所）。`applyUrlState` の比較 `st.page !== cur.page` はそのまま（s/o の違いだけでは再ロードしない。Task 2 で扱う）。

- [ ] **Step 4: app.js のハンドラで URL を書き、URL から復元する**

`spreadShift` と `spreadToggle` の click ハンドラ末尾（`updateShiftButtonStyle();` の後）に `scheduleUrlSync();` を追加する（既存関数。ページ送りと同じ 200ms デバウンスの replaceState）。

`loadImages` の**入口**で、タイルから開いたときは既定値に戻し、URL 復元のときは URL の値を使う。`loadImages(folder)` 先頭の `myGen` 取得の直後に:

```js
    // レイアウト設定は本をまたいで引き継がない（決定 Q17）。URL 復元時だけ URL の値を採る。
    {
      const st = (window.HashRouter && window.HashRouter.parseViewerHash) ? window.HashRouter.parseViewerHash() : null;
      const fromUrl = urlRestoring && st && st.folder === folder;
      landscapeSpread = fromUrl && st.s === 1 ? false : true;
      spreadOffset = fromUrl && st.o === 1 ? 1 : 0;
    }
```

（`HashRouter` の公開名は hash.js 末尾の `// Expose` ブロックで確認し、`parseViewerHash` が公開されていなければ追加する。）その直後に `updateOrientation()` を呼んで `pageMode` / `body.className` を再計算する（既存関数。`images` が空のうちは早期 return するので安全）。

- [ ] **Step 5: テスト実行**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_url_layout_params.py tests/test_url_state.py tests/test_browse_url_state.py tests/test_ui.py -q`
Expected: 全 PASS。`test_url_state.py` の `\?p=N$` アサーションは既定値のまま通る。

- [ ] **Step 6: コミット**

```bash
git add app/static/browse/hash.js app/static/app.js tests/test_url_layout_params.py
git commit -S -m "feat(url): 1P/2P（s）と見開きずらし（o）を URL に載せる

既定値は書かない・replaceState・本をまたいで引き継がない（決定 Q11/Q17）。

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 2: 本棚 `v` パラメータ

**Files:**
- Modify: `app/static/browse/hash.js` — `parseBrowseHash`, `browseHash`, `writeBrowseState`（`browseHash(` の呼び出し元を全部）, `restoreBrowse`
- Modify: `app/static/app.js` — `footerBtn.addEventListener("click"` ハンドラ
- Test: `tests/test_url_layout_params.py`（追記）

**Interfaces:**
- Produces: `parseBrowseHash()` → `{path, v: "tile"|"list"|"card"|null}`；`browseHash(path, viewMode)` は `viewMode` が `list`/`card` のとき `?v=` を付け、ルート（path 空）でも `#/browse/?v=list` を返す。tile かつルートなら従来どおり `""`。
- Consumes: `window.viewMode`（公開済み）、`renderView()`。

- [ ] **Step 1: 失敗するテストを追記**

```python
def test_view_mode_in_browse_url(page, server):
    page.goto(server + "/")
    page.wait_for_selector("#tile-grid .tile")
    page.locator("#footer-refresh-btn").click()   # tile → list
    expect(page).to_have_url(re.compile(r"#/browse/\?v=list$"))
    page.locator('#tile-grid [data-path="series"]').click()
    expect(page).to_have_url(re.compile(r"#/browse/series\?v=list$"))
    page.locator("#footer-refresh-btn").click()   # list → card
    expect(page).to_have_url(re.compile(r"#/browse/series\?v=card$"))
    page.locator("#footer-refresh-btn").click()   # card → tile（既定: パラメータ無し）
    expect(page).to_have_url(re.compile(r"#/browse/series$"))


def test_view_mode_restores_from_url(page, server):
    page.goto(server + "/#/browse/series?v=list")
    page.wait_for_selector("#tile-grid .item, #tile-grid .tile")
    assert page.evaluate("window.viewMode") == "list"
    expect(page.locator("#footer-refresh-btn")).to_have_text("📋")
```

- [ ] **Step 2: 失敗を確認する**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_url_layout_params.py -q -k view_mode`
Expected: 2 件 FAIL。

- [ ] **Step 3: 実装**

```js
  function parseBrowseHash() {
    var raw = location.hash;
    if (!raw.startsWith(BROWSE_HASH_PREFIX)) return null;
    var rest = raw.slice(BROWSE_HASH_PREFIX.length);
    var q = rest.indexOf("?");
    var rawPath = q === -1 ? rest : rest.slice(0, q);
    var path;
    try { path = decodeURIComponent(rawPath); } catch { return { path: "", v: null }; }
    var query = q === -1 ? "" : rest.slice(q + 1);
    var mv = query.match(/(?:^|&)v=(list|card)/);
    return { path, v: mv ? mv[1] : null };
  }

  // 既定 tile は書かない。ルートで tile なら "" （従来どおりハッシュ無し）。
  function browseHash(path, viewMode) {
    var p = (path === null || path === undefined) ? "" : path;
    var suffix = (viewMode === "list" || viewMode === "card") ? "?v=" + viewMode : "";
    if (p === "" && suffix === "") return "";
    return BROWSE_HASH_PREFIX + p.split("/").map(encodeURIComponent).join("/") + suffix;
  }
```

`browseHash(` の全呼び出し元に `window.viewMode` を渡す（`grep -n "browseHash(" app/static/browse/hash.js app/static/app.js`）。`restoreBrowse(path)` の呼び出し元 `applyUrlState` で `bs.v` があれば `window.viewMode = bs.v; if (window.updateFooterBtn) window.updateFooterBtn();` を `restoreBrowse(bs.path)` の前に入れる（`updateFooterBtn` は app.js 内関数なので `window.updateFooterBtn = updateFooterBtn;` を公開する）。`footerBtn` の click ハンドラ末尾（`renderView();` の後）に `if (window.HashRouter && window.HashRouter.writeBrowseState) window.HashRouter.writeBrowseState();` を追加（`writeBrowseState` が replaceState であることを確認。pushState なら `replaceBrowseState` 相当を使う。Back で表示モードを 1 つずつ戻らないのが要件）。

`applyUrlState` のルート判定 `if (!window.currentBrowsePath && location.hash.startsWith(BROWSE_HASH_PREFIX))` は「ルートかつ tile」のときだけハッシュを消すように `&& window.viewMode === "tile"` を足す。

- [ ] **Step 4: テスト実行**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_url_layout_params.py tests/test_browse_url_state.py tests/test_search_e2e.py tests/test_sidebar_scroll.py -q`
Expected: 全 PASS（`test_search_e2e.py` の viewMode 循環テストは URL を見ていない）。

- [ ] **Step 5: コミット**

```bash
git add app/static/browse/hash.js app/static/app.js tests/test_url_layout_params.py
git commit -S -m "feat(url): 本棚の表示モード（v=list|card）を URL に載せる

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 3: CLAUDE.md の方針更新と PR

**Files:**
- Modify: `CLAUDE.md` — 「**Layout prefs are deliberately NOT in the URL.**」の段落

- [ ] **Step 1: 段落を置き換える**

```markdown
- **Layout prefs live in the URL, not in localStorage** (decided 2026-09-21): viewer `#/view/<folder>?p=N[&s=1][&o=1]` — `s=1` = single-page in landscape (default 2P is not written), `o=1` = cover-solo pairing shift (dropped when `s=1`); browse `#/browse/<path>[?v=list|card]` (`tile` is not written; root browse with a non-default view is `#/browse/?v=list`). Layout changes use `replaceState` (Back never steps through layout changes). `s`/`o` are **not carried to the next book** — `loadImages` resets them unless it is a URL restore of that same folder. Search text and scroll positions stay runtime-only. Existing `?p=N$` test regexes remain valid because defaults are omitted.
```

- [ ] **Step 2: 全スイート、コミット、push、PR**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/ -q -x -n 4` → 全 PASS。

```bash
git add CLAUDE.md
git commit -S -m "docs: レイアウト設定を URL に載せる方針に CLAUDE.md を更新

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
git push -u origin ux/url-layout-params
gh pr create --repo kentoku24/bookq --base main --head ux/url-layout-params --title "feat(url): レイアウト設定（1P/2P・見開きずらし・表示モード）を URL に載せる" --body "Closes #202 ..."
```

---

## PR 5: ビューアの磨き込み（V3 / V4 / V5、Issue #203）

ブランチ: `ux/viewer-polish`（main から）。

### Task 4: 表紙ソロ時の空白ページを背景色にする（V3）

**Files:**
- Modify: `app/static/style.css`（`.page-cell` 付近）、必要なら `app/static/viewer/spread.js` / `app/static/app.js` のセル生成（`placeCell` / `regroupCells`、`isSpacer` で `""` を返す箇所）
- Test: `tests/test_viewer_polish.py`（新規）

**Interfaces:**
- Consumes: フィクスチャ `long/`（30 ページ）を横持ち（`landscape_page` 900x600）で開き、`#spread-shift` を押すと先頭ページがソロになり、隣のセルが空になる。

- [ ] **Step 1: 現象を固定するテストを書く（ピクセル判定）**

```python
# tests/test_viewer_polish.py
"""ビューア磨き込み（監査 V3/V4/V5）。"""
import io
import re

from PIL import Image
from playwright.sync_api import expect

DARK_MAX = 40  # 背景 #0d0d0d 近傍とみなす RGB 上限


def _px(page, x, y):
    img = Image.open(io.BytesIO(page.screenshot())).convert("RGB")
    return img.getpixel((int(x), int(y)))


def test_solo_cover_partner_cell_is_dark_not_white(landscape_page, server):
    landscape_page.goto(server + "/#/view/long?p=0")
    landscape_page.wait_for_selector("#img-track img")
    landscape_page.locator("#spread-shift").click()   # 表紙ソロ → 左隣は空セル
    landscape_page.wait_for_timeout(500)
    empty = landscape_page.evaluate(
        "() => { const c=[...document.querySelectorAll('.page-cell')].find(c=>!c.querySelector('img') && c.getBoundingClientRect().width>0 && c.getBoundingClientRect().left>=0 && c.getBoundingClientRect().right<=innerWidth); if(!c) return null; const r=c.getBoundingClientRect(); return {x:r.left+r.width/2, y:r.top+r.height/2, bg:getComputedStyle(c).backgroundColor}; }"
    )
    if empty is None:
        # 空セルが画面内に無い構成なら、ビューポート左端 5% の中央を見る
        empty = {"x": landscape_page.viewport_size["width"] * 0.05, "y": landscape_page.viewport_size["height"] / 2, "bg": None}
    r, g, b = _px(landscape_page, empty["x"], empty["y"])
    assert max(r, g, b) <= DARK_MAX, (empty, (r, g, b))
```

- [ ] **Step 2: 実行して原因を特定する**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_viewer_polish.py -q -k solo_cover`
Expected: 監査時（800x600 デスクトップ幅）は白い矩形が見えた。テストが FAIL するなら `empty.bg` と、空セルに残る子要素（`<img>` プールの再利用で白い placeholder が残る、`.spread-container` に背景色がある、等）を DevTools 相当の `outerHTML` ダンプで確認する。**PASS する場合**（監査時の白は画像ロード中の一時状態だった可能性）: そのままテストを回帰ガードとして残し、PR 本文に「再現せず、ガードのみ追加」と書く。

- [ ] **Step 3: 修正（原因に応じて 1 つ）**

- 空セルに背景色がある → `style.css` で `.page-cell{background:transparent}` を明示。
- プール再利用の `<img>` が白い（`src` 無しで `alt` 背景）→ セル生成で `isSpacer` のとき `img` を append しない／`img.hidden = true` にし、`.page-cell img[hidden]{display:none}`。
- `.spread-container` に背景 → 削除。

- [ ] **Step 4: テスト実行、コミット**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_viewer_polish.py tests/test_ui.py -q`

```bash
git add app/static/style.css app/static/viewer/spread.js app/static/app.js tests/test_viewer_polish.py
git commit -S -m "fix(viewer): 表紙ソロ時の空セルを背景色にし白い矩形を出さない（監査 V3）

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 5: ツールバー高さとスライダー当たり判定（V4）

**Files:**
- Modify: `app/static/style.css` — `#toolbar`（`padding:8px 12px`、`@media(max-width:600px)` の `flex-wrap:wrap` ブロック）、`#page-slider{height:8px}` とサム/トラック
- Test: `tests/test_viewer_polish.py`（追記）

**Interfaces:**
- Produces: 幅 390 で `#toolbar` の高さ ≤ 64px。`#page-slider` の当たり判定高さ ≥ 44px、トラック見た目 8px。

- [ ] **Step 1: 失敗するテストを追記**

```python
def test_toolbar_height_budget_portrait(page, server):
    page.goto(server + "/#/view/long?p=0")
    page.wait_for_selector("#img-track img")
    h = page.evaluate("document.getElementById('toolbar').getBoundingClientRect().height")
    assert h <= 64, h


def test_slider_hit_area_44px_but_thin_track(page, server):
    page.goto(server + "/#/view/long?p=0")
    page.wait_for_selector("#img-track img")
    dims = page.evaluate(
        "() => { const s=document.getElementById('page-slider'); const r=s.getBoundingClientRect(); return {h:r.height, track:getComputedStyle(s,'::-webkit-slider-runnable-track').height}; }"
    )
    assert dims["h"] >= 44, dims
    assert dims["track"] in ("8px",), dims
```

- [ ] **Step 2: 失敗を確認する**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_viewer_polish.py -q -k "toolbar or slider"`
Expected: 現状 93px / 8px で 2 件 FAIL。

- [ ] **Step 3: CSS**

```css
/* ツールバー: モバイル縦でも 1 段 + タイトル行。合計 ≤ 64px */
#toolbar{padding:6px 12px calc(6px + env(safe-area-inset-top, 0px));min-height:56px;box-sizing:border-box}
@media(max-width:600px){
  #toolbar{flex-wrap:wrap;row-gap:0}
  #folder-name{order:4;width:100%;font-size:11px;line-height:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin:0}
}
/* スライダー: 当たり判定 44px、見た目のトラック 8px */
#page-slider{height:44px;background:transparent;-webkit-appearance:none;appearance:none}
#page-slider::-webkit-slider-runnable-track{height:8px;border-radius:4px;background:#333}
#page-slider::-moz-range-track{height:8px;border-radius:4px;background:#333}
#page-slider::-webkit-slider-thumb{-webkit-appearance:none;width:24px;height:24px;border-radius:50%;margin-top:-8px}
```

既存の `#page-slider{height:8px;...}`（`style.css:131` 付近）とサムのルール（132-133）を上記に**置き換える**（重複定義を残さない）。`#menu-btn` は 44px を維持（`tests/test_ui_visual.py` が見る）。

- [ ] **Step 4: テスト、スクリーンショット、コミット**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_viewer_polish.py tests/test_ui_visual.py tests/test_ui.py tests/test_viewer_slider_per_book.py -q`

`./dev.sh up viewer-polish --fixture --local --port 8092` → 390x844 と 1920x1200 でビューアのスクリーンショットを撮り `output/2026-09-2x-viewer-polish/` に保存（PR 本文に貼る）→ `./dev.sh down viewer-polish`。

```bash
git add app/static/style.css tests/test_viewer_polish.py
git commit -S -m "ux(viewer): ツールバーを 64px 以下に、スライダーの当たり判定を 44px に（監査 V4）

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 6: タップゾーン 40 / 20 / 40（V5）

**Files:**
- Modify: `app/static/style.css:163-166`（`#zone-prev{width:45%}` / `#zone-toggle{left:45%;width:10%}` / `#zone-next{width:45%}`）
- Modify: `tests/test_ios.py:112-116`（0.45 → 0.40 の 2 アサーション、`tog` に 0.20 を追加）
- Test: `tests/test_viewer_polish.py`（追記）

- [ ] **Step 1: テストを更新・追記**

`tests/test_ios.py` の 2 行:
```python
    assert abs(dims["prev"] / dims["w"] - 0.40) < 0.02, dims
    assert abs(dims["nxt"] / dims["w"] - 0.40) < 0.02, dims
    assert abs(dims["tog"] / dims["w"] - 0.20) < 0.02, dims
```

`tests/test_viewer_polish.py` に追記:
```python
def test_center_zone_toggles_toolbar_at_35_percent(page, server):
    """中央ゾーンが 20% に広がったので、幅の 35% 位置のタップはツールバー開閉になる（旧 45/10/45 では次ページだった）。"""
    page.goto(server + "/#/view/long?p=0")
    page.wait_for_selector("#img-track img")
    w, h = page.viewport_size["width"], page.viewport_size["height"]
    page.mouse.click(w * 0.35, h * 0.5)
    expect(page.locator("#counter")).to_have_text(re.compile(r"^1 / 30"))
```

- [ ] **Step 2: 失敗を確認する** → `tests/test_ios.py` の 0.40 アサーションと新テストが FAIL。

- [ ] **Step 3: CSS**

```css
#zone-prev{left:0;width:40%}      /* tap → next page (advance) */
#zone-toggle{left:40%;width:20%}  /* tap → toggle toolbar */
#zone-next{right:0;width:40%}     /* tap → previous page (go back) */
```

- [ ] **Step 4: テスト、CLAUDE.md、コミット**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_viewer_polish.py tests/test_ios.py tests/test_ui.py tests/test_ui_logic.py tests/test_url_state.py -q`
CLAUDE.md の「Left tap zone (45%) = *next* page, right zone = *previous*, center 10% toggles the toolbar」を「Left tap zone (40%) … center 20% …」に更新。

```bash
git add app/static/style.css tests/test_ios.py tests/test_viewer_polish.py CLAUDE.md
git commit -S -m "ux(viewer): タップゾーンを 40/20/40 にして中央の誤爆を減らす（監査 V5）

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

全スイート → push → PR（`Closes #203`、before/after スクリーンショット添付、実機確認依頼を 1 行）。

---

## PR 6: 本棚の磨き込み（B2 / B3 / B5 / O2、Issue #204）

ブランチ: `ux/browse-polish`（main から）。

### Task 7: タイル名を 2 行に（B2）

**Files:**
- Modify: `app/static/browse/render.js` — `renderTiles`（`'<span class="tile-name-text">' + _escapeHtml(f.name) + '</span>'`）、`renderList`、`renderCards` の同等箇所
- Modify: `app/static/style.css` — `.tile-name`（66 行付近）、`.tile-name-text`（237 行付近）
- Test: `tests/test_browse_polish.py`（新規）

**Interfaces:**
- Produces: `window.BrowseRender.splitDisplayName(name)` → `{leaf, parent}`（`"a/b/c"` → `{leaf:"c", parent:"a/b"}`、`"x"` → `{leaf:"x", parent:""}`）。render.js の公開オブジェクト名は末尾の expose ブロックで確認して合わせる。

- [ ] **Step 1: 失敗するテストを書く**

```python
# tests/test_browse_polish.py
"""本棚磨き込み（監査 B2/B3/B5/O2）。"""
import re

from playwright.sync_api import expect


def test_flattened_tile_shows_leaf_and_parent(page, server):
    page.goto(server + "/")
    tile = page.locator('#tile-grid .tile[data-path="wrap/inner"]')
    expect(tile.locator(".tile-name-text")).to_have_text("inner")
    expect(tile.locator(".tile-name-parent")).to_have_text("wrap")


def test_plain_tile_has_no_parent_line(page, server):
    page.goto(server + "/")
    tile = page.locator('#tile-grid .tile[data-path="simple_manga"]')
    expect(tile.locator(".tile-name-text")).to_have_text("simple_manga")
    expect(tile.locator(".tile-name-parent")).to_have_count(0)


def test_split_display_name_unit(page, server):
    page.goto(server + "/")
    page.wait_for_selector("#tile-grid .tile")
    assert page.evaluate("window.BrowseRender.splitDisplayName('a/b/c')") == {"leaf": "c", "parent": "a/b"}
    assert page.evaluate("window.BrowseRender.splitDisplayName('x')") == {"leaf": "x", "parent": ""}
```

- [ ] **Step 2: 失敗を確認する** → 3 件 FAIL（`.tile-name-parent` 無し、関数無し）。

- [ ] **Step 3: 実装**

```js
  // render.js
  function splitDisplayName(name) {
    var s = String(name || "");
    var i = s.lastIndexOf("/");
    if (i === -1) return { leaf: s, parent: "" };
    return { leaf: s.slice(i + 1), parent: s.slice(0, i) };
  }
  function nameHtml(name) {
    var d = splitDisplayName(name);
    var h = '<span class="tile-name-text">' + _escapeHtml(d.leaf) + '</span>';
    if (d.parent) h += '<span class="tile-name-parent">' + _escapeHtml(d.parent) + '</span>';
    return h;
  }
```

`renderTiles` / `renderList` / `renderCards` の `'<span class="tile-name-text">' + _escapeHtml(f.name) + '</span>'`（変数名は各関数で `f`/`node`）を `nameHtml(f.name)` に置き換える。公開ブロックに `splitDisplayName` を追加。

```css
.tile-name{padding:6px 8px 4px;font-size:11px;display:flex;flex-direction:column;gap:1px;min-width:0}
.tile-name-text,.tile-name-parent{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;position:relative;z-index:1}
.tile-name-parent{font-size:9px;color:#8a8a8a}
```

（既存の `.tile-name` の他プロパティ—背景・行揃え—は残す。ellipsis は末尾側に統一。中央省略は B3 のピルだけ。）

- [ ] **Step 4: テスト、コミット**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_browse_polish.py tests/test_ui.py tests/test_search_e2e.py tests/test_download_button.py tests/test_ui_visual.py -q`

```bash
git add app/static/browse/render.js app/static/style.css tests/test_browse_polish.py
git commit -S -m "ux(browse): flatten されたタイル名を「子 + 親（小）」の 2 行で表示（監査 B2）

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 8: 「続きから」ピル（B3）

**Files:**
- Modify: `app/static/app.js` — `updateResumeButton`（`resumeBtnText.textContent = \`📖 ${bookName} p.${data.page + 1}\``）、`showResumeBtn` / `hideResumeBtn`
- Modify: `app/static/style.css` — `#resume-btn`（239 行付近）、`#tile-grid`
- Test: `tests/test_browse_polish.py`（追記）

**Interfaces:**
- Produces: `truncateMiddle(s, max=24)`（app.js 内、`window.__BOOKQ_DEBUG.truncateMiddle` で公開）: 長さ ≤ max はそのまま、超えたら先頭 12 + `…` + 末尾 8。`sidebar.classList.toggle("has-resume", visible)`。

- [ ] **Step 1: 失敗するテストを追記**

```python
def _make_progress(page, folder, pg=3):
    page.evaluate(
        "([f,p]) => localStorage.setItem('bookq_progress', JSON.stringify({[f]:{page:p,total:30,ts:Date.now()}}))",
        [folder, pg],
    )


def test_resume_pill_text_and_middle_truncation(page, server):
    page.goto(server + "/")
    _make_progress(page, "long")
    page.reload()
    expect(page.locator("#resume-btn-text")).to_have_text("📖 続きから: long p.4")
    assert page.evaluate("window.__BOOKQ_DEBUG.truncateMiddle('abcdefghijklmnopqrstuvwxyz0123', 24)") == "abcdefghijkl…stuvwxyz0123"[:12] + "…" + "wxyz0123"


def test_resume_pill_does_not_cover_last_tile(page, server):
    page.goto(server + "/")
    _make_progress(page, "long")
    page.reload()
    expect(page.locator("#resume-btn")).to_be_visible()
    page.evaluate("document.getElementById('sidebar-scroll').scrollTop = 1e9")
    page.wait_for_timeout(200)
    r = page.evaluate(
        "() => { const tiles=[...document.querySelectorAll('#tile-grid .tile')]; const last=tiles[tiles.length-1].getBoundingClientRect(); const pill=document.getElementById('resume-btn').getBoundingClientRect(); return {lastBottom:last.bottom, pillTop:pill.top}; }"
    )
    assert r["lastBottom"] <= r["pillTop"], r
```

（`truncateMiddle` の期待値は `"abcdefghijkl" + "…" + "wxyz0123"` — 上の式はそれに等しい。）

- [ ] **Step 2: 失敗を確認する** → 文言と重なりで FAIL。

- [ ] **Step 3: 実装**

```js
  function truncateMiddle(s, max = 24) {
    s = String(s || "");
    if (s.length <= max) return s;
    return s.slice(0, 12) + "…" + s.slice(-8);
  }
  // updateResumeButton 内
    resumeBtnText.textContent = `📖 続きから: ${truncateMiddle(bookName)} p.${data.page + 1}`;
```

`showResumeBtn` で `sidebar.classList.add("has-resume")`、`hideResumeBtn` で `remove`。`window.__BOOKQ_DEBUG` に `truncateMiddle` を追加。

```css
#resume-btn{max-width:calc(100vw - 32px);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#sidebar.has-resume #tile-grid{padding-bottom:72px}
```

- [ ] **Step 4: テスト、コミット**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_browse_polish.py tests/test_browse_url_state.py tests/test_ui_logic.py tests/test_ui_visual.py -q`

```bash
git add app/static/app.js app/static/style.css tests/test_browse_polish.py
git commit -S -m "ux(browse): 続きからピルの文言を「続きから: 名前 p.N」にし、タイルを隠さない余白を足す（監査 B3）

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 9: タッチ目標 44px（B5）

**Files:**
- Modify: `app/static/style.css` — `#filter-btn{min-width:32px;min-height:32px}`（314 行付近）、`#offline-status`（35-41）、`#harness-link`（新規ルール）
- Modify: `tests/test_ui_visual.py:75` のセレクタ配列

- [ ] **Step 1: テストを更新**

```python
    for sel in ["#menu-btn", "#logout-btn", "#footer-refresh-btn", "#search-input", "#filter-btn", "#offline-status", "#harness-link"]:
```

（`#harness-link` はテストモードで表示される。`_rect` が非表示要素で None を返す場合は `page.evaluate("localStorage.setItem('bookq_harness','1')")` + reload をテスト先頭に足す。）

- [ ] **Step 2: 失敗を確認する** → `#filter-btn` 32px、`#offline-status` 17px、`#harness-link` 15px で FAIL。

- [ ] **Step 3: CSS**

```css
#filter-btn{min-width:44px;min-height:44px}
#offline-status{display:inline-flex;align-items:center;min-height:44px;padding:0 8px;margin:-12px 0}
#harness-link{display:inline-flex;align-items:center;min-height:44px;padding:0 6px}
```

（`margin:-12px 0` はヘッダ高さを増やさずに当たり判定だけ広げる。ヘッダの見た目が崩れたら `margin` を外し、ヘッダ高さの増分を PR 本文に記す。）

- [ ] **Step 4: テスト、コミット**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_ui_visual.py tests/test_sidebar_scroll.py tests/test_search_e2e.py -q`

```bash
git add app/static/style.css tests/test_ui_visual.py
git commit -S -m "ux(browse): フィルタ・オンライン表示・Harness の当たり判定を 44px に（監査 B5）

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

### Task 10: ストレージ表示をフッタへ（O2）

**Files:**
- Modify: `app/templates/index.html:60`（`<div id="storage-bar">…</div>` を削除）、`index.html:66-72`（フッタに `<span id="storage-text"></span>` を `#folder-count` の直後に追加）
- Modify: `app/static/app.js` — `updateStorageBar`（`textEl.textContent=…` と `fill` の処理）
- Modify: `app/static/style.css` — `#storage-bar` / `#storage-text` / `#storage-fill`（42-43 行付近）を削除し `#sidebar-footer #storage-text{font-size:11px;color:#9a9a9a}` を追加
- Modify: `tests/test_sidebar_scroll.py:54,63,76,91`（`#storage-bar` の可視性を見ている箇所 → `#search-input` に差し替え）
- Test: `tests/test_browse_polish.py`（追記）

**Interfaces:**
- Produces: `#storage-text` の文言 `💾 <fmtBytes(usage)> / 空き <fmtBytes(quota - usage)>`、quota 不明時 `💾 <fmtBytes(usage)>`。`#storage-bar` / `#storage-fill` は DOM から消える。

- [ ] **Step 1: 失敗するテストを追記**

```python
def test_storage_text_in_footer_with_free_space(page, server):
    page.goto(server + "/")
    page.wait_for_selector("#tile-grid .tile")
    expect(page.locator("#storage-bar")).to_have_count(0)
    t = page.locator("#sidebar-footer #storage-text")
    expect(t).to_have_text(re.compile(r"^💾 [\d.]+ [KMG]?B / 空き [\d.]+ [KMG]?B$"))
```

（Chromium の `navigator.storage.estimate()` は quota を返すので `/ 空き` 側が出る。WebKit の 0/0 fallback は `💾 0.0 MB` のみで、`BROWSER=webkit` では `re.compile(r"^💾 ")` に緩める分岐を `os.environ.get("BROWSER")` で入れる。）

- [ ] **Step 2: 失敗を確認する** → `#storage-bar` が存在し、フッタに `#storage-text` が無いので FAIL。

- [ ] **Step 3: 実装**

`updateStorageBar` の末尾を:
```js
      const textEl=document.getElementById('storage-text');
      if(textEl){
        textEl.textContent = quota>0
          ? `💾 ${fmtBytes(usage)} / 空き ${fmtBytes(Math.max(0, quota-usage))}`
          : `💾 ${fmtBytes(usage)}`;
      }
```
（`fill` の処理と `pct` の計算を削除。関数名 `updateStorageBar` は呼び出し元が多いので変えない。）

`tests/test_sidebar_scroll.py` の `#storage-bar` 参照 4 箇所は「サイドバー上部の要素が見える／隠れる」の判定に使っているだけなので `#search-input` に置き換える（各行の意図をコメントで確認してから）。`tests/test_offline.py:296-304` と `tests/test_offline_harness.py:350-419` は `#storage-text` の部分一致（`GB` / `5.0`）なので変更不要。

- [ ] **Step 4: テスト、スクリーンショット、コミット**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_browse_polish.py tests/test_sidebar_scroll.py tests/test_offline.py tests/test_offline_harness.py tests/test_ios_pwa.py -q`

`./dev.sh up browse-polish --fixture --local --port 8093` → 390x844 でフッタが 1 行に収まっているスクリーンショット → `down`。

```bash
git add app/templates/index.html app/static/app.js app/static/style.css tests/test_sidebar_scroll.py tests/test_browse_polish.py
git commit -S -m "ux(browse): ストレージ表示をフッタのテキスト「💾 使用量 / 空き」に移し、上部バーを廃止（監査 O2）

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

全スイート → push → PR（`Closes #204`、スクリーンショット、実機確認依頼）。

---

## PR 7: UI 文言の日本語統一（X1、Issue #205）

ブランチ: `ux/japanese-labels`（main から、PR 4〜6 マージ後）。

### Task 11: 文言置換とテスト更新

**Files:**
- Modify: `app/templates/index.html`、`app/static/app.js`
- Modify: `tests/conftest.py::folder_count_text`、`tests/test_browse_url_state.py`（`"N folders"` literal 25 箇所）、`tests/test_filters_e2e.py:257`、`tests/test_ios.py:149,159`、`tests/test_url_state.py:93,121,193`

対訳表（**この表の右列を verbatim で使う**）:

| 場所 | 現在 | 変更後 |
|---|---|---|
| `index.html:7` `<title>` | `bookq — Login` | `bookq — ログイン` |
| `index.html:25` | `Sign in with Google` | `Google でサインイン` |
| `index.html:37` `<title>` | `Image Viewer` | `bookq` |
| `index.html:56` title | `Filters` | `絞り込み` |
| `index.html:61` placeholder | `Search...` | `検索…` |
| `index.html:67` title | `Switch view` | `表示切替` |
| `index.html:68` + `app.js` の `folder-count` 更新箇所 | `N folders` | `N 件` |
| `index.html:69` | `🧪 Harness` | `🧪 ハーネス` |
| `index.html:71` title | `Logout` | `ログアウト` |
| `index.html:78` | `Filters` | `絞り込み` |
| `index.html:83/87/91/95` | `Artist` / `Tags` / `Parody` / `Sort` | `作者` / `タグ` / `パロディ` / `並び順` |
| `index.html:97-99` | `Relevance` / `Newest` / `Rating` | `関連度` / `新しい順` / `評価順` |
| `index.html:104/105` | `Reset` / `Apply` | `リセット` / `適用` |
| `index.html:113` title | `Folders (F)` | `フォルダ一覧 (F)` |
| `index.html:115` title | `Page slider — right=first, left=last` | `ページスライダー（右端が 1 ページ目）` |
| `index.html:121/122/123` title | `Previous` / `Toggle toolbar` / `Next` | `前のページ` / `ツールバー表示切替` / `次のページ` |
| `index.html:125` `#hint` | `Swipe / ← → to flip pages &nbsp;\|&nbsp; Center tap → hide toolbar` | `スワイプ / ← → でページ送り &nbsp;\|&nbsp; 中央タップでツールバー` |
| `index.html:130` `#empty` | `📂 Select a folder from the sidebar` | `📂 サイドバーからフォルダを選んでください` |
| `app.js:1041` | `Failed to load folders` | `フォルダ一覧を読み込めませんでした` |
| `app.js:1572` `#empty` | `⚠️ Failed to load images` | `⚠️ 画像を読み込めませんでした` |
| `app.js` `updateFooterBtn` | `footerBtn.title = "View: " + viewMode` | `footerBtn.title = "表示: " + ({tile:"タイル",list:"リスト",card:"カード"}[viewMode] || viewMode)` |

- [ ] **Step 1: テストを先に更新する**

```bash
# folder-count
sed -i '' 's/ folders"/ 件"/g' tests/test_browse_url_state.py tests/test_filters_e2e.py
# conftest の fixture
sed -i '' 's/return f"{len(scan_tree(downloads_tree))} folders"/return f"{len(scan_tree(downloads_tree))} 件"/' tests/conftest.py
# #empty の 2 文言
sed -i '' 's/📂 Select a folder from the sidebar/📂 サイドバーからフォルダを選んでください/' tests/test_ios.py tests/test_url_state.py
sed -i '' 's/⚠️ Failed to load images/⚠️ 画像を読み込めませんでした/' tests/test_url_state.py
grep -rn "folders\"\|Select a folder\|Failed to load" tests/ | grep -v "^tests/README" || echo "no stale literals"
```

- [ ] **Step 2: 失敗を確認する**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_browse_url_state.py tests/test_ios.py tests/test_url_state.py tests/test_filters_e2e.py -q -x`
Expected: `N 件` / `#empty` 文言で FAIL。

- [ ] **Step 3: 対訳表どおりに置換する**

`app.js` の `folder-count` 更新箇所は `grep -n "folders" app/static/app.js` で全部洗い、テンプレート文字列 `` `${n} folders` `` を `` `${n} 件` `` に。`index.html:68` の初期値 `0 folders` → `0 件`。

- [ ] **Step 4: 全スイート、コミット、PR**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/ -q -x -n 4`

```bash
git add app/templates/index.html app/static/app.js tests/
git commit -S -m "ux(i18n): UI 文言を日本語に統一（監査 X1）

i18n 機構は入れず直書き置換のみ。folder-count は「N 件」、#empty の 2 文言もテストと同時更新。

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
git push -u origin ux/japanese-labels
gh pr create --repo kentoku24/bookq --base main --head ux/japanese-labels --title "ux(i18n): UI 文言を日本語に統一" --body "Closes #205 ..."
```

---

## Self-Review

- **Spec coverage**: decisions.md の PR 4（Q11/Q17）→ Task 1–3、PR 5（V3/V4/V5、Q21）→ Task 4–6、PR 6（B2/B3/B5/O2、Q22–Q24(b) + 「0.0 MB / 空き容量」）→ Task 7–10、PR 7（X1、Q15 a）→ Task 11、PR 8（Issue 化）→ 既に #197〜#201 で完了。
- **Placeholder scan**: Task 4 は「原因に応じて 1 つ」の分岐を持つが、各分岐の変更内容は具体化済み。PR 本文の「…」は実行時に埋める箇所のみ。
- **Type consistency**: `parseViewerHash` の `s`/`o` と `viewerHash(folder, page, layout)` の `layout.landscapeSpread`/`layout.spreadOffset`、`currentViewerState` の戻り値が一致。`splitDisplayName` / `nameHtml` / `.tile-name-parent`、`truncateMiddle` / `has-resume`、`#storage-text` の文言フォーマットが Task とテストで一致。`updateFooterBtn` の公開は Task 2 で行い Task 11 で title を変更。
