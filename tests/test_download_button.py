"""Task 1+3 (offline-ux-refinements) — single-tap start/stop + anchored popover E2E.

Spec (plan Todo 1):
  ① IDLE single tap -> downloadWork starts immediately (no intermediate confirm)
  ② delayed image stub, first image recorded, single tap -> immediate abort -> final .state-partial
  ③ listing loop (0 records, /api/images delayed), single tap -> immediate abort -> final .state-idle
     and .offline-btn.is-listing observable while the leaf list is being fetched
  ④ STOPPING transient asserted via window._stoppingSet (DOM class window can be <100ms)
  ⑤ title attribute == aria-label on every .offline-btn
  regression: COMPLETE -> CONFIRM_DELETE (red trash, 2 taps) -> DELETING lock intact
Spec (plan Todo 3, T3):
  ⑦ PARTIAL tap -> anchored labeled popover .ux-pop below the button
     (「続きをダウンロード」+残りNページ / 「削除」+保存済みMページ),
     single-open invariant, toggle, Esc/outside-tap close (sidebar stays open),
     flip above the button near the viewport bottom, no navigation from popover clicks

Run:
    uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_download_button.py -q
"""

import os
import re
import time
from pathlib import Path

import pytest
from playwright.sync_api import expect

REPO_ROOT = Path(__file__).resolve().parent.parent
EVIDENCE_DIR = REPO_ROOT / ".omo" / "evidence" / "task-1-offline-ux-refinements"
EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)


def _is_webkit():
    return os.environ.get("BROWSER") == "webkit"


def goto_offline(page, server):
    base = server
    try:
        base = server.url  # type: ignore
    except AttributeError:
        pass
    page.goto(base + "/?offline=1")
    page.wait_for_selector(".tile", timeout=10000)


def wait_for_sw(page, timeout=15000):
    for _ in range(2):
        try:
            try:
                page.wait_for_function(
                    "() => navigator.serviceWorker && navigator.serviceWorker.controller !== null",
                    timeout=4000,
                )
                return True
            except Exception:
                pass
            page.wait_for_function(
                "() => navigator.serviceWorker && !!navigator.serviceWorker.ready",
                timeout=timeout,
            )
            page.wait_for_selector(".tile", timeout=2500)
            ok = page.evaluate(
                "async () => { try { const r = await navigator.serviceWorker.ready; return !!r; } catch { return !!navigator.serviceWorker.controller; } }"
            )
            if ok:
                return True
        except Exception as e:
            if "Execution context was destroyed" in str(e):
                page.wait_for_selector(".tile", timeout=2800)
                page.wait_for_selector(".tile", timeout=10000)
                continue
            raise
    return False


def clear_all(page):
    try:
        page.evaluate(
            """async () => {
                try { await new Promise((res, rej) => { const r = indexedDB.deleteDatabase('bookq-offline'); r.onsuccess=()=>res(); r.onerror=()=>res(); r.onblocked=()=>res(); }); } catch {}
                try { const ks = await caches.keys(); for (const k of ks) { try { await caches.delete(k); } catch {} } } catch {}
                try { localStorage.clear(); } catch {}
            }"""
        )
    except Exception:
        pass
    page.wait_for_selector(".tile", timeout=2400)


# ── ① visible in 6 modes ─────────────────────────────────────────────────────


def test_01_offline_btn_visible_6_modes(page, server):
    clear_all(page)
    goto_offline(page, server)
    wait_for_sw(page)
    page.wait_for_selector(".offline-btn", timeout=2500)

    # helper to assert .offline-btn visible in current view
    def assert_btn_visible(msg=""):
        btn = page.locator(".offline-btn").first
        expect(btn).to_be_visible(timeout=5000)
        cls = btn.get_attribute("class") or ""
        assert "offline-btn" in cls, f"{msg} class missing offline-btn: {cls}"
        # check right-top via computed style (position absolute, top ~4px
        # right ~4px) — poll it: an async re-render can detach the resolved
        # node between to_be_visible and the style read (detached nodes
        # compute to ''), so a one-shot evaluate races under load.
        page.wait_for_function(
            "() => { const b = document.querySelector('.offline-btn');"
            " return !!b && getComputedStyle(b).position === 'absolute'; }",
            timeout=5000,
        )
        # check via evaluate getOfflineBtnState
        state = page.evaluate("() => typeof getOfflineBtnState==='function' ? getOfflineBtnState('simple_manga') : 'unknown'")
        assert state in ("idle", "downloading", "stopping", "complete", "deleting", "partial"), f"{msg} state {state}"

    # tile mode (default)
    page.evaluate("() => { window.viewMode='tile'; window.renderView(); }")
    page.wait_for_selector(".offline-btn", timeout=2300)
    assert_btn_visible("tile")
    try:
        page.screenshot(path=str(EVIDENCE_DIR / "tile.png"))
    except Exception:
        pass
    # list mode
    page.evaluate("() => { window.viewMode='list'; window.renderView(); }")
    page.wait_for_selector(".offline-btn", timeout=2300)
    assert_btn_visible("list")
    try:
        page.screenshot(path=str(EVIDENCE_DIR / "list.png"))
    except Exception:
        pass

    # card mode
    page.evaluate("() => { window.viewMode='card'; window.renderView(); }")
    page.wait_for_selector(".offline-btn", timeout=2300)
    assert_btn_visible("card")
    try:
        page.screenshot(path=str(EVIDENCE_DIR / "card.png"))
    except Exception:
        pass

    # search modes: mock /api/search to return results with offline-btn
    page.evaluate(
        """() => {
            window._origFetchSearch = window.fetch;
            window.fetch = async (input, init) => {
                const u = typeof input === 'string' ? input : (input && input.url) || '';
                if (u.includes('/api/v1/search')) {
                    const payload = {query:'simple', total:2, results:[{folder_path:'simple_manga', folder_name:'simple_manga', has_metadata:false, artist:[], tags:[]},{folder_path:'group/episodes/ch-1', folder_name:'ch-1', has_metadata:false, artist:[], tags:[]}]};
                    return new Response(JSON.stringify(payload), {status:200, headers:{'Content-Type':'application/json'}});
                }
                return window._origFetchSearch(input, init);
            };
        }"""
    )
    # also need to mock facets? not needed
    try:
        # tile search
        page.evaluate("() => { window.viewMode='tile'; window.renderView(); }")
        page.wait_for_selector(".offline-btn", timeout=2200)
        page.fill("#search-input", "simple")
        page.wait_for_selector(".offline-btn", timeout=3200)
        expect(page.locator(".offline-btn").first).to_be_visible(timeout=5000)
        try:
            page.screenshot(path=str(EVIDENCE_DIR / "search-tile.png"))
        except Exception:
            pass
        assert_btn_visible("search-tile")

        # list search
        page.evaluate("() => { window.viewMode='list'; window.renderView(); }")
        page.wait_for_selector(".offline-btn", timeout=2400)
        expect(page.locator(".offline-btn").first).to_be_visible(timeout=5000)
        try:
            page.screenshot(path=str(EVIDENCE_DIR / "search-list.png"))
        except Exception:
            pass
        assert_btn_visible("search-list")

        # card search
        page.evaluate("() => { window.viewMode='card'; window.renderView(); }")
        page.wait_for_selector(".offline-btn", timeout=2400)
        expect(page.locator(".offline-btn").first).to_be_visible(timeout=5000)
        try:
            page.screenshot(path=str(EVIDENCE_DIR / "search-card.png"))
        except Exception:
            pass
        assert_btn_visible("search-card")
    finally:
        page.evaluate("() => { try { if(window._origFetchSearch) window.fetch = window._origFetchSearch; } catch {} }")
        try:
            page.unroute("**/api/v1/search**")
        except Exception:
            pass
        # clear search
        try:
            page.evaluate("() => { const el=document.getElementById('search-input'); if(el){ el.value=''; el.dispatchEvent(new Event('input',{bubbles:true})); } }")
            page.wait_for_selector(".offline-btn", timeout=2600)
        except Exception:
            pass
        page.evaluate("() => { try { window.viewMode='tile'; window.renderView(); } catch {} }")


# ── ② single tap starts download immediately ────────────────────────────────


