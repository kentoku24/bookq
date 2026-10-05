# ビューア ピンチズーム + 俯瞰モード Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ピンチアウトでページを 1.0〜4.0x に拡大・パンし、ピンチインでシリーズ全体を RTL グリッドで俯瞰してタップで任意ページへ飛べるようにする。scale = 1.0 のときの既存ページめくりは一切変えない。

**Architecture:** 読書ストリップ `#img-track` は無変更。拡大は新ラッパー `#zoom-layer` への `transform`、俯瞰は別オーバーレイ `#overview`。新規 3 モジュール `viewer/gesture.js`（ピンチ認識 + ネイティブズーム封じ）、`viewer/zoom.js`（絶対ズーム状態機械 + パン + 端越え）、`viewer/overview.js`（グリッド + FLIP 入退場）を `spread.js` と `app.js` の間に defer ロードし、`app.js` には early return / reset 呼び出し / identifier 追跡 / `updateOrientation` ガードを足す。

**Tech Stack:** 素の ES2017 JS（ビルド無し、IIFE + `window.*` 公開）、CSS、FastAPI 配信の静的ファイル、Playwright（Chromium）+ CDP `Input.dispatchTouchEvent` による合成ピンチ、pytest。

**Spec:** `docs/superpowers/specs/2026-09-21-viewer-zoom-overview-design.md`（以降「spec」。§番号は spec のもの）

## Global Constraints

- 既存ページめくり（タップ／スワイプ／キー／スライダー、チャプター内・チャプター間遷移）は scale = 1.0 で挙動不変。既存テスト全件がそのまま通ること（spec §1）。
- `#img-track` の `style.transition` / `style.transform` に新モジュールは一切触らない。ズームは `#zoom-layer` のみ（spec §2）。
- 新モジュールは plain global script。`window.ViewerGesture`, `window.Zoom`, `window.Overview` を公開（spec §5）。
- script ロード順: `viewer/spread.js` → `viewer/gesture.js` → `viewer/zoom.js` → `viewer/overview.js` → `app.js`。`sw.js` の `SHELL_ASSETS` に同じ 3 ファイルを追加し、`tests/test_sw_offline_contract.py` の件数 16 → 19（spec §5.4、CLAUDE.md 契約）。
- ズーム定数: `ZOOM_MIN=0.35`, `ZOOM_MAX=4`, 俯瞰発火 `OV_FIRE=0.5`, 再アーム `OV_REARM=0.7`, 1.0 スナップ上限 `1.15`, バウンス `190ms`, 端越え `80px`、ラバーバンド追従 `0.35`、FLIP `220ms`（+80ms フォールバック）、退出後 click 抑止 `400ms`、慣性タップ除外 `100ms`、クロスフェード上限 `3000ms`、ピンチ成立 `10px`、パーム端 `20px`、俯瞰内ピンチアウト退出 `1.3`（spec §5）。
- `reset(true)` のアニメは `transform .3s cubic-bezier(0.25,0.1,0.25,1.0)`（spec §5.2）。
- グリッド: `direction:rtl`, `repeat(auto-fill, minmax(150px, 1fr))`, gap 8px, padding 8px, 全セル 3:4, `object-fit:contain`, サムネ `thumbUrl(folder,name,400)` → `onerror` で `imageUrl`。600 ページ超は現在位置 ±200 のみ `<img>`（spec §5.3）。
- `touchmove` ハンドラ内で `getBoundingClientRect`/`offsetTop` を呼ばない（spec §4）。
- ネイティブ封じは `#viewer.contains(e.target)` かつ `images.length > 0` にゲート。viewport meta は Simulator 実測後に判断（spec §5.1-5、§9）。
- テスト実行コマンド（`tests/README.md` どおり、`--with-requirements` 必須）:
  `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest <path> -q`
- コミットは署名付き（`git commit -S`）。メッセージ末尾に `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`。

---

## File Structure

| ファイル | 責務 |
|---|---|
| Create `app/static/viewer/gesture.js` | 接触点の有効/パーム分類、`activeTouches(e)`、ピンチ成立判定と `onStart/onMove/onEnd`、ネイティブズーム封じの 4 ガード |
| Create `app/static/viewer/zoom.js` | 絶対ズーム状態 `{zoom, tx, ty}`、帯域判定、`#zoom-layer` transform、パン・clamp・端越え、`reset/isActive/state`、俯瞰発火の呼び出し |
| Create `app/static/viewer/overview.js` | `#overview` の構築（セクション/セル/チップ）、先行構築、ウィンドウイング、FLIP 入場、退出 5 経路、`open/close/isOpen/prebuild/cellCenter` |
| Modify `app/static/app.js` | identifier 追跡、`Zoom.isActive()` early return、`snapTo` 先頭 reset、`updateOrientation` ガード、`zoneToggle`/`menu-btn`/`G` 分岐、`suppressClickFor` 公開、`loadImages` 後の `prebuild` |
| Modify `app/static/browse/hash.js` | `_teardownViewer` で `Zoom.reset(false)` と `Overview.close()` |
| Modify `app/templates/index.html` | `#zoom-layer`、`#overview`、`#overview-btn`、script タグ 3 本 |
| Modify `app/static/style.css` | `#zoom-layer`、`.ov-*`、`body.zoomed`/`body.overview-open`、`touch-action`、reduced-motion |
| Modify `app/static/sw.js` | `SHELL_ASSETS` に 3 ファイル |
| Create `tests/viewer_touch.py` | CDP 合成タッチヘルパ（pinch/tap/drag）と本を開くヘルパ |
| Create `tests/test_viewer_gesture.py` | gesture.js（パーム、成立、ガード） |
| Create `tests/test_viewer_zoom.py` | zoom.js（帯域、アンカー、パン、端越え、reset） |
| Create `tests/test_viewer_overview.py` | overview.js（グリッド、入退場、重なり、ウィンドウイング） |
| Modify `tests/conftest.py` | `zoom_series/series/ep-1..3`（5/4/7 頁）と `huge/book/part1,part2`（350+351 頁）を fixture tree に追加 |
| Modify `tests/test_sw_offline_contract.py` | 件数 16 → 19 |
| Modify `CLAUDE.md`, `README.md`, `tests/README.md` | 契約・使い方の追記 |

---

### Task 1: テストインフラ（CDP タッチヘルパと fixture tree）

**Files:**
- Create: `tests/viewer_touch.py`
- Modify: `tests/conftest.py:60-222`（`downloads_tree`）
- Test: `tests/test_viewer_gesture.py`（このタスクではヘルパの疎通のみ）

**Interfaces:**
- Produces: `tests/viewer_touch.py`
  - `open_book(page, server, folder: str, p: int, expect_local_total: int) -> None` — `page.goto(server + f"/#/view/{folder}?p={p}")` して `#counter` が `f"{p+1} / {expect_local_total}"` になるまで待つ
  - `touch_session(page)` → CDP session（`page.context.new_cdp_session(page)`）
  - `touch_start(session, points)`, `touch_move(session, points)`, `touch_end(session, points=[])` — `points` は `[{"x","y","id"}]`
  - `pinch(session, cx, cy, halves: list[int], hold=False)` — 2 点を `cx±half` で start → move。`hold=False` なら最後に end
  - `drag(session, x0, y0, x1, y1, steps=6, id=1, hold=False)` — 1 点ドラッグ
  - `zoom_state(page) -> dict` — `page.evaluate("window.Zoom.state()")`
- conftest fixture tree に追加（scan で `zoom_series` は single-child flattening され `zoom_series/series` 1 行になるが `_fullPath` は保持される）:
  - `zoom_series/series/ep-1/p01..p05.png`, `ep-2/p01..p04.png`, `ep-3/p01..p07.png`（合計 16 頁、3 チャプター）
  - `huge/book/part1/p001..p350.png`, `huge/book/part2/p001..p351.png`（701 頁）
  - `folder_count_text` は動的なので更新不要

- [ ] **Step 1: conftest に fixture を追加**

`tests/conftest.py` の `downloads_tree` 内、`kanji` ブロックの直前に追加:

```python
    # Zoom / overview fixtures (tests/test_viewer_zoom.py, test_viewer_overview.py).
    # Chapters sit at depth 2 (zoom_series/series/ep-N) so sibling concatenation
    # kicks in (SIBLING_AUTOLOAD_MIN_DEPTH = 2). Page counts 5/4/7 exercise odd
    # trailing pages and the __spacer__ at each seam.
    for ep, pages in [("ep-1", 5), ("ep-2", 4), ("ep-3", 7)]:
        d = root / "zoom_series" / "series" / ep
        d.mkdir(parents=True)
        for i in range(1, pages + 1):
            _make_png(d / f"p{i:02d}.png")
    # 701 pages across two chapters: exercises the ±200 <img> window of the overview.
    for part, pages in [("part1", 350), ("part2", 351)]:
        d = root / "huge" / "book" / part
        d.mkdir(parents=True)
        for i in range(1, pages + 1):
            _make_png(d / f"p{i:03d}.png")
```

docstring のツリー一覧にも 2 行追加:

```
      zoom_series/series/ep-1..3/  5/4/7 pages                (zoom + overview, depth-2 chapters)
      huge/book/part1,part2/       350 + 351 pages            (overview ±200 img window)
```

- [ ] **Step 2: ヘルパを書く**

`tests/viewer_touch.py`:

```python
"""Synthetic touch helpers for the zoom / overview tests.

Playwright has no pinch API; CDP Input.dispatchTouchEvent with two touch
points is deterministic in Chromium. All helpers are state-polled by the
callers (no time.sleep).
"""
from playwright.sync_api import expect


def open_book(page, server, folder: str, p: int, expect_local_total: int) -> None:
    page.goto(f"{server}/#/view/{folder}?p={p}")
    expect(page.locator("#counter")).to_have_text(f"{p + 1} / {expect_local_total}")


def touch_session(page):
    return page.context.new_cdp_session(page)


def _pts(points):
    return [{"x": float(p["x"]), "y": float(p["y"]), "id": int(p.get("id", i + 1))}
            for i, p in enumerate(points)]


def touch_start(session, points):
    session.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": _pts(points)})


def touch_move(session, points):
    session.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": _pts(points)})


def touch_end(session, points=()):
    session.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": _pts(points)})


def two(cx, cy, half):
    return [{"x": cx - half, "y": cy, "id": 1}, {"x": cx + half, "y": cy, "id": 2}]


def pinch(session, cx, cy, halves, hold=False):
    """Two fingers at cx±halves[0], then move through the remaining halves.
    Both fingers move symmetrically, so each crosses the 10px arm threshold
    as soon as |halves[i]-halves[0]| >= 10."""
    touch_start(session, two(cx, cy, halves[0]))
    for h in halves[1:]:
        touch_move(session, two(cx, cy, h))
    if not hold:
        touch_end(session)


def drag(session, x0, y0, x1, y1, steps=6, id=1, hold=False):
    touch_start(session, [{"x": x0, "y": y0, "id": id}])
    for i in range(1, steps + 1):
        t = i / steps
        touch_move(session, [{"x": x0 + (x1 - x0) * t, "y": y0 + (y1 - y0) * t, "id": id}])
    if not hold:
        touch_end(session)


def tap(session, x, y, id=1):
    touch_start(session, [{"x": x, "y": y, "id": id}])
    touch_end(session)


def zoom_state(page) -> dict:
    return page.evaluate("window.Zoom ? window.Zoom.state() : null")
```

- [ ] **Step 3: 疎通テストを書く**

`tests/test_viewer_gesture.py`:

```python
"""viewer/gesture.js — palm exclusion, pinch arming, native-zoom guards."""
import pytest
from playwright.sync_api import expect

from viewer_touch import open_book, touch_session, pinch, drag, touch_start, touch_move, touch_end

SERIES = "zoom_series/series/ep-1"


@pytest.fixture
def touch(ios_page):
    s = touch_session(ios_page)
    yield s
    s.detach()


def test_fixture_series_opens_with_three_chapters(ios_page, server):
    open_book(ios_page, server, SERIES, 0, 5)
    assert ios_page.evaluate("window.chapterList.length") == 3
    assert ios_page.evaluate("window.images.filter(i => !i.isSpacer).length") == 16
```

- [ ] **Step 4: 実行して通ることを確認**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_viewer_gesture.py -q`
Expected: 1 passed（`tests/` を `sys.path` に入れる conftest 設定が無ければ `from viewer_touch import` が失敗する。その場合は `tests/conftest.py` 先頭に `sys.path.insert(0, str(Path(__file__).parent))` を追加）

- [ ] **Step 5: 既存テストが fixture 追加で壊れていないことを確認**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_ui.py tests/test_browse_url_state.py -q`
Expected: all passed

