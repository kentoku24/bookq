import XCTest
@testable import Bookq

/// BookqAPI wire-level tests against the v1 contract — 202 index-building
/// retry, 304 generation match, 401 → refresh → retry Bearer swap.
final class BookqAPITests: XCTestCase {

    override func setUp() async throws { StubURLProtocol.reset() }

    private let treeEnv: [String: Any] = [
        "kind": "tree", "generation": 7,
        "nodes": [["name": "a", "type": "folder"]],
    ]

    /// 202 (index building) retries on Retry-After then unwraps the
    /// {kind,nodes,generation} envelope (test_index_building_202 parity).
    func testTree202Then200() async throws {
        var hits = 0
        StubURLProtocol.handler = { _ in
            hits += 1
            return hits == 1
                ? StubURLProtocol.json([:], status: 202, headers: ["Retry-After": "0"])
                : StubURLProtocol.json(self.treeEnv)
        }
        let r = try await StubURLProtocol.api().tree()
        guard case .nodes(let kind, let nodes, let gen, _) = r else {
            return XCTFail("expected .nodes")
        }
        XCTAssertEqual(hits, 2)
        XCTAssertEqual(kind, "tree")
        XCTAssertEqual(gen, 7)
        XCTAssertEqual(nodes.count, 1)
    }

    /// 304 (since generation matches) → .notModified — no body decode.
    func testTree304NotModified() async throws {
        StubURLProtocol.handler = { _ in
            StubURLProtocol.json([:], status: 304)
        }
        let r = try await StubURLProtocol.api().tree(since: 7)
        guard case .notModified = r else { return XCTFail("expected .notModified") }
        XCTAssertEqual(StubURLProtocol.hits(path: "api/v1/tree"), 1)
    }

    /// `since` rides the query; ETag rides If-None-Match.
    func testTreeSinceAndEtagOnWire() async throws {
        StubURLProtocol.handler = { _ in StubURLProtocol.json([:], status: 304) }
        _ = try await StubURLProtocol.api().tree(since: 3, etag: "\"d-g3\"")
        let rec = StubURLProtocol.last(path: "api/v1/tree")
        let comps = URLComponents(url: rec!.request.url!, resolvingAgainstBaseURL: false)
        XCTAssertEqual(comps?.queryItems?.first { $0.name == "since" }?.value, "3")
        XCTAssertEqual(rec?.request.value(forHTTPHeaderField: "If-None-Match"), "\"d-g3\"")
    }

    /// 401 → tokenRefresher → retry with the rotated Bearer; parallel 401s
    /// coalesce on one refresh (RFC 6750 / rotation contract).
    func test401RefreshThenRetry() async throws {
        var auths: [String?] = []
        StubURLProtocol.handler = { req in
            auths.append(req.value(forHTTPHeaderField: "Authorization"))
            return auths.count == 1
                ? StubURLProtocol.json([:], status: 401)
                : StubURLProtocol.json(self.treeEnv)
        }
        final class Counter: @unchecked Sendable { var n = 0 }
        let refreshCalls = Counter()
        let api = BookqAPI(baseURL: URL(string: "http://unit.test")!,
                           session: StubURLProtocol.session(),
                           tokenProvider: { "t1" },
                           tokenRefresher: { refreshCalls.n += 1; return "t2" })
        let r = try await api.tree()
        guard case .nodes = r else { return XCTFail("expected .nodes") }
        XCTAssertEqual(auths, ["Bearer t1", "Bearer t2"])
        XCTAssertEqual(refreshCalls.n, 1)
    }

    /// Refresh failure → the 401 propagates as unauthorized (→ re-login).
    func test401RefreshFailsPropagates() async throws {
        StubURLProtocol.handler = { _ in StubURLProtocol.json([:], status: 401) }
        let api = BookqAPI(baseURL: URL(string: "http://unit.test")!,
                           session: StubURLProtocol.session(),
                           tokenProvider: { "t1" },
                           tokenRefresher: { nil })
        do {
            _ = try await api.tree()
            XCTFail("expected unauthorized")
        } catch APIError.unauthorized { }
    }

