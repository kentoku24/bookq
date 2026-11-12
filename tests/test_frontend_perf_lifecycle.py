import json
import re
from pathlib import Path
from urllib.request import urlopen

import pytest

BASE = Path(__file__).resolve().parent.parent
APP_JS = BASE / "app" / "static" / "app.js"
CORE_JS = BASE / "app" / "static" / "offline-core.js"
PREFETCH_JS = BASE / "app" / "static" / "thumbs" / "prefetch.js"
SW_JS = BASE / "app" / "static" / "sw.js"
WRITER_JS = BASE / "app" / "static" / "offline" / "tree-writer.js"


def _new_page(browser, width=1280, height=800):
    context = browser.new_context(viewport={"width": width, "height": height})
    return context, context.new_page()


def _new_prefetch_page(browser, width=1280, height=800):
    """Install prefetch capture before app scripts can queue a real idle task."""
    context = browser.new_context(viewport={"width": width, "height": height})
    page = context.new_page()
    page.add_init_script(f"({PREFETCH_STUB_JS})()")
    return context, page


def _folder_entries(page):
    return page.evaluate(
        """() => performance.getEntriesByType('resource')
            .filter(e => e.name.includes('/api/v1/tree'))
            .map(e => e.name)"""
    )


def _stop_fill_poll(page):
    page.evaluate("() => { try { window.FillPoll.stop(); } catch(e){} }")


def _flush_frames(page):
    page.evaluate(
        """() => new Promise(resolve => {
            requestAnimationFrame(() => requestAnimationFrame(() => {
                setTimeout(resolve, 0);
            }));
        })"""
    )


CLOCK_ADVANCE_JS = """() => {
    if (!window.__realNow) window.__realNow = Date.now.bind(Date);
    window.__clockOff = (window.__clockOff || 0) + 300001;
    Date.now = () => window.__realNow() + window.__clockOff;
}"""


REVALIDATE_SPY_JS = """() => {
    window.__perf = {jsonCalls: 0, textCalls: 0, domMuts: 0, foldersReqs: []};
    const oj = Response.prototype.json;
    const ot = Response.prototype.text;
    Response.prototype.json = function(){ window.__perf.jsonCalls++; return oj.call(this); };
    Response.prototype.text = function(){ window.__perf.textCalls++; return ot.call(this); };
    window.__perfMo = new MutationObserver(muts => { window.__perf.domMuts += muts.length; });
    window.__perfMo.observe(document.getElementById('tile-grid'), {childList:true, subtree:true});
    const of = window.fetch.bind(window);
    window.fetch = (input, init) => {
        const u = typeof input === 'string' ? input : (input && input.url) || '';
        if (u.indexOf('/api/v1/tree') === 0) {
            window.__perf.foldersReqs.push({
                url: u,
                inm: (init && init.headers && init.headers['If-None-Match']) || null
            });
        }
        return of(input, init);
    };
}"""


def test_startup_fetches_folders_exactly_twice_and_seeds_etag(browser, server):
    context, page = _new_page(browser)
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        page.wait_for_function(
            "() => sessionStorage.getItem('bookq_auto_sync_done') === '1'",
            timeout=5000,
        )
        _stop_fill_poll(page)
        _flush_frames(page)
        entries = _folder_entries(page)
        assert len(entries) == 2, (
            f"startup must issue exactly 2 /api/v1/tree fetches "
            f"(shallow + full), got {len(entries)}: {entries}"
        )
        assert "depth=1" in entries[0], entries
        assert "depth=1" not in entries[1], entries
        full_etag = page.evaluate("() => window.__BOOKQ_DEBUG.etagByKey.get(':full')")
        assert full_etag, "validated full-tree ETag must seed etagByKey[':full']"
        shallow_etag = page.evaluate("() => window.__BOOKQ_DEBUG.etagByKey.get(':shallow1')")
        assert shallow_etag, "validated shallow ETag must seed etagByKey[':shallow1']"
    finally:
        context.close()


def test_warm_revalidate_coalesced_conditional_304_no_render(browser, server):
    context, page = _new_page(browser)
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        _stop_fill_poll(page)
        page.evaluate(REVALIDATE_SPY_JS)
        page.evaluate(CLOCK_ADVANCE_JS)
        page.evaluate(
            "() => Promise.all([window.revalidateFoldersForOnline(), "
            "window.revalidateFoldersForOnline()])"
        )
        reqs = page.evaluate("() => window.__perf.foldersReqs")
        assert len(reqs) == 1, f"coalesced revalidate must fire exactly one request: {reqs}"
        assert reqs[0]["url"].endswith("/api/v1/tree"), reqs
        assert reqs[0]["inm"], "warm revalidate must carry the seeded If-None-Match"
        assert page.evaluate("() => window.__perf.jsonCalls") == 0
        assert page.evaluate("() => window.__perf.textCalls") == 0
        assert page.evaluate("() => window.__perf.domMuts") == 0
        page.evaluate("() => window.revalidateFoldersForOnline()")
        assert page.evaluate("() => window.__perf.foldersReqs.length") == 1
    finally:
        context.close()


def test_revalidate_without_etag_falls_back_to_tree_compare(browser, server):
    context, page = _new_page(browser)
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        _stop_fill_poll(page)
        page.evaluate(REVALIDATE_SPY_JS)
        page.evaluate(CLOCK_ADVANCE_JS)
        page.evaluate("() => window.__BOOKQ_DEBUG.etagByKey.delete(':full')")
        page.evaluate("() => window.revalidateFoldersForOnline()")
        reqs = page.evaluate("() => window.__perf.foldersReqs")
        assert len(reqs) == 1 and reqs[0]["inm"] is None, reqs
        assert page.evaluate("() => window.__perf.jsonCalls") == 0
        assert page.evaluate("() => window.__perf.textCalls") == 1
        assert page.evaluate("() => window.__perf.domMuts") == 0
    finally:
        context.close()


def test_failed_revalidate_stays_retryable(browser, server):
    context, page = _new_page(browser)
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        _stop_fill_poll(page)
        page.evaluate(REVALIDATE_SPY_JS)
        page.evaluate(CLOCK_ADVANCE_JS)
        page.evaluate(
            """() => {
                const of = window.fetch.bind(window);
                window.fetch = (u, i) => {
                    const url = typeof u === 'string' ? u : (u && u.url) || '';
                    if (window.__failFolders && url === '/api/v1/tree')
                        return Promise.reject(new Error('net down'));
                    return of(u, i);
                };
                window.__failFolders = true;
            }"""
        )
        page.evaluate("() => window.revalidateFoldersForOnline()")
        assert page.evaluate("() => window.__perf.foldersReqs.length") == 0
        page.evaluate("() => { window.__failFolders = false; }")
        page.evaluate("() => window.revalidateFoldersForOnline()")
        reqs = page.evaluate("() => window.__perf.foldersReqs")
        assert len(reqs) == 1, f"failed revalidate must stay retryable: {reqs}"
    finally:
        context.close()


def test_malformed_full_response_keeps_shallow_state(browser, server):
    context, page = _new_page(browser)
    counts = {"full": 0}

    def handler(route):
        if "depth=1" in route.request.url:
            route.continue_()
        else:
            counts["full"] += 1
            route.fulfill(
                status=200, content_type="application/json", body='{"bad":1}'
            )

    page.route("**/api/v1/tree*", handler)
    try:
        page.goto(server + "/")
        page.wait_for_selector("#tile-grid .tile", timeout=8000)
        for _ in range(60):
            if counts["full"]:
                break
            page.wait_for_timeout(100)
        assert counts["full"] >= 1, "full-stage fetch never attempted"
        page.wait_for_timeout(400)
        assert page.evaluate("() => window.treeLoaded") is False, (
            "malformed full payload must not mark the tree full-ready"
        )
        badge = page.evaluate(
            "() => (document.getElementById('folder-count')||{}).textContent || ''"
        )
        assert "読込中" in badge, f"shallow waiting badge must remain: {badge!r}"
    finally:
        try:
            page.unroute("**/api/v1/tree*")
        except Exception:
            pass
        context.close()


def test_stale_revalidation_cannot_overwrite_new_load(browser, server):
    context, page = _new_page(browser)
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        _stop_fill_poll(page)
        out = page.evaluate(
            """async () => {
                const of = window.fetch.bind(window);
                let gate;
                const held = new Promise(r => { gate = r; });
                window.fetch = (u, i) => {
                    const url = typeof u === 'string' ? u : (u && u.url) || '';
                    const inm = i && i.headers && i.headers['If-None-Match'];
                    if (url === '/api/v1/tree' && inm)
                        return held.then(() => new Response(
                            '[{"name":"ZZZ","type":"folder","children":[{"name":"z.png","type":"image"}]}]',
                            {status: 200, headers: {'Content-Type': 'application/json', 'ETag': '"ZZZ"'}}));
                    return of(u, i);
                };
                const rn = Date.now.bind(Date);
                Date.now = () => rn() + 300001;
                const reval = window.revalidateFoldersForOnline();
                await new Promise(r => setTimeout(r, 30));
                await window.loadFolders({});
                const tree = window.__BOOKQ_DEBUG.getTree();
                const etag = window.__BOOKQ_DEBUG.etagByKey.get(':full');
                gate();
                await reval.catch(() => {});
                return {
                    sameTree: window.__BOOKQ_DEBUG.getTree() === tree,
                    etagSame: window.__BOOKQ_DEBUG.etagByKey.get(':full') === etag,
                };
            }"""
        )
        assert out["sameTree"], "stale revalidation must not overwrite the newer load's tree"
        assert out["etagSame"], "stale revalidation must not overwrite the newer load's ETag"
    finally:
        context.close()