- [ ] **Step 6: Commit**

```bash
git add tests/conftest.py tests/viewer_touch.py tests/test_viewer_gesture.py
git commit -S -m "test(viewer): CDP タッチヘルパと zoom/overview 用 fixture tree

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: `viewer/gesture.js` — 接触分類・ピンチ成立・ネイティブ封じ

**Files:**
- Create: `app/static/viewer/gesture.js`
- Modify: `app/templates/index.html`（script タグ）, `app/static/sw.js:9`, `tests/test_sw_offline_contract.py:39`
- Test: `tests/test_viewer_gesture.py`

**Interfaces:**
- Produces `window.ViewerGesture`:
  - `activeTouches(e: TouchEvent) -> Touch[]` — `e.touches` のうちパームでないもの（開始位置が画面端 20px 以内で、かつまだ 10px 動いていない接触を除外）
  - `attach(el: Element, handlers: {onStart({dist0, mid0, touches}), onMove({ratio, mid, touches}), onEnd({ratio, remaining: Touch|null})}) -> void`
  - `isPinching() -> boolean`
  - `installNativeGuards(viewerEl: Element, isActive: () => boolean) -> void` — spec §5.1-1..4（1 は CSS、2〜4 はここ）
  - テスト用: `window.ViewerGesture._debug = {palmIds: () => number[]}`

- [ ] **Step 1: 失敗するテストを書く（ガードとパーム）**

`tests/test_viewer_gesture.py` に追加:

```python
def _dispatch_touch_prevented(page, selector, n_touches, type_="touchmove"):
    return page.evaluate(
        """([sel, n, type]) => {
          const el = document.querySelector(sel);
          const r = el.getBoundingClientRect();
          const mk = (i) => new Touch({identifier: i, target: el, clientX: r.left + 50 + i*40, clientY: r.top + 300});
          const touches = Array.from({length: n}, (_, i) => mk(i+1));
          const ev = new TouchEvent(type, {touches, changedTouches: touches, bubbles: true, cancelable: true});
          el.dispatchEvent(ev);
          return ev.defaultPrevented;
        }""",
        [selector, n_touches, type_],
    )


def test_two_finger_touchmove_in_viewer_is_prevented_one_finger_is_not(ios_page, server):
    open_book(ios_page, server, SERIES, 0, 5)
    assert _dispatch_touch_prevented(ios_page, "#img-container", 2) is True
    assert _dispatch_touch_prevented(ios_page, "#img-container", 1) is False
    assert _dispatch_touch_prevented(ios_page, "#img-container", 2, "touchstart") is True


def test_two_finger_in_sidebar_is_not_prevented(ios_page, server):
    open_book(ios_page, server, SERIES, 0, 5)
    ios_page.keyboard.press("f")  # open sidebar
    expect(ios_page.locator("#sidebar")).to_have_class(lambda c: "open" in c)
    assert _dispatch_touch_prevented(ios_page, "#sidebar-scroll", 2) is False
    assert _dispatch_touch_prevented(ios_page, "#sidebar-scroll", 2, "touchstart") is False


def test_gesturestart_is_prevented_only_inside_viewer(ios_page, server):
    open_book(ios_page, server, SERIES, 0, 5)
    prevented = ios_page.evaluate(
        """() => {
          const ev = new Event('gesturestart', {bubbles: true, cancelable: true});
          document.querySelector('#img-container').dispatchEvent(ev);
          return ev.defaultPrevented;
        }"""
    )
    assert prevented is True


def test_guards_inactive_when_no_book_is_open(ios_page, server):
    # browse root: images.length === 0 → guards must not fire
    assert _dispatch_touch_prevented(ios_page, "#img-container", 2) is False


def test_edge_palm_is_excluded_until_it_moves(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 0, 5)
    # finger 1 in the middle, finger 2 resting 8px from the bottom edge (palm)
    touch_start(touch, [{"x": 200, "y": 400, "id": 1}, {"x": 200, "y": 836, "id": 2}])
    assert ios_page.evaluate("window.ViewerGesture._debug.palmIds()") == [2]
    touch_move(touch, [{"x": 200, "y": 400, "id": 1}, {"x": 200, "y": 820, "id": 2}])  # palm moved 16px → promoted
    assert ios_page.evaluate("window.ViewerGesture._debug.palmIds()") == []
    touch_end(touch)


def test_pinch_arms_only_after_both_fingers_move_10px(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 0, 5)
    ios_page.evaluate("window.__pinchStarts = 0; window.ViewerGesture.attach(document.querySelector('#img-container'), {onStart(){ window.__pinchStarts++; }, onMove(){}, onEnd(){}})")
    pinch(touch, 195, 420, [50, 55], hold=True)      # 5px each: not armed
    assert ios_page.evaluate("window.__pinchStarts") == 0
    touch_move(touch, two(195, 420, 62))              # 12px each: armed
    assert ios_page.evaluate("window.__pinchStarts") == 1
    touch_end(touch)
```

（`two` を import に追加: `from viewer_touch import ..., two`）

- [ ] **Step 2: 実行して失敗を確認**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_viewer_gesture.py -q`
Expected: 新規テストが `window.ViewerGesture is undefined` 等で FAIL

- [ ] **Step 3: gesture.js を実装**

`app/static/viewer/gesture.js`:

```js
/* bookq viewer/gesture — pinch recognizer + native-zoom guards.
 * Plain global script. Loaded after viewer/spread.js, before viewer/zoom.js.
 * Spec: docs/superpowers/specs/2026-09-21-viewer-zoom-overview-design.md §5.1
 *
 * Touch classification: a touch that starts within PALM_EDGE px of any screen
 * edge is a "palm" until it moves ARM_PX; activeTouches() hides palms so a
 * thumb resting on the bezel never kills the 1-finger swipe (app.js uses this
 * for its own count checks) and never counts as a pinch finger.
 * A pinch arms only when two active touches have BOTH moved ARM_PX from
 * their start — then onStart fires once; onMove per 2-finger move; onEnd when
 * fewer than two active touches remain (remaining = the one still down).
 */
(function () {
  'use strict';
  var PALM_EDGE = 20, ARM_PX = 10;

  var starts = {};   // identifier -> {x, y, palm}
  function noteStart(t) {
    var w = window.innerWidth, h = window.innerHeight;
    var palm = t.clientX < PALM_EDGE || t.clientY < PALM_EDGE || t.clientX > w - PALM_EDGE || t.clientY > h - PALM_EDGE;
    starts[t.identifier] = { x: t.clientX, y: t.clientY, palm: palm };
  }
  function noteMove(t) {
    var s = starts[t.identifier]; if (!s) { noteStart(t); s = starts[t.identifier]; }
    if (s.palm && Math.hypot(t.clientX - s.x, t.clientY - s.y) >= ARM_PX) s.palm = false;
  }
  function forget(t) { delete starts[t.identifier]; }
  function movedEnough(t) {
    var s = starts[t.identifier]; if (!s) return false;
    return Math.hypot(t.clientX - s.x, t.clientY - s.y) >= ARM_PX;
  }
  function activeTouches(e) {
    var out = [];
    for (var i = 0; i < e.touches.length; i++) {
      var t = e.touches[i];
      if (!starts[t.identifier]) noteStart(t);
      if (!starts[t.identifier].palm) out.push(t);
    }
    return out;
  }
  // Document-level bookkeeping so activeTouches() is correct for ANY listener,
  // including app.js handlers registered on #img-container.
  document.addEventListener('touchstart', function (e) { for (var i = 0; i < e.changedTouches.length; i++) noteStart(e.changedTouches[i]); }, { capture: true, passive: true });
  document.addEventListener('touchmove', function (e) { for (var i = 0; i < e.changedTouches.length; i++) noteMove(e.changedTouches[i]); }, { capture: true, passive: true });
  document.addEventListener('touchend', function (e) { for (var i = 0; i < e.changedTouches.length; i++) forget(e.changedTouches[i]); }, { capture: true, passive: true });
  document.addEventListener('touchcancel', function (e) { for (var i = 0; i < e.changedTouches.length; i++) forget(e.changedTouches[i]); }, { capture: true, passive: true });

  function dist(a, b) { return Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY) || 0.0001; }
  function mid(a, b) { return { x: (a.clientX + b.clientX) / 2, y: (a.clientY + b.clientY) / 2 }; }

  var pinching = false;
  function attach(el, h) {
    var armed = false, dist0 = 0, ids = null;
    function reset() { armed = false; dist0 = 0; ids = null; pinching = false; }
    el.addEventListener('touchstart', function () { /* arming happens on move */ }, { passive: true });
    el.addEventListener('touchmove', function (e) {
      var act = activeTouches(e);
      if (act.length < 2) { if (armed) { var rem = act.length === 1 ? act[0] : null; var d = dist0 ? dist(e.touches[0], e.touches[1] || e.touches[0]) : dist0; h.onEnd({ ratio: d / dist0, remaining: rem }); reset(); } return; }
      var a = act[0], b = act[1];
      if (!armed) {
        if (!(movedEnough(a) && movedEnough(b))) return;
        armed = true; pinching = true; ids = [a.identifier, b.identifier];
        dist0 = dist(a, b);
        h.onStart({ dist0: dist0, mid0: mid(a, b), touches: [a, b] });
        return;
      }
      h.onMove({ ratio: dist(a, b) / dist0, mid: mid(a, b), touches: [a, b] });
    }, { passive: true });
    function end(e) {
      if (!armed) return;
      var act = activeTouches(e);
      if (act.length >= 2) return;
      var lastRatio = (e.changedTouches.length && e.touches.length) ? dist(e.changedTouches[0], e.touches[0]) / dist0 : NaN;
      h.onEnd({ ratio: isFinite(lastRatio) ? lastRatio : undefined, remaining: act.length === 1 ? act[0] : null });
      reset();
    }
    el.addEventListener('touchend', end, { passive: true });
    el.addEventListener('touchcancel', end, { passive: true });
  }

  // Native-zoom suppression (spec §5.1 items 2-4). All gated on the viewer
  // subtree and on an open book so the browse UI keeps accessibility zoom.
  function installNativeGuards(viewerEl, isActive) {
    function inViewer(e) { return !!(viewerEl && e.target && viewerEl.contains(e.target)) && isActive(); }
    ['gesturestart', 'gesturechange', 'gestureend'].forEach(function (t) {
      document.addEventListener(t, function (e) { if (inViewer(e) && e.cancelable) e.preventDefault(); }, { passive: false, capture: true });
    });
    viewerEl.addEventListener('touchmove', function (e) {
      if (e.touches.length === 2 && isActive() && e.cancelable) e.preventDefault();
    }, { passive: false });
    document.addEventListener('touchstart', function (e) {
      if (e.touches.length >= 2 && inViewer(e) && e.cancelable) e.preventDefault();
    }, { passive: false, capture: true });
  }

  window.ViewerGesture = {
    activeTouches: activeTouches, attach: attach, installNativeGuards: installNativeGuards,
    isPinching: function () { return pinching; },
    _debug: { palmIds: function () { return Object.keys(starts).filter(function (k) { return starts[k].palm; }).map(Number); } }
  };
  var viewer = document.getElementById('viewer');
  if (viewer) installNativeGuards(viewer, function () { return !!(window.images && window.images.length); });
})();
```

`onEnd` の `ratio` 計算は実装時に単純化してよい（zoom.js は最後の `onMove` の値を保持して使うので、`onEnd.ratio` は参考値）。

- [ ] **Step 4: index.html / sw.js / SW テストを更新**

`app/templates/index.html` の `<script src="/static/viewer/spread.js" defer></script>` の直後に:

```html
<script src="/static/viewer/gesture.js" defer></script>
<script src="/static/viewer/zoom.js" defer></script>
<script src="/static/viewer/overview.js" defer></script>
```

zoom.js / overview.js はこの時点で空ファイルとして作成（`/* placeholder: filled in Task 4 / Task 6 */`）。404 を出さないため。

`app/static/sw.js:9` の `SHELL_ASSETS` の `"/static/viewer/spread.js"` の後に `"/static/viewer/gesture.js", "/static/viewer/zoom.js", "/static/viewer/overview.js"` を追加。

`tests/test_sw_offline_contract.py:39` を `assert len(entries) == 19, ...` に変更。