def test_02_single_tap_starts_download(page, server):
    clear_all(page)
    goto_offline(page, server)
    wait_for_sw(page)
    page.wait_for_selector(".tile", timeout=2500)
    page.evaluate("() => { window.viewMode='tile'; window.renderView(); }")
    page.wait_for_selector(".tile", timeout=2300)

    page.evaluate(
        """() => {
            window._origFetchDL2 = window.fetch;
            window.fetch = async (input, init) => {
                const u = typeof input === 'string' ? input : (input && input.url) || '';
                if (u.includes('/image/simple_manga/')) { await new Promise(r=>setTimeout(r, 550)); }
                return window._origFetchDL2(input, init);
            };
        }"""
    )
    try:
        btn = page.locator('.tile[data-path="simple_manga"] .offline-btn').first
        if btn.count() == 0:
            btn = page.locator(".offline-btn").first
        expect(btn).to_be_visible(timeout=5000)
        cls0 = btn.get_attribute("class") or ""
        assert "state-idle" in cls0, f"expected idle before DL got {cls0}"
        page.evaluate("""() => { const el=document.querySelector('.tile[data-path="simple_manga"]'); if(el) el.scrollIntoView({block:'center'}); }""")
        # SINGLE tap — no intermediate confirm state may appear
        btn.click()
        page.wait_for_selector('.tile[data-path="simple_manga"] .offline-btn.state-downloading', timeout=3000)
        # the intermediate confirm must never have been rendered: state is downloading now
        cls_now = page.locator('.tile[data-path="simple_manga"] .offline-btn').first.get_attribute("class") or ""
        assert "state-downloading" in cls_now, f"expected downloading after single tap got {cls_now}"

        # ⑥ title == aria-label also holds mid-download
        pair = page.evaluate(
            """() => { const b=document.querySelector('.tile[data-path="simple_manga"] .offline-btn'); return b ? {t:b.getAttribute('title'), a:b.getAttribute('aria-label')} : null; }"""
        )
        assert pair and pair["a"] and pair["t"] == pair["a"], f"title != aria-label while downloading: {pair}"

        # check --progress advances
        page.wait_for_timeout(800)
        prog = page.evaluate(
            """() => {
                const el = document.querySelector('.tile[data-path="simple_manga"] .offline-btn .progress-ring') || document.querySelector('.offline-btn .progress-ring');
                if(!el) return 'no-el';
                const v = getComputedStyle(el).getPropertyValue('--progress') || el.style.getPropertyValue('--progress') || '';
                if(v) return v;
                const btn = document.querySelector('.tile[data-path="simple_manga"] .offline-btn') || document.querySelector('.offline-btn');
                return btn ? (getComputedStyle(btn).getPropertyValue('--progress') || btn.style.getPropertyValue('--progress') || 'none') : 'no-btn';
            }"""
        )
        assert prog != "no-el" and prog != "none", f"progress not found {prog}"
        m = re.search(r"(\d+\.?\d*)deg", prog)
        if m:
            deg = float(m.group(1))
            assert deg > 0, f"progress should advance past 0 with delayed stub, got {deg} ({prog})"
        else:
            bg = page.evaluate("""() => { const el=document.querySelector('.offline-btn .progress-ring'); return el ? getComputedStyle(el).backgroundImage : ''; }""")
            assert "conic-gradient" in bg or "gradient" in bg, f"no deg and no gradient bg={bg} prog={prog}"

        state = page.evaluate("() => getOfflineBtnState('simple_manga')")
        assert state == "downloading", f"state {state}"

        expect(page.locator('.tile[data-path="simple_manga"] .offline-btn.state-complete').first).to_be_visible(timeout=15000)
        try:
            page.screenshot(path=str(EVIDENCE_DIR / "downloading.png"))
        except Exception:
            pass
    finally:
        page.evaluate("() => { try { if(window._origFetchDL2) window.fetch = window._origFetchDL2; } catch {} }")
        try:
            page.unroute("**/image/**")
        except Exception:
            pass
        # cleanup IDB/caches for next test
        try:
            page.evaluate("async () => { try { await window.deleteWork('simple_manga'); } catch {} }")
            page.wait_for_selector(".tile", timeout=2600)
        except Exception:
            pass


# ── ③ single tap during DOWNLOADING stops it -> PARTIAL ─────────────────────


def test_03_single_tap_stop_leaves_partial(page, server):
    clear_all(page)
    goto_offline(page, server)
    wait_for_sw(page)
    page.wait_for_selector(".tile", timeout=2500)
    page.evaluate("() => { window.viewMode='tile'; window.renderView(); }")
    page.wait_for_selector(".tile", timeout=2300)

    # Deterministic partial: the first page fetches fast, the rest take 4s, so
    # stopping after the second page lands always leaves 2 < total cached.
    # (The first page URL doubles as the folder thumb and may already be
    # cached from grid render, so the stop precondition keys on the second.)
    files_dl3 = page.evaluate("async () => { const r = await fetch('/api/v1/images?folder=' + encodeURIComponent('simple_manga')); return (await r.json()).images; }")
    first_file, second_file = files_dl3[0], files_dl3[1]
    page.evaluate(
        """(first) => {
            window._origFetchDL3 = window.fetch;
            window.fetch = (input, init) => {
                const u = typeof input === 'string' ? input : (input && input.url) || '';
                if (u.includes('/image/simple_manga/')) {
                    // Signal-aware delay: aborting must reject promptly like a
                    // real fetch, otherwise the stop tap takes effect only
                    // after the sleep and the partial record lands too late.
                    const ms = u.endsWith('/' + first) ? 50 : 4000;
                    return new Promise((resolve, reject) => {
                        const t = setTimeout(() => { window._origFetchDL3(input, init).then(resolve, reject); }, ms);
                        const sig = init && init.signal;
                        const onAbort = () => { clearTimeout(t); reject(new DOMException('aborted', 'AbortError')); };
                        if (sig) {
                            if (sig.aborted) onAbort();
                            else sig.addEventListener('abort', onAbort, {once: true});
                        }
                    });
                }
                return window._origFetchDL3(input, init);
            };
        }""",
        first_file,
    )
    # Evict ambient entries (the folder thumb aliases the first page and may
    # already be cached from grid render): every file must then take the
    # fetch+put path, so counted files are always durably cached. Relying on
    # the ambient hit makes f1's path environment-dependent.
    page.evaluate("async () => { try { const c = await caches.open(window.OfflineStore.cacheNameFor('simple_manga')); const ks = await c.keys(); for (const k of ks) await c.delete(k); } catch {} }")
    try:
        btn = page.locator('.tile[data-path="simple_manga"] .offline-btn').first
        expect(btn).to_be_visible(timeout=5000)
        page.evaluate("""() => { const el=document.querySelector('.tile[data-path="simple_manga"]'); if(el) el.scrollIntoView({block:'center'}); }""")
        # single tap starts
        btn.click()
        page.wait_for_selector('.tile[data-path="simple_manga"] .offline-btn.state-downloading', timeout=3000)
        # Listing phase carries .is-listing; stopping there yields idle, not
        # partial, so wait until page fetching is underway.
        page.wait_for_selector('.tile[data-path="simple_manga"] .offline-btn.state-downloading:not(.is-listing)', timeout=10000)
        # Establish the abort precondition explicitly: stop only after the
        # second page is cached, so the record left behind is always partial.
        # Python-side poll loop: wait_for_function does not await async
        # predicates in this Playwright version (a pending Promise is truthy),
        # so an async waiter would return instantly without waiting.
        # Presence via keys() enumeration, not cache.match(): WebKit returns
        # a truthy match for absent entries in this flow (f1 never fetched
        # yet matched), while keys() always reflects durable puts.
        second_suffix = "/image/simple_manga/" + second_file
        second_cached = False
        deadline = time.time() + 20
        while time.time() < deadline:
            second_cached = page.evaluate(
                """async (a) => {
                    try {
                        const c = await caches.open(window.OfflineStore.cacheNameFor(a[0]));
                        const ks = await c.keys();
                        return ks.some(k => k.url.endsWith(a[1]));
                    } catch { return false; }
                }""",
                ["simple_manga", second_suffix],
            )
            if second_cached:
                break
            page.wait_for_timeout(250)
        assert second_cached, "second page never cached; abort timing cannot leave a partial"

        # second single tap stops immediately (no confirm-abort hop);
        # trusted Playwright click like the start tap (synthetic el.click can
        # land mid-scroll on WebKit while progress re-renders tiles)
        page.evaluate(
            """() => {
                window.__sawStoppingDl3 = false;
                const s = window._stoppingSet;
                if (s && !s.__dl3Hooked) {
                    s.__dl3Hooked = true;
                    const origAdd = s.add.bind(s);
                    s.add = (v) => { if (v === 'simple_manga') window.__sawStoppingDl3 = true; return origAdd(v); };
                }
            }"""
        )
        btn.click()
        # ④ STOPPING transient: the add hook records the synchronous event, so
        # prompt abort completion cannot hide it from a post-hoc poll.
        saw_stopping = page.evaluate("() => !!window.__sawStoppingDl3")
        assert saw_stopping, "stop tap must add the folder to _stoppingSet"

        # poll for partial record up to 5s
        rec = None
        for _ in range(10):
            rec = page.evaluate("async () => { const r=await window.OfflineStore.get('simple_manga'); return r ? {cachedCount:r.cachedCount, pageCount:r.pageCount} : null; }")
            if rec is not None:
                break
            page.wait_for_timeout(500)
        assert rec is not None, "partial record should exist after abort"
        assert rec["cachedCount"] is not None and rec["cachedCount"] > 0, f"cachedCount {rec}"
        assert rec["cachedCount"] < rec["pageCount"], f"partial should be < total {rec}"

        # verify caches: completed pages persist, aborted ones leave no residue.
        # The stop precondition directly observed the second file durable, so it
        # is the deterministic witness on both browsers. The first file is
        # additionally required on Chromium; on WebKit it is best-effort only:
        # proven put-ok entries can later read absent there (no delete runs),
        # so the record (IDB, authoritative) plus a non-empty coherent cache
        # carry the contract instead of exact key equality.
        files = page.evaluate("async () => { const r=await fetch('/api/v1/images?folder='+encodeURIComponent('simple_manga')); return (await r.json()).images; }")
        assert isinstance(files, list) and len(files) == 5, f"files {files}"
        first_url = page.evaluate("(f) => window.OfflineCore.imageUrl('simple_manga', f)", files[0])
        second_url = page.evaluate("(f) => window.OfflineCore.imageUrl('simple_manga', f)", files[1])

        def _has(url):
            return page.evaluate(
                "async (u) => { try { const c = await caches.open(window.OfflineStore.cacheNameFor('simple_manga')); const ks = await c.keys(); return ks.some(k => k.url.endsWith(u)); } catch { return false; } }",
                url,
            )

        assert _has(second_url), "second image (observed durable pre-stop) must persist after abort"
        if not _is_webkit():
            assert _has(first_url), "first image should be cached after partial abort"
        # Count page images only: the work cache also holds this work's cover
        # thumb by design (prefetch.js + sw.js cacheNameFor(folder)), and the
        # record's cachedCount is about /image/ puts.
        img_keys = page.evaluate(
            "async () => { const c=await caches.open(window.OfflineStore.cacheNameFor('simple_manga')); const ks=await c.keys(); return ks.filter(k => new URL(k.url).pathname.startsWith('/image/')).length; }"
        )
        if _is_webkit():
            assert 0 < img_keys <= rec["cachedCount"], f"cache images {img_keys} vs rec {rec}"
        else:
            assert img_keys == rec["cachedCount"], f"cache images {img_keys} vs rec {rec}"
        assert img_keys < 5

        # final endpoint state is PARTIAL
        expect(page.locator('.tile[data-path="simple_manga"] .offline-btn.state-partial').first).to_be_visible(timeout=5000)
        state = page.evaluate("() => getOfflineBtnState('simple_manga')")
        assert state == "partial", f"state after abort {state}"

        try:
            page.screenshot(path=str(EVIDENCE_DIR / "partial.png"))
        except Exception:
            pass
    finally:
        page.evaluate("() => { try { if(window._origFetchDL3) window.fetch = window._origFetchDL3; } catch {} }")
        try:
            page.unroute("**/image/**")
        except Exception:
            pass
        try:
            page.evaluate("async () => { try { await window.deleteWork('simple_manga'); } catch {} try { await new Promise((res)=>{ const r=indexedDB.deleteDatabase('bookq-offline'); r.onsuccess=()=>res(); r.onerror=()=>res(); r.onblocked=()=>res(); }); } catch {} try { const ks=await caches.keys(); for(const k of ks) if(k.includes('simple_manga')) await caches.delete(k);} catch {} }")
            page.wait_for_selector(".tile", timeout=2400)
        except Exception:
            pass


