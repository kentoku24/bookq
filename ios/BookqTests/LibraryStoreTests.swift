import XCTest
@testable import Bookq

/// LibraryStore tree snapshot — offline-launch parity with the web SW's
/// NetworkFirst `/api/v1/tree` cache: save → new instance restores the
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

    /// v1 envelope: every /api/v1/tree 200 body is {kind,nodes,generation}.
    nonisolated private func envelope(_ gen: Int) -> [String: Any] {
        ["kind": "tree", "nodes": treeJSON, "generation": gen]
    }

    /// API requests must bypass URLCache — RFC 7231 heuristically caches
    /// error responses, so a transient 404 (e.g. during a server restart)
    /// would otherwise replay on every later boot and skip server sync.
    func testRequestsBypassURLCache() async {
        StubURLProtocol.handler = { _ in StubURLProtocol.json(self.envelope(1)) }
        let s = LibraryStore(api: api())
        await s.load()
        let rec = StubURLProtocol.last(path: "api/v1/tree")
        XCTAssertEqual(rec?.request.cachePolicy, .reloadIgnoringLocalCacheData)
    }

    /// A fetched tree is persisted — a fresh store constructed later
    /// (e.g. next launch, offline) restores it before any network.
    func testSnapshotRestoreOnInit() async {
        StubURLProtocol.handler = { req in
            StubURLProtocol.json(self.envelope(1))
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
    /// v1 envelopes always carry `generation` in the body (a `since`
    /// mismatch re-sends kind="tree" with the new gen — no ETag parse).
    func testDeltaMergeGeneration() async {
        var calls = 0
        StubURLProtocol.handler = { req in
            calls += 1
            if req.url?.query?.contains("since=") == true {
                return StubURLProtocol.json(self.envelope(7))
            }
            return StubURLProtocol.json(self.envelope(42),
                                        headers: ["ETag": "\"abc123def-g42\""])
        }
        let s = LibraryStore(api: api())
        await s.load()
        XCTAssertEqual(s.generation, 42)   // from envelope body
        await s.load()
        XCTAssertEqual(s.generation, 7)    // from re-sent envelope
        XCTAssertEqual(StubURLProtocol.hits(path: "api/v1/tree"), 2)
    }

    /// Fetch failure keeps the snapshot tree and surfaces error —
    /// offline launch degrades to browsing, not a dead screen.
    func testFailedLoadKeepsSnapshot() async {
        StubURLProtocol.handler = { _ in
            StubURLProtocol.json(self.envelope(1))
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

    // MARK: - Offline browse filtering (docs/offline-spec.md §6/§8)

    /// Two leaves + one two-episode series — the shape the uniform
    /// offline filter is judged against.
    nonisolated private var offlineTreeJSON: [[String: Any]] {
        [
            ["name": "作品A", "type": "folder", "children":
                [["name": "a1.png", "type": "image"]]],
            ["name": "作品B", "type": "folder", "children":
                [["name": "b1.png", "type": "image"]]],
            ["name": "series", "type": "folder", "children": [
                ["name": "ep1", "type": "folder", "children":
                    [["name": "e1.png", "type": "image"]]],
                ["name": "ep2", "type": "folder", "children":
                    [["name": "e2.png", "type": "image"]]]]],
        ]
    }

    private func offlineStore() -> OfflineStore {
        let tmp = FileManager.default.temporaryDirectory
            .appendingPathComponent("off-\(UUID().uuidString)", isDirectory: true)
        return OfflineStore(apiProvider: { nil }, root: tmp)
    }

    private func leafRec(_ folder: String,
                         _ names: [String] = ["p1.png"]) -> OfflineStore.LeafRecord {
        OfflineStore.LeafRecord(folder: folder, pageCount: names.count,
                                cachedCount: names.count, bytes: 1,
                                thumb: names.first, names: names,
                                addedAt: 0, lastReadAt: 0)
    }

    private func loadedOfflineStore(_ nodes: [[String: Any]]) async -> LibraryStore {
        StubURLProtocol.handler = { _ in
            StubURLProtocol.json(["kind": "tree", "nodes": nodes, "generation": 1])
        }
        let s = LibraryStore(api: api())
        await s.load()
        s.offline = OfflineStore(apiProvider: { nil }, root:
            FileManager.default.temporaryDirectory
                .appendingPathComponent("off-\(UUID().uuidString)", isDirectory: true))
        // Default state is .offline → the filter engages (no probes run).
        s.online = OnlineStatusStore(apiProvider: { nil })
        return s
    }

    /// Root level with downloads: every episode stays listed and only
    /// the downloaded one is readable; branches without downloads hide.
    func testOfflineRootLevelFilter() async {
        let s = await loadedOfflineStore(offlineTreeJSON)
        s.offline?.testPut(leafRec("作品A"))
        s.offline?.testPut(leafRec("series/ep1"))

        let root = s.levelFolders(path: "").map(\.path).sorted()
        XCTAssertEqual(root, ["series", "作品A", "作品B"].sorted(),
                       "level with downloads lists all episodes + eligible branches")
        XCTAssertFalse(s.tileUnavailable(path: "作品A", episodeLike: true))
        XCTAssertTrue(s.tileUnavailable(path: "作品B", episodeLike: true))
        XCTAssertTrue(s.workReadable("作品A"))
        XCTAssertFalse(s.workReadable("作品B"))
        XCTAssertFalse(s.workReadable("series"), "ep2 has no record")

        let eps = s.levelFolders(path: "series").map(\.path).sorted()
        XCTAssertEqual(eps, ["series/ep1", "series/ep2"])
        XCTAssertTrue(s.tileUnavailable(path: "series/ep2", episodeLike: true))
    }

    /// A branch with zero downloads disappears entirely offline —
    /// including while drilling (levelHasDl = subtreeHasDownload).
    func testOfflineBranchWithoutDownloadsHidden() async {
        let tree: [[String: Any]] = [
            ["name": "作品A", "type": "folder", "children":
                [["name": "a1.png", "type": "image"]]],
            ["name": "clean", "type": "folder", "children": [
                ["name": "c1", "type": "folder", "children":
                    [["name": "p.png", "type": "image"]]]]],
        ]
        let s = await loadedOfflineStore(tree)
        s.offline?.testPut(leafRec("作品A"))
        XCTAssertEqual(s.levelFolders(path: "").map(\.path), ["作品A"])
        XCTAssertTrue(s.levelFolders(path: "clean").isEmpty)
        XCTAssertFalse(s.subtreeHasDownload("clean"))
    }

    /// spec §8: a record whose path is missing from the snapshot still
    /// surfaces — synthesized intermediate + leaf nodes, readable.
    func testOfflineSynthesizesMissingRecordPaths() async {
        let s = await loadedOfflineStore(offlineTreeJSON)
        s.offline?.testPut(leafRec("dl-only/ch-1", ["p1.png", "p2.png"]))

        XCTAssertTrue(s.levelFolders(path: "").contains { $0.path == "dl-only" },
                      "synthesized parent must surface offline")
        let eps = s.levelFolders(path: "dl-only").map(\.path)
        XCTAssertEqual(eps, ["dl-only/ch-1"])
        XCTAssertTrue(s.workReadable("dl-only/ch-1"))
        XCTAssertTrue(BrowseModel.hasOnlyImages(s.node(at: "dl-only/ch-1")!),
                      "synthesized leaf behaves like an episode node")
        XCTAssertEqual(s.leafPaths(under: "dl-only"), ["dl-only/ch-1"])
    }

    /// A record nested under an existing LEAF is not materialized —
    /// synthesizing inside a leaf would double-render the flattened node
    /// (web `_ensureSynthPath` leaf-ancestor guard parity).
    func testRecordUnderLeafAncestorNotSynthesized() async {
        let tree: [[String: Any]] = [
            ["name": "chain", "type": "folder", "children":
                [["name": "p1.png", "type": "image"]]],
        ]
        let s = await loadedOfflineStore(tree)
        s.offline?.testPut(leafRec("chain/only/leaf"))
        XCTAssertEqual(s.levelFolders(path: "").map(\.path), ["chain"])
        XCTAssertNil(s.node(at: "chain/only/leaf"))
        XCTAssertTrue(s.subtreeHasDownload("chain"))
    }

    /// A record on an existing FLATTENED leaf (`a`→`b/c`) resolves via
    /// node(at:) — it must not synthesize a duplicate `a/b` folder chain
    /// beside the real node (web `findNodeByPath` missing-check parity).
    func testRecordOnFlattenedLeafNotDuplicated() async {
        let tree: [[String: Any]] = [
            ["name": "a", "type": "folder", "children": [
                ["name": "b/c", "type": "folder", "children":
                    [["name": "p1.png", "type": "image"]]]]],
        ]
        let s = await loadedOfflineStore(tree)
        s.offline?.testPut(leafRec("a/b/c"))
        let kids = s.levelFolders(path: "a").map(\.path)
        XCTAssertEqual(kids, ["a/b/c"],
                       "flattened leaf must not gain a synthetic sibling chain")
        XCTAssertTrue(s.workReadable("a/b/c"))
    }

    /// While online the filter disengages — the full tree is browsable
    /// and nothing is synthesized.
    func testOnlineDisablesOfflineFilter() async {
        let s = await loadedOfflineStore(offlineTreeJSON)
        s.offline?.testPut(leafRec("作品A"))
        s.online?.reportSuccess()   // .online
        let root = s.levelFolders(path: "").map(\.path).sorted()
        XCTAssertEqual(root, ["series", "作品A", "作品B"].sorted())
        XCTAssertFalse(s.tileUnavailable(path: "作品B", episodeLike: true))
    }

    /// spec §9 fail-open — offline boot with NO visibility snapshot still
    /// lists downloaded works (fail-closed `isVisible` would hide all).
    /// The download filter stays the stricter gate: `series` (no
    /// downloads) stays hidden.
    func testOfflineFailOpenWithoutVisibilitySnapshot() async {
        let s = await loadedOfflineStore(offlineTreeJSON)
        s.offline?.testPut(leafRec("作品A"))
        s.visibility = VisibilityStore(apiProvider: { nil }, cacheDir:
            FileManager.default.temporaryDirectory
                .appendingPathComponent("vis-\(UUID().uuidString)",
                                        isDirectory: true))
        let root = s.levelFolders(path: "").map(\.path)
        XCTAssertTrue(root.contains("作品A"),
                      "no-snapshot offline must fail-open for downloads")
        XCTAssertTrue(root.contains("作品B"),
                      "same level: all episodes listed when any is downloaded")
        XCTAssertFalse(root.contains("series"),
                       "download filter still applies under fail-open")
    }

    /// spec §9: fail-open is ONLY for the no-snapshot case — with a cached
    /// snapshot, hidden roots still apply offline (fail-closed).
    func testOfflineSnapshotStillAppliesHiddenRoots() async throws {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("vis-\(UUID().uuidString)",
                                    isDirectory: true)
        try FileManager.default.createDirectory(
            at: dir, withIntermediateDirectories: true)
        try JSONSerialization.data(withJSONObject: [
            "v": 1, "owner": "o", "revision": 1, "hiddenRoots": ["作品B"],
        ]).write(to: dir.appendingPathComponent("o.json"))
        try "o".write(to: dir.appendingPathComponent("last-owner.txt"),
                      atomically: true, encoding: .utf8)
        let s = await loadedOfflineStore(offlineTreeJSON)
        s.offline?.testPut(leafRec("作品A"))
        s.offline?.testPut(leafRec("作品B"))
        s.visibility = VisibilityStore(apiProvider: { nil }, cacheDir: dir)
        XCTAssertEqual(s.levelFolders(path: "").map(\.path), ["作品A"],
                       "snapshot hidden roots stay fail-closed offline")
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