- [ ] **Step 5: 実行して通ることを確認**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_viewer_gesture.py tests/test_sw_offline_contract.py -q`
Expected: all passed

- [ ] **Step 6: Commit**

```bash
git add app/static/viewer/gesture.js app/static/viewer/zoom.js app/static/viewer/overview.js app/templates/index.html app/static/sw.js tests/test_sw_offline_contract.py tests/test_viewer_gesture.py
git commit -S -m "feat(viewer): gesture.js — パーム除外つきピンチ認識とネイティブズーム封じ

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: `app.js` — identifier 追跡と `updateOrientation` ガード

**Files:**
- Modify: `app/static/app.js:760-790`（`updateOrientation`）, `app/static/app.js:2222-2350`（touch handlers）
- Test: `tests/test_viewer_gesture.py`

**Interfaces:**
- Consumes: `window.ViewerGesture.activeTouches(e)`
- Produces: app.js 内 `let dragTouchId = null;`、`let lastOrientW = -1, lastOrientH = -1;`。挙動は不変（scale 1.0）。

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_viewer_gesture.py` に追加:

```python
def test_swipe_survives_resting_palm_and_commits_on_swipe_finger_release(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 1, 5)  # start on page 2 so both directions exist
    # palm at bottom edge (id 2) stays put the whole time
    touch_start(touch, [{"x": 300, "y": 400, "id": 1}, {"x": 200, "y": 838, "id": 2}])
    for i in range(1, 7):
        touch_move(touch, [{"x": 300 - 20 * i, "y": 400, "id": 1}, {"x": 200, "y": 838, "id": 2}])
    # release the PALM first: must not commit anything
    touch_end(touch, [{"x": 300 - 120, "y": 400, "id": 1}])
    expect(ios_page.locator("#counter")).to_have_text("2 / 5")
    touch_move(touch, [{"x": 300 - 140, "y": 400, "id": 1}])
    touch_end(touch)  # swipe finger released → commit (finger moved left = previous page in RTL)
    expect(ios_page.locator("#counter")).to_have_text("1 / 5")


def test_initial_body_class_is_set_despite_orientation_guard(ios_page, ipad_landscape_page, server):
    assert ios_page.evaluate("document.body.className") == "portrait"
    assert ipad_landscape_page.evaluate("document.body.className") == "landscape"


def test_resize_without_dimension_change_does_not_call_snapTo(ios_page, server):
    open_book(ios_page, server, SERIES, 2, 5)
    ios_page.evaluate("window.__snaps = 0; const o = window.snapTo; window.snapTo = function(){ window.__snaps++; return o.apply(this, arguments); }")
    ios_page.evaluate("window.dispatchEvent(new Event('resize'))")
    assert ios_page.evaluate("window.__snaps") == 0
```

注: `touch_end(session, points)` の `points` は**離す点**（Chromium CDP の実装。実測: touchStart[1,2] → touchEnd[1] で changedTouches=[1], touches=[2]）。パームだけ離すなら `id: 2` を渡す。

- [ ] **Step 2: 実行して失敗を確認**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_viewer_gesture.py -q -k "palm_and_commits or orientation_guard or without_dimension"`
Expected: 1 番目は palm release がスワイプを確定させて FAIL、3 番目は `__snaps == 1` で FAIL、2 番目は現状 PASS（回帰検知用）

- [ ] **Step 3: `updateOrientation` にガードを入れる**

`app/static/app.js` の `function updateOrientation(){` 直前に `let lastOrientW = -1, lastOrientH = -1;` を追加し、関数先頭を:

```js
  function updateOrientation(){
    if (window.innerWidth === lastOrientW && window.innerHeight === lastOrientH) return; // keyboard/sidebar resize: nothing changed
    lastOrientW = window.innerWidth; lastOrientH = window.innerHeight;
    viewW = window.innerWidth; viewH = window.innerHeight;
```

- [ ] **Step 4: touch handlers を identifier 追跡に差し替える**

`app/static/app.js` の `let dragActive = false, ...` 行の直後に `let dragTouchId = null;` を追加。`touchstart` を:

```js
  imgContainer.addEventListener("touchstart", e => {
    if (!images.length) return;
    const act = window.ViewerGesture.activeTouches(e);
    if (act.length !== 1){ dragActive = false; dragTouchId = null; return; }
    const t0 = act[0];
    dragTouchId = t0.identifier;
    track.style.transition = "none";
    snapGen++; sliding = false;
    dragStartX = t0.clientX;
    dragStartY = t0.clientY;
    dragActive = true; dragDecided = false; dragPx = 0;
    viewerSwipeReady = false;
    viewerHapticFired = false;
    moveHistory = [{x: t0.clientX, t: performance.now()}];
  }, {passive: true});
```

`touchmove` の先頭 3 行を:

```js
  const findTouch = (list, id) => { for (let i = 0; i < list.length; i++) if (list[i].identifier === id) return list[i]; return null; };
  imgContainer.addEventListener("touchmove", e => {
    if (!dragActive) return;
    if (window.ViewerGesture.activeTouches(e).length !== 1) return;
    const t0 = findTouch(e.touches, dragTouchId);
    if (!t0) return;
    const dx = t0.clientX - dragStartX;
    const dy = t0.clientY - dragStartY;
```

以降の `e.touches[0].clientX` 参照（`moveHistory.push` の行）を `t0.clientX` に置換。

`touchend` の先頭を:

```js
  imgContainer.addEventListener("touchend", e => {
    if (!dragActive){ return; }
    const released = findTouch(e.changedTouches, dragTouchId);
    if (!released) return;               // a palm lifted, the swipe finger is still down
    dragActive = false; dragTouchId = null;
    if (!dragDecided) { moveHistory = []; return; }
    const t = released;
    const dxFinal = t.clientX - dragStartX;
```

`touchcancel` の先頭に同じ `released` チェックを入れ、`dragTouchId = null` を追加。

- [ ] **Step 5: 実行して通ることを確認**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_viewer_gesture.py tests/test_ui.py tests/test_ios.py -q`
Expected: all passed（既存のタップ・キー・スライダーテストが回帰していない）

- [ ] **Step 6: Commit**

```bash
git add app/static/app.js tests/test_viewer_gesture.py
git commit -S -m "refactor(viewer): スワイプ指を identifier で追跡、updateOrientation に寸法ガード

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: `#zoom-layer` と `viewer/zoom.js` 状態機械（拡大・スナップ・バウンス・reset）

**Files:**
- Create: `app/static/viewer/zoom.js`（Task 2 の placeholder を置換）
- Modify: `app/templates/index.html:118-124`, `app/static/style.css`, `app/static/app.js`（`snapTo` 先頭、touch early return、`_teardownViewer` は hash.js）, `app/static/browse/hash.js:215`
- Test: `tests/test_viewer_zoom.py`

**Interfaces:**
- Consumes: `ViewerGesture.attach`, `window.viewerSnapTo`（**`window.snapTo` は spread.js の休眠ツイン。app.js のローカル snapTo を Task 4 で `window.viewerSnapTo` として公開する**）, `window.currentIdx`, `window.spreads`, `window.anchorSpread`, `window.SpreadView.spreadMetrics(s) -> {w, h}`（実装時に戻り値のキー名を確認して合わせる）, `window.viewW/viewH`
- Produces `window.Zoom`:
  - `state() -> {zoom: number, tx: number, ty: number, band: 'rest'|'zoomed'|'shrink'}`
  - `isActive() -> boolean`（`zoom > 1`）
  - `reset(animate: boolean) -> void`
  - `onOverviewFire: (() => void) | null` — Task 7 で overview.js が設定する。`target <= 0.5` で 1 回呼ぶ
  - `driftTarget: (() => {x, y} | null) | null` — Task 7 で overview.js が設定する。縮小帯のドリフト先
  - 定数を `Zoom.K = {MIN, MAX, FIRE, REARM, SNAP, BOUNCE_MS}` で公開

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_viewer_zoom.py`:

```python
"""viewer/zoom.js — absolute zoom state machine, anchor, pan, edge turn, reset."""
import pytest
from playwright.sync_api import expect

from viewer_touch import open_book, touch_session, pinch, drag, tap, two, touch_move, touch_end, zoom_state

SERIES = "zoom_series/series/ep-1"
CX, CY = 195, 420


@pytest.fixture
def touch(ios_page):
    s = touch_session(ios_page)
    yield s
    s.detach()


def _wait_zoom(page, pred):
    page.wait_for_function(f"(() => {{ const z = window.Zoom.state(); return {pred}; }})()")


def test_zoom_layer_wraps_track_and_zones_are_outside(ios_page, server):
    open_book(ios_page, server, SERIES, 0, 5)
    assert ios_page.evaluate("document.querySelector('#img-track').parentElement.id") == "zoom-layer"
    assert ios_page.evaluate("document.querySelector('#zone-prev').parentElement.id") == "img-container"


def test_pinch_out_zooms_to_2x_and_persists(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 0, 5)
    pinch(touch, CX, CY, [40, 50, 60, 70, 80])  # ratio 2.0
    _wait_zoom(ios_page, "Math.abs(z.zoom - 2) < 0.05 && z.band === 'zoomed'")
    assert ios_page.evaluate("document.body.classList.contains('zoomed')")
    tf = ios_page.evaluate("getComputedStyle(document.querySelector('#zoom-layer')).transform")
    assert tf != "none"


def test_midpoint_anchor_keeps_point_under_fingers(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 0, 5)
    # The image point under (CX, CY) at rest must stay under (CX, CY) after a centered pinch.
    before = ios_page.evaluate(f"(() => {{ const el = document.querySelector('#img-track'); const r = el.getBoundingClientRect(); return {{x: ({CX} - r.left), y: ({CY} - r.top)}}; }})()")
    pinch(touch, CX, CY, [40, 60, 80])
    _wait_zoom(ios_page, "z.zoom > 1.9")
    after = ios_page.evaluate(f"(() => {{ const el = document.querySelector('#img-track'); const r = el.getBoundingClientRect(); const z = window.Zoom.state().zoom; return {{x: ({CX} - r.left) / z, y: ({CY} - r.top) / z}}; }})()")
    assert abs(after["x"] - before["x"]) < 3 and abs(after["y"] - before["y"]) < 3


def test_release_between_1_and_1_15_snaps_to_rest(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 0, 5)
    pinch(touch, CX, CY, [50, 54, 55])  # ratio 1.10
    _wait_zoom(ios_page, "z.zoom === 1 && z.band === 'rest'")


def test_shrink_band_release_bounces_back(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 0, 5)
    pinch(touch, CX, CY, [50, 40, 35])  # ratio 0.70: shrink band, above FIRE
    _wait_zoom(ios_page, "z.zoom === 1 && z.band === 'rest'")
    assert ios_page.evaluate("window.Overview ? window.Overview.isOpen() : false") is False


def test_zoom_in_from_2x_walks_through_1_without_firing_overview(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 0, 5)
    ios_page.evaluate("window.__fired = 0; window.Zoom.onOverviewFire = () => { window.__fired++; }")
    pinch(touch, CX, CY, [40, 60, 80])           # → 2.0
    _wait_zoom(ios_page, "z.zoom > 1.9")
    pinch(touch, CX, CY, [80, 60, 44])           # 2.0 × (44/80) = 1.10 → snaps to 1
    _wait_zoom(ios_page, "z.zoom === 1")
    assert ios_page.evaluate("window.__fired") == 0


def test_page_turn_resets_zoom_via_snapTo(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 0, 5)
    pinch(touch, CX, CY, [40, 60, 80])
    _wait_zoom(ios_page, "z.zoom > 1.9")
    ios_page.keyboard.press("ArrowLeft")  # next page
    expect(ios_page.locator("#counter")).to_have_text("2 / 5")
    _wait_zoom(ios_page, "z.zoom === 1")


def test_back_to_top_resets_zoom(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 0, 5)
    pinch(touch, CX, CY, [40, 60, 80])
    _wait_zoom(ios_page, "z.zoom > 1.9")
    ios_page.go_back()
    _wait_zoom(ios_page, "z.zoom === 1")
    assert ios_page.evaluate("document.body.classList.contains('zoomed')") is False


def test_new_gesture_clears_bounce_transition(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 0, 5)
    pinch(touch, CX, CY, [50, 40, 35])  # bounce begins
    pinch(touch, CX, CY, [40, 60], hold=True)
    assert ios_page.evaluate("document.querySelector('#zoom-layer').style.transition") in ("", "none")
    touch_end(touch)
```

- [ ] **Step 2: 実行して失敗を確認**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_viewer_zoom.py -q`
Expected: FAIL（`#zoom-layer` 不在、`window.Zoom` 未定義）

- [ ] **Step 3: index.html と CSS**

`app/templates/index.html` の `#img-container` 内を:

```html
  <div id="img-container">
    <div id="spinner"></div>
    <div id="zoom-layer"><div id="img-track"></div></div>
    <div id="zone-prev" title="Previous"></div>
    <div id="zone-toggle" title="Toggle toolbar"></div>
    <div id="zone-next" title="Next"></div>
  </div>
```

`app/static/style.css` の `#img-track{...}`（144 行付近）の直前に:

```css
#zoom-layer{position:absolute;inset:0;transform-origin:0 0;transition:none}
body.zoomed #zoom-layer{will-change:transform}
body.zoomed #zone-prev,body.zoomed #zone-next{pointer-events:none}
body.zoomed #zone-toggle{left:40%;width:20%}
body.zoomed #hint{display:none}
#img-container,#img-track,#img-track *,#zone-prev,#zone-next,#zone-toggle,#hint,#spread-controls,#spread-controls *,#empty{touch-action:none}
#toolbar #menu-btn,#toolbar #folder-name,#toolbar #counter{touch-action:none}
#page-slider-wrap,#page-slider{touch-action:pan-x}
```

reduced-motion ブロック（`@media (prefers-reduced-motion:reduce){` 内）の `#img-track{transition-duration:0.01ms !important}` の隣に `#zoom-layer{transition-duration:0.01ms !important}` を追加。

- [ ] **Step 4: zoom.js を実装（拡大・スナップ・バウンス・reset。パン/端越えは Task 5）**

`app/static/viewer/zoom.js`:

```js
/* bookq viewer/zoom — absolute zoom state machine on #zoom-layer.
 * Spec §5.2. Never touches #img-track's transition/transform.
 * target = startZoom * ratio. Bands (absolute): >1 zoomed (midpoint anchor),
 * 0.5<t<1 shrink (scale follows, position drifts to the overview cell),
 * <=0.5 fire overview once (re-arm above 0.7 while fingers stay down).
 */
(function () {
  'use strict';
  var K = { MIN: 0.35, MAX: 4, FIRE: 0.5, REARM: 0.7, SNAP: 1.15, BOUNCE_MS: 190, SLIDE_MS: 300, EASE: 'cubic-bezier(0.25,0.1,0.25,1.0)' };
  var layer = null, zoom = 1, tx = 0, ty = 0, band = 'rest';
  var startZoom = 1, startTx = 0, startTy = 0, mid0 = null, fired = false, bounceTimer = 0;
  var driftFrom = null;

  function el() { return layer || (layer = document.getElementById('zoom-layer')); }
  function apply(s, x, y) {
    zoom = s; tx = x; ty = y;
    band = s > 1 ? 'zoomed' : (s < 1 ? 'shrink' : 'rest');
    var L = el(); if (!L) return;
    L.style.transform = (s === 1 && x === 0 && y === 0) ? '' : 'translate(' + x + 'px,' + y + 'px) scale(' + s + ')';
    document.body.classList.toggle('zoomed', s > 1);
  }
  function killTransition() { var L = el(); if (L) L.style.transition = ''; if (bounceTimer) { clearTimeout(bounceTimer); bounceTimer = 0; } }
  function reducedMotion() { try { return window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (e) { return false; } }

  function reset(animate) {
    killTransition();
    driftFrom = null;
    var L = el();
    if (animate && !reducedMotion() && L && zoom !== 1) {
      L.style.transition = 'transform ' + K.SLIDE_MS + 'ms ' + K.EASE;
      apply(1, 0, 0);
      bounceTimer = setTimeout(function () { bounceTimer = 0; if (L) L.style.transition = ''; }, K.SLIDE_MS + 50);
    } else {
      apply(1, 0, 0);
    }
  }
  function bounceBack() {
    var L = el();
    if (!L || reducedMotion()) { apply(1, 0, 0); return; }
    L.style.transition = 'transform ' + K.BOUNCE_MS + 'ms ease';
    apply(1, 0, 0);
    bounceTimer = setTimeout(function () { bounceTimer = 0; L.style.transition = ''; }, K.BOUNCE_MS + 70);
  }
  function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }

  function onStart(g) {
    if (window.Overview && window.Overview.isOpen()) return;
    killTransition();
    if (zoom === 1 && window.sliding && window.viewerSnapTo) window.viewerSnapTo(window.currentIdx || 0, false); // reclaim an interrupted slide (app.js local snapTo; window.snapTo is the dormant twin)
    document.body.classList.add('zoomed'); // promote the compositing layer before the first tracked frame
    startZoom = zoom; startTx = tx; startTy = ty; mid0 = g.mid0; fired = false; driftFrom = null;
  }
  function onMove(g) {
    if (window.Overview && window.Overview.isOpen()) return;
    var target = clamp(startZoom * g.ratio, K.MIN, K.MAX);
    if (!fired && target <= K.FIRE) {
      fired = true;
      if (window.Zoom.onOverviewFire) window.Zoom.onOverviewFire();
      return;
    } else if (fired && target > K.REARM) { fired = false; }
    if (target >= 1) {
      // midpoint anchor: the layer point under mid0 at gesture start stays under mid
      var k = target / startZoom;
      var nx = g.mid.x - k * (mid0.x - startTx);
      var ny = g.mid.y - k * (mid0.y - startTy);
      apply(target, nx, ny);
      driftFrom = null;
    } else if (target > K.FIRE) {
      // shrink band: scale follows fingers, center drifts toward the overview cell
      var W = window.viewW || window.innerWidth, H = window.viewH || window.innerHeight;
      var c0 = { x: W / 2, y: H / 2 };
      var dest = (!reducedMotion() && window.Zoom.driftTarget) ? window.Zoom.driftTarget() : null;
      var p = clamp((1 - target) / 0.5, 0, 1);
      var cx = dest ? c0.x + p * (dest.x - c0.x) : c0.x;
      var cy = dest ? c0.y + p * (dest.y - c0.y) : c0.y;
      // layer is inset:0 → its natural center is (W/2, H/2); with origin 0 0: center = t + s*(W/2,H/2)
      apply(target, cx - target * W / 2, cy - target * H / 2);
    }
  }
  function onEnd(g) {
    if (window.Overview && window.Overview.isOpen()) { fired = false; return; }
    fired = false;
    if (zoom > K.SNAP) { /* persist */ }
    else if (zoom > 1) { apply(1, 0, 0); }
    else if (zoom < 1) { bounceBack(); }
    if (window.Zoom.onPinchEnd) window.Zoom.onPinchEnd(g);
  }

  window.Zoom = {
    K: K,
    state: function () { return { zoom: zoom, tx: tx, ty: ty, band: band }; },
    isActive: function () { return zoom > 1; },
    reset: reset,
    onOverviewFire: null, driftTarget: null, onPinchEnd: null,
    _apply: apply
  };
  var container = document.getElementById('img-container');
  if (container && window.ViewerGesture) window.ViewerGesture.attach(container, { onStart: onStart, onMove: onMove, onEnd: onEnd });
})();
```

**確認済み**: `sliding` は app.js のローカルで window に未公開。app.js の `let sliding = ...` 宣言直後に `Object.defineProperty(window, 'sliding', {get(){return sliding;}, set(v){sliding=v;}, configurable:true});` を追加する（既存の `images`/`currentIdx` と同じパターン）。`onStart` の回収は **`zoom === 1 && window.sliding` のときだけ** `snapTo(currentIdx,false)` を呼ぶ。無条件に呼ぶと `snapTo` 先頭の `Zoom.reset(true)` が 2x からの再ピンチをリセットしてしまう。`spreadMetrics` の戻り値は `{width,height,pageWidths}`（確認済み）、`window.stripImgs` は spread.js が公開している（確認済み）。

- [ ] **Step 5: app.js と hash.js のフック**

`app/static/app.js` の `snapTo` 定義の直後に `try { window.viewerSnapTo = snapTo; } catch(e) { console.warn(e); }` を追加し、`let sliding` 宣言直後に `Object.defineProperty(window, 'sliding', {get(){return sliding;}, set(v){sliding=v;}, configurable:true});` を追加する。`snapTo` 先頭（`if (idx < 0 || idx >= images.length) return;` の直後）に:

```js
    if (window.Zoom && window.Zoom.isActive()) window.Zoom.reset(true);
```

既存 `touchstart`/`touchmove`/`touchend`/`touchcancel` の各先頭（`if (!images.length) return;` の直後、他は最初の行）に:

```js
    if (window.Zoom && window.Zoom.isActive()) return; // zoomed: zoom.js owns 1-finger pan (Task 5)
```

`app/static/browse/hash.js` の `_teardownViewer` 先頭（`if (typeof window.teardownViewer === 'function')` の**前**）に:

```js
    try { if (window.Overview) window.Overview.close(); if (window.Zoom) window.Zoom.reset(false); } catch (e) { console.warn(e); }
```

- [ ] **Step 6: 実行して通ることを確認**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_viewer_zoom.py tests/test_ui.py tests/test_url_state.py -q`
Expected: all passed。`test_strip_keeps_only_a_window_of_page_cells` 等の DOM セル数テストが `#zoom-layer` 追加で影響を受けていないこと（セレクタが `#img-track .page-cell` なら無影響）

- [ ] **Step 7: Commit**

```bash
git add app/static/viewer/zoom.js app/templates/index.html app/static/style.css app/static/app.js app/static/browse/hash.js tests/test_viewer_zoom.py
git commit -S -m "feat(viewer): zoom.js — #zoom-layer への絶対ズーム状態機械（拡大・スナップ・バウンス・reset）

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: 拡大中のパン・clamp・端越えページ送り・中央ゾーン

**Files:**
- Modify: `app/static/viewer/zoom.js`, `app/static/app.js`（`zoneToggle` click、`suppressClickFor` 公開）
- Test: `tests/test_viewer_zoom.py`

**Interfaces:**
- Consumes: `window.SpreadView.spreadMetrics(window.anchorSpread)`（現在スプレッドの `{w,h}` px。キー名は実装時に確認）、`window.goNext({crossChapter:true})`/`window.goPrev` は app.js 内ローカルなので **`window.viewerGoNext`/`window.viewerGoPrev` として app.js が公開**する（`goNext({crossChapter:true})` を包む 1 行ずつ）。`window.suppressClickFor(ms)`。
- Produces: `Zoom.state()` に `edgeArmed: boolean` を追加。`Zoom.panStartFrom(touch)`（gesture の `onEnd.remaining` から呼ぶ）。

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_viewer_zoom.py` に追加:

```python
def _zoom_to_2x(page, touch):
    pinch(touch, CX, CY, [40, 60, 80])
    _wait_zoom(page, "z.zoom > 1.9")


def test_pan_moves_layer_and_clamps_to_spread(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 1, 5)
    _zoom_to_2x(ios_page, touch)
    before = zoom_state(ios_page)
    drag(touch, 300, 500, 100, 300)  # pan left/up
    after = zoom_state(ios_page)
    assert after["tx"] < before["tx"] and after["ty"] < before["ty"]
    # clamp: spread rect × zoom keeps covering the viewport
    covers = ios_page.evaluate("""(() => {
      const r = document.querySelector('#img-track .spread-container').getBoundingClientRect();
      return r.left <= 0.5 && r.top <= 0.5 && r.right >= window.innerWidth - 0.5 && r.bottom >= window.innerHeight - 0.5;
    })()""")
    assert covers
    expect(ios_page.locator("#counter")).to_have_text("2 / 5")  # no page turn


def test_left_zone_tap_does_not_turn_page_while_zoomed(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 1, 5)
    _zoom_to_2x(ios_page, touch)
    tap(touch, 60, 700)
    ios_page.wait_for_timeout(150)
    expect(ios_page.locator("#counter")).to_have_text("2 / 5")
    assert zoom_state(ios_page)["zoom"] > 1.9


def test_center_zone_tap_resets_zoom_and_keeps_toolbar(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 1, 5)
    hidden_before = ios_page.evaluate("document.querySelector('#toolbar').classList.contains('hidden')")
    _zoom_to_2x(ios_page, touch)
    tap(touch, 195, 700)
    _wait_zoom(ios_page, "z.zoom === 1")
    assert ios_page.evaluate("document.querySelector('#toolbar').classList.contains('hidden')") == hidden_before


def test_edge_turn_fires_only_when_drag_starts_with_no_room(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 1, 5)
    _zoom_to_2x(ios_page, touch)
    # push the image fully to the RIGHT so its LEFT edge is at the viewport edge (no room to pan right)
    for _ in range(4):
        drag(touch, 60, 500, 380, 500)
    # drag 1: started with room? (already clamped → no room) → 80px right → goNext
    ios_page.evaluate("window.__vib = 0; navigator.vibrate = () => { window.__vib++; return true; }")
    drag(touch, 100, 500, 220, 500)  # 120px right
    expect(ios_page.locator("#counter")).to_have_text("3 / 5")
    _wait_zoom(ios_page, "z.zoom === 1")
    assert ios_page.evaluate("window.__vib") == 1


def test_edge_turn_does_not_fire_from_inertia_within_one_drag(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 1, 5)
    _zoom_to_2x(ios_page, touch)
    # one long drag that reaches the clamp mid-way and keeps going 200px: must NOT turn
    drag(touch, 40, 500, 380, 500, steps=12)
    ios_page.wait_for_timeout(150)
    expect(ios_page.locator("#counter")).to_have_text("2 / 5")


def test_landscape_single_page_zoomed_can_still_turn(ipad_landscape_page, server):
    page = ipad_landscape_page
    open_book(page, server, SERIES, 1, 5)
    page.locator("#spread-toggle").click()  # 1P
    page.wait_for_function("document.body.className === 'landscape-single'")
    s = touch_session(page)
    pinch(s, 590, 410, [50, 60, 75])  # 1.5x — width still < viewW, no horizontal room
    page.wait_for_function("window.Zoom.state().zoom > 1.4")
    drag(s, 400, 400, 520, 400)  # 120px right → next page
    expect(page.locator("#counter")).to_have_text("3 / 5")
    s.detach()


def test_remaining_finger_pans_without_new_touchstart(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 1, 5)
    pinch(touch, CX, CY, [40, 60, 80], hold=True)
    _wait_zoom(ios_page, "z.zoom > 1.9")
    touch_end(touch, [{"x": CX + 80, "y": CY, "id": 2}])  # lift finger 1, finger 2 stays
    before = zoom_state(ios_page)
    touch_move(touch, [{"x": CX + 80 - 60, "y": CY - 40, "id": 2}])
    touch_move(touch, [{"x": CX + 80 - 120, "y": CY - 80, "id": 2}])
    after = zoom_state(ios_page)
    touch_end(touch)
    assert after["tx"] != before["tx"] or after["ty"] != before["ty"]


def test_pan_end_suppresses_click_for_400ms(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 1, 5)
    _zoom_to_2x(ios_page, touch)
    drag(touch, 300, 500, 250, 450)
    assert ios_page.evaluate("window.__suppressClickActive()") is True
```

`window.__suppressClickActive` はテスト用に app.js が `window.__BOOKQ_TEST_MODE__==='true'` のとき公開する（`() => suppressClick`）。

- [ ] **Step 2: 実行して失敗を確認**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_viewer_zoom.py -q -k "pan or zone or edge or landscape or remaining or suppress"`
Expected: FAIL

- [ ] **Step 3: app.js 側の公開と中央ゾーン**

`app/static/app.js` の `suppressClickFor` 定義直後に:

```js
  try { window.suppressClickFor = suppressClickFor; window.viewerGoNext = () => goNext({crossChapter:true}); window.viewerGoPrev = () => goPrev({crossChapter:true}); if (window.__BOOKQ_TEST_MODE__ === 'true') window.__suppressClickActive = () => suppressClick; } catch(e) { console.warn(e); }
```

`zoneToggle` の click を:

```js
  zoneToggle.addEventListener("click", e => {
    if (window.Zoom && window.Zoom.isActive()) { e.stopPropagation(); window.Zoom.reset(true); return; } // zoomed: center tap = back to 1.0, ignores suppressClick
    if (suppressClick) return; e.stopPropagation(); toolbar.classList.toggle("hidden"); spreadControls.classList.toggle("hidden");
  });
```

- [ ] **Step 4: zoom.js にパン・clamp・端越えを追加**

`zoom.js` の `onEnd` の前に追加し、`onEnd` から `panStartFrom(g.remaining)` を呼ぶ:

```js
  // ── 1-finger pan while zoomed (spec §5.2) ──
  var EDGE_PX = 80, RUBBER = 0.35;
  var pan = null; // {id, x0, y0, tx0, ty0, noRoomX: -1|0|1, armedDir: 0|±1, decided, vibrated}

  function spreadBox() {
    var s = window.anchorSpread || 0;
    var m = window.SpreadView && window.SpreadView.spreadMetrics ? window.SpreadView.spreadMetrics(s) : null;
    var W = window.viewW || window.innerWidth, H = window.viewH || window.innerHeight;
    var w = m ? (m.w || m.width) : W, h = m ? (m.h || m.height) : H;
    // spread is centred in the layer at rest: left/top offset inside the unscaled layer
    return { w: w, h: h, x: (W - w) / 2, y: (H - h) / 2, W: W, H: H };
  }
  // clamp so that the scaled spread keeps covering the viewport; axes where it
  // cannot cover are centred (= "no room")
  function clampPan(nx, ny) {
    var b = spreadBox(), s = zoom;
    var sw = b.w * s, sh = b.h * s;
    var out = { x: nx, y: ny, roomX: true, roomY: true };
    if (sw <= b.W) { out.x = (b.W - sw) / 2 - b.x * s; out.roomX = false; }
    else { var minX = b.W - (b.x * s + sw), maxX = -b.x * s; out.x = clamp(nx, minX, maxX); }
    if (sh <= b.H) { out.y = (b.H - sh) / 2 - b.y * s; out.roomY = false; }
    else { var minY = b.H - (b.y * s + sh), maxY = -b.y * s; out.y = clamp(ny, minY, maxY); }
    return out;
  }
  function noRoomDirAtStart() {
    // -1: cannot pan further toward +x (image left edge is at/inside viewport left) → finger→right turns NEXT
    // +1: cannot pan toward -x → finger→left turns PREV. 0: room both ways.
    var b = spreadBox(), s = zoom, sw = b.w * s;
    if (sw <= b.W) return 2; // centred axis: both directions count
    var maxX = -b.x * s, minX = b.W - (b.x * s + sw);
    var eps = 0.5;
    if (tx >= maxX - eps) return -1;
    if (tx <= minX + eps) return 1;
    return 0;
  }
  function panStartFrom(t) {
    if (!t || zoom <= 1) return;
    pan = { id: t.identifier, x0: t.clientX, y0: t.clientY, tx0: tx, ty0: ty, noRoom: noRoomDirAtStart(), decided: false, horizontal: false, vibrated: false, armed: false };
  }
  function findTouch(list, id) { for (var i = 0; i < list.length; i++) if (list[i].identifier === id) return list[i]; return null; }
  var container = document.getElementById('img-container');
  if (container) {
    container.addEventListener('touchstart', function (e) {
      if (zoom <= 1 || window.ViewerGesture.isPinching()) return;
      var act = window.ViewerGesture.activeTouches(e);
      if (act.length !== 1) { pan = null; return; }
      killTransition();
      panStartFrom(act[0]);
    }, { passive: true });
    container.addEventListener('touchmove', function (e) {
      if (!pan || zoom <= 1) return;
      if (window.ViewerGesture.activeTouches(e).length !== 1) { pan = null; return; }
      var t = findTouch(e.touches, pan.id); if (!t) return;
      var dx = t.clientX - pan.x0, dy = t.clientY - pan.y0;
      if (!pan.decided) { if (Math.abs(dx) < 8 && Math.abs(dy) < 8) return; pan.decided = true; pan.horizontal = Math.abs(dx) > Math.abs(dy); }
      var c = clampPan(pan.tx0 + dx, pan.ty0 + dy);
      var edgeDir = 0; // +1 finger→right with no room → NEXT ; -1 finger→left with no room → PREV
      if (pan.horizontal && (pan.noRoom === 2 || (pan.noRoom === -1 && dx > 0) || (pan.noRoom === 1 && dx < 0))) edgeDir = dx > 0 ? 1 : -1;
      if (edgeDir !== 0) {
        var over = Math.abs(dx) * RUBBER;
        c.x += Math.sign(dx) * over; // rubber band past the clamp
        if (!pan.vibrated && Math.abs(dx) >= EDGE_PX) { pan.vibrated = true; pan.armed = true; pan.armedDir = edgeDir; try { if (!reducedMotion() && 'vibrate' in navigator) navigator.vibrate(10); } catch (err) {} }
        if (Math.abs(dx) < EDGE_PX) pan.armed = false;
      }
      apply(zoom, c.x, c.y);
    }, { passive: true });
    function panEnd(e) {
      if (!pan) return;
      var released = findTouch(e.changedTouches, pan.id); if (!released) return;
      var p = pan; pan = null;
      if (window.suppressClickFor) window.suppressClickFor(400);
      if (p.armed) { if (p.armedDir > 0 && window.viewerGoNext) window.viewerGoNext(); else if (p.armedDir < 0 && window.viewerGoPrev) window.viewerGoPrev(); return; }
      var c = clampPan(tx, ty); apply(zoom, c.x, c.y); // settle the rubber band
    }
    container.addEventListener('touchend', panEnd, { passive: true });
    container.addEventListener('touchcancel', panEnd, { passive: true });
  }
```

`onEnd` の末尾に `panStartFrom(g.remaining);` を追加。`state()` に `edgeArmed: !!(pan && pan.armed)` を追加。`onMove` の拡大帯の `apply` を `var c = clampPan(nx, ny); apply(target, c.x, c.y);` に置き換える（拡大中も clamp）。

`spreadMetrics` の戻り値キーが `{w,h}` でない場合は `spreadBox()` を合わせる。`spreadBox().x/y`（rest 時のスプレッド位置）は `spread.js` の配置（`bottom:0` / portrait は縦中央）と一致させる。portrait では縦中央、landscape では下寄せなので `y` は `document.body.classList.contains('portrait') ? (H-h)/2 : H-h`。

- [ ] **Step 5: 実行して通ることを確認**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_viewer_zoom.py tests/test_ui.py tests/test_ios.py -q`
Expected: all passed

- [ ] **Step 6: Commit**

```bash
git add app/static/viewer/zoom.js app/static/app.js tests/test_viewer_zoom.py
git commit -S -m "feat(viewer): 拡大中のパン・clamp・端越えページ送り・中央タップで 1.0 復帰

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: `viewer/overview.js` — グリッド構築・開閉・重なり・ウィンドウイング

**Files:**
- Create: `app/static/viewer/overview.js`（placeholder を置換）
- Modify: `app/templates/index.html`（`#overview`, `#overview-btn`）, `app/static/style.css`, `app/static/app.js`（`G` キー、`#menu-btn`、`loadImages` 後 prebuild）
- Test: `tests/test_viewer_overview.py`

**Interfaces:**
- Consumes: `window.images`, `window.chapterList`, `window.chapterBoundaries`, `window.currentIdx`, `window.getCurrentChapterInfo(idx)`, `window.OfflineCore.thumbUrl/imageUrl`, `window.viewerSnapTo`（app.js ローカル。`window.snapTo` は使わない）, `window.suppressClickFor`, `window.closeSidebar`
- Produces `window.Overview`:
  - `open() -> void`（Task 7 で FLIP を付ける。ここでは即時表示）
  - `close() -> void`（クローン回収含む）
  - `isOpen() -> boolean`
  - `prebuild() -> void`（アイドル時に構築。`opacity:0; pointer-events:none`、`hidden` なし）
  - `cellCenter(gidx) -> {x, y} | null`（先行構築完了時にキャッシュした `.cur` 中心。Task 7 の `Zoom.driftTarget` 用）
  - `relayout() -> void`（ResizeObserver から）
  - `_debug = {imgWindow: () => {lo, hi}}`

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_viewer_overview.py`:

```python
"""viewer/overview.js — grid, entry/exit, stacking, windowing."""
import pytest
from playwright.sync_api import expect

from viewer_touch import open_book, touch_session, pinch, tap, drag, zoom_state, touch_start, touch_move, touch_end, two

SERIES = "zoom_series/series/ep-1"
CX, CY = 195, 420


@pytest.fixture
def touch(ios_page):
    s = touch_session(ios_page)
    yield s
    s.detach()


def _open_overview(page):
    page.keyboard.press("g")
    page.wait_for_function("window.Overview.isOpen()")