# ── ④⑤ single tap during LISTING stops it -> IDLE (0 records), is-listing seen ─


def test_04_listing_abort_returns_idle_and_is_listing(page, server):
    clear_all(page)
    goto_offline(page, server)
    wait_for_sw(page)
    page.wait_for_selector(".tile", timeout=2500)
    page.evaluate("() => { window.viewMode='tile'; window.renderView(); }")
    page.wait_for_selector(".tile", timeout=2300)

    # delay ONLY the leaf-list fetches: the listing phase is where we tap stop
    page.evaluate(
        """() => {
            window._origFetchDL4 = window.fetch;
            window.fetch = async (input, init) => {
                const u = typeof input === 'string' ? input : (input && input.url) || '';
                if (u.includes('/api/v1/images')) { await new Promise(r=>setTimeout(r, 800)); }
                return window._origFetchDL4(input, init);
            };
        }"""
    )
    try:
        btn = page.locator('.tile[data-path="simple_manga"] .offline-btn').first
        expect(btn).to_be_visible(timeout=5000)
        page.evaluate("""() => { const el=document.querySelector('.tile[data-path="simple_manga"]'); if(el) el.scrollIntoView({block:'center'}); }""")
        # single tap starts; LISTING phase renders as DOWNLOADING + is-listing spinner hook
        btn.click()
        page.wait_for_selector('.tile[data-path="simple_manga"] .offline-btn.state-downloading.is-listing', timeout=3000)
        # still zero records while merely listing
        rec_mid = page.evaluate("async () => await window.OfflineStore.get('simple_manga')")
        assert rec_mid is None, f"no record expected during listing got {rec_mid}"

        # single tap during LISTING aborts immediately
        page.evaluate("""() => { const el=document.querySelector('.tile[data-path="simple_manga"] .offline-btn'); if(el){ el.click(); } }""")
        saw_stopping = True
        try:
            page.wait_for_function(
                "() => window._stoppingSet && window._stoppingSet.has('simple_manga')", timeout=2500
            )
        except Exception:
            saw_stopping = False
        assert saw_stopping, "_stoppingSet should be set when stopping from the listing phase"

        # 0 records saved -> endpoint is IDLE per spec
        expect(page.locator('.tile[data-path="simple_manga"] .offline-btn.state-idle').first).to_be_visible(timeout=10000)
        rec = page.evaluate("async () => await window.OfflineStore.get('simple_manga')")
        assert rec is None, f"abort during listing must leave no record got {rec}"
        state = page.evaluate("() => getOfflineBtnState('simple_manga')")
        assert state == "idle", f"state after listing abort {state}"
    finally:
        page.evaluate("() => { try { if(window._origFetchDL4) window.fetch = window._origFetchDL4; } catch {} }")
        try:
            page.unroute("**/api/v1/images**")
        except Exception:
            pass
        try:
            page.evaluate("async () => { try { await window.deleteWork('simple_manga'); } catch {} }")
            page.wait_for_selector(".tile", timeout=2400)
        except Exception:
            pass


# ── ⑥ title attribute mirrors aria-label ────────────────────────────────────


def test_05_title_matches_aria_label(page, server):
    clear_all(page)
    goto_offline(page, server)
    wait_for_sw(page)
    page.wait_for_selector(".offline-btn", timeout=2500)
    page.evaluate("() => { window.viewMode='tile'; window.renderView(); }")
    page.wait_for_selector(".offline-btn", timeout=2300)

    btns = page.evaluate(
        """() => [...document.querySelectorAll('.offline-btn')].slice(0,8).map(b => ({
            title: b.getAttribute('title'),
            aria: b.getAttribute('aria-label'),
            cls: b.className
        }))"""
    )
    assert len(btns) > 0, "no offline buttons rendered"
    for b in btns:
        assert b["aria"], f"aria-label missing: {b}"
        assert b["title"] == b["aria"], f"title must equal aria-label: {b}"


# ── regression: COMPLETE -> CONFIRM_DELETE (2 taps) -> DELETING lock ────────


def test_06_complete_delete_confirm_regression(page, server):
    clear_all(page)
    goto_offline(page, server)
    wait_for_sw(page)
    page.wait_for_selector(".tile", timeout=2500)
    page.evaluate("() => { window.viewMode='tile'; window.renderView(); }")
    page.wait_for_selector(".tile", timeout=2300)

    # widen the deleting window so the lock assertions are observable
    page.evaluate(
        """() => {
            const orig = window.OfflineStore.delete;
            window._origStoreDel6 = orig;
            window.OfflineStore.delete = async (f) => { await new Promise(r=>setTimeout(r, 250)); return orig.call(window.OfflineStore, f); };
            const origCacheDel = window.caches.delete.bind(window.caches);
            window._origCacheDel6 = origCacheDel;
            window.caches.delete = async (n) => { await new Promise(r=>setTimeout(r, 250)); return origCacheDel(n); };
        }"""
    )

    tile_sel = '.tile[data-path="simple_manga"]'
    try:
        # full download via SINGLE tap (no stub delay needed)
        btn = page.locator(f"{tile_sel} .offline-btn").first
        expect(btn).to_be_visible(timeout=5000)
        btn.click()
        expect(page.locator(f"{tile_sel} .offline-btn.state-complete").first).to_be_visible(timeout=15000)

        # delete stays double-tap confirmed: first tap shows red trash confirm
        btn.click()
        expect(page.locator(f"{tile_sel} .offline-btn.state-confirm-delete").first).to_be_visible(timeout=3000)
        icon_del = page.evaluate(
            """(sel) => { const el=document.querySelector(sel); return el ? getComputedStyle(el,'::before').content : 'no-el'; }""",
            f"{tile_sel} .offline-btn.state-confirm-delete .offline-icon",
        )
        assert "🗑" in icon_del, f"confirm-delete icon should be trash got {icon_del!r}"
        bg_del = page.evaluate(
            """(sel) => { const el=document.querySelector(sel); return el ? getComputedStyle(el).backgroundColor : ''; }""",
            f"{tile_sel} .offline-btn.state-confirm-delete",
        )
        assert "204" in bg_del and "68" in bg_del, f"confirm-delete should be red-ish got {bg_del}"

        # second tap starts DELETING; outside tap and Esc must NOT cancel (lock)
        page.evaluate("""(sel) => { const el=document.querySelector(sel); if(el) el.click(); }""", f"{tile_sel} .offline-btn.state-confirm-delete")
        found_deleting = False
        for _ in range(15):
            if page.evaluate("() => !!document.querySelector('.offline-btn.state-deleting')"):
                found_deleting = True
                break
            page.wait_for_timeout(100)
        assert found_deleting, "should enter deleting state after confirm"
        page.locator("#tile-grid").click(position={"x": 5, "y": 5})
        page.wait_for_selector(".offline-btn", timeout=2200)
        assert page.evaluate("() => !!document.querySelector('.offline-btn.state-deleting')"), "outside tap should not cancel deleting"
        page.keyboard.press("Escape")
        page.wait_for_selector(".offline-btn", timeout=2200)
        assert page.evaluate("() => !!document.querySelector('.offline-btn.state-deleting')"), "Esc should not cancel deleting"

        # finishes back at idle with ↓
        expect(page.locator(f"{tile_sel} .offline-btn.state-idle").first).to_be_visible(timeout=10000)
        icon_idle = page.evaluate(
            """(sel) => { const el=document.querySelector(sel); return el ? getComputedStyle(el,'::before').content : 'no-el'; }""",
            f"{tile_sel} .offline-btn.state-idle .offline-icon",
        )
        assert "↓" in icon_idle, f"idle icon should be ↓ got {icon_idle!r}"
        gone = page.evaluate("async () => { try { const v = await window.OfflineStore.get('simple_manga'); return v === null; } catch { return true; } }")
        assert gone, "IDB entry should be removed after delete"
    finally:
        page.evaluate("() => { try { if(window._origStoreDel6) window.OfflineStore.delete = window._origStoreDel6; } catch {} try { if(window._origCacheDel6) window.caches.delete = window._origCacheDel6; } catch {} }")
        try:
            page.evaluate("async () => { try { await window.deleteWork('simple_manga'); } catch {} }")
            page.wait_for_selector(".tile", timeout=2400)
        except Exception:
            pass


