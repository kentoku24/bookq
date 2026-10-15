"""データ全削除（全消去）— 閲覧だけで作られた作品キャッシュまで消えること。

2026-10-13 の実機報告（ライブラリで画像を差し替えても端末が古いバイトを出し続ける）の
副産物として見つかった穴を塞ぐ:

  * ハーネスの「すべて削除」は OfflineStore に登録済みの作品しか消しておらず、
    **閲覧しただけ**で SW が作る `bookq-work-*` キャッシュが残っていた
  * さらに、保存作品が 0 件だとボタン自体が disabled になり、その状態では
    閲覧のみのキャッシュを消す手段が無かった

契約:
  1. 閲覧のみ（IndexedDB レコード無し）の work キャッシュも削除対象として数えられ、削除される
  2. 保存作品のキャッシュ + IndexedDB レコード + ツリー スナップショットも削除される
  3. api / fallback キャッシュも削除、アプリ本体の SHELL キャッシュは保持
  4. 読書進捗キーは削除、QA フラグ（bookq_harness / bookq_enable_sw / bookq_touch / bookq_perf）は保持

Run:
    BOOKQ_TEST_MODE=true uv run --with pytest --with playwright --with-requirements requirements-dev.txt \\
        pytest tests/test_wipe_all.py -q
"""

import os
import re

import pytest
from playwright.sync_api import expect

HARNESS = "/static/offline-harness.html?harness=1"


def _is_webkit():
    return os.environ.get("BROWSER") == "webkit"


pytestmark = pytest.mark.skipif(
    _is_webkit(),
    reason="SW-produced work caches are needed to exercise the viewing-only wipe (Chromium-only contract)",
)


def goto_offline(page, server):
    page.goto(server + "/?offline=1")
    page.wait_for_selector(".tile", timeout=10000)


def wait_for_sw(page, timeout=15000):
    for _ in range(2):
        try:
            page.wait_for_function(
                "() => navigator.serviceWorker && navigator.serviceWorker.controller !== null",
                timeout=4000,
            )
            return True
        except Exception:
            pass
        try:
            page.wait_for_function(
                "() => navigator.serviceWorker && !!navigator.serviceWorker.ready",
                timeout=timeout,
            )
            page.wait_for_timeout(500)
            return True
        except Exception:
            continue
    return False


def open_harness(page, server):
    page.goto(server + HARNESS, timeout=15000)
    page.wait_for_selector("#harness-status", timeout=10000)
    page.wait_for_function("() => window.OfflineStore && window.doWipeAll && window.OfflineCore", timeout=10000)


def cache_keys(page):
    return page.evaluate("async () => await caches.keys()")


def test_view_only_work_cache_is_wiped(page, server):
    """The gap: a work that was only *viewed* must be wipeable (and countable)."""
    goto_offline(page, server)
    assert wait_for_sw(page), "SW did not take control"
    page.locator('.tile[data-path="simple_manga"]').click()
    expect(page.locator("#counter")).to_have_text("1 / 5", timeout=8000)
    img = page.locator("#img-track .page-cell img").first
    expect(img).to_have_class(re.compile(r"\bloaded\b"), timeout=8000)
    url = img.evaluate("e => e.src")
    # one SW-mediated GET guarantees the per-work cache entry exists
    page.evaluate("async (u) => { await (await fetch(u)).arrayBuffer(); }", url)

    work_cache = page.evaluate("() => window.OfflineCore.cacheNameFor('simple_manga')")
    assert work_cache in cache_keys(page), f"viewing must create the per-work cache: {work_cache}"
    record = page.evaluate("async () => await window.OfflineStore.get('simple_manga')")
    assert record is None, "this test needs a cache WITHOUT a saved-work record (view-only)"

    # user state that a wipe must also clear, and flags it must keep
    page.evaluate(
        """async () => {
            localStorage.setItem('bookq_progress', '{"simple_manga":3}');
            localStorage.setItem('bookq_harness', '1');
            localStorage.setItem('bookq_enable_sw', '1');
            localStorage.setItem('bookq_touch', '1');
            localStorage.setItem('bookq_perf', '1');
            sessionStorage.setItem('bookq_auto_sync_done', '1');
            // distinctive entries: the api/fallback caches are recreated by the
            // harness refresh right after a wipe, so the *content* must be the proof.
            const api = await caches.open('bookq-api-v1');
            await api.put('/api/wipe-probe', new Response('{}'));
            const fb = await caches.open('bookq-images-fallback');
            await fb.put('/wipe-probe-fallback', new Response('x'));
        }"""
    )

    open_harness(page, server)
    targets = page.evaluate("async () => await window.OfflineCore.wipeTargets()")
    assert targets["caches"], "a view-only work cache must be counted as a wipe target"
    assert any(c["name"] == work_cache for c in targets["caches"]), f"caches={targets['caches']}"
    assert targets["entries"] >= 1
    assert targets["works"] == 0, "no saved work is expected here"
    assert page.evaluate("() => document.getElementById('btn-wipe-all').disabled") is False, (
        "the wipe button must stay enabled when only view-only caches exist"
    )

    page.evaluate("async () => { await window.doWipeAll(); }")
    keys = cache_keys(page)
    assert not [k for k in keys if k.startswith("bookq-work-")], f"work caches must be gone: {keys}"
    assert any(k.startswith("bookq-shell-") for k in keys), f"SHELL must be kept: {keys}"
    leftovers = page.evaluate(
        """async () => {
            const out = [];
            for (const n of ['bookq-api-v1', 'bookq-images-fallback']) {
                try {
                    const c = await caches.open(n);
                    for (const r of await c.keys()) out.push(n + ' ' + r.url);
                } catch (e) {}
            }
            return out;
        }"""
    )
    assert not [x for x in leftovers if "wipe-probe" in x], (
        f"pre-wipe api/fallback entries must be discarded (fresh refetch is fine): {leftovers}"
    )
    state = page.evaluate(
        """() => ({
            progress: localStorage.getItem('bookq_progress'),
            persist: localStorage.getItem('bookq_persist_toast'),
            autoSync: sessionStorage.getItem('bookq_auto_sync_done'),
            harness: localStorage.getItem('bookq_harness'),
            sw: localStorage.getItem('bookq_enable_sw'),
            touch: localStorage.getItem('bookq_touch'),
            perf: localStorage.getItem('bookq_perf'),
        })"""
    )
    assert state["progress"] is None, "reading progress is user data — wiped"
    assert state["autoSync"] is None, "auto-sync marker wiped"
    assert state["harness"] == "1" and state["sw"] == "1" and state["touch"] == "1" and state["perf"] == "1", (
        f"QA/registration flags must survive: {state}"
    )


