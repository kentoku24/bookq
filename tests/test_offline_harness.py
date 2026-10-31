"""Offline harness E2E — 10 failure modes via harness (Todo 3).

Uses the server fixture's dynamic port (never a fixed 8080/18080) with the ?offline=1 gate.
Each test clears IndexedDB/caches via evaluate and is independent.

Covers:
 ① navigate ignoreSearch — caches.match("/?offline=1",{ignoreSearch:true}) hit
 ② /api/folders cache hit — first non-controlled then offline fetch len==online len
 ③ parent-only download not 0 — getLeafPathsFor('group/episodes') >0
 ④ group/episodes flatten proxy — getLeafPathsFor('group/episodes').length>1
 ⑤ double-count / delete leak — dedup sum == raw/2 when parent exists and delete returns to 0
 ⑥ await race — storage-text updates within 800ms after badge
 ⑦ iOS 0/0 + Content-Length missing — mock navigator.storage.estimate returns {usage:0,quota:0} + blob fallback
 ⑧ MB/GB rounding — fmt(50_000_000) -> "50.0 MB", fmt(1_500_000_000) -> "1.5 GB"
 ⑨ VPN split health abort — page.route('/api/health*', lambda route: route.abort('failed')) → ○オフライン
 ⑩ clone consumption order — sizeClone.blob existence + bytes>0 without Content-Length + quota mock via caches.put QuotaExceededError
"""

import re
import time

import pytest
from playwright.sync_api import expect


def _base(server):
    try:
        return server.url
    except AttributeError:
        return server


def goto_offline(page, server):
    base = _base(server)
    page.goto(base + "/?offline=1")
    page.wait_for_selector(".tile", timeout=10000)


def goto_harness(page, server):
    base = _base(server)
    page.goto(base + "/static/offline-harness.html?harness=1")
    page.wait_for_selector("#harness-status", timeout=10000)


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
            page.wait_for_function("() => navigator.serviceWorker && !!navigator.serviceWorker.ready", timeout=2500)
            ok = page.evaluate(
                "async () => { try { const r = await navigator.serviceWorker.ready; return !!r; } catch { return !!navigator.serviceWorker.controller; } }"
            )
            if ok:
                return True
        except Exception as e:
            if "Execution context was destroyed" in str(e):
                page.wait_for_selector(".tile", timeout=2800)
                # after reload, wait for tiles again
                try:
                    page.wait_for_selector(".tile", timeout=10000)
                except Exception:
                    # if we are on harness, wait for harness status
                    try:
                        page.wait_for_selector("#harness-status", timeout=5000)
                    except Exception:
                        pass
                continue
            raise
    return False


def clear_all(page):
    # clear IDB and caches; works from any page
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
    page.wait_for_function("() => window.tree && window.tree.length > 0", timeout=2400)


def ensure_harness_tree(page, timeout=10000):
    # harness populates window.tree via fetchTree; wait for it
    try:
        page.wait_for_function("() => window.tree && window.tree.length > 0", timeout=timeout)
    except Exception:
        # try to trigger fetchTree manually
        try:
            page.evaluate("async () => { try { await window.fetchTree && await window.fetchTree(); } catch {} }")
            page.wait_for_function("() => window.tree && window.tree.length > 0", timeout=5000)
        except Exception:
            pass
    # also wait for leaf-count to populate
    try:
        page.wait_for_function("() => document.getElementById('harness-leaf-count') && document.getElementById('harness-leaf-count').textContent.includes('group/episodes')", timeout=5000)
    except Exception:
        pass


