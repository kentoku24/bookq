import XCTest
@testable import Bookq

/// OnlineStatusStore — health-probe driven ●/○ state, plus the
/// `online_check` wire markers matching updateOnlineStatus (app.js).
@MainActor
final class OnlineStatusTests: XCTestCase {

    override func setUp() async throws { StubURLProtocol.reset() }

    /// 200 probe → online.
    func testHealthyProbeFlipsOnline() async {
        StubURLProtocol.handler = { _ in StubURLProtocol.json([:]) }
        let api = StubURLProtocol.api()
        let store = OnlineStatusStore(apiProvider: { api })
        await store.check()
        XCTAssertTrue(store.online)
    }

    /// Any non-200 (server down, dead host) → offline.
    func testFailingProbeFlipsOffline() async {
        StubURLProtocol.handler = { _ in StubURLProtocol.json([:], status: 502) }
        let api = StubURLProtocol.api()
        let store = OnlineStatusStore(apiProvider: { api })
        await store.check()
        XCTAssertFalse(store.online)
    }

    /// No API client (unconfigured) → offline, not a crash.
    func testMissingAPIIsOffline() async {
        let store = OnlineStatusStore(apiProvider: { nil })
        await store.check()
        XCTAssertFalse(store.online)
    }

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