def test_async_index_matches_sync_dfs_output(browser, server):
    context, page = _new_page(browser)
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        out = page.evaluate(
            """async () => {
                const tree = [
                    {name:'夏 の 本', type:'folder', thumb:'x.png',
                     children:[{name:'x.png', type:'image'}]},
                    {name:'wrap/inner', type:'folder', thumb:'p.png',
                     thumbs:['a/1.png','a/1.png','b/2.png'],
                     children:[{name:'vol/1', type:'folder', thumb:'p.png',
                                children:[{name:'p.png', type:'image'}]}]},
                    {name:'mixed', type:'folder', thumb:'i.png',
                     children:[{name:'i.png', type:'image'},
                               {name:'sub', type:'folder',
                                children:[{name:'q.png', type:'image'}]}]},
                ];
                const snap = (map) => {
                    const idx = window.__cc_thumbIndex;
                    return {
                        paths: [...map.keys()],
                        single: idx.single.map(e => e.url),
                        full: idx.full.map(e => e.url),
                    };
                };
                const a = JSON.parse(JSON.stringify(tree));
                const mapA = window.setFullPathsAndIndex(a);
                const snapA = snap(mapA);
                const b = JSON.parse(JSON.stringify(tree));
                const mapB = await window.setFullPathsAndIndexAsync(b);
                const snapB = snap(mapB);
                window.setFullPathsAndIndex(window.__BOOKQ_DEBUG.getTree());
                return {a: snapA, b: snapB};
            }"""
        )
        assert out["a"] == out["b"], (
            f"async index must produce identical DFS output\nsync:  {out['a']}\nasync: {out['b']}"
        )
        assert len(out["b"]["paths"]) == 9
    finally:
        context.close()


def test_async_index_yields_real_macrotask(browser, server):
    context, page = _new_page(browser)
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        out = page.evaluate(
            """async () => {
                const kids = [];
                for (let i = 0; i < 20000; i++)
                    kids.push({name: 'i' + i + '.png', type: 'image'});
                const t = [{name:'big', type:'folder', children:kids}];
                let beats = 0;
                const ch = new MessageChannel();
                ch.port1.onmessage = () => {
                    beats++;
                    if (beats < 100) ch.port2.postMessage(0);
                };
                ch.port2.postMessage(0);
                const map = await window.setFullPathsAndIndexAsync(t);
                ch.port1.onmessage = null;
                window.setFullPathsAndIndex(window.__BOOKQ_DEBUG.getTree());
                return {beats, size: map ? map.size : -1};
            }"""
        )
        assert out["size"] == 20001, out
        assert out["beats"] >= 1, (
            "async index must yield a real task (MessageChannel/setTimeout), "
            "never microtask-only — heartbeat fired zero times during the build"
        )
    finally:
        context.close()


def test_superseded_async_index_never_publishes(browser, server):
    context, page = _new_page(browser)
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        out = page.evaluate(
            """async () => {
                const big = [{name:'big', type:'folder',
                    children: Array.from({length:20000}, (_,i) =>
                        ({name:'p'+i+'.png', type:'image'}))}];
                const p = window.setFullPathsAndIndexAsync(big);
                const small = [{name:'s', type:'folder',
                    children:[{name:'x.png', type:'image'}]}];
                const m2 = window.setFullPathsAndIndex(small);
                const r1 = await p;
                const published = window.__cc_thumbIndex;
                window.setFullPathsAndIndex(window.__BOOKQ_DEBUG.getTree());
                return {
                    cancelled: r1 === null,
                    publishedSmall: !!(published && published.tree === small),
                    syncSize: m2.size,
                };
            }"""
        )
        assert out["cancelled"], "superseded async build must resolve null"
        assert out["publishedSmall"], "newest build must own the published index"
        assert out["syncSize"] == 2
    finally:
        context.close()


def test_overlapping_loadfolders_newest_tree_wins(browser, server):
    context, page = _new_page(browser)
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        out = page.evaluate(
            """async () => {
                let release;
                const gate = new Promise(r => { release = r; });
                const mkRes = data => ({
                    ok: true, status: 200,
                    headers: {get: () => null},
                    json: async () => data,
                    text: async () => JSON.stringify(data),
                });
                const t1 = [{name:'t1', type:'folder',
                    children:[{name:'a.png', type:'image'}]}];
                const t2 = [{name:'t2', type:'folder',
                    children:[{name:'b.png', type:'image'}]}];
                const f1 = u => u.indexOf('depth=1') >= 0
                    ? Promise.resolve(mkRes(t1))
                    : gate.then(() => mkRes(t1));
                const f2 = u => Promise.resolve(mkRes(t2));
                const p1 = window.loadFolders({}, {fetch: f1});
                await new Promise(r => setTimeout(r, 0));
                const p2 = window.loadFolders({}, {fetch: f2});
                await p2;
                release();
                await p1;
                return window.__BOOKQ_DEBUG.getTree().map(n => n.name);
            }"""
        )
        assert out == ["t2"], f"stale full response must not overwrite newer tree: {out}"
    finally:
        context.close()


def test_superseded_full_index_never_publishes(browser, server):
    context, page = _new_page(browser)
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        _stop_fill_poll(page)
        out = page.evaluate(
            """async () => {
                const big = '[{"name":"big","type":"folder","children":[' +
                    Array.from({length: 20000}, (_, i) =>
                        '{"name":"p' + i + '.png","type":"image"}').join(',') + ']}]';
                const of = window.fetch.bind(window);
                let gate;
                const held = new Promise(r => { gate = r; });
                let nShallow = 0, nFull = 0;
                window.fetch = (u, i) => {
                    const url = typeof u === 'string' ? u : (u && u.url) || '';
                    if (url.indexOf('/api/v1/tree?depth=1') === 0)
                        return (++nShallow === 1) ? of(u, i) : held.then(() => of(u, i));
                    if (url.indexOf('/api/v1/tree') === 0)
                        return (++nFull === 1)
                            ? Promise.resolve(new Response(big,
                                {status: 200, headers: {'Content-Type': 'application/json'}}))
                            : held.then(() => of(u, i));
                    return of(u, i);
                };
                const origPM = MessagePort.prototype.postMessage;
                const heldIdx = [];
                try {
                    MessagePort.prototype.postMessage = function(...args){
                        if (args[0] === 0) { heldIdx.push({port: this, args}); return; }
                        return origPM.apply(this, args);
                    };
                    const pOld = window.loadFolders({});
                    await new Promise((res, rej) => {
                        const t0 = Date.now();
                        const t = setInterval(() => {
                            if (heldIdx.length) { clearInterval(t); res(); }
                            else if (Date.now() - t0 > 10000) {
                                clearInterval(t); rej(new Error('index yield never posted'));
                            }
                        }, 5);
                    });
                    const pNew = window.loadFolders({});
                    MessagePort.prototype.postMessage = origPM;
                    for (const m of heldIdx) origPM.apply(m.port, m.args);
                    await pOld;
                    const nodeMap = window.buildNodeMap();
                    const tree = window.__BOOKQ_DEBUG.getTree();
                    const idxTree = (window.__cc_thumbIndex || {}).tree;
                    gate();
                    await pNew.catch(() => {});
                    window.fetch = of;
                    return {
                        hasBigNode: nodeMap.has('big'),
                        hasBig: tree.map(x => x.name).includes('big'),
                        idxTracksTree: idxTree === tree,
                    };
                } finally {
                    MessagePort.prototype.postMessage = origPM;
                    gate();
                    window.fetch = of;
                }
            }"""
        )
        assert not out["hasBigNode"], "superseded full-index build must never publish node map"
        assert not out["hasBig"], "superseded full load must not publish its tree"
        assert out["idxTracksTree"], "published thumb index must track the live tree"
    finally:
        context.close()


def test_putjson_worker_roundtrip_and_supersede(browser, server):
    context, page = _new_page(browser)
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        out = page.evaluate(
            """async () => {
                const OrigWorker = window.Worker;
                window.__workerUsed = false;
                window.Worker = class extends OrigWorker {
                    constructor(...a){ window.__workerUsed = true; super(...a); }
                };
                const a = [{name:'A', type:'folder', children:[]}];
                const b = [{name:'B', type:'folder',
                    children:[{name:'x.png', type:'image'}]}];
                await window.OfflineTree.putJson(JSON.stringify(a));
                const r1 = await window.OfflineTree.getTree();
                const p = window.OfflineTree.putJson(JSON.stringify(a));
                await window.OfflineTree.putJson(JSON.stringify(b));
                await p.catch(() => {});
                const r2 = await window.OfflineTree.getTree();
                window.Worker = OrigWorker;
                return {
                    used: window.__workerUsed,
                    r1: r1 && r1.map(n => n.name),
                    r2: r2 && r2.map(n => n.name),
                    r2kids: r2 && r2[0].children.map(n => n.name),
                };
            }"""
        )
        assert out["used"], "putJson must route through the tree-writer worker"
        assert out["r1"] == ["A"], out
        assert out["r2"] == ["B"] and out["r2kids"] == ["x.png"], (
            f"latest-wins: superseding write must land, got {out['r2']}"
        )
    finally:
        context.close()


def test_putjson_envelope_opt_in(browser, server):
    context, page = _new_page(browser)
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        out = page.evaluate(
            """async () => {
                const env = JSON.stringify({tree: [{name:'ENV', type:'folder',
                    children:[{name:'e.png', type:'image'}]}], generation: 7});
                await window.OfflineTree.putJson(env, {treeEnvelope: true});
                const r1 = await window.OfflineTree.getTree();
                let threw = false;
                try { await window.OfflineTree.putJson(env); } catch(e){ threw = true; }
                const r2 = await window.OfflineTree.getTree();
                let threw2 = false;
                try {
                    await window.OfflineTree.putJson(
                        JSON.stringify({tree: {not: 'list'}, generation: 1}),
                        {treeEnvelope: true});
                } catch(e){ threw2 = true; }
                const r3 = await window.OfflineTree.getTree();
                return {
                    r1: r1 && r1.map(n => n.name),
                    threw, r2: r2 && r2.map(n => n.name),
                    threw2, r3: r3 && r3.map(n => n.name),
                };
            }"""
        )
        assert out["r1"] == ["ENV"], out
        assert out["threw"], "envelope JSON without treeEnvelope flag must reject"
        assert out["r2"] == ["ENV"], out
        assert out["threw2"], "envelope whose tree is not an array must reject"
        assert out["r3"] == ["ENV"], "rejected write must not overwrite a valid tree"
    finally:
        context.close()