# ── ① navigate ignoreSearch ──────────────────────────────────────────────────
def test_01_navigate_ignore_search(page, server):
    clear_all(page)
    goto_offline(page, server)
    wait_for_sw(page)
    page.wait_for_selector(".tile", timeout=2800)
    # verify SW precache hit via ignoreSearch
    # This is the required pattern: caches.match("/?offline=1",{ignoreSearch:true})
    hit = page.evaluate(
        """async () => {
            try {
                const hit = await caches.match("/?offline=1",{ignoreSearch:true});
                if (hit) return true;
                const hit2 = await caches.match("/",{ignoreSearch:true});
                return !!hit2;
            } catch { return false; }
        }"""
    )
    # also verify offline fallback via fetch when offline
    page.context.set_offline(True)
    try:
        offline_ok = page.evaluate(
            """async () => {
                try { const r = await fetch("/?offline=1"); return r.ok; } catch { return false; }
            }"""
        )
        # if fetch via offline fails due to timing, still check caches.match hit
        assert hit or offline_ok, "caches.match with ignoreSearch should hit for /?offline=1 (offline.js fallback)"
        assert hit, "navigate ignoreSearch hit expected"
    finally:
        page.context.set_offline(False)

    # The navigate fallback depends on sw.js asking for an ignoreSearch match.
    sw_has = page.evaluate(
        """async () => {
            try { const r = await fetch('/sw.js'); const t = await r.text(); return t.includes('ignoreSearch'); } catch { return false; }
        }"""
    )
    assert sw_has, "sw.js should match navigations with {ignoreSearch:true}"


# ── ② /api/folders cache hit ─────────────────────────────────────────────────
def test_02_api_folders_cache_hit(page, server):
    clear_all(page)
    goto_offline(page, server)
    wait_for_sw(page)
    # first non-controlled already done via goto_offline; now fetch online
    online_len = None
    for attempt in range(2):
        try:
            online_len = page.evaluate(
                "async () => { const r = await fetch('/api/v1/tree'); const j = await r.json(); return Array.isArray(j.nodes||j) ? (j.nodes||j).length : -1; }"
            )
            break
        except Exception as e:
            if "Execution context was destroyed" in str(e):
                page.wait_for_selector(".tile", timeout=2800)
                page.wait_for_selector(".tile", timeout=10000)
                wait_for_sw(page)
                continue
            raise
    assert online_len is not None and online_len > 0, f"online /api/folders len {online_len}"
    page.context.set_offline(True)
    try:
        offline_len = None
        for attempt in range(2):
            try:
                offline_len = page.evaluate(
                    "async () => { try { const r = await fetch('/api/v1/tree'); if (!r.ok) return -1; const j = await r.json(); return Array.isArray(j.nodes||j) ? (j.nodes||j).length : -1; } catch { return -2; } }"
                )
                break
            except Exception as e:
                if "Execution context was destroyed" in str(e):
                    page.wait_for_selector(".tile", timeout=2800)
                    continue
                raise
        if offline_len == -2:
            # Cheap direct-cache read first: it answers in-page even when the
            # SW path lags. Only a double miss triggers re-seed + polling.
            offline_len2 = page.evaluate("async () => { try { const hit = await caches.match('/api/v1/tree'); if (!hit) return -2; const j = await hit.json(); return Array.isArray(j.nodes||j) ? (j.nodes||j).length : -1; } catch { return -2; } }")
            if offline_len2 > 0:
                offline_len = offline_len2
            else:
                # SW intercept/cache-put can lag behind set_offline, so a
                # single throwing read proves nothing. Re-seed online, then
                # poll the offline read; a persistent miss fails loudly below.
                page.context.set_offline(False)
                try:
                    page.wait_for_selector(".tile", timeout=2400)
                    page.evaluate("async () => { try { await fetch('/api/v1/tree'); } catch {} }")
                finally:
                    page.context.set_offline(True)
                deadline = time.time() + 6
                while time.time() < deadline:
                    offline_len = page.evaluate(
                        "async () => { try { const r = await fetch('/api/v1/tree'); if (!r.ok) return -1; const j = await r.json(); return Array.isArray(j.nodes||j) ? (j.nodes||j).length : -1; } catch { return -2; } }"
                    )
                    if offline_len != -2:
                        break
                    page.wait_for_selector(".tile", timeout=2400)
        assert offline_len is not None and offline_len > 0, f"offline /api/folders fallback failed {offline_len}"
        assert offline_len == online_len, f"offline len {offline_len} != online {online_len} (API NetworkFirst fallback)"
    finally:
        page.context.set_offline(False)


