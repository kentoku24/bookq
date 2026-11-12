import XCTest
import SwiftUI
@testable import Bookq

/// Canonical-JSON + manifest-digest golden tests — the fixture is read from
/// tests/fixtures/reader_sync_cases.json `manifestDigestCases` (#280), the
/// same file the Python side pins in test_conformance_vectors.py. The digest
/// is never hand-copied here: any drift between the two languages fails.
final class CanonicalJSONTests: XCTestCase {

    /// `ios/BookqTests/PairSyncTests.swift` → repo root is ../..
    static func repoRoot() -> URL {
        let thisFile = URL(fileURLWithPath: #filePath)
        return thisFile
            .deletingLastPathComponent()   // BookqTests/
            .deletingLastPathComponent()   // ios/
            .deletingLastPathComponent()   // worktree root
    }

    /// (manifest content + initial, expected digest) from the shared fixture.
    static func digestCase() -> (manifest: [String: Any], digest: String) {
        let url = repoRoot()
            .appendingPathComponent("tests/fixtures/reader_sync_cases.json")
        let data = try! Data(contentsOf: url)
        let fixture = try! JSONSerialization.jsonObject(with: data) as! [String: Any]
        let cases = fixture["manifestDigestCases"] as! [[String: Any]]
        let first = cases[0]
        return (first["manifest"] as! [String: Any],
                first["expectDigest"] as! String)
    }

    /// Python: {"b": 1, "a": true, "u": "作品"}
    /// → {"a":true,"b":1,"u":"作品"} — sorted, compact, ASCII-escaped.
    func testCanonicalEncoding() {
        let s = CanonicalJSON.encode(["b": 1, "a": true, "u": "作品"])
        XCTAssertEqual(s, "{\"a\":true,\"b\":1,\"u\":\"\\u4f5c\\u54c1\"}")
    }

    /// NSNumber bridging must NOT collapse 1 → true / false → 0.
    /// (JSONSerialization decodes all JSON scalars as NSNumber.)
    func testBoolVersusNumberRoundTrip() throws {
        let src = "{\"a\":true,\"b\":1,\"c\":0,\"d\":false}"
        let obj = try JSONSerialization.jsonObject(
            with: Data(src.utf8)) as! [String: Any]
        XCTAssertEqual(CanonicalJSON.encode(obj), src)
    }

    /// Golden vector from the shared fixture — content keys only
    /// (v/folder/offset/chapters/pageRefs/spreadTable).
    func testManifestDigestGolden() {
        let (manifest, digest) = Self.digestCase()
        XCTAssertEqual(CanonicalJSON.manifestDigest(manifest), digest)
    }

    /// The digest must change when content changes (typo/classification).
    func testManifestDigestSensitive() {
        let (manifest, digest) = Self.digestCase()
        var m = manifest
        m["offset"] = 0
        XCTAssertNotEqual(CanonicalJSON.manifestDigest(m), digest)
    }

    static var manifestFixture: [String: Any] {
        var (manifest, digest) = digestCase()
        manifest["digest"] = digest
        return manifest
    }
}

/// PairManifest.parse — wire-dict → typed manifest.
final class PairManifestTests: XCTestCase {
    func testParse() {
        let m = PairManifest.parse(CanonicalJSONTests.manifestFixture)
        XCTAssertNotNil(m)
        XCTAssertEqual(m?.spreadTable.count, 2)
        XCTAssertEqual(m?.spreadIndex(byId: "s1"), 1)
        XCTAssertNil(m?.spreadTable[1].left)   // trailing blank side
    }