def test_putjson_stale_worker_callbacks_do_not_kill_new_write(browser, server):
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    page.add_init_script(
        """window.__fakeWorkers = [];
        window.Worker = class {
            constructor(){ this.onmessage = null; this.onerror = null;
                this.terminated = false; window.__fakeWorkers.push(this); }
            postMessage(m){ this.lastMsg = m; }
            terminate(){ this.terminated = true; }
        };"""
    )
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        _stop_fill_poll(page)
        out = page.evaluate(
            """async () => {
                await window.OfflineTree.delTree();
                const base = window.__fakeWorkers.length;
                const A = JSON.stringify([{name:'A', type:'folder', children:[]}]);
                const B = JSON.stringify([{name:'B', type:'folder', children:[]}]);
                const pa = window.OfflineTree.putJson(A).then(() => true, () => false);
                const wa = window.__fakeWorkers[base];
                const aErr = wa.onerror, aMsg = wa.onmessage;
                const pb = window.OfflineTree.putJson(B).then(() => true, () => false);
                const wb = window.__fakeWorkers[base + 1];
                const spawned = window.__fakeWorkers.length - base;
                const aTerminated = wa.terminated === true;
                if (aErr) aErr(new Error('stale'));
                if (aMsg) aMsg({data: {ok: false}});
                await new Promise(r => setTimeout(r, 0));
                const bAlive = !wb.terminated;
                if (wb.onmessage) wb.onmessage({data: {ok: true}});
                const pbOk = await pb;
                const paOk = await pa;
                return {aTerminated, bAlive, pbOk, paOk, spawned};
            }"""
        )
        assert out["spawned"] == 2, f"no extra fallback worker may spawn: {out}"
        assert out["aTerminated"], "superseded worker must be terminated"
        assert out["paOk"], "superseded write must resolve, not reject"
        assert out["bAlive"], "stale A callbacks must not terminate/clear worker B"
        assert out["pbOk"], "worker B must still settle its own promise"
    finally:
        context.close()


def test_putjson_worker_unsupported_falls_back(browser, server):
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    page.add_init_script("window.Worker = undefined;")
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        out = page.evaluate(
            """async () => {
                const a = [{name:'FB', type:'folder',
                    children:[{name:'y.png', type:'image'}]}];
                await window.OfflineTree.putJson(JSON.stringify(a));
                const r = await window.OfflineTree.getTree();
                return r && r.map(n => n.name);
            }"""
        )
        assert out == ["FB"], out
    finally:
        context.close()


def test_putjson_worker_error_falls_back_once(browser, server):
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    page.add_init_script(
        """window.Worker = class {
            constructor(){ setTimeout(() => {
                if (this.onerror) this.onerror(new Error('boom'));
            }, 0); }
            postMessage(){}
            terminate(){}
        };"""
    )
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        out = page.evaluate(
            """async () => {
                const a = [{name:'EFB', type:'folder',
                    children:[{name:'z.png', type:'image'}]}];
                await window.OfflineTree.putJson(JSON.stringify(a));
                const r = await window.OfflineTree.getTree();
                return r && r.map(n => n.name);
            }"""
        )
        assert out == ["EFB"], out
    finally:
        context.close()


def test_putjson_rejects_non_array_without_overwrite(browser, server):
    context, page = _new_page(browser)
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        out = page.evaluate(
            """async () => {
                const a = [{name:'KEEP', type:'folder', children:[]}];
                await window.OfflineTree.putJson(JSON.stringify(a));
                let threw = false;
                try { await window.OfflineTree.putJson('{"x":1}'); }
                catch(e){ threw = true; }
                const r = await window.OfflineTree.getTree();
                return {threw, names: r && r.map(n => n.name)};
            }"""
        )
        assert out["threw"], "non-array snapshot must reject"
        assert out["names"] == ["KEEP"], out
    finally:
        context.close()


def test_deltree_and_clear_cancel_pending_worker_write(browser, server):
    context, page = _new_page(browser)
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        out = page.evaluate(
            """async () => {
                const big = JSON.stringify([{name:'W', type:'folder',
                    children: Array.from({length:50000}, (_,i) =>
                        ({name:'p'+i+'.png', type:'image'}))}]);
                const p1 = window.OfflineTree.putJson(big);
                await window.OfflineTree.delTree();
                await p1.catch(() => {});
                await new Promise(r => setTimeout(r, 800));
                const t1 = await window.OfflineTree.getTree();
                const p2 = window.OfflineTree.putJson(big);
                await window.OfflineStore.clear();
                await p2.catch(() => {});
                await new Promise(r => setTimeout(r, 800));
                const t2 = await window.OfflineTree.getTree();
                const direct = [{name:'direct', type:'folder', children:[]}];
                const p3 = window.OfflineTree.putJson(big);
                await window.OfflineTree.putTree(direct);
                await p3.catch(() => {});
                await new Promise(r => setTimeout(r, 800));
                const t3 = await window.OfflineTree.getTree();
                return {
                    afterDel: !!t1,
                    afterClear: !!t2,
                    afterPutTree: t3 && t3.map(n => n.name),
                };
            }"""
        )
        assert out["afterDel"] is False, "delTree must cancel the pending worker write"
        assert out["afterClear"] is False, "OfflineStore.clear must cancel the pending write"
        assert out["afterPutTree"] == ["direct"], (
            "direct putTree must supersede the worker write, got "
            f"{out['afterPutTree']}"
        )
    finally:
        context.close()


def test_tree_writer_worker_shell_precached(server):
    sw_text = SW_JS.read_text(encoding="utf-8")
    m = re.search(r"SHELL_ASSETS\s*=\s*\[(.*?)\]", sw_text, re.S)
    assert m, "SHELL_ASSETS block not found"
    assert '"/static/offline/tree-writer.js"' in m.group(1), (
        "tree-writer.js worker must be in the shell precache so offline putJson works"
    )
    assert urlopen(server + "/static/offline/tree-writer.js").status == 200


def test_fillpoll_dict_envelope_passes_raw_text(browser, server):
    context, page = _new_page(browser)
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        _stop_fill_poll(page)
        page.evaluate(
            """() => {
                const of = window.fetch.bind(window);
                window.fetch = (u, i) => {
                    const url = typeof u === 'string' ? u : (u && u.url) || '';
                    if (url.indexOf('/api/v1/tree') === 0)
                        return Promise.resolve(new Response(
                            JSON.stringify({tree: [{name:'FP', type:'folder',
                                children:[{name:'e.png', type:'image'}]}], generation: 42}),
                            {status: 200, headers: {'Content-Type': 'application/json'}}));
                    return of(u, i);
                };
                window.__fp = null;
                window.FillPoll.noteFullLoad({etag: null, onTree: (t, raw, opts) => {
                    window.__fp = {name: t && t[0] && t[0].name, raw, opts};
                }});
            }"""
        )
        page.wait_for_function("() => !!window.__fp", timeout=10000)
        out = page.evaluate("() => window.__fp")
        assert out["name"] == "FP", out
        assert '"generation"' in out["raw"], out
        assert out["opts"]["treeEnvelope"] is True, out
    finally:
        context.close()


def test_offline_reload_restores_tree_and_viewer_deep_link(browser, server):
    context, page = _new_page(browser)
    try:
        page.goto(server + "/?offline=1")
        page.wait_for_function(
            "() => navigator.serviceWorker && navigator.serviceWorker.controller",
            timeout=15000,
        )
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        n = page.evaluate(
            """async () => {
                for (let i = 0; i < 100; i++) {
                    const t = await window.OfflineTree.getTree();
                    if (t && t.length) return t.length;
                    await new Promise(r => setTimeout(r, 50));
                }
                return 0;
            }"""
        )
        assert n > 0, "putJson snapshot never landed in IndexedDB"
        context.set_offline(True)
        try:
            page.reload()
            page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
            page.wait_for_selector("#tile-grid .tile", timeout=8000)
            leaf = page.evaluate(
                """() => {
                    let found = null;
                    (function walk(list, pp){
                        for (const node of list || []) {
                            const fp = pp ? pp + '/' + node.name : node.name;
                            if (!found && node.type === 'folder' && node.children &&
                                node.children.length &&
                                node.children.every(c => c.type === 'image'))
                                found = fp;
                            if (node.children) walk(node.children, fp);
                        }
                    })(window.__BOOKQ_DEBUG.getTree(), '');
                    return found;
                }"""
            )
            assert leaf, "no leaf folder found in restored tree"
            page.evaluate(
                "(fp) => { location.hash = window.viewerHash(fp, 0); }", leaf
            )
            page.wait_for_function(
                "() => window.currentFolder !== null", timeout=8000
            )
            assert page.evaluate("() => window.currentFolder") == leaf
            assert page.evaluate("() => window.images.length") > 0
        finally:
            context.set_offline(False)
    finally:
        context.close()


PREFETCH_STUB_JS = """() => {
    window.__pf = {calls: [], puts: [], maxInflight: 0, _inflight: 0, aborted: false};
    const of = window.fetch.bind(window);
    window.__pfFetch = of;
    window.fetch = (u, i) => {
        const url = typeof u === 'string' ? u : (u && u.url) || '';
        if (url.indexOf('/thumb/') === 0 || url.indexOf('/api/v1/thumb-sprite') === 0) {
            window.__pf.calls.push(url);
            window.__pf._inflight++;
            window.__pf.maxInflight = Math.max(window.__pf.maxInflight, window.__pf._inflight);
            if (i && i.signal)
                i.signal.addEventListener('abort', () => { window.__pf.aborted = true; });
            return new Promise(r => setTimeout(() => {
                window.__pf._inflight--;
                r(new Response('thumb-bytes', {status: 200}));
            }, 10));
        }
        return of(u, i);
    };
    caches.match = async () => null;
    const oo = caches.open.bind(caches);
    caches.open = async (name) => {
        const c = await oo(name);
        const op = c.put.bind(c);
        c.put = async (req, res) => {
            window.__pf.puts.push(typeof req === 'string' ? req : req.url);
            return op(req, res);
        };
        return c;
    };
    window.__idleQueue = window.__idleQueue || [];
    window.requestIdleCallback = (fn) => { window.__idleQueue.push(fn); return window.__idleQueue.length; };
    window.__flushIdle = () => {
        for (let i = 0; i < 10 && window.__idleQueue.length; i++) {
            const q = window.__idleQueue.splice(0);
            q.forEach(f => f());
        }
    };
}"""