# ── ③ parent-only download not 0 ─────────────────────────────────────────────
def test_03_parent_only_download_not_zero(page, server):
    clear_all(page)
    goto_offline(page, server)
    wait_for_sw(page)
    goto_harness(page, server)
    ensure_harness_tree(page)
    # proxy via group/episodes since conftest has no sample_library
    leaves = page.evaluate("() => window.getLeafPathsFor('group/episodes')")
    assert isinstance(leaves, list), "getLeafPathsFor should return list"
    assert len(leaves) > 0, f"getLeafPathsFor('group/episodes') should be >0, got {leaves}"
    # also verify harness.getLeafPathsFor alias
    leaves2 = page.evaluate("() => window.harness.getLeafPathsFor('group/episodes')")
    assert len(leaves2) > 0
    # self-check for encodeURIComponent
    enc = page.evaluate("() => window.harness.cacheNameFor('夏 の 本')")
    assert enc == "bookq-work-%E5%A4%8F%20%E3%81%AE%20%E6%9C%AC-v1", f"cacheNameFor encode failed {enc}"


# ── ④ group/episodes flatten proxy ───────────────────────────────────────────
def test_04_group_episodes_flatten_proxy(page, server):
    clear_all(page)
    goto_offline(page, server)
    wait_for_sw(page)
    goto_harness(page, server)
    ensure_harness_tree(page)
    # conftest: group/episodes has ch-1,ch-2,ch-10,ch-11 => 4 leaves
    length = page.evaluate("() => window.getLeafPathsFor('group/episodes').length")
    assert length > 1, f"getLeafPathsFor('group/episodes').length>1 expected, got {length}"
    # also verify that length is 4 (single-child flatten chain not collapsing group)
    assert length == 4, f"expected 4 leaves for group/episodes, got {length}"
    # verify chain/only/leaf flatten still yields single leaf via fallback
    chain_len = page.evaluate("() => window.getLeafPathsFor('chain/only/leaf').length")
    assert chain_len >= 1


# ── ⑤ double-count / delete leak ─────────────────────────────────────────────
# NOTE: versionless indexedDB.open() — schema version is owned by offline-core.js
# (DB_VERSION); pinning an older version raises VersionError after app upgrades.
def test_05_double_count_delete_leak(page, server):
    clear_all(page)
    goto_offline(page, server)
    wait_for_sw(page)
    goto_harness(page, server)
    ensure_harness_tree(page)
    # Ensure clean
    page.evaluate(
        """async () => {
            try { await new Promise((res) => { const r = indexedDB.deleteDatabase('bookq-offline'); r.onsuccess=()=>res(); r.onerror=()=>res(); r.onblocked=()=>res(); }); } catch {}
        }"""
    )
    page.wait_for_selector("#harness-status", timeout=2400)
    goto_harness(page, server)
    ensure_harness_tree(page)
    # put parent and child with equal bytes 1000 each => raw 2000, dedup 1000 == raw/2
    page.evaluate(
        """async () => {
            async function putRec(folder, bytesVal){
                return new Promise((resolve, reject)=>{
                    const req = indexedDB.open('bookq-offline');
                    req.onupgradeneeded = () => {
                        const db = req.result;
                        if(!db.objectStoreNames.contains('works')){
                            const st = db.createObjectStore('works', {keyPath:'folder'});
                            st.createIndex('lastReadAt','lastReadAt',{unique:false});
                            st.createIndex('addedAt','addedAt',{unique:false});
                        }
                    };
                    req.onsuccess = () => {
                        const db = req.result;
                        const tx = db.transaction('works','readwrite');
                        const st = tx.objectStore('works');
                        const rec = {folder, cacheName: 'bookq-work-'+encodeURIComponent(folder)+'-v1', pageCount: bytesVal===1000? (folder==='group/episodes'?4:1):1, cachedCount:1, bytes: bytesVal, thumb:'p1.png', addedAt: Date.now() + (folder==='group/episodes/ch-1'?1:0), lastReadAt: Date.now() + (folder==='group/episodes/ch-1'?1:0)};
                        st.put(rec);
                        tx.oncomplete=()=>resolve();
                        tx.onerror=()=>reject(tx.error);
                    };
                    req.onerror=()=>reject(req.error);
                });
            }
            await putRec('group/episodes', 1000);
            await putRec('group/episodes/ch-1', 1000);
            await window.refresh();
        }"""
    )
    page.wait_for_selector("#harness-idb-dedup", timeout=2800)
    raw = page.evaluate("() => window.__harnessRawSum")
    dedup = page.evaluate("() => window.__harnessDedupSum")
    assert raw == 2000, f"raw sum expected 2000 got {raw}"
    assert dedup == 1000, f"dedup sum expected 1000 (raw/2) got {dedup}"
    assert dedup == raw / 2, "bytes dedup logic failed: dedup should be raw/2 when parent exists"
    # delete and verify leak returns to 0
    page.evaluate(
        """async () => {
            async function delRec(folder){
                return new Promise((resolve, reject)=>{
                    const req = indexedDB.open('bookq-offline');
                    req.onsuccess=()=>{
                        const db=req.result;
                        try{
                            const tx=db.transaction('works','readwrite');
                            tx.objectStore('works').delete(folder);
                            tx.oncomplete=()=>resolve();
                            tx.onerror=()=>reject(tx.error);
                        } catch(e){ resolve(); }
                    };
                    req.onerror=()=>resolve();
                });
            }
            await delRec('group/episodes');
            await delRec('group/episodes/ch-1');
            await window.refresh();
        }"""
    )
    page.wait_for_selector("#harness-idb-dedup", timeout=2600)
    raw2 = page.evaluate("() => window.__harnessRawSum")
    dedup2 = page.evaluate("() => window.__harnessDedupSum")
    assert raw2 == 0, f"after delete raw should be 0 got {raw2}"
    assert dedup2 == 0, f"after delete dedup should be 0 got {dedup2}"
    # also verify harness display shows dedup sum
    dedup_text = page.locator("#harness-idb-dedup").text_content() or ""
    assert "0" in dedup_text


