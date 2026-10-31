import XCTest
@testable import Bookq

/// LibraryStore tree snapshot — offline-launch parity with the web SW's
/// NetworkFirst `/api/folders` cache: save → new instance restores the
/// last-known tree before any network.
@MainActor
final class LibraryStoreTests: XCTestCase {
    private var host: String!

    override func setUp() async throws {
        StubURLProtocol.reset()
        host = "lib-\(UUID().uuidString.prefix(8)).test"
    }

    override func tearDown() async throws {
        try? FileManager.default.removeItem(at: snapshotURL())
        StubURLProtocol.reset()
    }

    private func snapshotURL() -> URL {
        var cs = CharacterSet.alphanumerics
        cs.insert(charactersIn: "-_")
        let key = host.addingPercentEncoding(withAllowedCharacters: cs) ?? "d"
        return FileManager.default.urls(for: .applicationSupportDirectory,
                                        in: .userDomainMask)[0]
            .appendingPathComponent("tree-\(key).json")
    }

    private func api() -> BookqAPI { StubURLProtocol.api(host: host) }

    nonisolated private var treeJSON: [[String: Any]] {
        [["name": "作品A", "type": "folder", "children": [
            ["name": "p1.png", "type": "image"],
            ["name": "p2.png", "type": "image"]]]]
    }

    /// A fetched tree is persisted — a fresh store constructed later
    /// (e.g. next launch, offline) restores it before any network.
    func testSnapshotRestoreOnInit() async {
        StubURLProtocol.handler = { req in
            StubURLProtocol.json(self.treeJSON)
        }
        let s1 = LibraryStore(api: api())
        await s1.load()
        XCTAssertEqual(s1.tree.count, 1)
        XCTAssertTrue(FileManager.default.fileExists(atPath: snapshotURL().path))

        // Second instance with NO network stub — snapshot still loads.
        StubURLProtocol.handler = { _ in
            StubURLProtocol.Response(status: 500, body: Data())
        }
        let s2 = LibraryStore(api: api())
        XCTAssertEqual(s2.tree.count, 1)
        XCTAssertEqual(s2.tree.first?.name, "作品A")
    }

    /// Full fetch → delta fetch: generation advances and since= is sent.
    func testDeltaMergeGeneration() async {
        var calls = 0
        StubURLProtocol.handler = { req in
            calls += 1
            if req.url?.query?.contains("since=") == true {
                return StubURLProtocol.json(
                    ["tree": self.treeJSON, "generation": 7])
            }
            return StubURLProtocol.json(self.treeJSON,
                                        headers: ["ETag": "\"42-abc\""])
        }
        let s = LibraryStore(api: api())
        await s.load()
        XCTAssertEqual(s.generation, 42)   // from ETag
        await s.load()
        XCTAssertEqual(s.generation, 7)    // from delta
        XCTAssertEqual(StubURLProtocol.hits(path: "api/folders"), 2)
    }

    /// Fetch failure keeps the snapshot tree and surfaces error —
    /// offline launch degrades to browsing, not a dead screen.
    func testFailedLoadKeepsSnapshot() async {
        StubURLProtocol.handler = { _ in
            StubURLProtocol.json(self.treeJSON)
        }
        let s1 = LibraryStore(api: api())
        await s1.load()

        StubURLProtocol.handler = { _ in
            StubURLProtocol.Response(status: 500, body: Data())
        }
        let s2 = LibraryStore(api: api())
        await s2.load()
        XCTAssertEqual(s2.tree.count, 1)   // snapshot kept
        XCTAssertNotNil(s2.error)
    }
}

/// AppModel.initialPageRef — web `_resolveInitialPageRef`/`_pickRef`
/// parity: leaf = all children, mixed = image children only, fallback =
/// fetched listing, saved resume page clamped into range.
@MainActor
final class InitialPageRefTests: XCTestCase {

    private func node(_ name: String, _ type: String,
                      _ children: [TreeNode]? = nil) -> TreeNode {
        TreeNode(name: name, type: type, children: children)
    }

    /// Leaf folder → child names taken verbatim (no fetch).
    func testLeafUsesTreeChildren() async {
        // Flattened node: name carries the full path at root level
        // (scanner single-child hoist) — same shape the API returns.
        let leaf = node("W/ch1", "folder",
                        [node("p1.png", "image"), node("p2.png", "image")])
        let tree = [leaf]
        var fetchCalled = false
        let ref = await AppModel.initialPageRef(
            folder: "W/ch1", tree: tree,
            images: { fetchCalled = true; return nil },
            savedPage: nil)
        XCTAssertFalse(fetchCalled)
        XCTAssertEqual(ref?.name, "p1.png")
        XCTAssertEqual(ref?.folder, "W/ch1")
    }

    /// Mixed folder → only image children enter the ref list (web reads
    /// direct images only — subfolders are navigation, not pages).
    func testMixedUsesImageChildrenOnly() async {
        let mixed = node("M", "folder", [
            node("sub", "folder"), node("a.png", "image"),
            node("b.png", "image")])
        let ref = await AppModel.initialPageRef(
            folder: "M", tree: [mixed],
            images: { nil }, savedPage: nil)
        XCTAssertEqual(ref?.name, "a.png")
    }

    /// Unknown folder → listing fallback (fetched names).
    func testFallbackFetchesListing() async {
        let ref = await AppModel.initialPageRef(
            folder: "ghost", tree: [],
            images: { ["x1.png", "x2.png"] }, savedPage: nil)
        XCTAssertEqual(ref?.name, "x1.png")
    }

    /// Saved resume page selects the entry; out-of-range clamps to last.
    func testSavedPageClamp() async {
        let leaf = node("L", "folder",
                        [node("p1", "image"), node("p2", "image"),
                         node("p3", "image")])
        let ref = await AppModel.initialPageRef(
            folder: "L", tree: [leaf],
            images: { nil }, savedPage: 99)
        XCTAssertEqual(ref?.name, "p3")
    }

    /// Empty work (no tree node, empty listing) → nil, caller stays put.
    func testEmptyWorkReturnsNil() async {
        let ref = await AppModel.initialPageRef(
            folder: "ghost", tree: [], images: { [] }, savedPage: nil)
        XCTAssertNil(ref)
    }
}