ELIGIBLE_URLS_JS = """() => {
    const s = document.getElementById('sidebar-scroll');
    const r0 = s ? s.getBoundingClientRect() : null;
    const vh = window.innerHeight, vw = window.innerWidth;
    const vp = {
        top: Math.max(0, r0 ? r0.top : 0),
        bottom: Math.min(vh, r0 ? r0.bottom : vh),
        left: Math.max(0, r0 ? r0.left : 0),
        right: Math.min(vw, r0 ? r0.right : vw),
    };
    const h = Math.max(1, vp.bottom - vp.top);
    const out = [];
    const seen = new Set();
    document.querySelectorAll(
        '#tile-grid .tile[data-path], #tile-grid .item[data-path], #tile-grid .card[data-path]'
    ).forEach(el => {
        const r = el.getBoundingClientRect();
        if (!r || (r.width === 0 && r.height === 0)) return;
        if (r.right <= vp.left || r.left >= vp.right) return;
        const on = r.bottom > vp.top && r.top < vp.bottom;
        const near = (r.bottom <= vp.top && r.bottom >= vp.top - h) ||
                     (r.top >= vp.bottom && r.top <= vp.bottom + h);
        if (!on && !near) return;
        el.querySelectorAll('img[src]').forEach(img => {
            const u = img.getAttribute('src');
            if ((u.indexOf('/thumb/') === 0 || u.indexOf('/api/v1/thumb-sprite') === 0) && !seen.has(u)) {
                seen.add(u);
                out.push(u);
            }
        });
    });
    return out.slice(0, 50);
}"""


DOM_SRCS_JS = """() => Array.from(
    document.querySelectorAll('#tile-grid img[src]')
).map(i => i.getAttribute('src'))"""


def _seed_many_tiles(page, count):
    page.evaluate(
        """(count) => {
            const t = Array.from({length: count}, (_, i) => ({
                name: 'F' + i, type: 'folder', thumb: 't.png',
                children: [{name: 't.png', type: 'image'}],
            }));
            window.__BOOKQ_DEBUG.setTree(t);
            window.renderView();
        }""",
        count,
    )


def _quiesce_prefetch(page):
    page.wait_for_timeout(300)
    page.evaluate(
        """() => {
            if (window.__flushIdle) window.__flushIdle();
            window.ThumbPrefetch.cancelThumbPrefetch();
        }"""
    )
    page.wait_for_timeout(150)
    page.evaluate(
        """() => {
            window.__pf.calls.length = 0;
            window.__pf.puts.length = 0;
            window.__pf.aborted = false;
            window.__pf.maxInflight = 0;
            window.__pf._inflight = 0;
        }"""
    )


def test_prefetch_exact_dom_urls_viewport_only(browser, server):
    context, page = _new_prefetch_page(browser)
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        _stop_fill_poll(page)
        page.wait_for_selector("#tile-grid .tile")
        _quiesce_prefetch(page)
        page.evaluate("() => window.__BOOKQ_DEBUG.prefetchThumbs()")
        calls = page.evaluate("() => window.__pf.calls")
        puts = page.evaluate("() => window.__pf.puts")
        expected = page.evaluate(ELIGIBLE_URLS_JS)
        dom_srcs = page.evaluate(DOM_SRCS_JS)
        assert len(calls) > 0, "eligible viewport tiles produced no prefetch calls"
        assert set(puts) == set(calls), "fetched URLs must land in the thumb caches"
        extra = set(calls) - set(expected)
        assert not extra, f"fetched URLs outside viewport-eligible set: {extra}"
        assert set(calls) <= set(dom_srcs), (
            "must fetch verbatim rendered img[src] URLs — no width upgrades"
        )
        assert len(set(calls)) == min(50, len(expected)), (
            f"expected all {min(50, len(expected))} eligible URLs, "
            f"got {len(set(calls))}: {calls}"
        )
        assert page.evaluate("() => window.__pf.maxInflight") <= 3
    finally:
        context.close()


def test_prefetch_exact_urls_list_and_card_modes(browser, server):
    context, page = _new_prefetch_page(browser)
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        _stop_fill_poll(page)
        _quiesce_prefetch(page)
        for mode, sel in (("list", ".item[data-path]"), ("card", ".card[data-path]")):
            page.evaluate(
                f"() => {{ window.viewMode = '{mode}'; window.renderView(); }}"
            )
            page.wait_for_selector(f"#tile-grid {sel}", timeout=5000)
            page.evaluate("() => { window.__pf.calls.length = 0; }")
            page.evaluate("() => window.__BOOKQ_DEBUG.prefetchThumbs()")
            calls = page.evaluate("() => window.__pf.calls")
            expected = page.evaluate(ELIGIBLE_URLS_JS)
            dom_srcs = page.evaluate(DOM_SRCS_JS)
            assert set(calls) <= set(expected), (
                f"{mode} mode fetched out-of-viewport URLs: {set(calls) - set(expected)}"
            )
            assert set(calls) <= set(dom_srcs), (
                f"{mode} mode must fetch verbatim img[src] URLs"
            )
    finally:
        context.close()


def test_prefetch_cap_50(browser, server):
    context, page = _new_prefetch_page(browser, height=20000)
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        _stop_fill_poll(page)
        _seed_many_tiles(page, 70)
        page.wait_for_selector("#tile-grid .tile")
        _quiesce_prefetch(page)
        page.evaluate("() => window.__BOOKQ_DEBUG.prefetchThumbs()")
        calls = page.evaluate("() => window.__pf.calls")
        puts = page.evaluate("() => window.__pf.puts")
        eligible = page.evaluate(ELIGIBLE_URLS_JS)
        assert len(eligible) >= 50, f"fixture produced only {len(eligible)} eligible URLs"
        assert len(set(calls)) == 50, f"prefetch must cap at 50, got {len(set(calls))}"
        assert set(puts) == set(calls), "the 50 fetched URLs must land in the thumb caches"
    finally:
        context.close()


def test_prefetch_coalesced_schedule_and_close_cancel(browser, server):
    context, page = _new_prefetch_page(browser)
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        _stop_fill_poll(page)
        _quiesce_prefetch(page)
        queued = page.evaluate(
            """() => {
                window.ThumbPrefetch.scheduleThumbPrefetch();
                window.ThumbPrefetch.scheduleThumbPrefetch();
                window.ThumbPrefetch.scheduleThumbPrefetch();
                return window.__idleQueue.length;
            }"""
        )
        assert queued == 1, f"repeated scheduleThumbPrefetch must coalesce, queued {queued}"
        page.evaluate(
            """() => {
                const of = window.__pfFetch;
                window.fetch = (u, i) => {
                    const url = typeof u === 'string' ? u : (u && u.url) || '';
                    if (url.indexOf('/thumb/') === 0 || url.indexOf('/api/v1/thumb-sprite') === 0) {
                        window.__pf.calls.push(url);
                        if (i && i.signal)
                            i.signal.addEventListener('abort', () => { window.__pf.aborted = true; });
                        return new Promise((r, j) => setTimeout(
                            () => r(new Response('x', {status: 200})), 200));
                    }
                    return of(u, i);
                };
            }"""
        )
        out = page.evaluate(
            """async () => {
                window.__idleQueue.splice(0).forEach(f => f());
                await new Promise(r => setTimeout(r, 30));
                document.getElementById('sidebar').classList.remove('open');
                const at = {calls: window.__pf.calls.length, puts: window.__pf.puts.length};
                await new Promise(r => setTimeout(r, 450));
                return {at, later: {calls: window.__pf.calls.length, puts: window.__pf.puts.length},
                        aborted: window.__pf.aborted};
            }"""
        )
        assert out["aborted"], "closing the sidebar must abort in-flight fetches via observer"
        assert out["later"]["calls"] == out["at"]["calls"], (
            "stale batch must not fetch after cancel"
        )
        assert out["later"]["puts"] == out["at"]["puts"], (
            "stale batch must not cache after cancel"
        )
        assert out["at"]["puts"] == 0
    finally:
        context.close()


def test_prefetch_scroll_reopen_resume(browser, server):
    context, page = _new_prefetch_page(browser, width=1280, height=600)
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        _stop_fill_poll(page)
        _seed_many_tiles(page, 80)
        page.wait_for_selector("#tile-grid .tile")
        _quiesce_prefetch(page)
        bottom_url = page.evaluate(
            """() => {
                const els = document.querySelectorAll('#tile-grid .tile[data-path]');
                const img = els[els.length - 1].querySelector('img[src]');
                return img ? img.getAttribute('src') : null;
            }"""
        )
        assert bottom_url
        page.evaluate(
            "() => { document.getElementById('sidebar').classList.remove('open'); }"
        )
        page.wait_for_timeout(150)
        page.evaluate("() => window.__flushIdle()")
        page.evaluate("() => window.__BOOKQ_DEBUG.prefetchThumbs()")
        assert page.evaluate("() => window.__pf.calls.length") == 0, (
            "closed sidebar must fetch nothing — no arbitrary first-50 fallback"
        )
        page.evaluate(
            "() => { document.getElementById('sidebar').classList.add('open'); }"
        )
        page.evaluate(
            """() => {
                const s = document.getElementById('sidebar-scroll');
                s.scrollTop = s.scrollHeight;
                s.dispatchEvent(new Event('scroll'));
            }"""
        )
        page.wait_for_timeout(200)
        page.evaluate("() => window.__flushIdle()")
        page.wait_for_function(
            "() => window.__pf.calls.includes(%s)" % json.dumps(bottom_url),
            timeout=5000,
        )
    finally:
        context.close()


def test_prefetch_quota_fails_silent(browser, server):
    context, page = _new_prefetch_page(browser)
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        _stop_fill_poll(page)
        _quiesce_prefetch(page)
        page.evaluate(
            """() => {
                caches.open = async () => ({
                    put: async () => {
                        throw new DOMException('no space', 'QuotaExceededError');
                    },
                });
            }"""
        )
        out = page.evaluate(
            """async () => {
                try {
                    await window.__BOOKQ_DEBUG.prefetchThumbs();
                    return {threw: false, calls: window.__pf.calls.length};
                } catch(e) { return {threw: true, calls: window.__pf.calls.length}; }
            }"""
        )
        assert not out["threw"], "quota failure must resolve silently"
        assert out["calls"] <= 3, (
            f"quota failure must stop the batch early, got {out['calls']} fetches"
        )
    finally:
        context.close()


def test_recent_online_load_offline_online_invalidates_freshness(browser, server):
    """offline→online inside the 5-minute freshness window must revalidate:
    the 'offline' listener resets _onlineRevalidateLast, so the reconnect
    revalidation fires even though the online initial load just stamped it.
    """
    context, page = _new_page(browser)
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        _stop_fill_poll(page)
        page.evaluate(REVALIDATE_SPY_JS)
        # Recent online initial load: direct revalidate is freshness-gated.
        page.evaluate("() => window.revalidateFoldersForOnline()")
        assert page.evaluate("() => window.__perf.foldersReqs.length") == 0, (
            "fresh initial stamp must gate an immediate revalidation"
        )
        # Simulated offline→online flip (no fake clock): must revalidate now.
        page.evaluate("() => window.dispatchEvent(new Event('offline'))")
        page.evaluate("() => window.dispatchEvent(new Event('online'))")
        page.wait_for_function(
            "() => window.__perf.foldersReqs.length >= 1", timeout=8000
        )
        reqs = page.evaluate("() => window.__perf.foldersReqs")
        assert reqs[0]["url"].endswith("/api/v1/tree"), reqs
        assert reqs[0]["inm"], f"must be a conditional request: {reqs}"
    finally:
        context.close()


