import XCTest
@testable import Bookq

/// SearchModel — search/facets.js parity: generation guard drops stale
/// results, empty query exits search mode, filters ride the wire,
/// clear() resets, facets gen guard.
@MainActor
final class SearchModelTests: XCTestCase {
    private var api: BookqAPI!
    private var model: SearchModel!
    private var showAll = false

    override func setUp() async throws {
        StubURLProtocol.reset()
        api = StubURLProtocol.api()
        model = SearchModel(apiProvider: { self.api },
                            showAll: { self.showAll })
    }

    /// nonisolated — the URLProtocol handler runs off-actor.
    nonisolated private func result(_ name: String) -> [String: Any] {
        ["folder_path": "lib/\(name)", "folder_name": name,
         "title_jp": "", "title_en": name, "artist": [], "tags": [],
         "source": "", "has_metadata": false, "total_pages": 3]
    }

    nonisolated private func searchBody(_ names: [String]) -> [String: Any] {
        ["query": "q", "total": names.count,
         "results": names.map(result)]
    }

    /// Query → debounce → results populate + active.
    func testSearchPopulates() async {
        StubURLProtocol.handler = { req in
            req.url!.path == "/api/search"
                ? StubURLProtocol.json(self.searchBody(["x"]))
                : StubURLProtocol.json([:])
        }
        await model.search("hello")
        XCTAssertTrue(model.active)
        XCTAssertEqual(model.results.count, 1)
        XCTAssertEqual(model.results.first?.folder_name, "x")
    }

    /// Empty/whitespace query exits search mode and clears results.
    func testEmptyQueryExitsSearchMode() async {
        StubURLProtocol.handler = { _ in
            StubURLProtocol.json(self.searchBody(["x"]))
        }
        await model.search("hello")
        XCTAssertTrue(model.active)
        await model.search("   ")
        XCTAssertFalse(model.active)
        XCTAssertTrue(model.results.isEmpty)
    }

    /// Older in-flight results are dropped when a newer search supersedes.
    /// Gate the second search on the first request landing, or task start
    /// order can hand "r1" to the newer generation.
    func testStaleResultDiscarded() async {
        var calls = 0
        StubURLProtocol.handler = { _ in
            calls += 1
            return StubURLProtocol.json(self.searchBody(["r\(calls)"]))
        }
        let first = Task { @MainActor in await self.model.search("a") }
        for _ in 0..<100 where StubURLProtocol.hits(path: "api/search") < 1 {
            try? await Task.sleep(nanoseconds: 10_000_000)
        }
        let second = Task { @MainActor in await self.model.search("b") }
        _ = await (first.value, second.value)
        XCTAssertEqual(model.results.first?.folder_name, "r2")
    }

    /// artist/tag filters + include_hidden flag reach the wire.
    func testFiltersOnWire() async throws {
        var seenQ: String?
        StubURLProtocol.handler = { req in
            seenQ = req.url?.query
            return StubURLProtocol.json(self.searchBody([]))
        }
        model.artists = ["Alice", "Bob"]
        model.tags = ["tag1"]
        showAll = true
        await model.search("x")
        let q = try XCTUnwrap(seenQ, "query never observed")
        XCTAssertTrue(q.contains("q=x"))
        XCTAssertTrue(q.contains("artist=Alice%2CBob") || q.contains("artist=Alice,Bob"))
        XCTAssertTrue(q.contains("tag=tag1"))
        XCTAssertTrue(q.contains("include_hidden=1"))
    }

    /// clear() resets query/results/active and cancels pending debounce.
    func testClearResets() async {
        StubURLProtocol.handler = { _ in
            StubURLProtocol.json(self.searchBody(["x"]))
        }
        await model.search("hello")
        model.clear()
        XCTAssertFalse(model.active)
        XCTAssertTrue(model.results.isEmpty)
        XCTAssertEqual(model.query, "")
    }

    /// toggleArtist/toggleTag/resetFilters drive the filter sets.
    func testFilterToggles() {
        model.toggleArtist("A")
        model.toggleArtist("A")
        XCTAssertTrue(model.artists.isEmpty)
        model.toggleTag("t")
        model.toggleArtist("B")
        model.resetFilters()
        XCTAssertTrue(model.tags.isEmpty && model.artists.isEmpty)
    }

    /// loadFacets populates; stale facet responses are discarded.
    func testFacetsGenGuard() async {
        var calls = 0
        StubURLProtocol.handler = { req in
            calls += 1
            return StubURLProtocol.json(
                ["artists": ["a\(calls)"], "tags": ["t\(calls)"]])
        }
        let f1 = Task { @MainActor in await self.model.loadFacets() }
        for _ in 0..<100
            where StubURLProtocol.hits(path: "api/metadata/facets") < 1 {
            try? await Task.sleep(nanoseconds: 10_000_000)
        }
        let f2 = Task { @MainActor in await self.model.loadFacets() }
        _ = await (f1.value, f2.value)
        XCTAssertEqual(model.facets?.artists, ["a2"])
    }
}