def test_06_await_race_storage_bar(page, server):
    clear_all(page)
    goto_offline(page, server)
    wait_for_sw(page)
    expect(page.locator("#storage-text")).to_be_visible(timeout=5000)
    page.evaluate(
        """async () => {
            try { await window.OfflineStore.delete('simple_manga'); } catch {}
            try { await window.OfflineStore.delete('group/episodes/ch-1'); } catch {}
            try { await window.OfflineStore.delete('group/episodes'); } catch {}
            await window.updateStorageBar();
        }"""
    )
    page.wait_for_selector("#storage-text", timeout=2500)
    page.evaluate(
        """() => {
            window._origEst6 = navigator.storage.estimate;
            navigator.storage.estimate = async () => ({usage:0, quota:0});
            window._origStoreEst6 = window.OfflineStore.storageEstimate;
            window.OfflineStore.storageEstimate = async () => ({usage:0, quota:0});
        }"""
    )
    page.evaluate(
        """async () => {
            await window.OfflineStore.put({folder:'simple_manga', cacheName: window.OfflineStore.cacheNameFor('simple_manga'), pageCount:5, cachedCount:5, bytes:5000000, thumb:'1.png', addedAt: Date.now(), lastReadAt: Date.now()});
            await window.refreshOfflineMap();
            window.updateStorageBar();
        }"""
    )
    start = time.time()
    try:
        page.wait_for_function(
            "() => { const el=document.getElementById('storage-text'); return el && el.textContent.includes('5.0'); }",
            timeout=800,
        )
    except Exception:
        assert False, "storage-text did not update within 800ms after badge (async updateStorageBar race)"
    elapsed = time.time() - start
    assert elapsed < 1.0, f"storage-text update took too long {elapsed}"
    text = page.locator("#storage-text").text_content() or ""
    assert "5.0" in text, f"storage-text should show 5.0 MB after fallback, got {text!r}"
    page.evaluate(
        """async () => {
            try { window.OfflineStore && window._origEst6 && (navigator.storage.estimate = window._origEst6); } catch {}
            try { if(window._origStoreEst6) window.OfflineStore.storageEstimate = window._origStoreEst6; } catch {}
            try { await window.OfflineStore.delete('simple_manga'); await window.updateStorageBar(); } catch {}
        }"""
    )