def test_g_opens_overview_with_sections_rtl_and_current_at_third(ios_page, server):
    open_book(ios_page, server, "zoom_series/series/ep-2", 1, 4)  # global idx: 5 pages + spacer? → use data attr
    _open_overview(ios_page)
    assert ios_page.locator("#overview .ov-chapter").count() == 3
    assert ios_page.locator("#overview .ov-chapter .ov-heading").all_text_contents() == ["ep-1", "ep-2", "ep-3"]
    assert ios_page.locator("#overview .ov-cell").count() == 16  # spacers dropped
    # RTL: first cell of the first section is at the RIGHT edge
    first = ios_page.locator("#overview .ov-chapter:nth-child(1) .ov-cell").first.bounding_box()
    second = ios_page.locator("#overview .ov-chapter:nth-child(1) .ov-cell").nth(1).bounding_box()
    assert first["x"] > second["x"]
    # all cells 3:4
    ratios = ios_page.evaluate("Array.from(document.querySelectorAll('.ov-cell')).map(c => { const r = c.getBoundingClientRect(); return r.width / r.height; })")
    assert all(abs(r - 0.75) < 0.02 for r in ratios)
    # current cell highlighted and positioned around the upper third
    cur = ios_page.locator("#overview .ov-cell.cur")
    assert cur.count() == 1
    box = cur.bounding_box()
    vh = ios_page.evaluate("window.innerHeight")
    assert vh * 0.15 < box["y"] + box["height"] / 2 < vh * 0.5
    assert "ep-2" in ios_page.locator("#overview .ov-chip").text_content()


def test_cells_use_thumb_url_and_fall_back_to_image(ios_page, server):
    open_book(ios_page, server, SERIES, 0, 5)
    _open_overview(ios_page)
    src = ios_page.locator("#overview .ov-cell img").first.get_attribute("src") or ios_page.locator("#overview .ov-cell img").first.get_attribute("data-src")
    assert "/thumb/" in src and "w=400" in src
    fallback = ios_page.evaluate("""(() => { const img = document.querySelector('#overview .ov-cell img'); img.dispatchEvent(new Event('error')); return img.getAttribute('src'); })()""")
    assert "/image/" in fallback


def test_overview_sits_above_toolbar_and_slider_is_inert(ios_page, server):
    open_book(ios_page, server, SERIES, 2, 5)
    _open_overview(ios_page)
    z_ov = int(ios_page.evaluate("getComputedStyle(document.querySelector('#overview')).zIndex"))
    assert z_ov > 10
    assert ios_page.evaluate("getComputedStyle(document.querySelector('#toolbar')).pointerEvents") == "none"
    idx = ios_page.evaluate("window.currentIdx")
    # click where the slider is: must not change the page
    box = ios_page.locator("#page-slider").bounding_box()
    ios_page.mouse.click(box["x"] + 10, box["y"] + box["height"] / 2)
    assert ios_page.evaluate("window.currentIdx") == idx


def test_close_paths_esc_button_and_g_toggle(ios_page, server):
    open_book(ios_page, server, SERIES, 0, 5)
    _open_overview(ios_page)
    ios_page.keyboard.press("Escape")
    ios_page.wait_for_function("!window.Overview.isOpen()")
    expect(ios_page.locator("#sidebar")).not_to_have_class(lambda c: "open" in c)  # Esc did not reach the sidebar handler
    _open_overview(ios_page)
    ios_page.locator("#overview-close").click()
    ios_page.wait_for_function("!window.Overview.isOpen()")
    _open_overview(ios_page)
    ios_page.keyboard.press("g")
    ios_page.wait_for_function("!window.Overview.isOpen()")
    # Cmd+G ignored
    ios_page.keyboard.press("Meta+g")
    assert ios_page.evaluate("window.Overview.isOpen()") is False


def test_tap_cell_jumps_to_page_and_closes(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 0, 5)
    _open_overview(ios_page)
    cell = ios_page.locator('#overview .ov-cell[data-gidx]').nth(9)  # somewhere in ep-2/ep-3
    gidx = int(cell.get_attribute("data-gidx"))
    cell.scroll_into_view_if_needed()
    b = cell.bounding_box()
    tap(touch, b["x"] + b["width"] / 2, b["y"] + b["height"] / 2)
    ios_page.wait_for_function("!window.Overview.isOpen()")
    assert ios_page.evaluate("window.currentIdx") == gidx
    assert ios_page.evaluate("window.__suppressClickActive()") is True


def test_tap_during_scroll_inertia_does_not_open(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 0, 5)
    _open_overview(ios_page)
    ios_page.evaluate("document.querySelector('#overview').dispatchEvent(new Event('scroll'))")
    cell = ios_page.locator('#overview .ov-cell').nth(3)
    b = cell.bounding_box()
    tap(touch, b["x"] + b["width"] / 2, b["y"] + b["height"] / 2)
    ios_page.wait_for_timeout(50)
    assert ios_page.evaluate("window.Overview.isOpen()") is True


def test_menu_button_closes_overview_first(ios_page, server):
    open_book(ios_page, server, SERIES, 0, 5)
    _open_overview(ios_page)
    ios_page.evaluate("document.querySelector('#menu-btn').click()")
    ios_page.wait_for_function("!window.Overview.isOpen()")


def test_back_closes_overview(ios_page, server):
    open_book(ios_page, server, SERIES, 0, 5)
    _open_overview(ios_page)
    ios_page.go_back()
    ios_page.wait_for_function("!window.Overview.isOpen()")
    assert ios_page.evaluate("document.body.classList.contains('overview-open')") is False


def test_g_is_noop_without_a_book(ios_page, server):
    ios_page.keyboard.press("g")
    assert ios_page.evaluate("window.Overview.isOpen()") is False


def test_ipad_landscape_has_seven_columns(ipad_landscape_page, server):
    page = ipad_landscape_page
    open_book(page, server, SERIES, 0, 5)
    page.keyboard.press("g")
    page.wait_for_function("window.Overview.isOpen()")
    cols = page.evaluate("getComputedStyle(document.querySelector('#overview .ov-grid')).gridTemplateColumns.split(' ').length")
    assert cols == 7


def test_relayout_on_viewer_width_change(ipad_landscape_page, server):
    page = ipad_landscape_page
    open_book(page, server, SERIES, 0, 5)
    page.keyboard.press("g")
    page.wait_for_function("window.Overview.isOpen()")
    h1 = page.locator("#overview .ov-cell").first.bounding_box()["height"]
    page.set_viewport_size({"width": 820, "height": 1180})
    page.wait_for_function(f"document.querySelector('#overview .ov-cell').getBoundingClientRect().height !== {h1}")


def test_huge_book_windows_imgs_but_keeps_full_scroll(ios_page, server):
    open_book(ios_page, server, "huge/book/part2", 100, 351)
    _open_overview(ios_page)
    assert ios_page.locator("#overview .ov-cell").count() == 701
    with_img = ios_page.locator("#overview .ov-cell img").count()
    assert 380 <= with_img <= 420  # ±200 around current, clipped by ends
    win = ios_page.evaluate("window.Overview._debug.imgWindow()")
    cur = ios_page.evaluate("window.currentIdx")
    assert win["lo"] <= cur <= win["hi"]
```

- [ ] **Step 2: 実行して失敗を確認**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_viewer_overview.py -q`
Expected: FAIL（`#overview` 不在）

- [ ] **Step 3: index.html と CSS**

`app/templates/index.html` の toolbar の `#counter` の後に `<button id="overview-btn" title="Page grid (G)" aria-label="ページ一覧">⊞</button>`。`#empty` の直前に:

```html
  <div id="overview" hidden>
    <div class="ov-header"><span class="ov-folder"></span><span class="ov-chip"></span></div>
    <div class="ov-body"></div>
    <button id="overview-close" aria-label="閉じる">✕</button>
  </div>
```

`app/static/style.css` に追加:

```css
#overview{position:absolute;inset:0;z-index:12;overflow-y:auto;-webkit-overflow-scrolling:touch;background:#0b0e14;touch-action:pan-y;contain:layout paint}
#overview[hidden]{display:none}
#overview.prebuilt{opacity:0;pointer-events:none}
.ov-header{position:sticky;top:0;z-index:2;display:flex;justify-content:space-between;align-items:center;padding:10px 12px;background:rgba(11,14,20,.92);color:#aaa;font-size:13px}
.ov-chip{color:#8ab4f8;font-variant-numeric:tabular-nums}
.ov-chapter{padding:0 8px 8px}
.ov-heading{position:sticky;top:40px;z-index:1;padding:6px 4px;color:#ccc;font-size:13px;background:rgba(11,14,20,.92)}
.ov-grid{display:grid;direction:rtl;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
.ov-cell{position:relative;display:block;padding:0;border:0;border-radius:6px;background:#161b24;overflow:hidden;content-visibility:auto;cursor:pointer}
.ov-cell img{width:100%;height:100%;object-fit:contain;display:block;pointer-events:none;-webkit-user-drag:none}
.ov-cell .ov-num{position:absolute;right:4px;bottom:3px;font-size:11px;color:#ddd;background:rgba(0,0,0,.55);padding:1px 5px;border-radius:8px;direction:ltr}
.ov-cell.cur{outline:2px solid #8ab4f8;outline-offset:-2px}
#overview-close{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);width:44px;height:44px;border-radius:22px;border:1px solid #333;background:rgba(20,24,32,.95);color:#ddd;font-size:18px;z-index:13}
body.overview-open #toolbar,body.overview-open #spread-controls,body.overview-open #hint,body.overview-open #zone-prev,body.overview-open #zone-next,body.overview-open #zone-toggle{pointer-events:none!important;opacity:0}
@media (min-width:768px){.ov-cell{min-width:150px}}
```

- [ ] **Step 4: overview.js を実装（開閉・構築・ウィンドウイング）**

`app/static/viewer/overview.js`:

```js
/* bookq viewer/overview — series-wide RTL page grid. Spec §5.3.
 * Loaded BEFORE app.js so its Escape listener runs first (stopImmediatePropagation).
 */
(function () {
  'use strict';
  var IMG_WINDOW = 200, HUGE = 600, INERTIA_MS = 100, SUPPRESS_MS = 400, CELL_AR = 3 / 4;
  var root, body, header, chip, closeBtn, open = false, prebuiltFor = null, lastScrollTs = 0, io = null, curCenter = null, ro = null;

  function q(sel) { return document.querySelector(sel); }
  function images() { return window.images || []; }
  function chapters() { return window.chapterList || []; }
  function ensureEls() { root = q('#overview'); body = root && root.querySelector('.ov-body'); header = root && root.querySelector('.ov-folder'); chip = root && root.querySelector('.ov-chip'); closeBtn = q('#overview-close'); return !!root; }

  function realList() { var out = [], im = images(); for (var i = 0; i < im.length; i++) if (!im[i].isSpacer) out.push(i); return out; }
  function chapterOf(g) { return window.getCurrentChapterInfo ? window.getCurrentChapterInfo(g) : { chapterIdx: 0, chapterName: '', localIdx: g }; }
  function thumbSrc(im) { return window.OfflineCore.thumbUrl(im.folder, im.name, 400); }
  function fullSrc(im) { return window.OfflineCore.imageUrl(im.folder, im.name); }

  function build(curIdx) {
    if (!ensureEls() || !images().length) return false;
    body.innerHTML = '';
    var reals = realList(), huge = reals.length > HUGE;
    var curPos = Math.max(0, reals.indexOf(curIdx));
    var lo = huge ? Math.max(0, curPos - IMG_WINDOW) : 0, hi = huge ? Math.min(reals.length - 1, curPos + IMG_WINDOW) : reals.length - 1;
    root._win = { lo: reals[lo], hi: reals[hi] };
    var chs = chapters(), multi = chs.length > 1;
    var frag = document.createDocumentFragment(), sec = null, grid = null, lastCh = -1;
    var colW = cellWidth();
    reals.forEach(function (g, pos) {
      var im = images()[g], info = chapterOf(g);
      if (!sec || (multi && info.chapterIdx !== lastCh)) {
        sec = document.createElement('section'); sec.className = 'ov-chapter';
        if (multi) { var hd = document.createElement('div'); hd.className = 'ov-heading'; hd.textContent = info.chapterName; sec.appendChild(hd); }
        grid = document.createElement('div'); grid.className = 'ov-grid'; sec.appendChild(grid); frag.appendChild(sec); lastCh = info.chapterIdx;
      }
      var cell = document.createElement('button'); cell.type = 'button'; cell.className = 'ov-cell' + (g === curIdx ? ' cur' : '');
      cell.dataset.gidx = String(g); cell.style.height = Math.round(colW / CELL_AR) + 'px';
      if (pos >= lo && pos <= hi) cell.appendChild(makeImg(im));
      var num = document.createElement('span'); num.className = 'ov-num'; num.textContent = String(info.localIdx + 1); cell.appendChild(num);
      grid.appendChild(cell);
    });
    body.appendChild(frag);
    header.textContent = window.currentFolder || '';
    updateChip(curIdx);
    prebuiltFor = curIdx;
    return true;
  }
  function makeImg(im) {
    var img = document.createElement('img'); img.loading = 'lazy'; img.decoding = 'async'; img.alt = ''; img.draggable = false;
    img.src = thumbSrc(im);
    img.addEventListener('error', function onErr() { img.removeEventListener('error', onErr); img.src = fullSrc(im); });
    return img;
  }
  function cellWidth() {
    var W = (q('#viewer') || document.body).clientWidth || window.innerWidth;
    var inner = W - 16 - 16; // section padding + grid padding
    var cols = Math.max(1, Math.floor((inner + 8) / (150 + 8)));
    return (inner - 8 * (cols - 1)) / cols;
  }
  function updateChip(g) {
    if (!chip) return;
    var info = chapterOf(g), reals = realList(), pos = reals.indexOf(g);
    var chTotal = (chapters()[info.chapterIdx] && chapters()[info.chapterIdx].images) ? chapters()[info.chapterIdx].images.length : reals.length;
    chip.textContent = (chapters().length > 1 ? info.chapterName + ' ' : '') + (info.localIdx + 1) + '/' + chTotal + (chapters().length > 1 ? ' ・ 通算 ' + (pos + 1) + '/' + reals.length : '');
  }
  function scrollCurToThird() {
    var cur = root.querySelector('.ov-cell.cur'); if (!cur) return;
    root.scrollTop = Math.max(0, cur.offsetTop - root.clientHeight / 3 + cur.offsetHeight / 2);
    var r = cur.getBoundingClientRect(); curCenter = { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  }
  function slideWindow() {
    // huge books: move the ±200 <img> window to follow the scroll position
    if (!root._win || realList().length <= HUGE) return;
    var cells = root.querySelectorAll('.ov-cell'); var mid = root.scrollTop + root.clientHeight / 2, best = 0, bestD = Infinity;
    for (var i = 0; i < cells.length; i += 8) { var d = Math.abs(cells[i].offsetTop - mid); if (d < bestD) { bestD = d; best = i; } }
    var lo = Math.max(0, best - IMG_WINDOW), hi = Math.min(cells.length - 1, best + IMG_WINDOW);
    for (var j = 0; j < cells.length; j++) {
      var has = cells[j].querySelector('img');
      if (j >= lo && j <= hi) { if (!has) cells[j].insertBefore(makeImg(images()[+cells[j].dataset.gidx]), cells[j].firstChild); }
      else if (has) has.remove();
    }
    root._win = { lo: +cells[lo].dataset.gidx, hi: +cells[hi].dataset.gidx };
  }

  function prebuild() {
    if (open || !ensureEls() || !images().length) return;
    var run = function () { if (open) return; if (build(window.currentIdx || 0)) { root.hidden = false; root.classList.add('prebuilt'); scrollCurToThird(); } };
    if (window.requestIdleCallback) window.requestIdleCallback(run, { timeout: 1000 }); else setTimeout(run, 300);
  }
  function show() {
    if (open || !ensureEls() || !images().length) return;
    var cur = window.currentIdx || 0;
    if (prebuiltFor !== cur || !body.children.length) build(cur);
    root.hidden = false; root.classList.remove('prebuilt'); root.style.opacity = '';
    open = true; document.body.classList.add('overview-open');
    if (window.Zoom) window.Zoom.reset(false);
    scrollCurToThird();
    root.querySelector('.ov-cell.cur') && root.querySelector('.ov-cell.cur').focus({ preventScroll: true });
  }
  function close() {
    if (!ensureEls()) return;
    var wasOpen = open; open = false;
    document.body.classList.remove('overview-open');
    root.hidden = true; root.classList.remove('prebuilt'); root.style.opacity = '';
    if (window.Overview._finishFlight) { try { window.Overview._finishFlight(); } catch (e) {} }
    body.innerHTML = ''; prebuiltFor = null; curCenter = null;
    return wasOpen;
  }
  function exitTo(gidx) {
    close();
    if (window.suppressClickFor) window.suppressClickFor(SUPPRESS_MS);
    if (window.viewerSnapTo) window.viewerSnapTo(gidx, false);
  }
  function relayout() { if (!ensureEls() || root.hidden) return; var w = cellWidth(); root.querySelectorAll('.ov-cell').forEach(function (c) { c.style.height = Math.round(w / CELL_AR) + 'px'; }); scrollCurToThird(); }

  function init() {
    if (!ensureEls()) return;
    root.addEventListener('scroll', function () { lastScrollTs = performance.now(); slideWindow(); }, { passive: true });
    // tap (< 8px) → exit to page; ignore taps within INERTIA_MS of a scroll
    var tap = null;
    root.addEventListener('touchstart', function (e) { e.stopPropagation(); var t = e.touches[0]; tap = e.touches.length === 1 ? { x: t.clientX, y: t.clientY, id: t.identifier, dead: false } : null; }, { passive: true });
    root.addEventListener('touchmove', function (e) { e.stopPropagation(); if (!tap || e.touches.length !== 1) { if (tap) tap.dead = true; return; } var t = e.touches[0]; if (Math.hypot(t.clientX - tap.x, t.clientY - tap.y) >= 8) tap.dead = true; }, { passive: true });
    root.addEventListener('touchend', function (e) {
      e.stopPropagation();
      if (!tap || tap.dead || e.touches.length) { tap = null; return; }
      var target = document.elementFromPoint(tap.x, tap.y); tap = null;
      if (performance.now() - lastScrollTs < INERTIA_MS) return;
      var cell = target && target.closest ? target.closest('.ov-cell') : null;
      if (cell) exitTo(+cell.dataset.gidx);
    }, { passive: true });
    root.addEventListener('touchcancel', function (e) { e.stopPropagation(); tap = null; }, { passive: true });
    root.addEventListener('click', function (e) { if (!open) return; var cell = e.target.closest && e.target.closest('.ov-cell'); if (cell && performance.now() - lastScrollTs >= INERTIA_MS) exitTo(+cell.dataset.gidx); });
    if (closeBtn) closeBtn.addEventListener('click', function () { close(); });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && open) { e.stopImmediatePropagation(); e.preventDefault(); close(); return; }
      if ((e.key === 'g' || e.key === 'G') && !e.metaKey && !e.ctrlKey && !e.altKey && !(e.target && e.target.tagName === 'INPUT')) { if (open) close(); else if (images().length) show(); }
    });
    var btn = q('#overview-btn'); if (btn) btn.addEventListener('click', function () { if (open) close(); else show(); });
    var menu = q('#menu-btn'); if (menu) menu.addEventListener('click', function () { if (open) close(); }, true);
    var viewer = q('#viewer');
    if (viewer && window.ResizeObserver) { ro = new ResizeObserver(function () { relayout(); }); ro.observe(viewer); }
  }

  window.Overview = {
    open: show, close: close, isOpen: function () { return open; }, prebuild: prebuild, relayout: relayout,
    cellCenter: function () { return curCenter; },
    _debug: { imgWindow: function () { return root && root._win ? root._win : null; } }
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
```

- [ ] **Step 5: app.js — `loadImages` 成功後に prebuild**

`app/static/app.js` の `loadImages` 成功パスで `showHint();` の直後に:

```js
        if (window.Overview && window.Overview.prebuild) window.Overview.prebuild();
```

（`G` キーと `#menu-btn` は overview.js 側で処理するので app.js の `keydown` には追加しない。app.js の `case "f"` はそのまま。）

- [ ] **Step 6: 実行して通ることを確認**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_viewer_overview.py tests/test_ui.py -q`
Expected: all passed。`test_huge_book_windows_imgs...` で 701 セルの構築が 5 秒以内であること。

- [ ] **Step 7: Commit**

```bash
git add app/static/viewer/overview.js app/templates/index.html app/static/style.css app/static/app.js tests/test_viewer_overview.py
git commit -S -m "feat(viewer): overview.js — シリーズ全体の RTL 俯瞰グリッド（開閉・重なり・±200 窓）

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: ピンチ入場（発火・ドリフト・FLIP）と退出（クロスフェード・俯瞰内ピンチアウト）

**Files:**
- Modify: `app/static/viewer/overview.js`, `app/static/viewer/zoom.js`（配線）
- Test: `tests/test_viewer_overview.py`

**Interfaces:**
- Consumes: `Zoom.onOverviewFire`, `Zoom.driftTarget`, `Overview.cellCenter()`, `ViewerGesture.attach(#overview, ...)`
- Produces: `Overview.open()` に FLIP、`Overview._finishFlight`、`Overview._debug.flightActive()`

- [ ] **Step 1: 失敗するテストを書く**

```python
def test_pinch_in_to_half_opens_overview_once_with_hysteresis(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 2, 5)
    ios_page.evaluate("window.__vib = 0; navigator.vibrate = () => { window.__vib++; return true; }")
    pinch(touch, CX, CY, [50, 40, 30, 24], hold=True)  # ratio 0.48 → fire
    ios_page.wait_for_function("window.Overview.isOpen()")
    assert ios_page.evaluate("window.__vib") == 1
    touch_move(touch, two(CX, CY, 40)); touch_move(touch, two(CX, CY, 24))  # widen above 0.7 then squeeze again: latch re-arms but overview already open → no double
    touch_end(touch)
    assert ios_page.evaluate("window.Overview.isOpen()") is True


def test_pinch_to_0_51_does_not_open(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 2, 5)
    pinch(touch, CX, CY, [50, 40, 26])  # 0.52
    ios_page.wait_for_function("window.Zoom.state().zoom === 1")
    assert ios_page.evaluate("window.Overview.isOpen()") is False


def test_shrink_band_drifts_toward_cur_cell(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 2, 5)
    ios_page.wait_for_function("!!window.Overview.cellCenter()")
    dest = ios_page.evaluate("window.Overview.cellCenter()")
    pinch(touch, CX, CY, [50, 40, 30], hold=True)  # 0.6 — mid drift
    st = zoom_state(ios_page)
    W, H = ios_page.evaluate("[window.innerWidth, window.innerHeight]")
    center_x = st["tx"] + st["zoom"] * W / 2
    center_y = st["ty"] + st["zoom"] * H / 2
    # centre has moved from (W/2,H/2) toward dest
    assert abs(center_x - W / 2) > 1 or abs(center_y - H / 2) > 1
    d_before = ((W / 2 - dest["x"]) ** 2 + (H / 2 - dest["y"]) ** 2) ** 0.5
    d_now = ((center_x - dest["x"]) ** 2 + (center_y - dest["y"]) ** 2) ** 0.5
    assert d_now < d_before
    touch_end(touch)


def test_entry_flight_then_reveal_and_close_mid_flight_removes_clone(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 2, 5)
    pinch(touch, CX, CY, [50, 40, 24], hold=True)
    ios_page.wait_for_function("window.Overview.isOpen()")
    assert ios_page.locator("#ov-fly").count() in (0, 1)
    ios_page.wait_for_function("!document.querySelector('#ov-fly')", timeout=1500)
    touch_end(touch)
    # again, but close during the flight
    ios_page.keyboard.press("g"); ios_page.wait_for_function("!window.Overview.isOpen()")
    pinch(touch, CX, CY, [50, 40, 24], hold=True)
    ios_page.wait_for_function("window.Overview.isOpen()")
    ios_page.evaluate("window.Overview.close()")
    assert ios_page.locator("#ov-fly").count() == 0
    touch_end(touch)


def test_entry_gesture_widening_does_not_exit(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 2, 5)
    pinch(touch, CX, CY, [50, 40, 24], hold=True)
    ios_page.wait_for_function("window.Overview.isOpen()")
    touch_move(touch, two(CX, CY, 60)); touch_move(touch, two(CX, CY, 90))  # ratio 1.8 within the same gesture
    assert ios_page.evaluate("window.Overview.isOpen()") is True
    touch_end(touch)


def test_new_pinch_out_in_overview_exits_to_cell_under_midpoint(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 0, 5)
    _open_overview(ios_page)
    cell = ios_page.locator("#overview .ov-cell").nth(2)
    cell.scroll_into_view_if_needed(); b = cell.bounding_box()
    gidx = int(cell.get_attribute("data-gidx"))
    mx, my = b["x"] + b["width"] / 2, b["y"] + b["height"] / 2
    pinch(touch, mx, my, [30, 36, 42, 50])  # 1.67 > 1.3
    ios_page.wait_for_function("!window.Overview.isOpen()")
    assert ios_page.evaluate("window.currentIdx") == gidx


def test_tap_exit_shows_crossfade_that_is_removed_on_load_or_error(ios_page, server, touch):
    open_book(ios_page, server, SERIES, 0, 5)
    _open_overview(ios_page)
    cell = ios_page.locator('#overview .ov-cell').nth(12)
    cell.scroll_into_view_if_needed(); b = cell.bounding_box()
    tap(touch, b["x"] + b["width"] / 2, b["y"] + b["height"] / 2)
    ios_page.wait_for_function("!window.Overview.isOpen()")
    # crossfade clone exists briefly, then disappears (load fires quickly on the fixture)
    ios_page.wait_for_function("!document.querySelector('#ov-xfade')", timeout=3500)
```

