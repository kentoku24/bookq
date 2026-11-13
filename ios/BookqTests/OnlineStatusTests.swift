import XCTest
@testable import Bookq

/// OnlineStatusStore — the docs/offline-spec.md §2 state machine:
/// offline → connecting → online, a finite 3-probe recovery cycle, no
/// offline auto-retry, and request-outcome demotion. Test cycles use
/// shrunken backoffs so the suite never waits the real ~15 s budget.
@MainActor
final class OnlineStatusTests: XCTestCase {

    override func setUp() async throws { StubURLProtocol.reset() }
    override func tearDown() async throws { StubURLProtocol.reset() }

    private func makeStore(api: BookqAPI?,
                           attempts: Int = 3) -> OnlineStatusStore {
        OnlineStatusStore(apiProvider: { api },
                          probeTimeout: 2,
                          backoffs: [0.01, 0.01, 0.01, 0.01],
                          maxAttempts: attempts)
    }

    private func healthHits() -> Int {
        StubURLProtocol.hits(path: "api/v1/health")
    }

    // MARK: - Cycle basics

    /// spec §1: the machine boots offline — never optimistic.
    func testInitialStateIsOffline() {
        let s = makeStore(api: nil)
        XCTAssertEqual(s.state, .offline)
        XCTAssertFalse(s.isOnline)
        XCTAssertFalse(s.online)
    }

    /// A healthy probe lands online on the FIRST attempt.
    func testHealthyProbeFlipsOnline() async {
        StubURLProtocol.handler = { _ in StubURLProtocol.json([:]) }
        let s = makeStore(api: StubURLProtocol.api())
        let ok = await s.connect().value
        XCTAssertTrue(ok)
        XCTAssertEqual(s.state, .online)
        XCTAssertTrue(s.isOnline)
        XCTAssertEqual(healthHits(), 1)
    }

    /// Dead server → exactly `maxAttempts` probes → offline; spec §3:
    /// nothing probes again until a valid trigger arrives.
    func testFailingCycleExhaustsBudgetThenStops() async {
        StubURLProtocol.failWith = .notConnectedToInternet
        let s = makeStore(api: StubURLProtocol.api())
        let ok = await s.connect().value
        XCTAssertFalse(ok)
        XCTAssertEqual(s.state, .offline)
        XCTAssertEqual(healthHits(), 3)
        try? await Task.sleep(nanoseconds: 150_000_000)
        XCTAssertEqual(healthHits(), 3, "offline must not auto-retry")
    }

    /// No API client (unconfigured) → offline, not a crash.
    func testMissingAPIIsOffline() async {
        let s = makeStore(api: nil)
        let ok = await s.connect().value
        XCTAssertFalse(ok)
        XCTAssertEqual(s.state, .offline)
    }

    /// Concurrent connect()s share ONE cycle — the probe count stays
    /// at the single-cycle total.
    func testConnectCoalescesConcurrentCalls() async {
        StubURLProtocol.handler = { _ in StubURLProtocol.json([:]) }
        let s = makeStore(api: StubURLProtocol.api())
        async let a: Bool = s.connect().value
        async let b: Bool = s.connect().value
        let (okA, okB) = await (a, b)
        XCTAssertTrue(okA && okB)
        XCTAssertEqual(healthHits(), 1)
    }

    // MARK: - Verdicts

    /// spec §2: an online request with no response demotes immediately
    /// to connecting; the failing cycle then settles offline.
    func testReportFailDemotesOnlineToConnecting() async {
        StubURLProtocol.handler = { _ in StubURLProtocol.json([:]) }
        let s = makeStore(api: StubURLProtocol.api())
        _ = await s.connect().value
        XCTAssertEqual(s.state, .online)

        StubURLProtocol.handler = nil
        StubURLProtocol.failWith = .notConnectedToInternet
        let demotion = s.reportFail()
        XCTAssertEqual(s.state, .connecting)
        let ok = await demotion?.value
        XCTAssertEqual(ok, false)
        XCTAssertEqual(s.state, .offline)
    }