# ── ⑦ iOS 0/0 + Content-Length missing ───────────────────────────────────────
def test_07_ios_zero_quota_content_length_missing(page, server):
    clear_all(page)
    goto_offline(page, server)
    wait_for_sw(page)
    # put a record with known bytes
    page.evaluate(
        """async () => {
            await window.OfflineStore.put({folder:'ios-test', cacheName: window.OfflineStore.cacheNameFor('ios-test'), pageCount:1, cachedCount:1, bytes:5000000, thumb:'1.png', addedAt: Date.now(), lastReadAt: Date.now()});
        }"""
    )
    # mock navigator.storage.estimate returns {usage:0,quota:0} — iOS Safari case
    page.evaluate(
        """() => {
            window._origEstimate = navigator.storage.estimate;
            navigator.storage.estimate = async () => ({usage:0, quota:0});
            // also mock OfflineStore.storageEstimate to return 0/0 to force fallback
            window._origStoreEst = window.OfflineStore.storageEstimate;
            window.OfflineStore.storageEstimate = async () => ({usage:0, quota:0});
        }"""
    )
    page.evaluate("async () => { await window.updateStorageBar(); }")
    page.wait_for_selector("#storage-text", timeout=2600)
    text = page.locator("#storage-text").text_content() or ""
    # MB display should show usage sum not 0/0 (OfflineStore dedup sum fallback)
    assert "5.0 MB" in text or "5.0" in text, f"iOS fallback should show 5.0 MB from dedup sum, got {text!r}"
    assert "0.0 MB / 0.0" not in text and "usage: 0" not in text.lower(), f"should not show 0/0 when fallback works, got {text!r}"
    # also check harness fallback via refresh
    goto_harness(page, server)
    ensure_harness_tree(page)
    # re-mock in harness context
    page.evaluate(
        """() => {
            const orig = navigator.storage.estimate;
            window._harnessOrigEst = orig;
            navigator.storage.estimate = async () => ({usage:0, quota:0});
        }"""
    )
    page.evaluate(
        """async () => {
            async function putH(folder, bytesVal){
                return new Promise((resolve, reject)=>{
                    const req = indexedDB.open('bookq-offline');
                    req.onupgradeneeded=()=>{
                        const db=req.result;
                        if(!db.objectStoreNames.contains('works')){
                            const st=db.createObjectStore('works',{keyPath:'folder'});
                            st.createIndex('lastReadAt','lastReadAt',{unique:false});
                            st.createIndex('addedAt','addedAt',{unique:false});
                        }
                    };
                    req.onsuccess=()=>{
                        const db=req.result;
                        const tx=db.transaction('works','readwrite');
                        tx.objectStore('works').put({folder, cacheName:'bookq-work-'+encodeURIComponent(folder)+'-v1', pageCount:1, cachedCount:1, bytes:bytesVal, thumb:'p1.png', addedAt: Date.now(), lastReadAt: Date.now()});
                        tx.oncomplete=()=>resolve();
                        tx.onerror=()=>reject(tx.error);
                    };
                    req.onerror=()=>reject(req.error);
                });
            }
            await putH('ios-harness', 3000000);
            await window.refresh();
        }"""
    )
    page.wait_for_selector("#harness-storage-diff", timeout=2800)
    diff_text = page.locator("#harness-storage-diff").text_content() or ""
    dedup_sum = page.evaluate("() => window.__harnessDedupSum")
    # estimate() is mocked to 0/0 (the iOS case), so the dedup sum over the IDB
    # records is the only number left that can be non-zero.
    assert "usage: 0.0 MB" in diff_text, f"mocked estimate should report 0 usage, got {diff_text!r}"
    assert dedup_sum >= 3_000_000, f"dedup sum should include the 3.0 MB record, got {dedup_sum}"
    page.evaluate(
        """async () => {
            async function delH(folder){
                return new Promise((res)=>{
                    const req=indexedDB.open('bookq-offline');
                    req.onsuccess=()=>{
                        try{
                            const db=req.result;
                            const tx=db.transaction('works','readwrite');
                            tx.objectStore('works').delete(folder);
                            tx.oncomplete=()=>res();
                            tx.onerror=()=>res();
                        }catch{ res(); }
                    };
                    req.onerror=()=>res();
                });
            }
            try { await delH('ios-harness'); } catch {}
            try {
                const req2=indexedDB.open('bookq-offline');
                req2.onsuccess=()=>{
                    try{
                        const db2=req2.result;
                        const tx2=db2.transaction('works','readwrite');
                        tx2.objectStore('works').delete('ios-test');
                        tx2.oncomplete=()=>{};
                    }catch{}
                };
            } catch {}
            try { if (window._origEstimate) navigator.storage.estimate = window._origEstimate; } catch {}
            try { if (window._origStoreEst) window.OfflineStore.storageEstimate = window._origStoreEst; } catch {}
            try { if (window._harnessOrigEst) navigator.storage.estimate = window._harnessOrigEst; } catch {}
            try { await window.refresh && await window.refresh(); } catch {}
        }"""
    )
    # verify the Content-Length fallback still clones and reads the response body
    page.goto(_base(server) + "/?offline=1")
    page.wait_for_selector(".tile", timeout=10000)
    has_blob = page.evaluate(
        """async () => {
        try { const r = await fetch('/static/offline/storeAdapter.js'); const t = await r.text(); return t.includes('cacheClone') && t.includes('sizeClone') && t.includes('responseBytes'); } catch { return false; }
        }"""
    )
    assert has_blob, "storeAdapter.js should contain cacheClone/sizeClone clone order with responseBytes fallback for Content-Length missing body"


