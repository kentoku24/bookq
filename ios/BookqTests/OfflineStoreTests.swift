import XCTest
@testable import Bookq

/// OfflineStore state-machine and path tests — mirrors storeAdapter.js
/// semantics (getOfflineBtnState / isFolderOffline / partialProgress).
@MainActor
final class OfflineStoreTests: XCTestCase {
    private var root: URL!
    private var store: OfflineStore!

    override func setUp() async throws {
        root = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        store = OfflineStore(apiProvider: { nil }, root: root)
    }

    override func tearDown() async throws {
        try? FileManager.default.removeItem(at: root)
    }

    private func putRecord(_ leaf: String, cached: Int, total: Int,
                         names: [String] = []) {
        store.testPut(OfflineStore.LeafRecord(
            folder: leaf, pageCount: total, cachedCount: cached, bytes: 0,
            thumb: names.first, names: names, addedAt: 1, lastReadAt: 1))
    }

    // MARK: buttonState ordering (confirm > deleting > stopping >
    // downloading > partial > complete > idle)

    func testIdleByDefault() {
        XCTAssertEqual(store.buttonState("a", leaves: ["a"]), .idle)
    }

    func testCompleteWhenRecordFull() {
        putRecord("a", cached: 3, total: 3)
        XCTAssertEqual(store.buttonState("a", leaves: ["a"]), .complete)
    }

    func testPartialWhenRecordShort() {
        putRecord("a", cached: 2, total: 3)
        XCTAssertEqual(store.buttonState("a", leaves: ["a"]), .partial)
    }

    func testMultiLeafCompleteOnlyWhenAllRecorded() {
        let leaves = ["w/ch1", "w/ch2"]
        putRecord("w/ch1", cached: 2, total: 2)
        // one leaf missing → partial (aggregated pct 2/4)
        putRecord("w/ch2", cached: 1, total: 2)
        XCTAssertEqual(store.buttonState("w", leaves: leaves), .partial)
        putRecord("w/ch2", cached: 2, total: 2)
        XCTAssertEqual(store.buttonState("w", leaves: leaves), .complete)
    }

    func testParentFolderNotOfflineWithoutAllLeaves() {
        putRecord("w/ch1", cached: 1, total: 1)
        XCTAssertFalse(store.isFolderOffline("w", leaves: ["w/ch1", "w/ch2"]))
        putRecord("w/ch2", cached: 1, total: 1)
        XCTAssertTrue(store.isFolderOffline("w", leaves: ["w/ch1", "w/ch2"]))
    }

    /// Web: om[folder] directly true when a record exists for that key.
    func testExactKeyRecordIsOffline() {
        putRecord("a", cached: 1, total: 3)
        XCTAssertTrue(store.isFolderOffline("a", leaves: ["a"]))
    }

    // MARK: partialProgress aggregates leaf-most leaves

    func testPartialProgressSkipsAncestorLeaves() {
        // "w" is an ancestor of "w/ch1" — only leaf-most counted.
        putRecord("w", cached: 2, total: 2)
        putRecord("w/ch1", cached: 1, total: 2)
        let p = store.partialProgress(leaves: ["w", "w/ch1"])
        XCTAssertEqual(p.cached, 1)
        XCTAssertEqual(p.total, 2)
    }

    // MARK: paths

    func testDirURLEncodesSlashes() {
        let u = store.dirURL(for: "作品B/第1話")
        XCTAssertFalse(u.lastPathComponent.contains("/"))
        XCTAssertTrue(u.lastPathComponent.contains("%"))
    }

    func testRootLeafDir() {
        XCTAssertEqual(store.dirURL(for: "").lastPathComponent, "__root__")
    }

    func testLocalFileURLRoundTrip() throws {
        let leaf = "作品B/第1話"
        try FileManager.default.createDirectory(
            at: store.dirURL(for: leaf), withIntermediateDirectories: true)
        let f = store.fileURL(folder: leaf, name: "p1.jpg")
        try Data([1, 2, 3]).write(to: f)
        XCTAssertEqual(store.localFileURL(folder: leaf, name: "p1.jpg"), f)
        XCTAssertNil(store.localFileURL(folder: leaf, name: "p9.jpg"))
    }

    // MARK: remote URL → local mapping (/view, /image, /thumb)

    func testRemoteURLMapping() throws {
        let leaf = "作品B/第1話"
        try FileManager.default.createDirectory(
            at: store.dirURL(for: leaf), withIntermediateDirectories: true)
        let f = store.fileURL(folder: leaf, name: "p 1.jpg")
        try Data([1]).write(to: f)
        let base = URL(string: "https://x.example")!
        for prefix in ["/view/", "/image/", "/thumb/"] {
            let url = URL(string: prefix +
                          "作品B/第1話/p%201.jpg", relativeTo: base)!
            XCTAssertEqual(store.localFileURL(remoteURL: url), f)
        }
    }

    // MARK: downloadedNames filters to existing files

    func testDownloadedNamesIntersection() throws {
        let leaf = "w/ch1"
        try FileManager.default.createDirectory(
            at: store.dirURL(for: leaf), withIntermediateDirectories: true)
        try Data([1]).write(to: store.fileURL(folder: leaf, name: "a.jpg"))
        putRecord(leaf, cached: 1, total: 2, names: ["a.jpg", "b.jpg"])
        XCTAssertEqual(store.downloadedNames(leaf: leaf), ["a.jpg"])
        XCTAssertNil(store.downloadedNames(leaf: "w/ch2"))
    }

    // MARK: confirm arm consumes second tap

    func testArmConfirmSecondTapFires() {
        var fired = 0
        store.armConfirm("a") { fired += 1 }
        XCTAssertEqual(store.buttonState("a", leaves: ["a"]), .confirmDelete)
        store.armConfirm("a") { fired += 1 }
        XCTAssertEqual(fired, 1)
        XCTAssertEqual(store.buttonState("a", leaves: ["a"]), .idle)
    }
}