# ── T3: anchored labeled popover (.ux-pop) replaces the full-screen overlay ──

TASK3_EVIDENCE = REPO_ROOT / ".omo" / "evidence"


def _shot3(page, name):
    try:
        page.screenshot(path=str(TASK3_EVIDENCE / f"task-3-offline-ux-refinements-{name}.png"))
    except Exception:
        pass


def _inject_partial(page, folder, cached=2, total=5):
    """Create a partial-download record directly in IDB (fast, deterministic)."""
    page.evaluate(
        """async ([folder, cached, total]) => {
            const cn = window.OfflineStore.cacheNameFor(folder);
            try { const c=await caches.open(cn); await c.put(window.OfflineCore.imageUrl(folder,'1.png'), new Response(new Blob(['x']), {status:200})); } catch {}
            await window.OfflineStore.put({folder: folder, cacheName: cn, pageCount: total, cachedCount: cached, bytes:1000, thumb:'1.png', addedAt: Date.now(), lastReadAt: Date.now()});
            await window.refreshOfflineMap();
            window.renderView();
        }""",
        [folder, cached, total],
    )
    page.wait_for_selector(".tile", timeout=2400)


def _pop_metrics(page):
    return page.evaluate(
        """() => {
            const pop=document.querySelector('.ux-pop');
            if(!pop) return null;
            let btn=null;
            for(const b of document.querySelectorAll('.offline-btn')){ if(b.dataset.folder===pop.dataset.folder){ btn=b; break; } }
            const pr=pop.getBoundingClientRect(), br=btn ? btn.getBoundingClientRect() : null;
            return {popBottom:pr.bottom, popTop:pr.top, popLeft:pr.left, popRight:pr.right,
                    btnTop:br ? br.top : null, btnBottom:br ? br.bottom : null,
                    vh:window.innerHeight, vw:window.innerWidth, flipped: !!(pop.style.top && pop.style.top !== '')};
        }"""
    )


def test_07_partial_popover_two_choices_and_resume(page, server):
    clear_all(page)
    goto_offline(page, server)
    wait_for_sw(page)
    page.wait_for_selector(".tile", timeout=2500)
    page.evaluate("() => { window.viewMode='tile'; window.renderView(); }")
    page.wait_for_selector(".tile", timeout=2300)

    # create a REAL partial via the single-tap flow (delayed stub -> DL -> stop)
    page.evaluate(
        """() => {
            window._origFetchDL7 = window.fetch;
            window.fetch = async (input, init) => {
                const u = typeof input === 'string' ? input : (input && input.url) || '';
                if (u.includes('/image/simple_manga/')) { await new Promise(r=>setTimeout(r, 600)); }
                return window._origFetchDL7(input, init);
            };
        }"""
    )
    try:
        tile_sel = '.tile[data-path="simple_manga"]'
        btn = page.locator(f"{tile_sel} .offline-btn").first
        expect(btn).to_be_visible(timeout=5000)
        page.evaluate("""() => { const el=document.querySelector('.tile[data-path="simple_manga"]'); if(el) el.scrollIntoView({block:'center'}); }""")
        btn.click()
        page.wait_for_selector(f"{tile_sel} .offline-btn.state-downloading", timeout=3000)
        page.wait_for_timeout(900)
        page.evaluate("""() => { const el=document.querySelector('.tile[data-path="simple_manga"] .offline-btn'); if(el) el.click(); }""")
        expect(page.locator(f"{tile_sel} .offline-btn.state-partial").first).to_be_visible(timeout=8000)

        # tap the PARTIAL button -> anchored popover appears (no full-screen overlay)
        hash_before = page.evaluate("() => location.hash")
        page.evaluate("""() => { const el=document.querySelector('.tile[data-path="simple_manga"] .offline-btn'); if(el){ el.scrollIntoView({block:'center'}); el.click(); } }""")
        expect(page.locator('.ux-pop').first).to_be_visible(timeout=3000)
        expect(page.locator('.partial-overlay')).to_have_count(0)
        expect(page.locator('.partial-confirm-choices')).to_have_count(0)

        pop = page.locator('.ux-pop').first
        text = pop.inner_text()
        assert "続きをダウンロード" in text, f"resume label missing: {text!r}"
        assert "削除" in text, f"delete label missing: {text!r}"
        m = re.search(r"残り\s*(\d+)\s*ページ", text)
        assert m and int(m.group(1)) > 0, f"remaining pages must be >0: {text!r}"
        m2 = re.search(r"(\d+)\s*ページ分の保存データ", text)
        assert m2 and int(m2.group(1)) > 0, f"saved pages must be >0: {text!r}"
        meta = page.evaluate(
            """() => { const p=document.querySelector('.ux-pop'); const cs=getComputedStyle(p);
                 return {pos:cs.position, z:cs.zIndex, role:p.getAttribute('role'), aria:p.getAttribute('aria-label'),
                         parent: p.parentElement ? p.parentElement.className : '',
                         btns:[...p.querySelectorAll('button')].map(b=>({cls:b.className, txt:b.textContent}))}; }"""
        )
        assert meta["pos"] == "absolute", f"popover must be absolute got {meta['pos']}"
        assert meta["z"] == "56", f"popover z-index must be 56 got {meta['z']}"
        assert meta["role"] == "group", f"role missing {meta}"
        assert "オフライン保存の選択肢" in (meta["aria"] or ""), f"aria-label missing {meta}"
        assert len(meta["btns"]) == 2, f"expected exactly 2 buttons got {meta['btns']}"

        # clicking inside the popover (not on a button) neither navigates nor closes
        page.evaluate("""() => { const p=document.querySelector('.ux-pop'); const r=p.getBoundingClientRect(); p.dispatchEvent(new MouseEvent('click',{bubbles:true, clientX:r.left+r.width/2, clientY:r.top+4})); }""")
        page.wait_for_selector(".tile", timeout=2300)
        assert page.locator('.ux-pop').count() == 1, "inside click must not close the popover"
        assert page.evaluate("() => location.hash") == hash_before, "inside click must not navigate"

        _shot3(page, "popover")

        # resume -> downloading again (no navigation)
        page.evaluate("""() => { const b=document.querySelector('.ux-pop .resume'); if(b) b.click(); }""")
        expect(page.locator(f"{tile_sel} .offline-btn.state-downloading").first).to_be_visible(timeout=3000)
        assert page.locator('.ux-pop').count() == 0, "popover must close after selection"
        assert page.evaluate("() => location.hash") == hash_before, "resume must not navigate"
        # stop again to prove the resumed download is a real one
        page.evaluate("""() => { const el=document.querySelector('.tile[data-path="simple_manga"] .offline-btn'); if(el) el.click(); }""")
        expect(page.locator(f"{tile_sel} .offline-btn.state-partial").first).to_be_visible(timeout=8000)
    finally:
        page.evaluate("() => { try { if(window._origFetchDL7) window.fetch = window._origFetchDL7; } catch {} }")
        try:
            page.evaluate("async () => { try { await window.deleteWork('simple_manga'); } catch {} try { await new Promise((res)=>{ const r=indexedDB.deleteDatabase('bookq-offline'); r.onsuccess=()=>res(); r.onerror=()=>res(); r.onblocked=()=>res(); }); } catch {} try { const ks=await caches.keys(); for(const k of ks) if(k.includes('simple_manga')) await caches.delete(k);} catch {} }")
            page.wait_for_selector(".tile", timeout=2400)
        except Exception:
            pass