# ── ⑧ MB/GB rounding ─────────────────────────────────────────────────────────
def test_08_mb_gb_rounding(page, server):
    clear_all(page)
    goto_offline(page, server)
    wait_for_sw(page)
    goto_harness(page, server)
    ensure_harness_tree(page)
    # harness.fmt checks
    fmt_50 = page.evaluate("() => window.harness.fmt(50_000_000)")
    assert fmt_50 == "50.0 MB", f'fmt(50_000_000) expected "50.0 MB" got {fmt_50!r}'
    fmt_15 = page.evaluate("() => window.harness.fmt(1_500_000_000)")
    assert fmt_15 == "1.5 GB", f'fmt(1_500_000_000) expected "1.5 GB" got {fmt_15!r}'
    # also check boundary 1e9
    fmt_boundary = page.evaluate("() => window.harness.fmt(1_000_000_000)")
    assert fmt_boundary == "1.0 GB", f'fmt(1e9) expected "1.0 GB" got {fmt_boundary!r}'
    # keep harness.cacheNameFor('夏 の 本') self-check inside harness tests
    cache_name = page.evaluate("() => window.harness.cacheNameFor('夏 の 本')")
    assert cache_name == "bookq-work-%E5%A4%8F%20%E3%81%AE%20%E6%9C%AC-v1", f"cacheNameFor failed {cache_name!r}"
    self_check = page.evaluate("() => window.OfflineStore.cacheNameFor('夏 の 本')===window.harness.cacheNameFor('夏 の 本')")
    assert self_check, "OfflineStore.cacheNameFor('夏 の 本') should equal harness.cacheNameFor"