- [ ] **Step 2: 実行して失敗を確認**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_viewer_overview.py -q -k "pinch or drift or flight or widening or crossfade"`
Expected: FAIL

- [ ] **Step 3: overview.js に FLIP 入場・クロスフェード退出・俯瞰内ピンチアウトを追加し、zoom.js と配線**

`overview.js` の `show()` を `show(opts)` に変え、`opts && opts.fly` のとき FLIP:

```js
  var FLY_MS = 220, XFADE_MAX_MS = 3000;
  function reducedMotion() { try { return window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (e) { return false; } }
  function currentPageImg() {
    var pool = window.stripImgs || {}; var el = pool[window.currentIdx || 0];
    if (el && el.isConnected) return el;
    var imgs = document.querySelectorAll('#img-track img');
    for (var i = 0; i < imgs.length; i++) { var r = imgs[i].getBoundingClientRect(); if (r.width > 2 && r.height > 2) return imgs[i]; }
    return null;
  }
  function flyIn() {
    var cur = root.querySelector('.ov-cell.cur'), srcEl = currentPageImg();
    if (!cur || !srcEl || reducedMotion()) return false;
    var src = srcEl.getBoundingClientRect(), dst = cur.getBoundingClientRect();
    if (src.width < 2 || dst.width < 2) return false;
    var fly = document.createElement('img'); fly.id = 'ov-fly'; fly.alt = ''; fly.src = srcEl.currentSrc || srcEl.src;
    fly.style.cssText = 'position:fixed;z-index:14;pointer-events:none;object-fit:contain;left:' + src.left + 'px;top:' + src.top + 'px;width:' + src.width + 'px;height:' + src.height + 'px;transform-origin:center center';
    document.body.appendChild(fly);
    root.style.opacity = '0';
    void fly.offsetWidth;
    fly.style.transition = 'transform ' + FLY_MS + 'ms ease';
    fly.style.transform = 'translate(' + ((dst.left + dst.width / 2) - (src.left + src.width / 2)) + 'px,' + ((dst.top + dst.height / 2) - (src.top + src.height / 2)) + 'px) scale(' + (dst.width / src.width) + ',' + (dst.height / src.height) + ')';
    var done = false;
    function finish() { if (done) return; done = true; if (fly.parentNode) fly.parentNode.removeChild(fly); root.style.opacity = ''; window.Overview._finishFlight = null; }
    window.Overview._finishFlight = finish;
    fly.addEventListener('transitionend', finish);
    setTimeout(finish, FLY_MS + 80);
    return true;
  }
```

`show(opts)` 内、`scrollCurToThird();` の後に `if (opts && opts.fly) flyIn();`。

退出クロスフェード（`exitTo` を置換）:

```js
  function exitTo(gidx) {
    var cell = root.querySelector('.ov-cell[data-gidx="' + gidx + '"]'), im = images()[gidx];
    close();
    if (window.suppressClickFor) window.suppressClickFor(SUPPRESS_MS);
    if (window.viewerSnapTo) window.viewerSnapTo(gidx, false);
    if (!im || reducedMotion()) return;
    var x = document.createElement('img'); x.id = 'ov-xfade'; x.alt = '';
    x.src = window.OfflineCore.thumbUrl(im.folder, im.name, 800);
    x.style.cssText = 'position:fixed;inset:0;width:100%;height:100%;object-fit:contain;z-index:11;pointer-events:none;background:#0d0d0d;transition:opacity .2s';
    document.body.appendChild(x);
    var gone = false; function remove() { if (gone) return; gone = true; x.style.opacity = '0'; setTimeout(function () { x.remove(); }, 220); }
    var real = window.stripImgs && window.stripImgs[gidx];
    var poll = setInterval(function () { var el = (window.stripImgs || {})[gidx]; if (el && el.complete && el.naturalWidth > 0) { clearInterval(poll); remove(); } }, 50);
    setTimeout(function () { clearInterval(poll); remove(); }, XFADE_MAX_MS);
    x.addEventListener('error', remove);
  }
```

（`window.stripImgs` が公開されていなければ、`#img-track img[src="<fullSrc>"]` を探す形にする。実装時に `grep -n "stripImgs" app/static/app.js app/static/viewer/spread.js` で確認。）

俯瞰内ピンチアウト（`init()` 内）:

```js
    if (window.ViewerGesture) window.ViewerGesture.attach(root, {
      onStart: function () {}, onMove: function () {},
      onEnd: function (g) {
        if (!open || !(g.ratio > 1.3)) return;
        var m = window.Overview._lastMid; var target = m ? document.elementFromPoint(m.x, m.y) : null;
        var cell = target && target.closest ? target.closest('.ov-cell') : null;
        exitTo(cell ? +cell.dataset.gidx : (window.currentIdx || 0));
      }
    });
```

`attach` の `onMove` で `window.Overview._lastMid = g.mid` を保持し、`onEnd.ratio` が `undefined` のときは最後の `onMove.ratio` を使う（overview.js 側で `_lastRatio` を持つ）。

zoom.js との配線（overview.js の `init()` 末尾）:

```js
    if (window.Zoom) {
      window.Zoom.onOverviewFire = function () { if (!open && images().length) { try { if ('vibrate' in navigator && !reducedMotion()) navigator.vibrate(10); } catch (e) {} show({ fly: true }); } };
      window.Zoom.driftTarget = function () { return curCenter; };
    }
```

`show()` は `open` が真なら no-op なので、同一ジェスチャ内で再アームして再発火しても二重に開かない。入場ジェスチャの指はターゲットが `#img-container` のまま（`root` の `attach` には届かない）ので、広げても退出しない。

- [ ] **Step 4: 実行して通ることを確認**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_viewer_overview.py tests/test_viewer_zoom.py -q`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add app/static/viewer/overview.js app/static/viewer/zoom.js tests/test_viewer_overview.py
git commit -S -m "feat(viewer): ピンチイン発火・セルへの縮小ドリフト・FLIP 入場・クロスフェード退出・俯瞰内ピンチアウト

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: 全テスト・Simulator 手動 QA・viewport meta 判断・ドキュメント

**Files:**
- Modify: `CLAUDE.md:99-101,113-140`, `README.md`, `tests/README.md`, `app/templates/index.html:36`（実測結果次第）, `docs/superpowers/specs/2026-09-21-viewer-zoom-overview-design.md`（§9 の結論）

- [ ] **Step 1: 全テスト実行**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/ -q`
Expected: all passed。失敗があれば該当タスクへ戻る。

- [ ] **Step 2: dev 環境を立て Simulator で手動 QA**

```bash
./dev.sh down demo; ./dev.sh up demo --local --fixture
./scripts/open-simulator.sh "http://127.0.0.1:8080/#/view/zoom_episodes/series/ep-1?p=0"
```

チェックリスト（spec §6 末尾）:
- [ ] ピンチインで細字が消えてグリッドに落ちる（FLIP が見える）。EP 見出しが 3 本、スクロール中は現在チャプターの見出しが上に残る
- [ ] グリッド右上が EP1 P1。チップが `ep-1 1/5 ・ 通算 1/16`
- [ ] セルタップで戻る。黒画面が出ない（サムネ → 本画像へフェード）
- [ ] 拡大 → 左ゾーンタップで進まない → 中央タップで 1.0 に戻る
- [ ] 拡大 → 左端まで寄せて右へ 80px でラバーバンド + 振動 → 次ページ 1.0
- [ ] 親指の付け根を下端に置いたまま 1 本指スワイプでページが進む
- [ ] **Safari のタブ一覧が出ない**（scale 1.0 でのピンチイン）
- [ ] `#page-slider` のつまみがドラッグできる
- [ ] iPad（`SIMULATOR_DEVICE="iPad Pro 13-inch (M4)"`）横持ち 1P で 1.5x に拡大 → 右ドラッグで次ページ。俯瞰は 7 列

viewport meta の判断（spec §5.1-5）: 上記「タブ一覧が出ない」が **現状の meta（`maximum-scale` なし）で満たされれば meta は触らない**。満たされなければ `index.html:36` に `,maximum-scale=1,user-scalable=no` を追加し、CLAUDE.md に副作用（ブラウズ UI のアクセシビリティズーム喪失）を書く。結論を spec §9 に 1 行追記。

- [ ] **Step 3: ドキュメント更新**

`CLAUDE.md`:
- 99 行目 `nine plain scripts` → `twelve plain scripts`、101 行目の一覧に `viewer/gesture`, `viewer/zoom`, `viewer/overview` を `viewer/spread` の後に追加。
- ビューア節に追記:

```
- **Zoom and overview live outside the strip.** `#zoom-layer` wraps `#img-track` and is the only element zoom.js transforms; the strip's own `translateX`/transition are untouched, so anchor math is unaffected. `snapTo` calls `Zoom.reset(true)` first — every page turn (tap, swipe, key, slider, overview tap) lands at 1.0. `_teardownViewer` (hash.js) closes the overview and resets zoom, because `resetToTop`/`restoreBrowse` never pass through `loadImages`. While zoomed, `#zone-prev/next` are `pointer-events:none` and the center zone means "back to 1.0"; an edge-turn fires only for a drag that *started* with no horizontal room (clamped edge or centred axis) and moved 80px. `updateOrientation` early-returns when `innerWidth/Height` are unchanged (keyboard/sidebar resizes), with a `-1` sentinel so the init call still runs. The 1-finger swipe tracks its `Touch.identifier` and counts fingers via `ViewerGesture.activeTouches(e)` (edge-20px palms excluded until they move 10px). overview.js loads before app.js so its Escape listener wins via `stopImmediatePropagation`. Overview/zoom state is never in the URL.
```

`README.md` のビューア操作説明にピンチ拡大・俯瞰（`G` / ⊞）の 3 行。`tests/README.md` のカバー範囲表に `ズーム/俯瞰 | ピンチ帯域、パン/端越え、俯瞰グリッド/入退場（CDP 合成タッチ、tests/viewer_touch.py）` を追加。

- [ ] **Step 4: 全テストを再実行して commit**

Run: `uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/ -q`
Expected: all passed

```bash
git add CLAUDE.md README.md tests/README.md app/templates/index.html docs/superpowers/specs/2026-09-21-viewer-zoom-overview-design.md
git commit -S -m "docs(viewer): ズーム/俯瞰の契約を CLAUDE.md・README に追記、viewport meta の実測結論

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
./dev.sh down demo
```

- [ ] **Step 5: PR**

既存 PR #188 とは別に、`main` 向けの新 PR を作る（`gh pr create --repo kentoku24/bookq --base main`）。本文に spec と plan のパス、Simulator QA の結果、viewport meta の判断を書く。末尾に `🤖 Generated with [Claude Code](https://claude.com/claude-code)`。

---

## Self-Review

- **Spec coverage**: §4 数値基準 → Task 4/5/6 のテスト（16ms は自動化不可、Simulator QA）。§5.1 → Task 2/3。§5.2 → Task 4/5。§5.3 → Task 6/7。§5.4 差分 → Task 3/4/5/6（`updateOrientation`、identifier、`snapTo`、`_teardownViewer`、`prebuild`、`zoneToggle`、`menu-btn`、`G`、`suppressClickFor` 公開）。§6 テスト表 → 各タスク。§8 Medium → Task 5（中央ゾーン 20%、suppress 無視、`body.zoomed` を onStart で付与）、Task 6（IntersectionObserver ではなく scroll ベースの窓移動を採用。60fps を割るなら IO に置換）、Task 7。§9 → Task 8。
- **Placeholder scan**: `spreadMetrics` の戻り値キー、`window.sliding`/`window.stripImgs` の有無は「実装時に grep で確認」と明記済み。それ以外に TBD なし。
- **Type consistency**: `Zoom.state()` は `{zoom, tx, ty, band, edgeArmed}`、`Overview.{open, close, isOpen, prebuild, relayout, cellCenter, _debug.imgWindow}`、`ViewerGesture.{activeTouches, attach, installNativeGuards, isPinching, _debug.palmIds}`、`window.viewerGoNext/viewerGoPrev/suppressClickFor/__suppressClickActive` で全タスク一致。