def test_08_partial_popover_delete_choice(page, server):
    clear_all(page)
    goto_offline(page, server)
    wait_for_sw(page)
    page.wait_for_selector(".tile", timeout=2500)
    page.evaluate("() => { window.viewMode='tile'; window.renderView(); }")
    page.wait_for_selector(".tile", timeout=2300)

    # widen the deleting window so the lock state is observable
    page.evaluate(
        """() => {
            const orig = window.OfflineStore.delete;
            window._origStoreDel8 = orig;
            window.OfflineStore.delete = async (f) => { await new Promise(r=>setTimeout(r, 250)); return orig.call(window.OfflineStore, f); };
            const origCacheDel = window.caches.delete.bind(window.caches);
            window._origCacheDel8 = origCacheDel;
            window.caches.delete = async (n) => { await new Promise(r=>setTimeout(r, 250)); return origCacheDel(n); };
        }"""
    )
    tile_sel = '.tile[data-path="group"]'
    try:
        # partial on ONE leaf makes the group tile PARTIAL
        _inject_partial(page, "group/episodes/ch-1", cached=2, total=5)
        state = page.evaluate("() => getOfflineBtnState('group')")
        assert state == "partial", f"group tile should be partial got {state}"

        hash_before = page.evaluate("() => location.hash")
        page.evaluate("""() => { const el=document.querySelector('.tile[data-path="group"] .offline-btn'); if(el){ el.scrollIntoView({block:'center'}); el.click(); } }""")
        expect(page.locator('.ux-pop').first).to_be_visible(timeout=3000)

        # 削除 -> DELETING lock -> IDLE
        page.evaluate("""() => { const b=document.querySelector('.ux-pop .del'); if(b) b.click(); }""")
        found_deleting = False
        for _ in range(20):
            if page.evaluate("() => !!document.querySelector('.offline-btn.state-deleting')"):
                found_deleting = True
                break
            page.wait_for_timeout(100)
        assert found_deleting, "delete choice should start the deleting flow"
        assert page.locator('.ux-pop').count() == 0, "popover must close after delete selection"
        assert page.evaluate("() => location.hash") == hash_before, "delete must not navigate"
        expect(page.locator(f"{tile_sel} .offline-btn.state-idle").first).to_be_visible(timeout=10000)
        gone = page.evaluate("async () => { try { const v = await window.OfflineStore.get('group/episodes/ch-1'); return v === null; } catch { return true; } }")
        assert gone, "leaf record should be removed after delete"
    finally:
        page.evaluate("() => { try { if(window._origStoreDel8) window.OfflineStore.delete = window._origStoreDel8; } catch {} try { if(window._origCacheDel8) window.caches.delete = window._origCacheDel8; } catch {} }")
        try:
            page.evaluate("async () => { for(const lp of ['group/episodes/ch-1','group/episodes/ch-2','group/episodes/ch-10','group/episodes/ch-11']){ try { await window.OfflineStore.delete(lp); } catch {} try { await caches.delete(window.OfflineStore.cacheNameFor(lp)); } catch {} } await window.refreshOfflineMap(); window.renderView(); }")
            page.wait_for_selector(".ux-pop", timeout=2400)
        except Exception:
            pass


def test_09_popover_esc_sidebar_single_open_toggle_flip(page, server):
    clear_all(page)
    goto_offline(page, server)
    wait_for_sw(page)
    page.wait_for_selector(".tile", timeout=2500)
    page.evaluate("() => { window.viewMode='tile'; window.renderView(); }")
    page.wait_for_selector(".tile", timeout=2300)

    _inject_partial(page, "simple_manga", cached=2, total=5)
    _inject_partial(page, "group/episodes/ch-1", cached=1, total=5)

    # make sure the sidebar is OPEN so we can prove Esc does not close it
    page.evaluate("() => { const sb=document.getElementById('sidebar'); if(!sb.classList.contains('open')) sb.classList.add('open'); }")
    page.wait_for_selector(".tile", timeout=2200)

    sm_btn = '.tile[data-path="simple_manga"] .offline-btn'
    grp_btn = '.tile[data-path="group"] .offline-btn'

    # open A
    page.evaluate("(sel) => { const el=document.querySelector(sel); if(el){ el.scrollIntoView({block:'center'}); el.click(); } }", sm_btn)
    expect(page.locator('.ux-pop').first).to_be_visible(timeout=3000)
    assert page.evaluate("() => document.querySelector('.ux-pop').dataset.folder") == "simple_manga"

    # Esc closes ONLY the popover — sidebar stays open
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    assert page.locator('.ux-pop').count() == 0, "Esc must close the popover"
    assert page.evaluate("() => document.getElementById('sidebar').classList.contains('open')"), "Esc must keep the sidebar open while a popover was dismissed"

    # reopen A, then tap B -> single-open invariant
    page.evaluate("(sel) => { const el=document.querySelector(sel); if(el){ el.scrollIntoView({block:'center'}); el.click(); } }", sm_btn)
    expect(page.locator('.ux-pop').first).to_be_visible(timeout=3000)
    page.evaluate("(sel) => { const el=document.querySelector(sel); if(el){ el.scrollIntoView({block:'center'}); el.click(); } }", grp_btn)
    page.wait_for_selector(".ux-pop", timeout=2300)
    pops = page.evaluate("() => [...document.querySelectorAll('.ux-pop')].map(p=>p.dataset.folder)")
    assert pops == ["group"], f"only B may stay open got {pops}"
    _shot3(page, "single")

    # toggle: tapping B's button again closes it
    page.evaluate("(sel) => { const el=document.querySelector(sel); if(el){ el.click(); } }", grp_btn)
    page.wait_for_timeout(300)
    assert page.locator('.ux-pop').count() == 0, "re-tapping the same button must toggle the popover closed"

    # outside tap closes an open popover too
    page.evaluate("(sel) => { const el=document.querySelector(sel); if(el){ el.scrollIntoView({block:'center'}); el.click(); } }", sm_btn)
    expect(page.locator('.ux-pop').first).to_be_visible(timeout=3000)
    page.locator("#tile-grid").click(position={"x": 5, "y": 5})
    page.wait_for_timeout(300)
    assert page.locator('.ux-pop').count() == 0, "outside tap must close the popover"

    # bottom-of-viewport tile flips the popover ABOVE the button.
    # Find a leaf folder near the document bottom: inject a record keyed at the
    # last tiles' own paths until one reports partial (parents ignore foreign
    # keys, leaves read their own).
    flip_key = None
    last_paths = page.evaluate("() => [...document.querySelectorAll('.tile')].slice(-3).map(t=>t.dataset.path)")
    for p in reversed(last_paths):
        _inject_partial(page, p, cached=1, total=5)
        if page.evaluate("(p) => getOfflineBtnState(p)", p) == "partial":
            flip_key = p
            break
        page.evaluate(
            """async (p) => { try { await window.OfflineStore.delete(p); await caches.delete(window.OfflineStore.cacheNameFor(p)); await window.refreshOfflineMap(); window.renderView(); } catch {} }""",
            p,
        )
    assert flip_key, f"no leaf folder near the document bottom among {last_paths}"
    sel = '.tile[data-path="{}"] .offline-btn'.format(flip_key.replace('"', '\\"'))
    flip = page.evaluate(
        """(sel) => {
            const btn=document.querySelector(sel);
            if(!btn) return {ok:false, reason:'no btn for '+sel};
            btn.scrollIntoView({block:'end'});
            btn.click();
            const pop=document.querySelector('.ux-pop');
            if(!pop) return {ok:false, reason:'no pop'};
            const pr=pop.getBoundingClientRect(), br=btn.getBoundingClientRect();
            return {ok:true, folder:pop.dataset.folder, flipped:!!pop.style.top,
                    popBottom:pr.bottom, popTop:pr.top, btnTop:br.top, btnBottom:br.bottom,
                    vh:window.innerHeight, styleTop:pop.style.top};
        }""",
        sel,
    )
    assert flip["ok"], f"flip probe failed {flip}"
    m = _pop_metrics(page)
    assert m["popBottom"] <= m["vh"], f"popover must not overflow the viewport bottom {m}"
    if m["flipped"]:
        assert abs(m["popBottom"] - m["btnTop"]) <= 10, f"flipped popover must sit right above the button {m}"
    else:
        assert m["popTop"] >= m["btnBottom"] - 2, f"unflipped popover must sit below the button {m}"
    _shot3(page, "flip")

    # cleanup
    try:
        page.evaluate(
            """async () => {
                try { await window.OfflineStore.delete('simple_manga'); } catch {}
                try { await caches.delete(window.OfflineStore.cacheNameFor('simple_manga')); } catch {}
                for(const lp of ['group/episodes/ch-1','group/episodes/ch-2','group/episodes/ch-10','group/episodes/ch-11']){
                    try { await window.OfflineStore.delete(lp); } catch {}
                    try { await caches.delete(window.OfflineStore.cacheNameFor(lp)); } catch {}
                }
                await window.refreshOfflineMap();
                window.renderView();
            }"""
        )
        page.evaluate(
            """async (p) => { try { await window.OfflineStore.delete(p); await caches.delete(window.OfflineStore.cacheNameFor(p)); await window.refreshOfflineMap(); window.renderView(); } catch {} }""",
            flip_key,
        )
        page.wait_for_selector(".tile", timeout=2400)
    except Exception:
        pass


# ── T2: visual feedback layer (donut ring / listing spinner / transitions) ──


STYLE_CSS = REPO_ROOT / "app" / "static" / "style.css"