def test_sw_offline_reload_revalidates_on_reconnect(browser, server):
    """Full offline-cache reload then reconnect <5min: the initial offline
    load must NOT stamp the freshness gate, so the online event sends a full
    conditional /api/v1/tree revalidation (no fake time)."""
    context, page = _new_page(browser)
    try:
        page.goto(server + "/?offline=1")
        page.wait_for_function(
            "() => navigator.serviceWorker && navigator.serviceWorker.controller",
            timeout=15000,
        )
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        # Warm the SW API cache snapshot (full tree) before going offline.
        page.wait_for_function(
            """async () => {
                try {
                    const c = await caches.open('bookq-api-v1');
                    return !!(await c.match('/api/v1/tree'));
                } catch (e) { return false; }
            }""",
            timeout=10000,
        )
        context.set_offline(True)
        try:
            page.reload()
            page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
            page.wait_for_selector("#tile-grid .tile", timeout=8000)
            _stop_fill_poll(page)
            page.evaluate(REVALIDATE_SPY_JS)
        finally:
            context.set_offline(False)
        # Reconnect: the real 'online' event (or the explicit one below —
        # Playwright may not deliver it in headless) debounces into a
        # revalidation that must not be eaten by the freshness gate.
        page.evaluate("() => window.dispatchEvent(new Event('online'))")
        page.wait_for_function(
            """() => window.__perf.foldersReqs.some(
                r => r.url.endsWith('/api/v1/tree'))""",
            timeout=10000,
        )
        reqs = page.evaluate("() => window.__perf.foldersReqs")
        full = [r for r in reqs if r["url"].endswith("/api/v1/tree")]
        assert full, f"reconnect must issue a full /api/v1/tree request: {reqs}"
        assert full[0]["inm"], (
            f"reconnect revalidation must be conditional (If-None-Match "
            f"seeded from the SW-cached response): {full}"
        )
    finally:
        context.close()


def test_putjson_defer_holds_until_idle_and_writes(browser, server):
    context, page = _new_page(browser)
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        _stop_fill_poll(page)
        out = page.evaluate(
            """async () => {
                const OrigWorker = window.Worker;
                window.__workerMade = 0;
                window.Worker = class extends OrigWorker {
                    constructor(...a){ window.__workerMade++; super(...a); }
                };
                const held = [];
                const origRIC = window.requestIdleCallback;
                window.requestIdleCallback = (fn) => { held.push(fn); return held.length; };
                try {
                    await window.OfflineTree.delTree();
                    held.length = 0;
                    const p = window.OfflineTree.putJson(
                        JSON.stringify([{name:'D', type:'folder', children:[]}]),
                        {defer: true});
                    let settled = false;
                    p.then(() => { settled = true; }, () => { settled = true; });
                    await new Promise(r => setTimeout(r, 60));
                    const before = {workers: window.__workerMade, settled, queued: held.length};
                    held.splice(0).forEach(f => f());
                    await p;
                    const t = await window.OfflineTree.getTree();
                    return {before,
                            workersAfter: window.__workerMade,
                            names: t && t.map(n => n.name)};
                } finally {
                    window.requestIdleCallback = origRIC;
                    window.Worker = OrigWorker;
                }
            }"""
        )
        assert out["before"]["workers"] == 0, (
            "deferred putJson must not construct the Worker before idle"
        )
        assert out["before"]["settled"] is False, (
            "deferred putJson promise must stay pending until idle runs"
        )
        assert out["before"]["queued"] >= 1, "deferred write must be queued on rIC"
        assert out["names"] == ["D"], out
        assert out["workersAfter"] >= 1, (
            "released idle job must launch the real worker slot"
        )
    finally:
        context.close()


def test_putjson_defer_fallback_after_idle(browser, server):
    """Worker-undefined fallback: the JSON.parse/clone must also wait for idle,
    then round-trip once released."""
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    page.add_init_script("window.Worker = undefined;")
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        out = page.evaluate(
            """async () => {
                const held = [];
                const origRIC = window.requestIdleCallback;
                const origParse = JSON.parse;
                let parseCalls = 0;
                window.requestIdleCallback = (fn) => { held.push(fn); return held.length; };
                JSON.parse = function (s) {
                    if (typeof s === 'string' && s.indexOf('DFB') !== -1) parseCalls++;
                    return origParse.apply(JSON, arguments);
                };
                try {
                    await window.OfflineTree.delTree();
                    held.length = 0;
                    const p = window.OfflineTree.putJson(
                        JSON.stringify([{name:'DFB', type:'folder', children:[]}]),
                        {defer: true});
                    await new Promise(r => setTimeout(r, 60));
                    const parsedBefore = parseCalls;
                    held.splice(0).forEach(f => f());
                    await p;
                    const t = await window.OfflineTree.getTree();
                    return {parsedBefore, names: t && t.map(n => n.name)};
                } finally {
                    window.requestIdleCallback = origRIC;
                    JSON.parse = origParse;
                }
            }"""
        )
        assert out["parsedBefore"] == 0, (
            "deferred fallback must not JSON.parse before idle"
        )
        assert out["names"] == ["DFB"], out
    finally:
        context.close()


def test_deferred_putjson_superseded_settles_before_idle(browser, server):
    """A newer putJson/putTree/delTree/clear must cancel the pending deferred
    write: the promise settles immediately and a manually-fired stale idle
    callback does nothing."""
    context, page = _new_page(browser)
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        _stop_fill_poll(page)
        out = page.evaluate(
            """async () => {
                const held = [];
                const origRIC = window.requestIdleCallback;
                window.requestIdleCallback = (fn) => { held.push(fn); return held.length; };
                const mk = n => JSON.stringify([{name:n, type:'folder', children:[]}]);
                const names = async () => {
                    const t = await window.OfflineTree.getTree();
                    return t && t.map(n => n.name);
                };
                try {
                    await window.OfflineTree.delTree();
                    held.length = 0;
                    // 1) newer putJson supersedes
                    const pA = window.OfflineTree.putJson(mk('A'), {defer:true});
                    const cbA = held[held.length - 1];
                    let settledA = false;
                    pA.then(() => { settledA = true; }, () => { settledA = true; });
                    const pB = window.OfflineTree.putJson(mk('B'), {defer:true});
                    await pA;   // canceled job settles before any idle fires
                    if (cbA) cbA();           // stale manual fire: no-op
                    held.splice(0).forEach(f => f());
                    await pB;
                    const t1 = await names();
                    // 2) direct putTree supersedes
                    held.length = 0;
                    const pC = window.OfflineTree.putJson(mk('C'), {defer:true});
                    const cbC = held[held.length - 1];
                    let settledC = false;
                    pC.then(() => { settledC = true; }, () => { settledC = true; });
                    await window.OfflineTree.putTree(
                        [{name:'direct', type:'folder', children:[]}]);
                    await pC;
                    if (cbC) cbC();           // stale manual fire: no-op
                    const t2 = await names();
                    // 3) delTree supersedes
                    const pD = window.OfflineTree.putJson(mk('D'), {defer:true});
                    let settledD = false;
                    pD.then(() => { settledD = true; }, () => { settledD = true; });
                    await window.OfflineTree.delTree();
                    await pD;
                    // 4) OfflineStore.clear supersedes
                    const pE = window.OfflineTree.putJson(mk('E'), {defer:true});
                    let settledE = false;
                    pE.then(() => { settledE = true; }, () => { settledE = true; });
                    await window.OfflineStore.clear();
                    await pE;
                    const t3 = await names();
                    return {settledA, settledC, settledD, settledE, t1, t2, t3};
                } finally { window.requestIdleCallback = origRIC; }
            }"""
        )
        assert out["settledA"], "canceled deferred putJson must settle before idle"
        assert out["t1"] == ["B"], out
        assert out["settledC"], "putTree must cancel the pending deferred write"
        assert out["t2"] == ["direct"], out
        assert out["settledD"], "delTree must cancel the pending deferred write"
        assert out["settledE"], "clear must cancel the pending deferred write"
        assert out["t3"] is None, f"canceled writes must never land: {out['t3']}"
    finally:
        context.close()


def test_persist_tree_snapshot_defers_for_load_and_revalidate(browser, server):
    """App-level: the normal full load AND the revalidate path must forward
    opts.defer to OfflineTree.putJson (idle persistence, no main-thread
    parse/clone)."""
    context, page = _new_page(browser)
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        _stop_fill_poll(page)
        seen = page.evaluate(
            """async () => {
                const seen = [];
                const ot = window.OfflineTree;
                const origJ = ot.putJson.bind(ot);
                const origT = ot.putTree.bind(ot);
                ot.putJson = (text, opts) => {
                    seen.push({m:'putJson', defer: !!(opts && opts.defer)});
                    return origJ(text, opts);
                };
                ot.putTree = (data, opts) => {
                    seen.push({m:'putTree', defer: !!(opts && opts.defer)});
                    return origT(data, opts);
                };
                const rn = Date.now.bind(Date);
                Date.now = () => rn() + 300001;   // only to pass the 5-min freshness gate
                const of = window.fetch.bind(window);
                const changed = [{name:'CHANGED', type:'folder',
                    children:[{name:'c.png', type:'image'}]}];
                window.fetch = (u, i) => {
                    const url = typeof u === 'string' ? u : (u && u.url) || '';
                    if (url === '/api/v1/tree')
                        return Promise.resolve(new Response(JSON.stringify(changed),
                            {status: 200, headers: {'Content-Type': 'application/json',
                                                    'ETag': '"C1"'}}));
                    return of(u, i);
                };
                try {
                    await window.revalidateFoldersForOnline();  // changed tree → persist
                    await window.loadFolders({});               // full load → persist
                } finally {
                    window.fetch = of;
                    ot.putJson = origJ;
                    ot.putTree = origT;
                }
                return seen;
            }"""
        )
        assert len(seen) >= 2, f"expected persist calls on both paths: {seen}"
        assert all(c["defer"] for c in seen), (
            f"every tree-snapshot persist must defer to idle: {seen}"
        )
    finally:
        context.close()


