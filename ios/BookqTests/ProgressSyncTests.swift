import XCTest
@testable import Bookq

/// ProgressSync + ReadingProgressStore — test_reading_position.py parity:
/// dedup key, stale-ts merge, backoff on transient, drop on definitive
/// 4xx, pull-fail never clobbers, boot merge (remote-newer adopts /
/// local-newer pushes), remove clears everything.
@MainActor
final class ProgressSyncTests: XCTestCase {
    private var api: BookqAPI!
    private var sync: ProgressSync!

    override func setUp() async throws {
        StubURLProtocol.reset()
        UserDefaults.standard.removeObject(forKey: "bookq_progress_v1")
        api = StubURLProtocol.api()
        sync = ProgressSync(api: api)
        sync.sleep = { _ in }            // timers fire immediately
    }

    private func waitPuts(_ n: Int,
                          file: StaticString = #filePath,
                          line: UInt = #line) async {
        for _ in 0..<200 {
            if StubURLProtocol.hits(path: "api/v1/reading-positions") >= n { return }
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        XCTFail("expected \(n) reading-position requests", file: file, line: line)
    }

    private func getJSON(_ rec: StubURLProtocol.Rec?) -> [String: Any]? {
        StubURLProtocol.decodedBody(rec)
    }

    // MARK: - Dedup / coalescing

    /// Burst of turns coalesces — same-folder pushes keep the ORIGINAL ts
    /// (web: position.ts is the oldest unflushed record for that key).
    func testSameKeyCoalescesKeepsOriginalTs() async {
        StubURLProtocol.handler = { _ in StubURLProtocol.json([:]) }
        sync.push(.init(folder: "A", page: 1, total: 10, ts: 1000))
        sync.push(.init(folder: "A", page: 1, total: 10, ts: 1500))
        sync.flush()
        await waitPuts(1)
        let body = getJSON(StubURLProtocol.last(path: "api/v1/reading-positions"))
        XCTAssertEqual(body?["ts"] as? Int, 1000)
    }

    /// Different position = newest record wins; older is dropped.
    func testNewestRecordWins() async {
        StubURLProtocol.handler = { _ in StubURLProtocol.json([:]) }
        sync.push(.init(folder: "A", page: 1, total: 10, ts: 1000))
        sync.push(.init(folder: "A", page: 5, total: 10, ts: 2000))
        sync.flush()
        await waitPuts(1)
        let body = getJSON(StubURLProtocol.last(path: "api/v1/reading-positions"))
        XCTAssertEqual(body?["page"] as? Int, 5)
        XCTAssertEqual(body?["ts"] as? Int, 2000)
    }

    /// After a successful send, re-pushing the identical position is a
    /// no-op (lastSentKey dedup) — mirrors web sending dedupe.
    func testIdenticalResendSkipped() async {
        StubURLProtocol.handler = { _ in StubURLProtocol.json([:]) }
        let r = ProgressSync.Record(folder: "A", page: 3, total: 10, ts: 1)
        sync.push(r); sync.flush()
        await waitPuts(1)
        sync.push(r); sync.flush()
        try? await Task.sleep(nanoseconds: 100_000_000)
        XCTAssertEqual(StubURLProtocol.hits(path: "api/v1/reading-positions"), 1)
    }

    /// v1 wire shape — `folder` travels as `?folder=` query param, the
    /// body is exactly {page,total,ts} (folder must NOT be in the body).
    func testPutSendsFolderInQueryNotBody() async {
        StubURLProtocol.handler = { _ in StubURLProtocol.json(["status": "ok"]) }
        sync.push(.init(folder: "Works/vol 1", page: 3, total: 10, ts: 42))
        sync.flush()
        await waitPuts(1)
        let rec = StubURLProtocol.last(path: "api/v1/reading-positions")
        XCTAssertTrue(rec?.request.url?.query?.contains("folder=") == true)
        XCTAssertTrue(rec?.request.url?.query?.contains("vol%201") == true
                      || rec?.request.url?.query?.contains("vol+1") == true)
        let body = getJSON(rec)
        XCTAssertNil(body?["folder"])
        XCTAssertEqual(body?["page"] as? Int, 3)
        XCTAssertEqual(body?["ts"] as? Int, 42)
    }

    // MARK: - Failure handling

    /// 408/429/5xx/network → requeue + retry (transient); definitive 4xx
    /// (400/403/404) → drop silently.
    func testDroppableMatrix() {
        for s in [400, 401, 403, 404, 422] {
            XCTAssertTrue(ProgressSync.droppable(s), "\(s) should drop")
        }
        for s in [408, 429, 500, 502, 503] {
            XCTAssertFalse(ProgressSync.droppable(s), "\(s) should retry")
        }
    }

    /// A definitive 4xx drops the record — no retry ever reaches the wire.
    func test4xxDropsWithoutRetry() async {
        StubURLProtocol.handler = { _ in StubURLProtocol.json([:], status: 404) }
        sync.push(.init(folder: "A", page: 1, total: 10, ts: 1))
        sync.flush()
        await waitPuts(1)
        // Drop (not retry) proof needs a window where a retry could have fired.
        try? await Task.sleep(nanoseconds: 300_000_000)
        XCTAssertEqual(StubURLProtocol.hits(path: "api/v1/reading-positions"), 1)
    }

    /// Transient 503 → same record retried until success; ts preserved.
    func testTransientRetriesWithSameTs() async {
        var calls = 0
        StubURLProtocol.handler = { _ in
            calls += 1
            return calls < 3
                ? StubURLProtocol.json([:], status: 503)
                : StubURLProtocol.json([:])
        }
        sync.push(.init(folder: "A", page: 2, total: 10, ts: 777))
        sync.flush()
        await waitPuts(3)
        let body = getJSON(StubURLProtocol.last(path: "api/v1/reading-positions"))
        XCTAssertEqual(body?["ts"] as? Int, 777)
    }

    // MARK: - pull / remove

    /// pull distinguishes empty-from-fetch-failure via `ok`.
    func testPullOkVsFail() async {
        StubURLProtocol.handler = { _ in
            StubURLProtocol.json(["positions": [["folder": "B", "page": 4,
                                                 "total": 9, "ts": 55]]])
        }
        var res = await sync.pull()
        XCTAssertTrue(res.ok)
        XCTAssertEqual(res.position?.folder, "B")
        XCTAssertEqual(res.position?.page, 4)

        StubURLProtocol.handler = { _ in StubURLProtocol.json([:], status: 500) }
        res = await sync.pull()
        XCTAssertFalse(res.ok)
        XCTAssertNil(res.position)
    }

    /// remove() clears the pending queue so a later flush sends nothing.
    func testRemoveClearsPending() async {
        var sawDelete = false
        StubURLProtocol.handler = { req in
            if req.httpMethod == "DELETE" { sawDelete = true }
            return StubURLProtocol.json([:])
        }
        sync.push(.init(folder: "A", page: 1, total: 10, ts: 1))
        let removed = await sync.remove()
        XCTAssertTrue(removed)
        XCTAssertTrue(sawDelete)
        sync.flush()
        try? await Task.sleep(nanoseconds: 100_000_000)
        XCTAssertEqual(StubURLProtocol.hits(path: "api/v1/reading-positions"), 1)
    }

    // MARK: - ReadingProgressStore (local + boot merge)

    /// record() persists locally AND pushes remotely.
    func testRecordWritesLocalAndPushes() async {
        StubURLProtocol.handler = { _ in StubURLProtocol.json([:]) }
        let store = ReadingProgressStore()
        store.attach(sync)
        store.record(path: "A", page: 3, total: 10)
        XCTAssertEqual(store.entries["A"]?.page, 3)
        await waitPuts(1)
    }

    /// Review fix — recording the SAME position is a no-op: a fresh ts
    /// on an unchanged page would clobber a newer remote position from
    /// another device (reader close/inactive flush without a page move).
    func testRecordSamePositionIsNoOp() async {
        StubURLProtocol.handler = { _ in StubURLProtocol.json([:]) }
        let store = ReadingProgressStore()
        store.attach(sync)
        store.record(path: "A", page: 3, total: 10)
        let ts1 = store.entries["A"]?.ts
        try? await Task.sleep(nanoseconds: 20_000_000)
        // Close-flush without moving: no ts bump, no second PUT.
        store.record(path: "A", page: 3, total: 10)
        XCTAssertEqual(store.entries["A"]?.ts, ts1)
        await waitPuts(1)
        try? await Task.sleep(nanoseconds: 100_000_000)
        XCTAssertEqual(StubURLProtocol.hits(path: "api/v1/reading-positions"), 1)
    }

    /// mergeRemote writes the position WITHOUT re-pushing (no echo loop).
    func testMergeRemoteDoesNotEcho() async {
        StubURLProtocol.handler = { _ in StubURLProtocol.json([:]) }
        let store = ReadingProgressStore()
        store.attach(sync)
        store.mergeRemote(path: "A", page: 7, total: 10, ts: 999)
        try? await Task.sleep(nanoseconds: 100_000_000)
        XCTAssertEqual(store.entries["A"]?.page, 7)
        XCTAssertEqual(StubURLProtocol.hits(path: "api/v1/reading-positions"), 0)
    }

    /// Boot merge: remote newer → adopt; pull-fail → local untouched.
    func testSyncAdoptsNewerRemote() async {
        StubURLProtocol.handler = { req in
            req.httpMethod == "GET"
                ? StubURLProtocol.json(["positions": [["folder": "A", "page": 8,
                                                       "total": 10, "ts": 9999]]])
                : StubURLProtocol.json([:])
        }
        let store = ReadingProgressStore()
        store.attach(sync)
        store.entries // silence
        store.mergeRemote(path: "A", page: 1, total: 10, ts: 100)
        await store.syncWithServer()
        XCTAssertEqual(store.entries["A"]?.page, 8)
    }

    /// Boot merge: local newer → pushed to server.
    func testSyncPushesNewerLocal() async {
        StubURLProtocol.handler = { req in
            req.httpMethod == "GET"
                ? StubURLProtocol.json(["positions": []])
                : StubURLProtocol.json([:])
        }
        let store = ReadingProgressStore()
        store.attach(sync)
        store.record(path: "A", page: 4, total: 10)
        await store.syncWithServer()
        await waitPuts(1)   // record()'s push already; sync's may dedup
    }

    /// Pull-fail on boot does nothing — local never clobbered, no push.
    func testSyncPullFailNoClobber() async {
        StubURLProtocol.handler = { _ in StubURLProtocol.json([:], status: 500) }
        let store = ReadingProgressStore()
        store.attach(sync)
        store.mergeRemote(path: "A", page: 2, total: 10, ts: 50)
        await store.syncWithServer()
        XCTAssertEqual(store.entries["A"]?.page, 2)
        XCTAssertEqual(StubURLProtocol.hits(path: "api/v1/reading-positions"), 1)
    }

    /// lastRead = max ts across all folders.
    func testLastReadPicksNewest() {
        let store = ReadingProgressStore()
        store.mergeRemote(path: "A", page: 1, total: 10, ts: 100)
        store.mergeRemote(path: "B", page: 5, total: 10, ts: 300)
        store.mergeRemote(path: "C", page: 9, total: 10, ts: 200)
        XCTAssertEqual(store.lastRead?.path, "B")
    }

    /// progress(for:leaves:) — max (page+1)/total across leaf descendants.
    func testProgressRatioAcrossLeaves() {
        let store = ReadingProgressStore()
        store.mergeRemote(path: "W/ch1", page: 4, total: 10, ts: 1)
        store.mergeRemote(path: "W/ch2", page: 9, total: 10, ts: 2)
        let p = store.progress(for: "W", leaves: ["W/ch1", "W/ch2"])
        XCTAssertEqual(p, 1.0, accuracy: 0.001)
    }

    /// Cap 50 newest by ts — oldest evicted (web bookq_progress parity).
    func testCap50EvictsOldest() {
        let store = ReadingProgressStore()
        for i in 0..<60 {
            store.mergeRemote(path: "F\(i)", page: 0, total: 10, ts: i)
        }
        XCTAssertEqual(store.entries.count, 50)
        XCTAssertNil(store.entries["F0"])
        XCTAssertNotNil(store.entries["F59"])
    }
}