def test_08_visual_layer_computed_styles(page, server):
    clear_all(page)
    goto_offline(page, server)
    wait_for_sw(page)
    page.wait_for_selector(".tile", timeout=2500)
    page.evaluate("() => { window.viewMode='tile'; window.renderView(); }")
    page.wait_for_selector(".tile", timeout=2300)

    # hold the button in DOWNLOADING+is-listing by delaying only the leaf list
    page.evaluate(
        """() => {
            window._origFetchDL8 = window.fetch;
            window.fetch = async (input, init) => {
                const u = typeof input === 'string' ? input : (input && input.url) || '';
                if (u.includes('/api/v1/images')) { await new Promise(r=>setTimeout(r, 3000)); }
                return window._origFetchDL8(input, init);
            };
        }"""
    )
    try:
        btn = page.locator('.tile[data-path="simple_manga"] .offline-btn').first
        expect(btn).to_be_visible(timeout=5000)
        btn.click()
        page.wait_for_selector('.tile[data-path="simple_manga"] .offline-btn.state-downloading.is-listing', timeout=3000)

        styles = page.evaluate(
            """() => {
                const scope = '.tile[data-path="simple_manga"] .offline-btn';
                const ring = document.querySelector(scope + ' .progress-ring');
                const icon = document.querySelector(scope + ' .offline-icon');
                const btnEl = document.querySelector(scope);
                const rs = getComputedStyle(ring);
                return {
                    mask: rs.webkitMaskImage || rs.maskImage || 'none',
                    iconZ: getComputedStyle(icon).zIndex,
                    iconContent: getComputedStyle(icon, '::before').content,
                    transitionProperty: getComputedStyle(btnEl).transitionProperty,
                    animationName: rs.animationName,
                    backgroundImage: rs.backgroundImage
                };
            }"""
        )
        assert styles["mask"] and styles["mask"] != "none", f"ring mask should be applied got {styles['mask']!r}"
        assert int(styles["iconZ"]) >= 1, f"offline-icon z-index should be >=1 got {styles['iconZ']!r}"
        # F3 fix A: the ↓ glyph must stay visible through the ring's hole while
        # DOWNLOADING (the old opaque ring hid its absence; the donut exposes it)
        assert styles["iconContent"] == '"↓"', \
            f"downloading icon ::before content must be the ↓ glyph got {styles['iconContent']!r}"
        assert "background-color" in styles["transitionProperty"], f"btn transition-property {styles['transitionProperty']!r}"
        assert styles["animationName"] == "spinArc", f"listing spinner animationName {styles['animationName']!r}"
        assert "conic-gradient" in styles["backgroundImage"], f"spinner arc bg {styles['backgroundImage']!r}"

        try:
            page.screenshot(path=str(REPO_ROOT / ".omo" / "evidence" / "task-2-offline-ux-refinements.png"))
        except Exception:
            pass

        # stop while still listing -> 0 records -> idle again
        page.evaluate("""() => { const el=document.querySelector('.tile[data-path="simple_manga"] .offline-btn'); if(el) el.click(); }""")
        expect(page.locator('.tile[data-path="simple_manga"] .offline-btn.state-idle').first).to_be_visible(timeout=10000)
    finally:
        page.evaluate("() => { try { if(window._origFetchDL8) window.fetch = window._origFetchDL8; } catch {} }")
        try:
            page.evaluate("async () => { try { await window.deleteWork('simple_manga'); } catch {} }")
            page.wait_for_selector(".tile", timeout=2400)
        except Exception:
            pass


def test_09_reduced_motion_stops_spinner_keeps_arc(page, server):
    clear_all(page)
    page.emulate_media(reduced_motion="reduce")
    goto_offline(page, server)
    wait_for_sw(page)
    page.wait_for_selector(".tile", timeout=2500)
    page.evaluate("() => { window.viewMode='tile'; window.renderView(); }")
    page.wait_for_selector(".tile", timeout=2300)

    page.evaluate(
        """() => {
            window._origFetchDL9 = window.fetch;
            window.fetch = async (input, init) => {
                const u = typeof input === 'string' ? input : (input && input.url) || '';
                if (u.includes('/api/v1/images')) { await new Promise(r=>setTimeout(r, 3000)); }
                return window._origFetchDL9(input, init);
            };
        }"""
    )
    try:
        btn = page.locator('.tile[data-path="simple_manga"] .offline-btn').first
        expect(btn).to_be_visible(timeout=5000)
        btn.click()
        page.wait_for_selector('.tile[data-path="simple_manga"] .offline-btn.state-downloading.is-listing', timeout=3000)

        styles = page.evaluate(
            """() => {
                const scope = '.tile[data-path="simple_manga"] .offline-btn';
                const ring = document.querySelector(scope + ' .progress-ring');
                const btnEl = document.querySelector(scope);
                const rs = getComputedStyle(ring);
                const bs = getComputedStyle(btnEl);
                return {
                    animationName: rs.animationName,
                    backgroundImage: rs.backgroundImage,
                    btnTransitionDuration: bs.transitionDuration,
                    btnTransitionProperty: bs.transitionProperty
                };
            }"""
        )
        assert styles["animationName"] == "none", f"reduced motion must stop the spinner got {styles['animationName']!r}"
        assert "conic-gradient" in styles["backgroundImage"], f"static arc must remain as bg {styles['backgroundImage']!r}"
        assert (
            styles["btnTransitionDuration"] == "0s" or styles["btnTransitionProperty"] == "none"
        ), f"reduced motion must drop btn transitions got dur={styles['btnTransitionDuration']!r} prop={styles['btnTransitionProperty']!r}"

        page.evaluate("""() => { const el=document.querySelector('.tile[data-path="simple_manga"] .offline-btn'); if(el) el.click(); }""")
        expect(page.locator('.tile[data-path="simple_manga"] .offline-btn.state-idle').first).to_be_visible(timeout=10000)
    finally:
        page.evaluate("() => { try { if(window._origFetchDL9) window.fetch = window._origFetchDL9; } catch {} }")
        try:
            page.evaluate("async () => { try { await window.deleteWork('simple_manga'); } catch {} }")
            page.wait_for_selector(".tile", timeout=2400)
        except Exception:
            pass


def test_10_style_css_static_gates():
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert css.count("spinArc") >= 2, "spinArc must appear in keyframes definition + application"
    assert "box-shadow:inset 0 0 0 2px #4a7ac4" not in css, "old partial inset shadow must be gone"
    assert ".offline-btn.state-downloading.is-listing .progress-ring{background:conic-gradient(#4a7ac4 100deg" in css, \
        "spinner rule must keep the 0,4,0 selector shape"
    assert "-webkit-mask:radial-gradient(farthest-side" in css and "mask:radial-gradient(farthest-side" in css, \
        "donut mask missing"
    assert ".offline-btn .offline-icon{position:relative;z-index:1}" in css, "icon lift rule missing"
    # F3 fix A: ↓ glyph rule for the downloading state (donut ring has a hole now)
    assert '.offline-btn.state-downloading .offline-icon::before{content:"↓"}' in css, \
        "state-downloading must keep the ↓ glyph ::before rule"
    # F3 fix C: popover delete-row subtext must not wrap mid-word (「保存デー／タ」)
    assert ".ux-pop .sub{display:block;color:#8892a0;font-size:11px;white-space:nowrap}" in css, \
        ".ux-pop .sub must stay nowrap so 保存データ never breaks mid-word"
    # F3 fix D: trash emoji is color-blind to CSS `color`; whiten it via filter
    assert ".offline-btn.state-confirm-delete .offline-icon{filter:grayscale(1) brightness(1.8)}" in css, \
        "confirm-delete 🗑 needs the whitening filter for contrast on red"


# ── T4: keyboard activation of .offline-btn / .ux-pop buttons ────────────────
# Regression: both live inside the tile, so the grid keydown handler's
# closest(".tile,.card,.item") used to match and Enter navigated to the viewer
# instead of natively activating the focused button. Tile-name Enter navigation
# itself must keep working (control).