    /// spec §5: a real response mid-cycle lands online immediately —
    /// the in-flight cycle must not demote it back to offline.
    func testReportSuccessLandsOnlineMidCycle() async {
        StubURLProtocol.failWith = .notConnectedToInternet
        let s = makeStore(api: StubURLProtocol.api())
        let cycle = s.connect()
        XCTAssertEqual(s.state, .connecting)
        s.reportSuccess()
        XCTAssertEqual(s.state, .online)
        let ok = await cycle.value
        XCTAssertTrue(ok, "reportSuccess must short-circuit the cycle")
        XCTAssertEqual(s.state, .online)
    }

    /// A stray reportFail while offline is a no-op — offline never
    /// self-promotes into another cycle.
    func testReportFailIgnoredWhileOffline() async {
        StubURLProtocol.failWith = .notConnectedToInternet
        let s = makeStore(api: StubURLProtocol.api())
        _ = await s.connect().value
        XCTAssertEqual(s.state, .offline)
        XCTAssertNil(s.reportFail())
        try? await Task.sleep(nanoseconds: 150_000_000)
        XCTAssertEqual(s.state, .offline)
        XCTAssertEqual(healthHits(), 3)
    }

    // MARK: - Triggers

    /// Explicit retry from offline starts a fresh cycle (badge tap).
    func testRetryStartsNewCycleFromOffline() async {
        StubURLProtocol.failWith = .notConnectedToInternet
        let s = makeStore(api: StubURLProtocol.api())
        _ = await s.connect().value
        XCTAssertEqual(s.state, .offline)

        StubURLProtocol.failWith = nil
        StubURLProtocol.handler = { _ in StubURLProtocol.json([:]) }
        let ok = await s.retry().value
        XCTAssertTrue(ok)
        XCTAssertEqual(s.state, .online)
    }

    /// spec §3: foregrounding is a retry trigger — but only when not
    /// already online (an online foreground probes nothing).
    func testForegroundedTriggersCycleWhenNotOnline() async {
        StubURLProtocol.failWith = .notConnectedToInternet
        let s = makeStore(api: StubURLProtocol.api())
        _ = await s.connect().value
        XCTAssertEqual(healthHits(), 3)

        StubURLProtocol.failWith = nil
        StubURLProtocol.handler = { _ in StubURLProtocol.json([:]) }
        let cycle = s.foregrounded()
        XCTAssertEqual(s.state, .connecting)
        let ok = await cycle?.value
        XCTAssertEqual(ok, true)
        XCTAssertEqual(s.state, .online)

        let hits = healthHits()
        // online → single silent probe (web revalidate parity)
        let ok2 = await s.foregrounded()?.value
        XCTAssertEqual(ok2, true)
        XCTAssertEqual(s.state, .online)
        XCTAssertEqual(healthHits(), hits + 1)
    }

    /// spec §3 / web revalidate parity: foreground while online still
    /// probes once — a server that died while backgrounded demotes into
    /// the finite cycle instead of staying stale-online.
    func testForegroundedDeadServerDemotes() async {
        StubURLProtocol.handler = { _ in StubURLProtocol.json([:]) }
        let s = makeStore(api: StubURLProtocol.api())
        _ = await s.connect().value
        XCTAssertEqual(s.state, .online)
        let hits0 = healthHits()

        StubURLProtocol.failWith = .notConnectedToInternet
        let ok = await s.foregrounded()?.value
        XCTAssertEqual(ok, false)
        XCTAssertEqual(s.state, .offline)
        XCTAssertEqual(healthHits(), hits0 + 1 + 3,
                       "fg probe + the demoted cycle's 3 attempts")
    }

    // MARK: - Wire shape

    /// The probe marks itself on the wire: `?online_check=1` +
    /// `X-Online-Check: 1` — identical to updateOnlineStatus's fetch.
    func testHealthOnlineCheckMarkersOnWire() async throws {
        StubURLProtocol.handler = { _ in StubURLProtocol.json([:]) }
        try await StubURLProtocol.api().health(onlineCheck: true)
        let rec = StubURLProtocol.last(path: "api/v1/health")
        let req = try XCTUnwrap(rec?.request)
        let comps = URLComponents(url: req.url!, resolvingAgainstBaseURL: false)
        XCTAssertTrue(comps?.queryItems?.contains(
            URLQueryItem(name: "online_check", value: "1")) == true)
        XCTAssertEqual(req.value(forHTTPHeaderField: "X-Online-Check"), "1")
    }
}
