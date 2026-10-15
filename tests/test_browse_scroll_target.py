"""フォルダ一覧の表示位置（スクロールアンカー）の契約。

決定ルール（優先順）:
  1. ビューア復帰 → ビューアで表示していた章のタイルを上端に
  2. その階層に保存位置がある restore → 従来どおり保存位置を復元
  3. それ以外の新規オープン → 直下の既読エピソードのうち一覧順で最後を上端に
  4. 該当なし → 上端

390x844 では fixture のほとんどの階層がスクロール不能（実測: group は
scrollHeight == clientHeight == 728）で空振りするため、このファイルは専用
ビューポート 390x430（tile モードで 3 件以上の階層が確実にスクロールする高さ）
で測る。位置を測るテストは _assert_scrollable() で scrollHeight > clientHeight
（＝そもそもスクロールできる階層か）を確認し、空振りを構造的に排除する。
decide() の決定表と保存位置の pin は DOM 位置を測らないので対象外。

fixture（tests/conftest.py）:
  zoom_series = zoom_series/series/ep-1..3（scanner の flatten で zoom_series の直下）
  group = deep_series / episodes / fullwidth_episodes / other_work
          （deep_series/vol1 は group の孫＝「直下の子のみ」の確認に使う）
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
        "(p) => window.currentFolder === p || window.currentBrowsePath === p", arg=path, timeout=5000
    )
    page.wait_for_timeout(150)


def _wait_browse(page, path):
    """ビューアが閉じて一覧に戻るまで（☰ / Back）。"""
    page.wait_for_function(
        "(p) => window.currentFolder === null && window.currentBrowsePath === p",
        arg=path,
        timeout=5000,
    )
    page.wait_for_timeout(400)


def _navigate(page, path, child):
    """navigateToFolder(path) して、その階層のタイル（child）が描かれるまで待つ。"""
    page.evaluate("(p) => window.navigateToFolder(p)", path)
    page.wait_for_function(
        "(a) => window.currentBrowsePath === a.path &&"
        " [...document.querySelectorAll('#tile-grid [data-path]')]"
        ".some(e => e.dataset.path === a.child)",
        arg={"path": path, "child": child},
        timeout=5000,
    )
    page.wait_for_timeout(150)


def _seed_progress(page, entries):
    page.evaluate("(o) => localStorage.setItem('bookq_progress', JSON.stringify(o))", entries)
    page.reload()
    page.wait_for_selector(".tile")
    page.wait_for_function("() => window.treeLoaded === true")


def _chapter(page):
    return page.evaluate("() => (window.currentViewerState() || {}).folder || null")


def _decide(page, opts):
    """BrowseScroll.decide を DOM 計測なしで評価する（決定表の pin 用）。"""
    return page.evaluate("(o) => window.BrowseScroll.decide(o)", opts)


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
    # クリック時の自動スクロールを持ち越さない（これが無いと現状コードでも ±2 で通ってしまう）
    small.evaluate("() => { document.getElementById('sidebar-scroll').scrollTop = 0; }")
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
    _assert_scrollable(small)
    _open(small, EP3)
    small.evaluate(
        "() => { window.__rv = 0; const orig = window.renderView;"
        " window.renderView = function(){ window.__rv++; return orig.apply(this, arguments); }; }"
    )
    # クリック時の自動スクロールを持ち越さない（これが無いと現状コードでも ±2 で通ってしまう）
    small.evaluate("() => { document.getElementById('sidebar-scroll').scrollTop = 0; }")
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
    _assert_scrollable(small)
    _open(small, EP3)
    assert _chapter(small) == EP3, "前提: ep-3 を表示している"
    small.keyboard.press("ArrowLeft")
    small.wait_for_timeout(300)
    # クリック時の自動スクロールを持ち越さない（これが無いと現状コードでも ±2 で通ってしまう）
    small.evaluate("() => { document.getElementById('sidebar-scroll').scrollTop = 0; }")
    small.go_back()
    _wait_browse(small, SERIES)
    ep3_top = _tile_top(small, EP3)
    top = _scroll(small)["top"]
    assert abs(top - ep3_top) <= 2, f"Back でも ep-3 が上端 (top={top} expected={ep3_top})"


def test_breadcrumb_back_keeps_saved_scroll(small):
    """回帰 pin: ↩ で親に戻るときの保存位置復元は従来どおり（アンカーは働かない）。"""
    _open(small, "group")
    _open(small, "group/deep_series")            # ← ここで group の保存位置が入る
    saved = small.evaluate("() => window.scrollPositions[window.getScrollKey('group')]")
    assert saved is not None and saved > 0, f"前提: group に保存位置がある (saved={saved})"
    small.evaluate("() => { document.getElementById('sidebar-scroll').scrollTop = 0; }")  # 復元されなければ 0 のまま
    small.locator("#browse-header").click()      # ↩
    small.wait_for_timeout(600)
    top = _scroll(small)["top"]
    assert abs(top - saved) <= 2, f"親の保存位置({saved}) が復元される (top={top})"


def test_viewer_return_beats_saved_scroll(small):
    """ビューア復帰 > 保存位置: 保存位置が残っている階層でも表示していた章に合う。"""
    _open(small, "group")
    _open(small, "group/deep_series")            # ← ここで group の保存位置が入る
    small.locator("#browse-header").click()      # ↩ で group に戻る（保存位置を消費）
    small.wait_for_timeout(600)
    saved = small.evaluate("() => window.scrollPositions[window.getScrollKey('group')]")
    assert saved is not None, "前提: group に保存位置がある"
    _assert_scrollable(small)  # 前提: group がスクロール可能（アンカーが空振りしない）
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
    _assert_scrollable(small)  # 前提: ルートがスクロールできる（上端リセットが空振りしない）
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
    d = lambda o: _decide(small, o)  # noqa: E731
    assert d({"level": None, "cause": "restore", "hasSaved": False}) == {"via": "top", "anchor": None}
    assert d({"level": None, "cause": "restore", "hasSaved": True}) == {"via": "saved", "anchor": None}
    assert d({"level": SERIES, "cause": "restore", "hasSaved": True}) == {"via": "saved", "anchor": None}
    small.evaluate(
        "(e) => localStorage.setItem('bookq_progress', JSON.stringify(e))",
        {EP3: {"page": 0, "total": 7, "ts": 1}},
    )
    assert d({"level": SERIES, "cause": "open", "hasSaved": False}) == {"via": "progress", "anchor": EP3}
    # 保存位置 > 最大既読: 既読（ep-3）があっても、その階層に保存位置があれば保存位置
    assert d({"level": SERIES, "cause": "restore", "hasSaved": True}) == {"via": "saved", "anchor": None}
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

    # 直下の子のみ: group 直下に既読は無い（既読は孫の deep_series/vol1 だけ）→ 上端
    small.evaluate(
        "(e) => localStorage.setItem('bookq_progress', JSON.stringify(e))",
        {"group/deep_series/vol1": {"page": 0, "total": 2, "ts": 1}},
    )
    _navigate(small, "group", "group/deep_series")
    assert d({"level": "group", "cause": "open", "hasSaved": False}) == {"via": "top", "anchor": None}
    # 対照: 同じ既読がその直下の階層では効く（シード自体が有効なことの確認）
    _navigate(small, "group/deep_series", "group/deep_series/vol1")
    assert d({"level": "group/deep_series", "cause": "open", "hasSaved": False}) == {
        "via": "progress", "anchor": "group/deep_series/vol1"
    }


def test_marker_chapter_not_in_visible_children_falls_back(small):
    """§4: 章がこの階層の表示子に無い → viewer にせず saved / top へ落ちる。"""
    _open(small, SERIES)
    small.evaluate("(p) => window.BrowseScroll.noteViewerReturn(p.level, p.chapter)",
                   {"level": SERIES, "chapter": "group/other_work"})
    assert _decide(small, {"level": SERIES, "cause": "restore", "hasSaved": False}) == {
        "via": "top", "anchor": None
    }
    assert _decide(small, {"level": SERIES, "cause": "restore", "hasSaved": True}) == {
        "via": "saved", "anchor": None
    }


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