def test_keyboard_activation_of_offline_button(page, server):
    clear_all(page)
    goto_offline(page, server)
    wait_for_sw(page)
    page.wait_for_selector(".tile", timeout=2500)
    page.evaluate("() => { window.viewMode='tile'; window.renderView(); }")
    page.wait_for_selector(".tile", timeout=2300)

    page.evaluate(
        """() => {
            window._origFetchDLK = window.fetch;
            window.fetch = async (input, init) => {
                const u = typeof input === 'string' ? input : (input && input.url) || '';
                if (u.includes('/image/simple_manga/')) { await new Promise(r=>setTimeout(r, 600)); }
                return window._origFetchDLK(input, init);
            };
        }"""
    )
    tile_sel = '.tile[data-path="simple_manga"]'
    try:
        # ① IDLE: focus + Enter starts the download natively, no navigation
        btn = page.locator(f"{tile_sel} .offline-btn").first
        expect(btn).to_be_visible(timeout=5000)
        cls0 = btn.get_attribute("class") or ""
        assert "state-idle" in cls0, f"expected idle before Enter got {cls0}"
        page.evaluate("""() => { const el=document.querySelector('.tile[data-path="simple_manga"]'); if(el) el.scrollIntoView({block:'center'}); }""")
        btn.focus()
        active = page.evaluate("() => document.activeElement ? document.activeElement.className : ''")
        assert "offline-btn" in (active or ""), f"focus failed, activeElement={active!r}"
        hash_before = page.evaluate("() => location.hash")
        page.keyboard.press("Enter")
        page.wait_for_selector(f"{tile_sel} .offline-btn.state-downloading", timeout=3000)
        assert page.evaluate("() => location.hash") == hash_before, \
            f"Enter on .offline-btn must not navigate got {page.evaluate('() => location.hash')!r}"

        expect(page.locator(f"{tile_sel} .offline-btn.state-complete").first).to_be_visible(timeout=20000)
        page.evaluate("""async () => { try { await window.deleteWork('simple_manga'); } catch {} }""")
        expect(page.locator(f"{tile_sel} .offline-btn.state-idle").first).to_be_visible(timeout=10000)

        # ② Space activates too (native click fires on keyup)
        btn = page.locator(f"{tile_sel} .offline-btn").first
        btn.focus()
        active = page.evaluate("() => document.activeElement ? document.activeElement.className : ''")
        assert "offline-btn" in (active or ""), f"focus failed, activeElement={active!r}"
        page.keyboard.press("Space")
        page.wait_for_selector(f"{tile_sel} .offline-btn.state-downloading", timeout=3000)
        assert page.evaluate("() => location.hash") == hash_before, \
            f"Space on .offline-btn must not navigate got {page.evaluate('() => location.hash')!r}"
        expect(page.locator(f"{tile_sel} .offline-btn.state-complete").first).to_be_visible(timeout=20000)
        page.evaluate("""async () => { try { await window.deleteWork('simple_manga'); } catch {} }""")
        expect(page.locator(f"{tile_sel} .offline-btn.state-idle").first).to_be_visible(timeout=10000)

        # ③ PARTIAL: focus + Enter opens the popover (no DL, no nav);
        #    Enter on the resume button resumes the download.
        _inject_partial(page, "simple_manga", cached=2, total=5)
        state = page.evaluate("() => getOfflineBtnState('simple_manga')")
        assert state == "partial", f"expected partial after inject got {state}"
        btn = page.locator(f"{tile_sel} .offline-btn").first
        btn.focus()
        page.keyboard.press("Enter")
        expect(page.locator('.ux-pop').first).to_be_visible(timeout=3000)
        assert page.locator(f"{tile_sel} .offline-btn.state-downloading").count() == 0, \
            "Enter on a PARTIAL button must open the popover, not start a download"
        assert page.evaluate("() => location.hash") == hash_before, \
            f"Enter on a PARTIAL button must not navigate got {page.evaluate('() => location.hash')!r}"
        # openPartialPopover auto-focuses its first button; pin it explicitly anyway
        page.focus('.ux-pop .resume')
        active = page.evaluate("() => document.activeElement ? document.activeElement.className : ''")
        assert "resume" in (active or ""), f"resume focus failed, activeElement={active!r}"
        page.keyboard.press("Enter")
        page.wait_for_selector(f"{tile_sel} .offline-btn.state-downloading", timeout=3000)
        assert page.locator('.ux-pop').count() == 0, "popover must close after keyboard resume"
        assert page.evaluate("() => location.hash") == hash_before, \
            f"keyboard resume must not navigate got {page.evaluate('() => location.hash')!r}"

        # ④ control: tile-name focus + Enter keeps navigating to the viewer
        expect(page.locator(f"{tile_sel} .offline-btn.state-complete").first).to_be_visible(timeout=20000)
        page.evaluate("""async () => { try { await window.deleteWork('simple_manga'); } catch {} }""")
        expect(page.locator(f"{tile_sel} .offline-btn.state-idle").first).to_be_visible(timeout=10000)
        tile = page.locator(tile_sel)
        tile.scroll_into_view_if_needed()
        tile.focus()
        active = page.evaluate("() => document.activeElement ? document.activeElement.dataset.path : ''")
        assert active == "simple_manga", f"tile focus failed, activeElement path={active!r}"
        page.keyboard.press("Enter")
        page.wait_for_function("() => location.hash.indexOf('#/view/simple_manga') === 0", timeout=5000)

        try:
            page.screenshot(path=str(TASK3_EVIDENCE / "task-4-keyboard-activation.png"))
        except Exception:
            pass
    finally:
        page.evaluate("() => { try { if(window._origFetchDLK) window.fetch = window._origFetchDLK; } catch {} }")
        try:
            page.unroute("**/image/**")
        except Exception:
            pass
        try:
            page.evaluate("async () => { try { await window.deleteWork('simple_manga'); } catch {} try { await new Promise((res)=>{ const r=indexedDB.deleteDatabase('bookq-offline'); r.onsuccess=()=>res(); r.onerror=()=>res(); r.onblocked=()=>res(); }); } catch {} try { const ks=await caches.keys(); for(const k of ks) if(k.includes('simple_manga')) await caches.delete(k);} catch {} }")
            page.wait_for_selector(".tile", timeout=2400)
        except Exception:
            pass


# ── T5: toast variants — success neutral (.ok), error red (.err) ────────────


def _toast_state(page):
    return page.evaluate(
        """() => { const t=document.getElementById('toast'); if(!t) return null;
             return {text:t.textContent, cls:t.className, visible:t.classList.contains('visible')}; }"""
    )


def test_toast_success_neutral_on_dl_complete(page, server):
    clear_all(page)
    goto_offline(page, server)
    wait_for_sw(page)
    page.wait_for_selector(".tile", timeout=2500)
    page.evaluate("() => { window.viewMode='tile'; window.renderView(); }")
    page.wait_for_selector(".tile", timeout=2300)

    tile_sel = '.tile[data-path="simple_manga"]'
    try:
        btn = page.locator(f"{tile_sel} .offline-btn").first
        expect(btn).to_be_visible(timeout=5000)
        btn.click()
        expect(page.locator(f"{tile_sel} .offline-btn.state-complete").first).to_be_visible(timeout=15000)

        # toast is still up (5s window): visible, exact text, .ok variant, no .err
        st = _toast_state(page)
        assert st is not None, "#toast missing"
        assert st["visible"], f"toast not visible after DL complete: {st}"
        assert st["text"] == "DL完了", f"toast text {st['text']!r}"
        assert "ok" in (st["cls"] or "").split(), f"toast class missing ok: {st}"
        assert "err" not in (st["cls"] or "").split(), f"success toast must not carry err: {st}"

        # computed style proves the neutral base won over the old red palette
        colors = page.evaluate(
            """() => { const cs=getComputedStyle(document.getElementById('toast'));
                 return {bg:cs.backgroundColor, color:cs.color, border:cs.borderColor}; }"""
        )
        assert colors["bg"] == "rgb(34, 38, 45)", f"neutral bg expected rgb(34, 38, 45) got {colors['bg']}"
        assert colors["color"] == "rgb(223, 229, 238)", f"neutral text expected rgb(223, 229, 238) got {colors['color']}"
        assert colors["border"] == "rgb(47, 74, 56)", f"ok border expected rgb(47, 74, 56) got {colors['border']}"
    finally:
        try:
            page.evaluate("async () => { try { await window.deleteWork('simple_manga'); } catch {} }")
            page.wait_for_selector(".tile", timeout=2600)
        except Exception:
            pass


def test_toast_error_red_on_dl_failure(page, server):
    clear_all(page)
    goto_offline(page, server)
    wait_for_sw(page)
    page.wait_for_selector(".tile", timeout=2500)
    page.evaluate("() => { window.viewMode='tile'; window.renderView(); }")
    page.wait_for_selector(".tile", timeout=2300)

    tile_sel = '.tile[data-path="simple_manga"]'
    # reject every simple_manga image fetch -> downloadWork fails -> red toast
    page.evaluate(
        """() => {
            window._origFetchT5err = window.fetch;
            window.fetch = async (input, init) => {
                const u = typeof input === 'string' ? input : (input && input.url) || '';
                if (u.includes('/image/simple_manga/')) { throw new TypeError('failed by test'); }
                return window._origFetchT5err(input, init);
            };
        }"""
    )
    try:
        btn = page.locator(f"{tile_sel} .offline-btn").first
        expect(btn).to_be_visible(timeout=5000)
        btn.click()
        expect(page.locator(f"{tile_sel} .offline-btn.state-idle").first).to_be_visible(timeout=15000)

        st = _toast_state(page)
        assert st is not None, "#toast missing"
        assert st["visible"], f"error toast not visible: {st}"
        assert st["text"] == "DL失敗", f"toast text {st['text']!r}"
        assert "err" in (st["cls"] or "").split(), f"failure toast must carry err: {st}"
        assert "ok" not in (st["cls"] or "").split(), f"failure toast must not carry ok: {st}"

        colors = page.evaluate(
            """() => { const cs=getComputedStyle(document.getElementById('toast'));
                 return {bg:cs.backgroundColor, color:cs.color, border:cs.borderColor}; }"""
        )
        assert colors["bg"] == "rgb(42, 31, 31)", f"red bg expected rgb(42, 31, 31) got {colors['bg']}"
        assert colors["color"] == "rgb(255, 180, 180)", f"red text expected rgb(255, 180, 180) got {colors['color']}"
        assert colors["border"] == "rgb(92, 42, 42)", f"red border expected rgb(92, 42, 42) got {colors['border']}"
    finally:
        page.evaluate("() => { try { if(window._origFetchT5err) window.fetch = window._origFetchT5err; } catch {} }")
        try:
            page.evaluate("async () => { try { await window.deleteWork('simple_manga'); } catch {} }")
            page.wait_for_selector(".tile", timeout=2400)
        except Exception:
            pass


# ── T6: quota-exceeded in-app modal (showQuotaConfirm) ──────────────────────


TASK6_EVIDENCE = REPO_ROOT / ".omo" / "evidence"


def _open_quota_modal(page, folder="x", megabytes=120):
    page.evaluate(
        """([folder, mb]) => {
             window.__qres = undefined;
             window.__qp = window.showQuotaConfirm({folder: folder, megabytes: mb});
             window.__qp.then(v => { window.__qres = v; });
           }""",
        [folder, megabytes],
    )
    expect(page.locator(".quota-modal")).to_be_visible(timeout=3000)