def test_buildnodemap_repair_keeps_full_index_alive(browser, server):
    """Shallow fetch fails; the full async index build is held mid-yield.
    buildNodeMap() on the still-initial empty tree must repair without
    superseding — the full load must complete and publish tree+map."""
    big = (
        '[{"name":"big","type":"folder","children":['
        + ",".join(
            f'{{"name":"p{i}.png","type":"image"}}' for i in range(20000)
        )
        + "]}]"
    )
    context, page = _new_page(browser)
    page.add_init_script(
        """window.__heldIdx = [];
        window.__origPM = MessagePort.prototype.postMessage;
        MessagePort.prototype.postMessage = function(...args){
            if (args[0] === 0) { window.__heldIdx.push({port: this, args}); return; }
            return window.__origPM.apply(this, args);
        };"""
    )

    def handler(route):
        if "depth=1" in route.request.url:
            route.abort()
        else:
            route.fulfill(status=200, content_type="application/json", body=big)

    page.route("**/api/v1/tree*", handler)
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.__heldIdx.length > 0", timeout=15000)
        # tree is still the initial [] and no index is published — a map
        # read/repair here must not cancel the in-flight async build.
        page.evaluate("() => window.buildNodeMap()")
        page.evaluate(
            """() => {
                MessagePort.prototype.postMessage = window.__origPM;
                const q = window.__heldIdx.splice(0);
                for (const m of q) window.__origPM.apply(m.port, m.args);
            }"""
        )
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        assert page.evaluate("() => window.buildNodeMap().has('big')"), (
            "full index must publish after the held build resumes"
        )
        assert page.evaluate(
            "() => window.__cc_thumbIndex.tree === window.__BOOKQ_DEBUG.getTree()"
        ), "published thumb index must track the loaded full tree"
    finally:
        try:
            page.unroute("**/api/v1/tree*")
        except Exception:
            pass
        context.close()


def test_stale_recovery_cannot_mark_newer_load_ready(browser, server):
    """An old load's recovery (IDB snapshot / fallback render) must not mark
    or render while a newer load's shallow stage is still pending."""
    context, page = _new_page(browser)
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        _stop_fill_poll(page)
        out = page.evaluate(
            """async () => {
                const mkRes = data => ({
                    ok: true, status: 200,
                    headers: {get: () => null},
                    json: async () => data,
                    text: async () => JSON.stringify(data),
                });
                const ot = window.OfflineTree;
                const origGet = ot.getTree.bind(ot);
                let releaseGet;
                ot.getTree = () => new Promise(r => { releaseGet = () => r(null); });
                window.__BOOKQ_DEBUG.treeLoaded = false;
                let releaseShallow;
                const shallowHeld = new Promise(r => { releaseShallow = r; });
                const tB = [{name:'tB', type:'folder',
                    children:[{name:'b.png', type:'image'}]}];
                const failAll = () => Promise.reject(new Error('net down'));
                const fB = u => u.indexOf('depth=1') >= 0
                    ? shallowHeld.then(() => mkRes(tB))
                    : Promise.resolve(mkRes(tB));
                try {
                    // Load A fails both stages and parks in recovery's getTree.
                    const pA = window.loadFolders({}, {fetch: failAll});
                    await new Promise(r => setTimeout(r, 60));
                    // Load B starts (bumps the generation) with shallow held.
                    const pB = window.loadFolders({}, {fetch: fB});
                    await new Promise(r => setTimeout(r, 10));
                    releaseGet();       // A's recovery resumes while B pending
                    await pA;
                    const markedByA = window.__BOOKQ_DEBUG.treeLoaded === true;
                    releaseShallow();
                    await pB;
                    return {markedByA,
                            final: window.__BOOKQ_DEBUG.getTree().map(n => n.name),
                            loaded: window.__BOOKQ_DEBUG.treeLoaded};
                } finally { ot.getTree = origGet; }
            }"""
        )
        assert not out["markedByA"], (
            "superseded recovery must not mark the newer pending load as ready"
        )
        assert out["final"] == ["tB"], out
        assert out["loaded"] is True, "the newer load must complete and mark ready"
    finally:
        context.close()


# ── connection/load-generation ownership (offline-vs-revalidation races) ──────

GATE_REVALIDATE_JS = """() => {
    // Records every post-install full /api/v1/tree request (the revalidation
    // path; depth=1 shallow and the initial load are excluded) and lets the
    // test park each response behind a per-index release gate.
    window.__rv = {reqs: [], gates: {}, plans: {}};
    const of = window.fetch.bind(window);
    window.fetch = (u, i) => {
        const url = typeof u === 'string' ? u : (u && u.url) || '';
        if (url === '/api/v1/tree') {
            const idx = window.__rv.reqs.length;
            window.__rv.reqs.push({
                url,
                inm: (i && i.headers && i.headers['If-None-Match']) || null,
            });
            const plan = window.__rv.plans[idx];
            if (plan) {
                return new Promise(resolve => {
                    window.__rv.gates[idx] = () => resolve(plan());
                });
            }
        }
        return of(u, i);
    };
}"""

HOLD_INDEX_JS = """() => {
    window.__heldIdx = [];
    window.__origPM = MessagePort.prototype.postMessage;
    MessagePort.prototype.postMessage = function(...args){
        window.__heldIdx.push({port: this, args});
    };
}"""

# add_init_script evaluates but does not invoke `() => {}` expressions —
# the same hookup as bare statements for page-load-time installation.
HOLD_INDEX_INIT_JS = """
    window.__heldIdx = [];
    window.__origPM = MessagePort.prototype.postMessage;
    MessagePort.prototype.postMessage = function(...args){
        window.__heldIdx.push({port: this, args});
    };
"""

# Restore BEFORE replaying: each replayed post wakes a continuation that may
# post the next yield — that must go through the real postMessage or the
# build re-stalls on a post nobody will release.
RELEASE_INDEX_JS = """() => {
    MessagePort.prototype.postMessage = window.__origPM;
    const q = window.__heldIdx.splice(0);
    for (const m of q) window.__origPM.apply(m.port, m.args);
}"""


def test_revalidate_waits_out_held_full_load_index(browser, server):
    """A revalidation must wait for the in-flight full load instead of racing
    it for the shared index: the first load's async index is held mid-yield
    while a revalidation is queued — it must not fetch, must not bump the
    index generation under the held build, and must finish without owning
    readiness. On release the load publishes tree+map and drains the pending
    deep link itself."""
    big = (
        '[{"name":"big","type":"folder","children":['
        + ",".join(
            f'{{"name":"p{i}.png","type":"image"}}' for i in range(20000)
        )
        + "]}]"
    )
    shallow = '[{"name":"big","type":"folder","thumb":"p0.png"}]'
    hits = []

    context, page = _new_page(browser)
    page.add_init_script(HOLD_INDEX_INIT_JS)

    def handler(route):
        url = route.request.url
        hits.append(url)
        if "depth=1" in url:
            route.fulfill(
                status=200, content_type="application/json", body=shallow
            )
        else:
            route.fulfill(status=200, content_type="application/json", body=big)

    page.route("**/api/v1/tree*", handler)
    try:
        page.goto(server + "/")
        page.wait_for_function(
            "() => window.__heldIdx.length > 0", timeout=15000
        )
        assert page.evaluate("() => window.__BOOKQ_DEBUG.shallow") is True
        assert page.evaluate("() => window.treeLoaded === false") is True
        # Deep link queued while !treeLoaded — applyUrlState early-returns and
        # only the full load's drain may re-run it.
        page.evaluate("() => { location.hash = '#/browse/big'; }")
        page.evaluate(
            """() => {
                window.__rDone = false;
                window.revalidateFoldersForOnline().then(
                    () => { window.__rDone = true; },
                    () => { window.__rDone = true; });
            }"""
        )
        page.wait_for_timeout(400)
        assert page.evaluate("() => window.__rDone") is False, (
            "revalidation must still be waiting on the in-flight load"
        )
        full_hits = [u for u in hits if "depth=1" not in u]
        assert len(full_hits) == 1, (
            f"revalidation must not fetch while a load owns the pipeline: {hits}"
        )
        # Release the held index yields → the load must complete unharmed —
        # if the revalidation had superseded it, the held build resolves null
        # and treeLoaded never flips.
        page.evaluate(RELEASE_INDEX_JS)
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        page.wait_for_function("() => window.__rDone === true", timeout=8000)
        assert page.evaluate("() => window.__BOOKQ_DEBUG.shallow") is False
        assert page.evaluate("() => window.buildNodeMap().has('big')")
        assert page.evaluate(
            "() => window.__cc_thumbIndex.tree === window.__BOOKQ_DEBUG.getTree()"
        )
        page.wait_for_function(
            "() => window.currentBrowsePath === 'big'", timeout=8000
        )
        # The revalidation drained through the freshness gate after the load
        # stamped — it never issued its own request.
        full_hits = [u for u in hits if "depth=1" not in u]
        assert len(full_hits) == 1, (
            f"revalidation must not have fetched at all: {hits}"
        )
    finally:
        try:
            page.unroute("**/api/v1/tree*")
        except Exception:
            pass
        context.close()


def test_offline_settling_304_does_not_stamp_reconnect_gate(browser, server):
    """A conditional request whose 304 lands while offline must NOT stamp the
    freshness gate: a reconnect inside the 5-minute window then issues a fresh
    conditional request. No clock spoofing anywhere — the offline event itself
    resets the stamp."""
    context, page = _new_page(browser)
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        _stop_fill_poll(page)
        page.evaluate(GATE_REVALIDATE_JS)
        page.evaluate(
            "() => { window.__rv.plans[0] = "
            "() => new Response(null, {status: 304}); }"
        )
        # Trigger a revalidation on the synthetic event flip (the 'offline'
        # listener resets the stamp; the 'online' debounce fires the request).
        page.evaluate(
            "() => { window.dispatchEvent(new Event('offline'));"
            "        window.dispatchEvent(new Event('online')); }"
        )
        page.wait_for_function("() => window.__rv.reqs.length === 1", timeout=10000)
        # The request is held; go really offline (navigator.onLine=false) and
        # let the stale 304 settle there.
        context.set_offline(True)
        try:
            page.evaluate("() => window.__rv.gates[0]()")
            page.wait_for_timeout(300)
        finally:
            context.set_offline(False)
        # Reconnect (<5min, real clock): if the stale 304 had stamped the gate,
        # this revalidation would be eaten and no second request would appear.
        page.wait_for_function("() => window.__rv.reqs.length >= 2", timeout=10000)
        reqs = page.evaluate("() => window.__rv.reqs")
        assert reqs[1]["url"].endswith("/api/v1/tree"), reqs
        assert reqs[1]["inm"], (
            f"reconnect must issue a fresh conditional request: {reqs}"
        )
    finally:
        context.close()