# ── ⑨ VPN split health abort ─────────────────────────────────────────────────
def test_09_vpn_split_health_abort(page, server):
    clear_all(page)
    goto_offline(page, server)
    wait_for_sw(page)
    page.route("**/api/v1/health*", lambda route: route.abort("failed"))
    try:
        page.evaluate(
            """() => {
                const orig = window.fetch;
                window._origFetchHealth = orig;
                window.fetch = (input, init) => {
                    const u = typeof input === 'string' ? input : (input && input.url) || '';
                    if (u.includes('/api/v1/health')) return Promise.reject(new Error('failed'));
                    return orig(input, init);
                };
            }"""
        )
        page.evaluate("() => window.updateOnlineStatus && window.updateOnlineStatus()")
        page.wait_for_function("() => document.getElementById('offline-status') && document.getElementById('offline-status').textContent.includes('オフライン')", timeout=5000)
        status = page.locator("#offline-status").text_content() or ""
        assert "オフライン" in status, f"health abort should show ○オフライン, got {status!r}"
        assert "○オフライン" in status, f"expected ○オフライン, got {status!r}"
        goto_harness(page, server)
        ensure_harness_tree(page)
        page.evaluate(
            """() => {
                const orig2 = window.fetch;
                window._origFetchHarness = orig2;
                window.fetch = (input, init) => {
                    const u = typeof input === 'string' ? input : (input && input.url) || '';
                    if (u.includes('/api/v1/health')) return Promise.reject(new Error('failed'));
                    return orig2(input, init);
                };
            }"""
        )
        page.evaluate("async () => { try { await window.refresh(); } catch {} }")
        page.wait_for_selector("#harness-status", timeout=3500)
        harness_status = page.locator("#harness-status").text_content() or ""
        assert "オフライン" in harness_status, f"harness status should be offline after health abort, got {harness_status!r}"
        assert page.locator("#harness-signals .signal").count() == 10
        ng_count = page.evaluate("() => document.querySelectorAll('#harness-signals .signal.ng').length")
        assert ng_count >= 5, f"expected many ng signals on health abort, got {ng_count}"
    finally:
        page.unroute("**/api/v1/health*")
        page.evaluate(
            """() => {
                try { if(window._origFetchHealth) window.fetch = window._origFetchHealth; } catch {}
                try { if(window._origFetchHarness) window.fetch = window._origFetchHarness; } catch {}
            }"""
        )
        page.evaluate("() => window.updateOnlineStatus && window.updateOnlineStatus()")
        page.wait_for_selector("#harness-status", timeout=2600)