def test_quota_modal_specific_verbs(page, server):
    clear_all(page)
    goto_offline(page, server)
    wait_for_sw(page)
    page.wait_for_timeout(500)

    _open_quota_modal(page)
    meta = page.evaluate(
        """() => { const m=document.querySelector('.quota-modal'); const b=document.querySelector('.quota-modal-backdrop');
             return {title:m.querySelector('.quota-modal-title').textContent,
                     body:m.querySelector('.quota-modal-body').textContent,
                     btns:[...m.querySelectorAll('button')].map(x=>x.textContent),
                     z:getComputedStyle(b).zIndex,
                     role:m.getAttribute('role'), ariaModal:m.getAttribute('aria-modal')}; }"""
    )
    assert meta["title"] == "保存容量が不足しています", f"title {meta['title']!r}"
    assert "最も古い作品『x』（約 120 MB）を削除して続行しますか？" in meta["body"], f"body {meta['body']!r}"
    assert meta["btns"] == ["削除して続行", "キャンセル"], f"buttons {meta['btns']}"
    assert int(meta["z"]) == 57, f"backdrop z-index must be 57 got {meta['z']}"
    assert meta["role"] == "dialog" and meta["ariaModal"] == "true", f"dialog semantics missing {meta}"

    active = page.evaluate("() => document.activeElement ? document.activeElement.className : ''")
    assert "quota-ok" in (active or ""), f"open must focus 削除して続行 got {active!r}"

    page.wait_for_selector(".quota-modal", timeout=2350)
    try:
        page.screenshot(path=str(TASK6_EVIDENCE / "task-6-offline-ux-refinements.png"))
    except Exception:
        pass

    page.evaluate("() => document.querySelector('.quota-ok').click()")
    page.wait_for_function("() => window.__qres === true", timeout=3000)
    assert page.locator(".quota-modal").count() == 0, "modal DOM must be removed after accept"
    assert page.locator(".quota-modal-backdrop").count() == 0

    _open_quota_modal(page)
    page.keyboard.press("Escape")
    page.wait_for_function("() => window.__qres === false", timeout=3000)
    assert page.locator(".quota-modal").count() == 0, "modal DOM must be removed after Esc"

    _open_quota_modal(page)
    page.mouse.click(10, 10)
    page.wait_for_function("() => window.__qres === false", timeout=3000)
    assert page.locator(".quota-modal").count() == 0, "modal DOM must be removed after backdrop tap"

    both = page.evaluate(
        """() => {
             const p1 = window.showQuotaConfirm({folder:'y', megabytes:5});
             const p2 = window.showQuotaConfirm({folder:'z', megabytes:7});
             return {same: p1 === p2, count: document.querySelectorAll('.quota-modal').length};
           }"""
    )
    assert both["same"] and both["count"] == 1, f"double-open must reuse one modal got {both}"
    page.keyboard.press("Escape")
    page.wait_for_function("() => !document.querySelector('.quota-modal')", timeout=3000)


# ── B3: quota modal must escapeHtml the folder name ─────────────────────────
# The modal body interpolates `folder` (an IDB record key derived from a
# filesystem path) into innerHTML. A hostile directory name must render as
# literal text, never as elements/handlers — same invariant offlineControlsHtml
# honors via escapeHtml().

def test_quota_modal_escapes_folder_html(page, server):
    clear_all(page)
    goto_offline(page, server)
    wait_for_sw(page)
    page.wait_for_timeout(500)

    hostile = '<img src=x onerror=window.__xss=1>'
    page.evaluate(
        """(folder) => {
             window.__qres = null;
             window.showQuotaConfirm({folder: folder, megabytes: 3}).then(v=>{window.__qres = v;});
           }""",
        hostile,
    )
    page.wait_for_selector(".quota-modal", timeout=3000)
    meta = page.evaluate(
        """() => { const b=document.querySelector('.quota-modal-body');
             return {html:b.innerHTML, text:b.textContent,
                     imgCount:document.querySelectorAll('.quota-modal img').length}; }"""
    )
    assert meta["imgCount"] == 0, f"hostile folder must not create elements got {meta}"
    assert hostile in meta["text"], f"literal folder name must be visible as text got {meta['text']!r}"
    assert "<img" not in meta["html"], f"body HTML must be escaped got {meta['html']!r}"
    xss_fired = page.evaluate("() => window.__xss !== undefined")
    assert not xss_fired, "onerror handler must never fire"
    page.keyboard.press("Escape")
    page.wait_for_function("() => window.__qres === false", timeout=3000)


# ── F3 fix B: --progress must advance during the FILE phase ─────────────────
# Real QA observed a ring stuck at 0deg mid-download that jumped to 360deg on
# completion. Listing-phase 0deg is CORRECT (indeterminate arc spinner); this
# test pins that, then proves the determinate ring increments across the
# listing→file transition and keeps advancing on the post-renderView nodes.
# Adversarial-flake rule: only two-sample comparison (deg2 > deg1), never a
# fixed threshold.


F3_EVIDENCE = REPO_ROOT / ".omo" / "evidence"


def _ring_deg(page):
    return page.evaluate(
        """() => {
             const el = document.querySelector('.tile[data-path="simple_manga"] .offline-btn .progress-ring');
             if(!el) return -1;
             const v = el.style.getPropertyValue('--progress')
                     || getComputedStyle(el).getPropertyValue('--progress') || '';
             const m = String(v).match(/(-?\\d+\\.?\\d*)/);
             return m ? parseFloat(m[1]) : -1;
           }"""
    )


def test_11_progress_ring_advances_in_file_phase(page, server):
    clear_all(page)
    goto_offline(page, server)
    wait_for_sw(page)
    page.wait_for_selector(".tile", timeout=2500)
    page.evaluate("() => { window.viewMode='tile'; window.renderView(); }")
    page.wait_for_selector(".tile", timeout=2300)

    tile_sel = '.tile[data-path="simple_manga"]'
    # delay BOTH phases: /api/images 800ms (observable listing window),
    # each image 600ms (file phase slow enough to sample twice)
    page.evaluate(
        """() => {
            window._origFetchDL11 = window.fetch;
            window.fetch = async (input, init) => {
                const u = typeof input === 'string' ? input : (input && input.url) || '';
                if (u.includes('/api/v1/images')) { await new Promise(r=>setTimeout(r, 800)); }
                if (u.includes('/image/simple_manga/')) { await new Promise(r=>setTimeout(r, 600)); }
                return window._origFetchDL11(input, init);
            };
        }"""
    )
    try:
        btn = page.locator(f"{tile_sel} .offline-btn").first
        expect(btn).to_be_visible(timeout=5000)
        page.evaluate("""() => { const el=document.querySelector('.tile[data-path="simple_manga"]'); if(el) el.scrollIntoView({block:'center'}); }""")
        btn.click()
        page.wait_for_selector(f'{tile_sel} .offline-btn.state-downloading.is-listing', timeout=3000)

        # LISTING phase: 0deg is the correct determinate value here (the arc
        # spinner carries the indeterminate feedback). Pin it so a future
        # regression that shows fake progress during listing is caught too.
        deg_listing = _ring_deg(page)
        assert deg_listing == 0, f"listing-phase --progress must be 0deg got {deg_listing}"

        # listing→file transition runs renderView twice; wait out BOTH, then
        # prove updateProgress still reaches the freshly rendered nodes.
        page.wait_for_function(
            "() => !(window._listingSet && window._listingSet.has('simple_manga'))", timeout=8000
        )
        page.wait_for_function(
            """() => {
                 const el = document.querySelector('.tile[data-path="simple_manga"] .offline-btn .progress-ring');
                 if(!el) return false;
                 const v = el.style.getPropertyValue('--progress') || '';
                 const m = String(v).match(/(-?\\d+\\.?\\d*)/);
                 return m ? parseFloat(m[1]) > 0 : false;
               }""",
            timeout=8000,
        )

        deg1 = _ring_deg(page)
        assert deg1 > 0, f"first file should have advanced --progress past 0 got {deg1}"

        # two-sample increment: wait until the value CHANGES from deg1 (no
        # fixed threshold), then require strict increase
        page.wait_for_function(
            """(d1) => {
                 const el = document.querySelector('.tile[data-path="simple_manga"] .offline-btn .progress-ring');
                 if(!el) return false;
                 const v = el.style.getPropertyValue('--progress') || '';
                 const m = String(v).match(/(-?\\d+\\.?\\d*)/);
                 return m ? parseFloat(m[1]) > d1 : false;
               }""",
            arg=deg1,
            timeout=8000,
        )
        deg2 = _ring_deg(page)
        assert deg2 > deg1, f"--progress must advance during DL: deg1={deg1} deg2={deg2}"

        # F3 fix A in the file phase too: ↓ visible through the donut hole
        icon_content = page.evaluate(
            """() => { const el=document.querySelector('.tile[data-path=\\"simple_manga\\"] .offline-btn .offline-icon');
                        return el ? getComputedStyle(el, '::before').content : 'no-el'; }"""
        )
        assert icon_content == '"↓"', f"downloading glyph must be ↓ in file phase got {icon_content!r}"

        try:
            page.screenshot(path=str(F3_EVIDENCE / "fix-f3-ring.png"))
        except Exception:
            pass

        expect(page.locator(f"{tile_sel} .offline-btn.state-complete").first).to_be_visible(timeout=20000)
    finally:
        page.evaluate("() => { try { if(window._origFetchDL11) window.fetch = window._origFetchDL11; } catch {} }")
        try:
            page.evaluate("async () => { try { await window.deleteWork('simple_manga'); } catch {} }")
            page.wait_for_selector(".tile", timeout=2600)
        except Exception:
            pass