    /// `+` and other reserved chars in folder paths must be percent-encoded
    /// on the wire — URLQueryItem leaves "+" literal and the server reads
    /// it as a space, which broke downloads for "+" folders (#414).
    func testImagesFolderQueryEncoding() async throws {
        StubURLProtocol.handler = { _ in
            StubURLProtocol.json(["folder": "a+b", "images": ["p1.jpg"]])
        }
        _ = try await StubURLProtocol.api().images(folder: "a+b/第1話 x")
        let rec = StubURLProtocol.last(path: "api/v1/images")
        XCTAssertEqual(
            rec?.request.url?.absoluteString,
            "http://unit.test/api/v1/images?folder=a%2Bb%2F%E7%AC%AC1%E8%A9%B1%20x")
    }

    /// search() unwraps the 202 → 200 {query,total,results} envelope.
    func testSearch202Then200() async throws {
        var hits = 0
        StubURLProtocol.handler = { _ in
            hits += 1
            return hits == 1
                ? StubURLProtocol.json([:], status: 202)
                : StubURLProtocol.json([
                    "query": "x", "total": 0, "results": []])
        }
        let r = try await StubURLProtocol.api().search(
            q: "x", artist: [], tag: [], includeHidden: false)
        XCTAssertEqual(hits, 2)
        XCTAssertEqual(r?.total, 0)
    }

    // MARK: - Connectivity reporter (docs/offline-spec.md §5)

    private final class Verdicts: @unchecked Sendable {
        var ok = 0, fail = 0
    }

    private func apiWithReporter(_ v: Verdicts) -> BookqAPI {
        BookqAPI(baseURL: URL(string: "http://unit.test")!,
                 session: StubURLProtocol.session(),
                 tokenProvider: { nil },
                 reporter: ConnectivityReporter(
                    success: { v.ok += 1 },
                    unreachable: { v.fail += 1 }))
    }

    /// A 2xx body is the only "online" proof — the server answered.
    func testReporter2xxIsSuccess() async throws {
        let v = Verdicts()
        StubURLProtocol.handler = { _ in StubURLProtocol.json([:]) }
        try await apiWithReporter(v).health()
        XCTAssertEqual(v.ok, 1); XCTAssertEqual(v.fail, 0)
    }

    /// No response at all (unreachable URLError) → fail verdict.
    func testReporterUnreachableErrorIsFail() async {
        let v = Verdicts()
        StubURLProtocol.failWith = .notConnectedToInternet
        try? await apiWithReporter(v).health()
        XCTAssertEqual(v.ok, 0); XCTAssertEqual(v.fail, 1)
    }

    /// Proxy-band 5xx means the server did not answer → fail verdict.
    func testReporter503IsFail() async {
        let v = Verdicts()
        StubURLProtocol.handler = { _ in StubURLProtocol.json([:], status: 503) }
        try? await apiWithReporter(v).health()
        XCTAssertEqual(v.ok, 0); XCTAssertEqual(v.fail, 1)
    }

    /// A real 4xx/5xx response is neutral — the server answered, the
    /// machine must not demote over an application-level error.
    func testReporter404IsNeutral() async {
        let v = Verdicts()
        StubURLProtocol.handler = { _ in StubURLProtocol.json([:], status: 404) }
        try? await apiWithReporter(v).health()
        XCTAssertEqual(v.ok, 0); XCTAssertEqual(v.fail, 0)
    }

    /// Cancellations are caller-driven (intentional aborts) — never
    /// connectivity evidence (spec §5 note).
    func testReporterCancelledIsNeutral() async {
        let v = Verdicts()
        StubURLProtocol.failWith = .cancelled
        try? await apiWithReporter(v).health()
        XCTAssertEqual(v.ok, 0); XCTAssertEqual(v.fail, 0)
    }
}