    func testParseRejectsMalformed() {
        var bad = CanonicalJSONTests.manifestFixture
        bad["spreadTable"] = [["id": 42]]
        XCTAssertNil(PairManifest.parse(bad))
        bad = CanonicalJSONTests.manifestFixture
        bad.removeValue(forKey: "chapters")
        XCTAssertNil(PairManifest.parse(bad))
    }
}

/// Scripted PairTransporting — records outbound frames, replays inbound
/// wire text on demand. connect() yields `.open` immediately.
actor FakeTransport: PairTransporting {
    private var cont: AsyncStream<PairSocket.Event>.Continuation?
    private var sentRaw: [String] = []
    /// Emits that arrive before connect() — buffered and flushed when a
    /// link exists, so test-side races cannot drop frames.
    private var queuedEmits: [String] = []
    /// Number of connect() calls — reconnect tests wait for the fresh
    /// link before emitting, or frames land on the finished stream.
    private(set) var connectCount = 0
    /// Index into sentRaw at which closeSocket() ran — ordering tests
    /// assert LEAVE landed before the close (nil = still open).
    private(set) var closedAfterSendCount: Int?
    private(set) var closeCount = 0

    func connect(url: URL, token: String?) -> AsyncStream<PairSocket.Event> {
        connectCount += 1
        let (s, c) = AsyncStream.makeStream(of: PairSocket.Event.self)
        cont = c
        c.yield(.open)
        for text in queuedEmits { c.yield(.frame(text)) }
        queuedEmits.removeAll()
        return s
    }
    func send(_ text: String) async throws { sentRaw.append(text) }
    func closeSocket() {
        closeCount += 1
        closedAfterSendCount = sentRaw.count
        cont?.yield(.closed(code: 1000, reason: "closed"))
        cont?.finish()
    }

    /// Raw outbound wire text — tests decode on their own actor so the
    /// stream stays Sendable.
    func sent() -> [String] { sentRaw }
    func emit(_ wireText: String) {
        if let cont { cont.yield(.frame(wireText)) }
        else { queuedEmits.append(wireText) }
    }
    func drop() { cont?.yield(.failed("link drop")) }
}

/// PairStore protocol/state-machine tests — FakeTransport replays the
/// server frame stream; assertions check wire output + published state.
@MainActor
final class PairStoreTests: XCTestCase {
    private var fake: FakeTransport!
    private var store: PairStore!
    private var key: String!

    override func setUp() async throws {
        fake = FakeTransport()
        key = "test.pair.\(UUID().uuidString)"
        store = PairStore(
            url: { URL(string: "ws://test/ws/reader-sync") },
            token: { "tok" },
            transportFactory: { self.fake },
            persistKey: { self.key })
    }

    override func tearDown() async throws {
        UserDefaults.standard.removeObject(forKey: key)
    }