def test_fast_reconnect_supersedes_held_revalidation(browser, server):
    """offline→online while the old revalidation is still held: the new
    connection must start a NEW request before the old settles, a concurrent
    revalidate must coalesce with the new one (no third fetch), and the stale
    old response must not mutate tree/ETag/stamp."""
    context, page = _new_page(browser)
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        _stop_fill_poll(page)
        page.evaluate(GATE_REVALIDATE_JS)
        stale_json = (
            '[{"name":"STALEMARK","type":"folder",'
            '"children":[{"name":"s.png","type":"image"}]}]'
        )
        page.evaluate(
            """(staleJson) => {
                window.__rv.plans[0] = () => new Response(staleJson, {
                    status: 200,
                    headers: {'Content-Type': 'application/json',
                              'ETag': '"STALETAG"'}});
                window.__rv.plans[1] = () => new Response(null, {status: 304});
            }""",
            stale_json,
        )
        out = page.evaluate(
            """async () => {
                const tick = () => new Promise(r => setTimeout(r, 60));
                const D = window.__BOOKQ_DEBUG;
                const etag0 = D.etagByKey.get(':full');
                // Connection 1: revalidation starts and parks on the held fetch.
                window.dispatchEvent(new Event('offline'));
                window.dispatchEvent(new Event('online'));
                const p1 = window.revalidateFoldersForOnline();
                await tick();
                const reqsAfter1 = window.__rv.reqs.length;
                // Fast offline→online: the held request is now obsolete; the
                // reconnect must start a NEW request without waiting for it.
                window.dispatchEvent(new Event('offline'));
                window.dispatchEvent(new Event('online'));
                const p2 = window.revalidateFoldersForOnline();
                await tick();
                const reqsAfter2 = window.__rv.reqs.length;
                // A concurrent revalidation must coalesce with the NEW
                // request — same tracked promise, no third fetch.
                const p3 = window.revalidateFoldersForOnline();
                const coalesced = (p3 === p2);
                // Resolve the stale request while the new one is still held.
                window.__rv.gates[0]();
                await p1;
                await tick();
                const afterStale = {
                    tree: D.getTree().map(n => n.name),
                    etag: D.etagByKey.get(':full'),
                };
                // New request completes (304 → freshness stamp only).
                window.__rv.gates[1]();
                await p2;
                // The NEW request's stamp now gates an immediate revalidate —
                // no further fetch.
                await window.revalidateFoldersForOnline();
                return {reqsAfter1, reqsAfter2, coalesced,
                        etag0, afterStale, reqs: window.__rv.reqs};
            }"""
        )
        assert out["reqsAfter1"] == 1, out
        assert out["reqsAfter2"] == 2, (
            f"reconnect must start a new request while the old is held: {out['reqs']}"
        )
        assert out["coalesced"] is True, (
            "concurrent revalidate must return the in-flight new request"
        )
        assert len(out["reqs"]) == 2, (
            f"coalescing must prevent a third fetch: {out['reqs']}"
        )
        assert "STALEMARK" not in out["afterStale"]["tree"], (
            f"stale response must not replace the tree: {out['afterStale']}"
        )
        assert out["afterStale"]["etag"] == out["etag0"], (
            f"stale ETag must not overwrite the warm tag: {out['afterStale']}"
        )
    finally:
        context.close()


def test_offline_mid_revalidation_index_cannot_publish(browser, server):
    """Going offline while the revalidation's async index is held kills that
    build through the connection predicate — the shared index generation is
    NOT bumped (a foreign in-progress build, standing in for an active full
    load's, still publishes across the same transition). The stale map never
    reaches _nodeMapCache/tree/ETag, and the reconnect fetches afresh."""
    stale_json = (
        '[{"name":"STALEMARK","type":"folder","children":['
        + ",".join(
            f'{{"name":"s{i}.png","type":"image"}}' for i in range(600)
        )
        + "]}]"
    )
    recon_json = (
        '[{"name":"RECONMARK","type":"folder",'
        '"children":[{"name":"r.png","type":"image"}]}]'
    )
    context, page = _new_page(browser)
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        _stop_fill_poll(page)
        page.evaluate(GATE_REVALIDATE_JS)
        page.evaluate(
            """([staleJson, reconJson]) => {
                window.__rv.plans[0] = () => new Response(staleJson, {
                    status: 200,
                    headers: {'Content-Type': 'application/json',
                              'ETag': '"STALETAG"'}});
                window.__rv.plans[1] = () => new Response(reconJson, {
                    status: 200,
                    headers: {'Content-Type': 'application/json',
                              'ETag': '"NEWTAG"'}});
            }""",
            [stale_json, recon_json],
        )
        page.evaluate(HOLD_INDEX_JS)
        base = page.evaluate(
            """() => ({
                tree: window.__BOOKQ_DEBUG.getTree().map(n => n.name),
                etag: window.__BOOKQ_DEBUG.etagByKey.get(':full'),
            })"""
        )
        # Phase 1: a foreign async index build (no isCurrent predicate — the
        # same shape a full load's build has) is held across an offline flip.
        page.evaluate(
            """() => {
                const mt = [{name:'manualmarker', type:'folder',
                    children: Array.from({length: 600}, (_, i) =>
                        ({name:'m'+i+'.png', type:'image'}))}];
                window.__pManual = window.setFullPathsAndIndexAsync(mt);
            }"""
        )
        page.wait_for_function("() => window.__heldIdx.length >= 1", timeout=8000)
        # Start the revalidation on this connection — it parks on the held
        # conditional fetch while the manual build is still paused.
        page.evaluate(
            "() => { window.dispatchEvent(new Event('offline'));"
            "        window.dispatchEvent(new Event('online'));"
            "        window.__pRv = window.revalidateFoldersForOnline(); }"
        )
        out1 = page.evaluate(
            """async () => {
                const q = window.__heldIdx.splice(0);
                for (const m of q) window.__origPM.apply(m.port, m.args);
                const map = await window.__pManual;
                return {published: !!map};
            }"""
        )
        assert out1["published"], (
            "'offline' must not bump the shared index gen — the foreign "
            "build that was held across the transition still publishes"
        )
        # Phase 2: the revalidation's fetch resolves; its index build (latest
        # generation, so indexGen alone cannot kill it) is held mid-yield.
        page.wait_for_function("() => window.__rv.reqs.length === 1", timeout=8000)
        page.evaluate("() => window.__rv.gates[0]()")
        page.wait_for_function("() => window.__heldIdx.length >= 1", timeout=8000)
        page.evaluate("() => window.dispatchEvent(new Event('offline'))")
        out2 = page.evaluate(
            """async () => {
                const q = window.__heldIdx.splice(0);
                MessagePort.prototype.postMessage = window.__origPM;
                for (const m of q) window.__origPM.apply(m.port, m.args);
                await window.__pRv;
                await new Promise(r => setTimeout(r, 30));
                const D = window.__BOOKQ_DEBUG;
                const idxName = (window.__cc_thumbIndex
                    && window.__cc_thumbIndex.tree
                    && window.__cc_thumbIndex.tree[0]
                    && window.__cc_thumbIndex.tree[0].name) || null;
                return {tree: D.getTree().map(n => n.name),
                        etag: D.etagByKey.get(':full'),
                        idxName};
            }"""
        )
        assert "STALEMARK" not in out2["tree"], (
            f"stale revalidation must not replace the tree: {out2}"
        )
        assert out2["idxName"] != "STALEMARK", (
            f"stale index build must never publish its map: {out2}"
        )
        assert out2["etag"] == base["etag"], (
            f"stale response must not write the ETag slot: {out2}"
        )
        assert out2["tree"] == base["tree"], (
            f"stale response must not mutate the tree: {out2}"
        )
        # Reconnect: a fresh conditional request completes and publishes.
        page.evaluate(
            "() => { window.dispatchEvent(new Event('online'));"
            "        window.__pRv2 = window.revalidateFoldersForOnline(); }"
        )
        page.wait_for_function("() => window.__rv.reqs.length === 2", timeout=8000)
        page.evaluate("() => window.__rv.gates[1]()")
        page.wait_for_function(
            "() => window.__BOOKQ_DEBUG.getTree()[0].name === 'RECONMARK'",
            timeout=8000,
        )
        reqs = page.evaluate("() => window.__rv.reqs")
        assert reqs[1]["inm"], f"reconnect request must be conditional: {reqs}"
    finally:
        context.close()


def test_invalid_writer_inputs_do_not_cancel_pending_deferred(browser, server):
    """putTree(non-array)/putJson(empty|non-string) are resolved no-ops taken
    BEFORE latest-wins cancellation: a pending valid deferred write must
    survive them untouched and still land once idle flushes."""
    context, page = _new_page(browser)
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        _stop_fill_poll(page)
        out = page.evaluate(
            """async () => {
                const held = [];
                const origRIC = window.requestIdleCallback;
                window.requestIdleCallback = (fn) => {
                    held.push(fn);
                    return held.length;
                };
                try {
                    await window.OfflineTree.delTree();
                    held.length = 0;
                    const baseHeld = held.length;
                    const p = window.OfflineTree.putJson(
                        JSON.stringify([{name:'VALID', type:'folder',
                            children:[]}]),
                        {defer: true});
                    // Every malformed input below must resolve as a no-op
                    // WITHOUT cancelling the pending valid write. (Non-empty
                    // strings intentionally still reach the worker/fallback —
                    // no synchronous JSON.parse probe of the raw payload.)
                    await Promise.all([
                        window.OfflineTree.putTree(null),
                        window.OfflineTree.putTree({}),
                        window.OfflineTree.putTree('not-a-tree'),
                        window.OfflineTree.putTree(7),
                        window.OfflineTree.putJson(''),
                        window.OfflineTree.putJson(null),
                        window.OfflineTree.putJson(42),
                    ]);
                    await new Promise(r => setTimeout(r, 60));
                    // Other modules may legitimately enqueue their own idle
                    // work — only the deferred write's callback must remain.
                    const stillQueued = held.length - baseHeld;
                    let settled = false;
                    p.then(() => { settled = true; }, () => { settled = true; });
                    await new Promise(r => setTimeout(r, 30));
                    const settledEarly = settled;
                    held.splice(0).forEach(f => f());
                    await p;
                    const t = await window.OfflineTree.getTree();
                    return {stillQueued, settledEarly,
                            names: t && t.map(n => n.name)};
                } finally { window.requestIdleCallback = origRIC; }
            }"""
        )
        assert out["stillQueued"] >= 1, (
            f"invalid inputs must not cancel the pending deferred write: {out}"
        )
        assert out["settledEarly"] is False, (
            f"the valid write must stay pending until idle runs: {out}"
        )
        assert out["names"] == ["VALID"], out
    finally:
        context.close()


