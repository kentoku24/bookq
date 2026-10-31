"""性能予算（ms に依存しない決定的指標）。

ms の KPI は scripts/perf/measure.py で example-host 実データに対して測る。ここでは
CI の合成フィクスチャでも安定する「回数・バイト比・long task 数」だけを守る。
decisions.md（2026-09-21）の KPI/PR 順序に対応。
"""

FOLDERS_ENTRIES_JS = """
() => performance.getEntriesByType('resource')
  .filter(e => e.name.includes('/api/v1/tree'))
  .map(e => ({name: e.name, start: e.startTime, transfer: e.transferSize, encoded: e.encodedBodySize, decoded: e.decodedBodySize}))
"""


def _wait_for_folders_entries(page, n: int, timeout_ms: int = 10_000):
    page.wait_for_function(
        "(n) => performance.getEntriesByType('resource').filter(e => e.name.includes('/api/v1/tree')).length >= n",
        arg=n,
        timeout=timeout_ms,
    )


def test_top_page_fetches_folders_body_once(server, browser):
    """初回ロードで /api/v1/tree のボディ転送は shallow + full のちょうど 2 回。

    Todo2 の shallow-first 契約: 1 回目 `?depth=1` (first paint)、2 回目 full
    (同一 DOM への追従)。ボディ判定は transfer >= encoded の実転送で行う。
    起動時の tryAutoSync は freshness gate（5 分）に守られ第 3 リクエストを
    出さない — 3 回目はゲートを越えた手動 revalidateFoldersForOnline() の
    If-None-Match 付き warm 条件付き再検証で 304 になり、転送量はヘッダ分のみ
    （< 2KB）でなければならない。
    """
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    try:
        page.goto(server + "/")
        page.wait_for_selector("#tile-grid .tile")
        page.wait_for_function("() => window.treeLoaded === true", timeout=10_000)
        page.wait_for_function(
            "() => sessionStorage.getItem('bookq_auto_sync_done') === '1'",
            timeout=5_000,
        )
        page.evaluate("() => { try { window.FillPoll.stop(); } catch(e){} }")
        page.evaluate(
            """() => new Promise(resolve => {
                requestAnimationFrame(() => requestAnimationFrame(() => {
                    setTimeout(resolve, 0);
                }));
            })"""
        )
        entries = page.evaluate(FOLDERS_ENTRIES_JS)
        print(f"[perf-budget] folders_entries={entries}")
        assert len(entries) == 2, entries
        # ボディを運ぶ 200 応答は shallow + full のちょうど 2 回。304 は
        # transfer < encoded（ヘッダのみ転送・ボディは HTTP キャッシュ）で識別する
        # — decoded はキャッシュボディのサイズを報告するため 304 でも > 0 になる。
        bodies = sorted(
            [e for e in entries if e["transfer"] >= e["encoded"]],
            key=lambda e: e["start"],
        )
        assert len(bodies) == 2, entries
        shallow, full = bodies
        # 順序: shallow `?depth=1` が先、full（クエリなし）が後
        assert "?depth=1" in shallow["name"], (shallow, full)
        assert "depth" not in full["name"], (shallow, full)
        assert shallow["start"] < full["start"], (shallow, full)
        # gzip が効いている: full の転送は展開後の 1/3 未満（旧単発ボディの意図を維持）
        assert full["encoded"] * 3 < full["decoded"], full
        # shallow ボディは full より小さい（軽量先行の意味）
        assert shallow["decoded"] < full["decoded"], (shallow, full)
        assert shallow["transfer"] < full["transfer"], (shallow, full)
        # 合計転送は旧単発ボディ予算の 2 倍以内（full を単発予算の代理とする）
        assert shallow["transfer"] + full["transfer"] <= 2 * full["transfer"], (
            shallow,
            full,
        )
        etag = page.evaluate("() => window.__BOOKQ_DEBUG.etagByKey.get(':full')")
        assert etag, "full-tree ETag must seed etagByKey for warm revalidation"
        page.evaluate(
            """() => {
                window.__perfReqs = [];
                window.__perfBody = {json: 0, text: 0};
                const oj = Response.prototype.json;
                const ot = Response.prototype.text;
                Response.prototype.json = function(){ window.__perfBody.json++; return oj.call(this); };
                Response.prototype.text = function(){ window.__perfBody.text++; return ot.call(this); };
                const of = window.fetch.bind(window);
                window.fetch = (input, init) => {
                    const u = typeof input === 'string' ? input : (input && input.url) || '';
                    const p = of(input, init);
                    if (u.indexOf('/api/v1/tree') === 0)
                        window.__perfReqs.push({
                            inm: (init && init.headers && init.headers['If-None-Match']) || null,
                            status: p.then(r => r.status),
                        });
                    return p;
                };
                const r = Date.now.bind(Date);
                Date.now = () => r() + 300001;
            }"""
        )
        page.evaluate("() => window.revalidateFoldersForOnline()")
        _wait_for_folders_entries(page, 3)
        entries = page.evaluate(FOLDERS_ENTRIES_JS)
        inm = page.evaluate("() => window.__perfReqs.map(r => r.inm)")
        statuses = page.evaluate("() => Promise.all(window.__perfReqs.map(r => r.status))")
        assert len(inm) == 1 and inm[0] == etag, (
            f"revalidation must send If-None-Match with the seeded ETag: {inm}"
        )
        assert statuses == [304], f"warm revalidation must be answered 304: {statuses}"
        body_calls = page.evaluate("() => window.__perfBody")
        assert body_calls == {"json": 0, "text": 0}, f"304 must never parse a body: {body_calls}"
        # 3 回目以降はボディを運ばない
        rest = entries[2:]
        assert len(rest) == 1, entries
        for e in rest:
            assert e["transfer"] < 2048, e
            assert e["encoded"] == 0, e
            # 304 は 200 より必ず小さい（no-store だと同じ gzip ボディを再送するので同サイズになる）
            assert e["transfer"] < full["transfer"], (full, e)
    finally:
        context.close()


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
        # 固定待ちではなく「件数が 2 回連続で変わらない」まで待つ（遅い CI で遅着があっても安定）
        count_js = "() => performance.getEntriesByType('resource').filter(e => e.name.includes('/image/')).length"
        count = page.evaluate(count_js)
        for _ in range(16):  # 最大 ~5s
            page.wait_for_timeout(300)
            nxt = page.evaluate(count_js)
            if nxt == count:
                break
            count = nxt
        print(f"[perf-budget] open_image_requests={count} expected={expected}")
        # 厳密一致で良い: ビューアのページは /image/、サムネイルは /thumb/ と別エンドポイント
        assert count == expected
    finally:
        context.close()


def _measure_page_turn_long_tasks(page, server) -> int:
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
        page.wait_for_timeout(450)  # スライド（300ms）+ アプリ側の transitionend フォールバック（380ms）を超えて待つ
    return page.evaluate("window.__ccLongTasks")


def test_page_turns_have_no_long_tasks(server, browser):
    """10 回のページ送りで 50ms 超の long task が 0（baseline: CPU 4x でも 0）。

    閾値は緩めない（KPI は 0）。ただし `pytest -n 4` の CPU 競合による単発の GC/スケジューラ
    停止と本物の回帰を区別するため、0 でなかった場合だけ 1 回やり直し、2 回目の値で判定する。
    回帰なら 2 回とも出る。
    """
    context = browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    try:
        long_tasks = _measure_page_turn_long_tasks(page, server)
        attempts = 1
        if long_tasks != 0:
            attempts = 2
            long_tasks = _measure_page_turn_long_tasks(page, server)
        print(f"[perf-budget] page_turn_long_tasks={long_tasks} attempts={attempts}")
        assert long_tasks == 0
    finally:
        context.close()