def test_saved_work_records_and_tree_snapshot_are_wiped(page, server):
    open_harness(page, server)
    page.evaluate(
        """async () => {
            await window.OfflineStore.put({folder:'wipe-work', cacheName: window.OfflineCore.cacheNameFor('wipe-work'),
                pageCount:2, cachedCount:2, bytes:2048, thumb:'1.png', addedAt: Date.now(), lastReadAt: Date.now()});
            const c = await caches.open(window.OfflineCore.cacheNameFor('wipe-work'));
            await c.put('/image/wipe-work/1.png', new Response('a'));
            await c.put('/image/wipe-work/2.png', new Response('b'));
            const shell = await caches.open('bookq-shell-v2');
            await shell.put('/static/shell-keep-probe', new Response('shell'));
            const api = await caches.open('bookq-api-v1');
            await api.put('/api/keep-probe', new Response('{}'));
            await window.OfflineTree.putTree([{name:'wipe-work', type:'folder', children:[]}]);
            localStorage.setItem('bookq_progress', '{"wipe-work":1}');
            localStorage.setItem('bookq_persist_toast', 'toast');
        }"""
    )

    targets = page.evaluate("async () => await window.OfflineCore.wipeTargets()")
    assert targets["works"] == 1, f"seeded work record must be counted: {targets}"
    assert targets["tree"] is True, "tree snapshot must be a wipe target"
    assert any(c["name"].startswith("bookq-work-") and c["entries"] == 2 for c in targets["caches"]), targets

    report = page.evaluate("async () => await window.OfflineCore.wipeClientData()")
    assert report["works"] == 1, f"report must include the cleared work: {report}"
    assert report["tree"] is True
    assert "bookq_progress" in report["keys"] and "bookq_persist_toast" in report["keys"]

    after = page.evaluate(
        """async () => ({
            works: (await window.OfflineStore.getAll()).length,
            tree: await window.OfflineTree.getTree(),
            keys: await caches.keys(),
        })"""
    )
    assert after["works"] == 0, "IndexedDB work records must be cleared"
    assert after["tree"] is None, "the cached tree snapshot must be cleared"
    assert not [k for k in after["keys"] if k.startswith(("bookq-work-", "bookq-api-", "bookq-images-"))], after["keys"]
    assert "bookq-shell-v2" in after["keys"], f"SHELL must be kept: {after['keys']}"


def test_cache_classification_and_shell_guard(page, server):
    """The wipe only touches data caches — the shell (app code) is never deleted."""
    open_harness(page, server)
    got = page.evaluate(
        """() => ({
            work: window.OfflineCore.isDataCacheName('bookq-work-a%2Fb-v1'),
            api: window.OfflineCore.isDataCacheName('bookq-api-v1'),
            fallback: window.OfflineCore.isDataCacheName('bookq-images-fallback'),
            shell: window.OfflineCore.isDataCacheName('bookq-shell-v2'),
            other: window.OfflineCore.isDataCacheName('ms-playwright-cache'),
        })"""
    )
    assert got == {"work": True, "api": True, "fallback": True, "shell": False, "other": False}, got