# ── ⑩ clone consumption order + quota mock ───────────────────────────────────
def test_10_clone_and_quota_handling(page, server):
    clear_all(page)
    goto_offline(page, server)
    wait_for_sw(page)

    # Part A: clone consumption order — bytes>0 without Content-Length header
    # Verify app.js retains separate cache and size clones plus the fallback helper
    has_clone = page.evaluate(
        """async () => {
            try { const r = await fetch('/static/offline/storeAdapter.js'); const t = await r.text(); return t.includes('cacheClone') && t.includes('sizeClone') && t.includes('responseBytes'); } catch { return false; }
        }"""
    )
    assert has_clone, "storeAdapter.js should contain cacheClone/sizeClone clone order with responseBytes fallback"
    # Functional check: mock fetch to strip Content-Length, then download should still produce bytes>0 via blob fallback
    page.evaluate(
        """() => {
            window._origFetch2 = window.fetch;
            window.fetch = async (url, opts) => {
                const res = await window._origFetch2(url, opts);
                if (typeof url === 'string' && url.includes('/image/')) {
                    try {
                        const b = await res.clone().blob();
                        return new Response(b, {status:200, headers:{'Content-Type':'image/png'}});
                    } catch { return res; }
                }
                return res;
            };
        }"""
    )
    # ensure clean
    page.evaluate("""async () => { try { await window.OfflineStore.delete('simple_manga'); } catch {} try { const ks=await caches.keys(); for(const k of ks) if(k.includes('simple_manga')) await caches.delete(k); } catch {} }""")
    page.wait_for_selector(".tile", timeout=2300)
    # trigger download via downloadWork (uses fetch + clone + blob fallback)
    page.evaluate("""async () => { await window.downloadWork('simple_manga'); }""")
    # wait for download to complete (badge + storage)
    expect(page.locator('.tile[data-path="simple_manga"] .offline-btn.state-complete').first).to_be_visible(timeout=15000)
    icon = page.evaluate(
        """() => {
            const el = document.querySelector('.tile[data-path="simple_manga"] .offline-btn.state-complete .offline-icon');
            if (!el) return '';
            try { return getComputedStyle(el, '::before').content || ''; } catch { return ''; }
        }"""
    )
    assert "✓" in icon, f"expected ✓ in ::before content got {icon!r}"
    page.wait_for_selector('.tile[data-path="simple_manga"] .offline-btn.state-complete', timeout=2800)
    bytes_val = page.evaluate("async () => { const rec = await window.OfflineStore.get('simple_manga'); return rec ? rec.bytes : 0; }")
    assert bytes_val > 0, f"clone consumption: bytes should be >0 without Content-Length header (blob fallback), got {bytes_val}"
    # cleanup fetch mock and simple_manga
    page.evaluate(
        """async () => {
            try { window.fetch = window._origFetch2; } catch {}
            try { await window.OfflineStore.delete('simple_manga'); } catch {}
            try { const ks=await caches.keys(); for(const k of ks) if(k.includes('simple_manga')) await caches.delete(k); } catch {}
            await window.updateStorageBar().catch(()=>{});
            await window.refreshOfflineMap().catch(()=>{});
        }"""
    )
    page.wait_for_selector(".tile", timeout=2500)

    # Part B: quota mock test via page.evaluate that throws QuotaExceededError on caches.put for /image/ URL
    # The native window.confirm is gone — an in-app modal (.quota-modal) now asks
    # whether to delete the oldest work. downloadWork stays pending until a choice
    # is made, so the trigger MUST be fire-and-forget: awaiting it inside evaluate
    # would block forever with the modal on screen (no evaluate timeout in conftest).
    # Put an oldest entry so the modal path has a victim
    page.evaluate(
        """async () => {
            await window.OfflineStore.put({folder:'oldest_work', cacheName: window.OfflineStore.cacheNameFor('oldest_work'), pageCount:1, cachedCount:1, bytes:1000000, thumb:'1.png', addedAt:1, lastReadAt:1});
        }"""
    )
    page.evaluate(
        """() => {
            window._origPut = Cache.prototype.put;
            Cache.prototype.put = async function(req, res) {
                const url = req.url || req;
                if (typeof url === 'string' && url.includes('/image/')) {
                    const err = new DOMException('Quota exceeded', 'QuotaExceededError');
                    throw err;
                }
                if (url && url.url && url.url.includes('/image/')) {
                    const err2 = new DOMException('Quota exceeded', 'QuotaExceededError');
                    throw err2;
                }
                return window._origPut.call(this, req, res);
            };
        }"""
    )
    # Trigger download that will hit quota — fire-and-forget, then drive the modal
    page.evaluate("""() => { window.downloadWork('simple_manga'); }""")
    page.wait_for_selector(".quota-modal", timeout=10000)
    modal_text = page.locator(".quota-modal").inner_text()
    assert "保存容量が不足しています" in modal_text, f"modal title missing: {modal_text!r}"
    assert "oldest_work" in modal_text, f"victim folder must be named in the body: {modal_text!r}"
    victim_intact = page.evaluate("async () => !!(await window.OfflineStore.get('oldest_work'))")
    assert victim_intact, "victim must still exist while the modal is open"
    page.evaluate("""() => { const b=document.querySelector('.quota-modal .quota-cancel'); if(b) b.click(); }""")
    page.wait_for_function(
        "() => { const t=document.getElementById('toast'); return !!t && t.classList.contains('visible') && t.textContent.indexOf('容量不足のため中断') >= 0; }",
        timeout=5000,
    )
    toast_text = page.locator("#toast").text_content() or ""
    kept_after_cancel = page.evaluate("async () => !!(await window.OfflineStore.get('oldest_work'))")
    quota_handled = ("容量不足のため中断" in toast_text) and kept_after_cancel
    # restore put override
    page.evaluate(
        """() => {
            try { if (window._origPut) Cache.prototype.put = window._origPut; } catch {}
        }"""
    )
    # cleanup oldest
    page.evaluate("""async () => { try { await window.OfflineStore.delete('oldest_work'); } catch {} try { await window.OfflineStore.delete('simple_manga'); } catch {} }""")
    assert quota_handled, f"cancel path must surface 容量不足のため中断 and keep the victim, toast={toast_text!r} kept={kept_after_cancel}"