def test_putjson_large_worker_roundtrip_no_main_parse(browser, server):
    """Synthetic large-snapshot proof: the raw JSON text must go through the
    Worker path (never the main-thread JSON.parse) and still land in IDB."""
    context, page = _new_page(browser)
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        _stop_fill_poll(page)
        out = page.evaluate(
            """async () => {
                await window.OfflineTree.delTree();
                const tree = [{name:'large', type:'folder',
                    children: Array.from({length: 176000}, (_, i) =>
                        ({name:'page-'+i+'.png', type:'image'}))}];
                const raw = JSON.stringify(tree);
                const origParse = JSON.parse;
                const OrigWorker = window.Worker;
                let mainParses = 0, workers = 0;
                JSON.parse = function (s) {
                    if (s === raw) mainParses++;
                    return origParse.apply(JSON, arguments);
                };
                window.Worker = class extends OrigWorker {
                    constructor(...a){ workers++; super(...a); }
                };
                try {
                    await window.OfflineTree.putJson(raw);
                } finally {
                    JSON.parse = origParse;
                    window.Worker = OrigWorker;
                }
                const t = await window.OfflineTree.getTree();
                return {rawLen: raw.length, mainParses, workers,
                        kids: t && t[0] && t[0].children.length};
            }"""
        )
        assert out["rawLen"] >= 3800000, out
        assert out["workers"] >= 1, (
            f"the large write must use the Worker path: {out}"
        )
        assert out["mainParses"] == 0, (
            f"raw payload must never hit main-thread JSON.parse: {out}"
        )
        assert out["kids"] == 176000, out
    finally:
        context.close()


def test_putjson_worker_timeout_falls_back_once(browser, server):
    """Controlled 10s timeout: the captured timeout callback is invoked
    manually — no real 10s sleep. It must terminate the hung worker and run
    exactly one fallback persist, and a stale job's timer must not terminate
    or write the newer job."""
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    page.add_init_script(
        """window.__fakeWorkers = [];
        window.__treeTimers = [];
        const __origST = window.setTimeout;
        window.setTimeout = function (fn, ms) {
            if (ms === 10000) {
                window.__treeTimers.push(fn);
                return -900 - window.__treeTimers.length;
            }
            return __origST.apply(window, arguments);
        };
        window.Worker = class {
            constructor(){
                this.onmessage = null; this.onerror = null;
                this.terminated = false; window.__fakeWorkers.push(this);
            }
            postMessage(m){ this.lastMsg = m; }
            terminate(){ this.terminated = true; }
        };"""
    )
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        _stop_fill_poll(page)
        out = page.evaluate(
            """async () => {
                await window.OfflineTree.delTree();
                const mk = n => JSON.stringify(
                    [{name:n, type:'folder', children:[]}]);
                const base = window.__fakeWorkers.length;
                const tBase = window.__treeTimers.length;
                // Job A: worker never answers → parks on its 10s timer.
                const pA = window.OfflineTree.putJson(mk('JOB_A'))
                    .then(() => true, () => false);
                const wA = window.__fakeWorkers[base];
                // Job B supersedes: A is terminated + settled, B parks on its
                // own timer.
                const pB = window.OfflineTree.putJson(mk('JOB_B'))
                    .then(() => true, () => false);
                const wB = window.__fakeWorkers[base + 1];
                const aTerminatedEarly = wA.terminated === true;
                const paEarly = await pA;
                // Stale timer A fires — must be a no-op: cannot terminate
                // worker B or write a stale fallback.
                if (window.__treeTimers[tBase]) window.__treeTimers[tBase]();
                await new Promise(r => setTimeout(r, 60));
                const bAliveAfterStale = !wB.terminated;
                const t1 = await window.OfflineTree.getTree();
                // Timer B fires → terminate B + exactly one fallback persist.
                if (window.__treeTimers[tBase + 1]) {
                    window.__treeTimers[tBase + 1]();
                }
                const pbOk = await pB;
                const bTerminated = wB.terminated === true;
                const t2 = await window.OfflineTree.getTree();
                return {aTerminatedEarly, paEarly, bAliveAfterStale,
                        t1: t1 && t1.map(n => n.name),
                        pbOk, bTerminated,
                        t2: t2 && t2.map(n => n.name),
                        workers: window.__fakeWorkers.length - base};
            }"""
        )
        assert out["aTerminatedEarly"], (
            f"superseding write must terminate worker A: {out}"
        )
        assert out["paEarly"] is True, (
            f"canceled job A must resolve, not reject: {out}"
        )
        assert out["bAliveAfterStale"], (
            f"stale timer A must not terminate worker B: {out}"
        )
        assert out["t1"] is None, (
            f"stale timer A must not persist a fallback: {out}"
        )
        assert out["workers"] == 2, (
            f"no extra worker may spawn for the fallback: {out}"
        )
        assert out["bTerminated"], f"timer B must terminate worker B: {out}"
        assert out["pbOk"] is True, out
        assert out["t2"] == ["JOB_B"], (
            f"exactly one fallback persist, of the newest job: {out}"
        )
    finally:
        context.close()


def test_stale_body_and_reject_never_warn_or_fall_through(browser, server):
    """Superseded loads must not warn 'Malformed folders response'/'falling
    through to full' nor issue their stale full-stage request — a delayed
    stale response.body and a stale rejected shallow fetch both bail on the
    generation guard. A CURRENT malformed response still warns and falls
    through (the resilience path is preserved)."""
    context, page = _new_page(browser)
    try:
        page.goto(server + "/")
        page.wait_for_function("() => window.treeLoaded === true", timeout=15000)
        _stop_fill_poll(page)
        out = page.evaluate(
            """async () => {
                const warns = [];
                const origWarn = console.warn;
                console.warn = function(...a){
                    warns.push(a.map(String).join(' '));
                    return origWarn.apply(console, a);
                };
                const tB = [{name:'LOADB', type:'folder',
                    children:[{name:'b.png', type:'image'}]}];
                const mkRes = data => ({
                    ok: true, status: 200,
                    headers: {get: () => null},
                    text: async () => JSON.stringify(data),
                    json: async () => data,
                });
                const fetches = [];
                const isShallow = u => u.indexOf('depth=1') >= 0;
                const isFull = u => u === '/api/v1/tree';
                const tick = (ms) => new Promise(r => setTimeout(r, ms));
                try {
                    // (1) Delayed stale response.body: load A's shallow text()
                    // parks until load B already owns the generation, then
                    // arrives malformed — the staleness gate must swallow it.
                    let releaseText;
                    const shallowA = {
                        ok: true, status: 200, headers: {get: () => null},
                        text: () => new Promise(r => {
                            releaseText = () => r('not-json!!');
                        }),
                    };
                    const fA = u => {
                        fetches.push('A:' + u);
                        if (isShallow(u)) return Promise.resolve(shallowA);
                        return Promise.resolve(mkRes(
                            [{name:'ASTALE', type:'folder', children:[]}]));
                    };
                    const pA = window.loadFolders({}, {fetch: fA});
                    await tick(60);
                    const pB = window.loadFolders({}, {fetch: u => {
                        fetches.push('B:' + u);
                        return Promise.resolve(mkRes(tB));
                    }});
                    releaseText();
                    await pA; await pB;
                    // (2) Stale rejected shallow fetch: same guard, through
                    // the catch arm.
                    let rejectShallow;
                    const fC = u => {
                        fetches.push('C:' + u);
                        if (isShallow(u))
                            return new Promise((res, rej) => {
                                rejectShallow = rej;
                            });
                        return Promise.resolve(mkRes(
                            [{name:'CSTALE', type:'folder', children:[]}]));
                    };
                    const pC = window.loadFolders({}, {fetch: fC});
                    await tick(30);
                    const pD = window.loadFolders({}, {fetch: u => {
                        fetches.push('D:' + u);
                        return Promise.resolve(mkRes(tB));
                    }});
                    rejectShallow(new Error('stale net drop'));
                    await pC; await pD;
                    const staleWarns = warns.slice();
                    // (3) A CURRENT malformed shallow response still warns and
                    // falls through to the full fetch — resilience preserved.
                    const fE = u => {
                        fetches.push('E:' + u);
                        if (isShallow(u)) return Promise.resolve({
                            ok: true, status: 200,
                            headers: {get: () => null},
                            text: async () => 'still-not-json',
                        });
                        return Promise.resolve(mkRes(tB));
                    };
                    await window.loadFolders({}, {fetch: fE});
                    const warnsE = warns.slice(staleWarns.length);
                    return {staleWarns, warnsE, fetches,
                            tree: window.__BOOKQ_DEBUG.getTree()
                                .map(n => n.name)};
                } finally { console.warn = origWarn; }
            }"""
        )
        assert out["tree"] == ["LOADB"], out
        bad = [
            w for w in out["staleWarns"]
            if "Malformed" in w or "falling through" in w
        ]
        assert not bad, (
            f"stale responses must not warn or fall through: {bad}"
        )
        stale_fulls = [
            f for f in out["fetches"]
            if f.startswith(("A:", "C:")) and "depth=1" not in f
        ]
        assert not stale_fulls, (
            f"superseded loads must not issue a full fetch: {stale_fulls}"
        )
        real_warn = [
            w for w in out["warnsE"]
            if "Malformed" in w or "falling through" in w
        ]
        assert real_warn, (
            "a current malformed shallow response must still warn and fall "
            f"through to the full fetch: {out['warnsE']}"
        )
        e_fulls = [
            f for f in out["fetches"]
            if f.startswith("E:") and "depth=1" not in f
        ]
        assert len(e_fulls) == 1, (
            f"current malformed shallow must fall through to one full fetch: "
            f"{out['fetches']}"
        )
    finally:
        context.close()