    /// Poll until the fake has ≥ n sent frames (or time out = fail).
    private func waitForSent(_ n: Int,
                             file: StaticString = #filePath,
                             line: UInt = #line) async -> [[String: Any]] {
        for _ in 0..<100 {
            let s = await fake.sent()
            if s.count >= n { return decoded(s) }
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        XCTFail("expected \(n) sent frames", file: file, line: line)
        return decoded(await fake.sent())
    }

    /// Encode a wire dict on this actor, then hand the Sendable String
    /// to the fake transport.
    private func wire(_ obj: [String: Any]) -> String {
        let d = try! JSONSerialization.data(withJSONObject: obj)
        return String(decoding: d, as: UTF8.self)
    }
    /// Decode sent raw frames back to dicts for assertion.
    private func decoded(_ texts: [String]) -> [[String: Any]] {
        texts.compactMap {
            (try? JSONSerialization.jsonObject(with: Data($0.utf8)))
            as? [String: Any]
        }
    }
    private func hello() async { await fake.emit(wire(["v": 1, "type": "HELLO"])) }

    private func ack(_ rid: String, request: String,
                     data: [String: Any]) async {
        await fake.emit(wire(["v": 1, "type": "ACK", "requestId": rid,
                              "request": request, "data": data]))
    }

    private let bindData: [String: Any] = [
        "sessionId": "s1", "memberId": "m1", "role": "leader",
        "resumeToken": "rt1", "code": "123456",
    ]

    private func state(rev: Int, epoch: Int = 0, mv: Int = 0,
                       phase: String = "waiting",
                       spreadId: String? = nil) -> [String: Any] {
        var f: [String: Any] = [
            "v": 1, "type": "STATE", "sessionId": "s1",
            "revision": rev, "workEpoch": epoch,
            "manifestVersion": mv, "phase": phase,
            "offset": 1, "leaderSide": "right",
            "dataPlane": "server-ws",
            "presence": [
                "m1": ["role": "leader", "status": "active",
                       "name": "iPhone", "ready": false],
                "m2": ["role": "follower", "status": "active",
                       "name": "iPad", "ready": false],
            ],
        ]
        if let spreadId { f["spreadId"] = spreadId }
        return f
    }

    // MARK: - Entry / pairing

    /// CREATE → HELLO → CREATE frame on wire → ACK binds member +
    /// persists creds + surfaces the pairing code.
    func testCreateBindsOnAck() async throws {
        let task = Task { @MainActor in try await store.create() }
        await hello()
        let sent = await waitForSent(1)
        guard let first = sent.first else { return }
        XCTAssertEqual(first["type"] as? String, "CREATE")
        XCTAssertEqual(sent[0]["v"] as? Int, 1)
        XCTAssertNotNil(sent[0]["requestId"] as? String)
        await ack(first["requestId"] as! String, request: "CREATE",
                  data: bindData)
        try await task.value
        XCTAssertEqual(store.code, "123456")
        XCTAssertEqual(store.role, .leader)
        XCTAssertTrue(store.bound)
        // Creds persisted for resume.
        XCTAssertNotNil(UserDefaults.standard.data(forKey: key))
    }

    /// JOIN request → joinRequest surfaced for leader approval;
    /// APPROVE goes on the wire with memberId.
    func testJoinRequestApproveFlow() async throws {
        let task = Task { @MainActor in try await store.create() }
        await hello()
        let sent = await waitForSent(1)
        guard let first = sent.first else { return }
        await ack(first["requestId"] as! String, request: "CREATE",
                  data: bindData)
        try await task.value

        await fake.emit(wire(["v": 1, "type": "JOIN_REQUEST",
                                    "memberId": "m2", "name": "iPad"]))
        // Auto-approve (web #114): the APPROVE goes out on its own —
        // joinRequest is transient state, so assert on the wire instead.
        let sent2 = await waitForSent(2)
        XCTAssertEqual(sent2[1]["type"] as? String, "APPROVE")
        XCTAssertEqual((sent2[1]["payload"] as? [String: Any])?["memberId"]
                       as? String, "m2")
    }

    /// REJECT with reason resolves the entry request with an error and
    /// leaves the store unbound.
    func testJoinRejected() async throws {
        let task = Task { @MainActor in try await store.join(code: "654321") }
        await hello()
        let sent = await waitForSent(1)
        guard let first = sent.first else { return }
        XCTAssertEqual(first["type"] as? String, "JOIN")
        await fake.emit(wire(["v": 1, "type": "REJECT",
                              "requestId": first["requestId"] as! String,
                              "reason": "bad_code"]))
        do {
            try await task.value
            XCTFail("expected REJECT to throw")
        } catch { /* expected */ }
        XCTAssertFalse(store.bound)
    }

    // MARK: - STATE / manifest / reading

    /// Feed the store into 'reading': CREATE+ACK, manifest chunks, STATE.
    private func enterReading() async throws {
        let task = Task { @MainActor in try await store.create() }
        await hello()
        let sent = await waitForSent(1)
        guard let first = sent.first else { return }
        await ack(first["requestId"] as! String, request: "CREATE",
                  data: bindData)
        try await task.value

        // Manifest arrives in 2 chunks (canonical bytes split).
        let manifestData = try JSONSerialization.data(
            withJSONObject: CanonicalJSONTests.manifestFixture)
        let text = String(decoding: manifestData, as: UTF8.self)
        let mid = text.index(text.startIndex, offsetBy: text.count / 2)
        let part1 = String(text[..<mid]), part2 = String(text[mid...])
        let digest = CanonicalJSONTests.manifestFixture["digest"] as! String
        for (i, part) in [part1, part2].enumerated() {
            await fake.emit(wire([
                "v": 1, "type": "MANIFEST", "kind": "manifest-chunk",
                "sessionId": "s1", "workEpoch": 1, "manifestVersion": 1,
                "digest": digest, "index": i, "count": 2, "data": part]))
        }
        // READY goes out once reassembled + digest-verified.
        let sent2 = await waitForSent(2)
        XCTAssertEqual(sent2[1]["type"] as? String, "READY")
        await fake.emit(wire(state(rev: 5, epoch: 1, mv: 1,
                                   phase: "reading", spreadId: "s0")))
        for _ in 0..<50 where store.projection == nil {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
    }

    func testManifestReassemblyAndProjection() async throws {
        try await enterReading()
        let p = try XCTUnwrap(store.projection)
        XCTAssertEqual(p.spreadIndex, 0)
        XCTAssertEqual(p.spreadTotal, 2)
        XCTAssertEqual(p.ref?.name, "p1.png")   // leader+right side → right page
        XCTAssertEqual(p.chapterFolder, "作品A/第1話")
    }

    /// TURN is a relative intent — no baseRevision requirement, carries
    /// delta/crossChapter/manifestVersion.
    func testTurnSendsRelativeIntent() async throws {
        try await enterReading()
        XCTAssertTrue(store.turn(delta: 1, crossChapter: true,
                                 fromChapter: "作品A/第1話"))
        let sent = await waitForSent(3)
        let turn = try XCTUnwrap(sent.last)
        XCTAssertEqual(turn["type"] as? String, "TURN")
        let pl = turn["payload"] as? [String: Any]
        XCTAssertEqual(pl?["delta"] as? Int, 1)
        XCTAssertEqual(pl?["crossChapter"] as? Bool, true)
        XCTAssertEqual(pl?["fromChapter"] as? String, "作品A/第1話")
        XCTAssertEqual(pl?["manifestVersion"] as? Int, 1)
    }

    /// Out-of-order / duplicate STATE must not re-drive the projection.
    func testStaleStateIgnored() async throws {
        try await enterReading()
        await fake.emit(wire(state(rev: 4, epoch: 1, mv: 1,
                                   phase: "reading", spreadId: "s1")))
        try? await Task.sleep(nanoseconds: 100_000_000)
        XCTAssertEqual(store.projection?.spreadIndex, 0)   // rev 5 wins
        await fake.emit(wire(state(rev: 6, epoch: 1, mv: 1,
                                   phase: "reading", spreadId: "s1")))
        for _ in 0..<50 where store.projection?.spreadIndex != 1 {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        XCTAssertEqual(store.projection?.spreadIndex, 1)
    }

    /// A corrupted chunk (digest mismatch) fails verification — the
    /// manifest is never applied and errorText surfaces.
    func testManifestDigestMismatchRejected() async throws {
        let task = Task { @MainActor in try await store.create() }
        await hello()
        let sent = await waitForSent(1)
        guard let first = sent.first else { return }
        await ack(first["requestId"] as! String, request: "CREATE",
                  data: bindData)
        try await task.value
        let data = try JSONSerialization.data(
            withJSONObject: CanonicalJSONTests.manifestFixture)
        await fake.emit(wire([
            "v": 1, "type": "MANIFEST", "kind": "manifest-chunk",
            "sessionId": "s1", "workEpoch": 1, "manifestVersion": 1,
            "digest": "sha256:wrong", "index": 0, "count": 1,
            "data": String(decoding: data, as: UTF8.self)]))
        for _ in 0..<50 where store.errorText == nil {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        XCTAssertEqual(store.errorText, "manifest_error")
        XCTAssertNil(store.manifest)
    }

    // MARK: - Resume / teardown

    /// Link drop after bind → RESUME attempt on reconnect (offlineSince
    /// kept), creds replayed on the wire.
    func testResumeAfterDrop() async throws {
        try await enterReading()
        await fake.drop()
        // Retry backoff must elapse and a fresh link must exist before
        // HELLO — frames emitted early die on the finished stream.
        for _ in 0..<200 where await fake.connectCount < 2 {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        // Reconnect link → HELLO → RESUME with stored creds.
        await hello()
        let sent = await waitForSent(3)
        guard let last = sent.last else { return }
        let resume = try XCTUnwrap(last)
        XCTAssertEqual(resume["type"] as? String, "RESUME")
        let pl = resume["payload"] as? [String: Any]
        XCTAssertEqual(pl?["memberId"] as? String, "m1")
        XCTAssertEqual(pl?["resumeToken"] as? String, "rt1")
    }

    /// bad_resume → session ends terminally, creds cleared.
    func testBadResumeEndsSession() async throws {
        try await enterReading()
        await fake.drop()
        for _ in 0..<200 where await fake.connectCount < 2 {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        await hello()
        let sent = await waitForSent(3)
        guard let last = sent.last else { return }
        await fake.emit(wire(["v": 1, "type": "REJECT",
                              "requestId": last["requestId"] as! String,
                              "reason": "bad_resume"]))
        for _ in 0..<50 {
            if case .ended = store.status { break }
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        guard case .ended = store.status else {
            XCTFail("expected ended"); return
        }
        XCTAssertNil(UserDefaults.standard.data(forKey: key))
    }

    /// #216: background → link drop → foreground during the reconnect must
    /// not re-assert "suspended" on RESUME. Before the fix, scenePhase
    /// guarded on `bound`, so .active during .connecting never cleared the
    /// local flag and the RESUME bind re-sent suspended — wedging both
    /// members in `paused` (verified live on sim pair).
    func testActiveDuringReconnectDoesNotReassertSuspended() async throws {
        try await enterReading()
        store.scenePhase(.background)
        let sent1 = await waitForSent(3)
        let susp = try XCTUnwrap(sent1.last)
        XCTAssertEqual(susp["type"] as? String, "SET_ACTIVITY")
        XCTAssertEqual(
            (susp["payload"] as? [String: Any])?["state"] as? String,
            "suspended")
        await fake.drop()
        for _ in 0..<200 where await fake.connectCount < 2 {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        // Foreground while the reconnect is in flight — bound==false here
        // (status==.connecting) but the flag must still clear.
        store.scenePhase(.active)
        await hello()
        let sent2 = await waitForSent(4)
        let resume = try XCTUnwrap(sent2.last)
        XCTAssertEqual(resume["type"] as? String, "RESUME")
        await ack(resume["requestId"] as! String, request: "RESUME",
                  data: bindData)
        try? await Task.sleep(nanoseconds: 200_000_000)
        let all = decoded(await fake.sent())
        let postResume = all.drop { $0["type"] as? String != "RESUME" }
            .dropFirst()
        XCTAssertFalse(postResume.contains {
            ($0["payload"] as? [String: Any])?["state"] as? String == "suspended"
        }, "RESUME must not re-assert suspended for a foregrounded app")
    }

    // MARK: - Intent matrix (test_viewer_navigation parity)

    /// Slider drag → GOTO with page ref + manifestVersion.
    func testGotoSendsIntent() async throws {
        try await enterReading()
        XCTAssertTrue(store.goto(page: PairPageRef(
            folder: "作品A/第1話", name: "p2.png")))
        let sent = await waitForSent(3)
        let g = try XCTUnwrap(sent.last)
        XCTAssertEqual(g["type"] as? String, "GOTO")
        let page = (g["payload"] as? [String: Any])?["page"] as? [String: Any]
        XCTAssertEqual(page?["folder"] as? String, "作品A/第1話")
        XCTAssertEqual(page?["name"] as? String, "p2.png")
        XCTAssertEqual((g["payload"] as? [String: Any])?["manifestVersion"] as? Int, 1)
    }

    /// Spread-shift → SET_OFFSET; swap → SWAP_SIDES flips leaderSide.
    func testSetOffsetAndSwapSides() async throws {
        try await enterReading()
        XCTAssertTrue(store.setOffset(0))
        XCTAssertTrue(store.swapSides())
        let sent = await waitForSent(4)
        let off = sent[2], swap = sent[3]
        XCTAssertEqual(off["type"] as? String, "SET_OFFSET")
        XCTAssertEqual((off["payload"] as? [String: Any])?["offset"] as? Int, 0)
        XCTAssertEqual(swap["type"] as? String, "SWAP_SIDES")
        XCTAssertEqual((swap["payload"] as? [String: Any])?["leaderSide"] as? String, "left")
    }

    /// Movement intents are gated on phase==reading — in `waiting` none
    /// of them reach the wire (web sendIntent guard).
    func testIntentsGatedOutsideReading() async throws {
        let task = Task { @MainActor in try await store.create() }
        await hello()
        let sent = await waitForSent(1)
        guard let first = sent.first else { return }
        await ack(first["requestId"] as! String, request: "CREATE", data: bindData)
        try await task.value
        // phase=waiting — every intent refused, zero frames emitted.
        XCTAssertFalse(store.turn(delta: 1, crossChapter: true, fromChapter: nil))
        XCTAssertFalse(store.goto(page: PairPageRef(folder: "x", name: "y")))
        XCTAssertFalse(store.setOffset(0))
        XCTAssertFalse(store.swapSides())
        try? await Task.sleep(nanoseconds: 100_000_000)
        let sentCount = await fake.sent().count
        XCTAssertEqual(sentCount, 1)
    }

    /// Follower cannot OPEN_WORK (leader-only intent); leader pickWork
    /// routes through the resolver and emits it.
    func testOpenWorkLeaderOnly() async throws {
        try await enterReading()   // bound as leader (bindData role)
        store.openWorkResolver = { folder in
            PairPageRef(folder: folder, name: "p1.png")
        }
        let picked = await store.pickWork(folder: "作品B")
        XCTAssertTrue(picked)
        let sent = await waitForSent(3)
        let ow = try XCTUnwrap(sent.last)
        XCTAssertEqual(ow["type"] as? String, "OPEN_WORK")
        XCTAssertEqual((ow["payload"] as? [String: Any])?["folder"] as? String, "作品B")
    }

    /// Hidden-root veto: a blocked folder never emits OPEN_WORK.
    func testOpenWorkVisibilityVeto() async throws {
        try await enterReading()
        store.visibilityBlocks = { _ in true }
        store.openWorkResolver = { folder in
            PairPageRef(folder: folder, name: "p1.png")
        }
        let picked = await store.pickWork(folder: "hidden")
        XCTAssertFalse(picked)
        try? await Task.sleep(nanoseconds: 100_000_000)
        let sentCount = await fake.sent().count
        XCTAssertEqual(sentCount, 2)
    }

    // MARK: - Heartbeat / activity / persistence

    /// Server PING → PONG preserving seq (test_reader_pairing ping_pong).
    func testPingAnswersPong() async throws {
        try await enterReading()
        await fake.emit(wire(["v": 1, "type": "PING", "seq": 7]))
        let sent = await waitForSent(3)
        let pong = try XCTUnwrap(sent.last)
        XCTAssertEqual(pong["type"] as? String, "PONG")
        XCTAssertEqual(pong["seq"] as? Int, 7)
    }

    /// MATCH sends MATCH and binds the ACKed role (follower).
    func testMatchBinds() async throws {
        let task = Task { @MainActor in try await store.match() }
        await hello()
        let sent = await waitForSent(1)
        guard let first = sent.first else { return }
        XCTAssertEqual(first["type"] as? String, "MATCH")
        await ack(first["requestId"] as! String, request: "MATCH",
                  data: ["sessionId": "s9", "memberId": "m9",
                         "role": "follower", "resumeToken": "rt9"])
        try await task.value
        XCTAssertEqual(store.role, .follower)
        XCTAssertTrue(store.bound)
    }

    /// JOIN code must be exactly 6 digits — malformed never opens a link.
    func testJoinMalformedCodeRejected() async throws {
        do {
            try await store.join(code: "12345")
            XCTFail("expected malformed")
        } catch { /* expected */ }
        let sentCount = await fake.sent().count
        XCTAssertEqual(sentCount, 0)
        XCTAssertFalse(store.bound)
    }

    /// Suspended activity survives a reconnect — RESUME ACK reasserts
    /// SET_ACTIVITY suspended (web pairClient suspended-reassert).
    func testSuspendedReassertedAfterResume() async throws {
        try await enterReading()
        store.setActivity("suspended")
        let sent = await waitForSent(3)
        XCTAssertEqual(sent[2]["type"] as? String, "SET_ACTIVITY")
        await fake.drop()
        for _ in 0..<200 where await fake.connectCount < 2 {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        await hello()
        let sent2 = await waitForSent(4)
        guard let resume = sent2.last else { return }
        XCTAssertEqual(resume["type"] as? String, "RESUME")
        await ack(resume["requestId"] as! String, request: "RESUME",
                  data: ["sessionId": "s1", "memberId": "m1",
                         "role": "leader", "resumeToken": "rt1"])
        let sent3 = await waitForSent(5)
        let act = try XCTUnwrap(sent3.last)
        XCTAssertEqual(act["type"] as? String, "SET_ACTIVITY")
        XCTAssertEqual((act["payload"] as? [String: Any])?["state"] as? String, "suspended")
    }

    /// leave() clears persisted creds — next boot won't auto-resume.
    func testLeaveClearsPersistedCreds() async throws {
        try await enterReading()
        XCTAssertNotNil(UserDefaults.standard.data(forKey: key))
        store.leave()
        for _ in 0..<50 {
            if case .ended = store.status { break }
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        XCTAssertNil(UserDefaults.standard.data(forKey: key))
        let sent = await waitForSent(3)
        XCTAssertEqual(sent.last?["type"] as? String, "LEAVE")
    }

    /// Resume window expiry ends the session locally instead of retrying
    /// forever (web _resumeWindow parity — 300s, shrunk here for test).
    func testResumeWindowExpiryEndsSession() async throws {
        store = PairStore(
            url: { URL(string: "ws://test/ws/reader-sync") },
            token: { "tok" },
            transportFactory: { self.fake },
            persistKey: { self.key },
            resumeWindow: 0.05)
        try await enterReading()
        await fake.drop()
        // First backoff already exceeds the window → terminal end.
        for _ in 0..<50 {
            if case .ended = store.status { break }
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        guard case .ended(let reason) = store.status else {
            XCTFail("expected ended"); return
        }
        XCTAssertEqual(reason, "resume_expired")
        let connects = await fake.connectCount
        XCTAssertEqual(connects, 1)   // no reconnect attempt
    }

    /// leave() while the link is dead ends immediately — no reconnect,
    /// creds cleared (web _leaveBound final-offline parity).
    func testLeaveWhileDisconnectedEndsLocally() async throws {
        try await enterReading()
        await fake.drop()
        for _ in 0..<50 where store.status != .connecting {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        store.leave()
        for _ in 0..<50 {
            if case .ended = store.status { break }
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        guard case .ended = store.status else {
            XCTFail("expected ended"); return
        }
        XCTAssertNil(UserDefaults.standard.data(forKey: key))
        // Retry machine torn down — no reconnect fires afterwards.
        try? await Task.sleep(nanoseconds: 700_000_000)
        let connects = await fake.connectCount
        XCTAssertEqual(connects, 1)
    }

    /// A new entry op supersedes an in-flight one — the superseded
    /// caller throws instead of hanging (web _entryOp supersede parity).
    func testEntrySupersededByNewEntry() async throws {
        let joinTask = Task { @MainActor in try await store.join(code: "123456") }
        await hello()
        let sent1 = await waitForSent(1)
        XCTAssertEqual(sent1[0]["type"] as? String, "JOIN")
        // No ACK — create() takes over: join's entry waiter must throw.
        let createTask = Task { @MainActor in try await store.create() }
        do {
            try await joinTask.value
            XCTFail("superseded join should throw")
        } catch { /* expected */ }
        await hello()
        let sent2 = await waitForSent(2)
        let create = try XCTUnwrap(sent2.last)
        XCTAssertEqual(create["type"] as? String, "CREATE")
        await ack(create["requestId"] as! String, request: "CREATE",
                  data: bindData)
        try await createTask.value
        XCTAssertTrue(store.bound)
    }

    /// Review fix — LEAVE must land on the wire BEFORE the socket closes
    /// (web _leaveBound: send → close chained, never reordered).
    func testLeaveSendsBeforeSocketClose() async throws {
        try await enterReading()
        store.leave()
        for _ in 0..<100 {
            let closes = await fake.closeCount
            if closes > 0 { break }
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        let sent = await waitForSent(3)
        XCTAssertEqual(sent.last?["type"] as? String, "LEAVE")
        // closeSocket() ran only after the LEAVE send completed — the
        // sent-count captured at close time includes the LEAVE frame.
        let closedAfter = await fake.closedAfterSendCount
        let sentCount = await fake.sent().count
        XCTAssertEqual(closedAfter, sentCount)
        for _ in 0..<50 {
            if case .ended = store.status { break }
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        guard case .ended = store.status else {
            XCTFail("expected ended"); return
        }
    }

    /// Review fix — LEAVE issued while the entry ACK is still in flight
    /// is deferred until the ACK binds member→link; an earlier send is
    /// rejected NO_SESSION and strands the peer (web _leaveBound).
    func testLeaveWaitsForEntryAck() async throws {
        let createTask = Task { @MainActor in try await store.create() }
        await hello()
        let sent = await waitForSent(1)
        let createRid = sent[0]["requestId"] as! String
        // Leave BEFORE the CREATE ACK — the bounded wait begins.
        store.leave()
        try? await Task.sleep(nanoseconds: 100_000_000)
        // LEAVE not yet on the wire — still waiting for the entry ACK.
        let sentNow = await fake.sent()
        XCTAssertEqual(sentNow.count, 1)
        // ACK arrives → LEAVE flushes, then the socket closes.
        await ack(createRid, request: "CREATE", data: bindData)
        try await createTask.value
        let sent2 = await waitForSent(2)
        XCTAssertEqual(sent2.last?["type"] as? String, "LEAVE")
        for _ in 0..<100 {
            let closes = await fake.closeCount
            if closes > 0 { break }
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        let closes = await fake.closeCount
        XCTAssertEqual(closes, 1)
    }
}
