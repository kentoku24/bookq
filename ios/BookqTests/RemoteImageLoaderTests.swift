import XCTest
@testable import Bookq

/// RemoteImageLoader cache-epoch tests — a fetch that outlives a
/// server/account switch (clear()) must not write the old scope's
/// image back into the cache.
@MainActor
final class RemoteImageLoaderTests: XCTestCase {
    /// 1x1 transparent PNG.
    private let png = Data(base64Encoded:
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")!

    /// Review fix — in-flight fetch completing after clear() returns nil
    /// and does not re-cache the stale image; the next load refetches.
    func testClearDropsInFlightFetch() async {
        let png = self.png
        let loader = RemoteImageLoader()
        loader.fetcher = { _ in
            try await Task.sleep(nanoseconds: 200_000_000)
            return png
        }
        let url = URL(string: "https://x.example/image/a/1.jpg")!
        async let pending = loader.load(url)
        try? await Task.sleep(nanoseconds: 50_000_000)
        loader.clear()
        let stale = await pending
        XCTAssertNil(stale)                      // stale fetch dropped
        XCTAssertNil(loader.image(for: url))     // and never re-cached
        loader.fetcher = { _ in png }
        let fresh = await loader.load(url)
        XCTAssertNotNil(fresh)                   // new epoch refetches
        XCTAssertNotNil(loader.image(for: url))
    }

    func testLoadCachesAcrossCalls() async {
        let png = self.png
        let loader = RemoteImageLoader()
        var calls = 0
        loader.fetcher = { _ in calls += 1; return png }
        let url = URL(string: "https://x.example/image/a/1.jpg")!
        let first = await loader.load(url)
        let second = await loader.load(url)
        XCTAssertNotNil(first)
        XCTAssertNotNil(second)                  // cache hit
        XCTAssertEqual(calls, 1)
    }
}
