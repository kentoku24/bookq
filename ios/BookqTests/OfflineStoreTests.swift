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

    /// Review fix — nested archive page: `work.cbz/sub/p1.jpg` must
    /// resolve via the longest recorded leaf prefix, not the naive
    /// last-component split (which would look in leaf "work.cbz/sub").
    func testRemoteURLNestedInnerPath() throws {
        let leaf = "work.cbz"
        putRecord(leaf, cached: 1, total: 1, names: ["sub/p1.jpg"])
        let nested = store.dirURL(for: leaf).appendingPathComponent("sub")
        try FileManager.default.createDirectory(
            at: nested, withIntermediateDirectories: true)
        let f = store.fileURL(folder: leaf, name: "sub/p1.jpg")
        try Data([1]).write(to: f)
        let url = URL(string: "https://x.example/view/work.cbz/sub/p1.jpg")!
        XCTAssertEqual(store.localFileURL(remoteURL: url), f)
        // And the naive split must NOT accidentally resolve elsewhere.
        XCTAssertNil(store.localFileURL(folder: "work.cbz/sub", name: "p1.jpg"))
    }

    /// Review fix — records/files are namespaced per server+account:
    /// a scope switch exposes an empty store, not another user's data.
    func testScopeIsolatesRecordsAndFiles() throws {
        var scope = "h1|a@x"
        let s1 = OfflineStore(apiProvider: { nil },
                              scopeProvider: { scope }, root: root)
        s1.testPut(OfflineStore.LeafRecord(
            folder: "w", pageCount: 1, cachedCount: 1, bytes: 1,
            thumb: nil, names: ["p1.jpg"], addedAt: 1, lastReadAt: 1))
        XCTAssertEqual(s1.records.count, 1)
        // Same root, different account → nothing visible.
        let s2 = OfflineStore(apiProvider: { nil },
                              scopeProvider: { "h1|b@x" }, root: root)
        XCTAssertTrue(s2.records.isEmpty)
        // Same account, different server → also isolated.
        let s3 = OfflineStore(apiProvider: { nil },
                              scopeProvider: { "h2|a@x" }, root: root)
        XCTAssertTrue(s3.records.isEmpty)
        // scopeChanged() on a live store drops in-memory state and
        // reloads the new scope's records.
        scope = "h1|b@x"
        s1.scopeChanged()
        XCTAssertTrue(s1.records.isEmpty)
    }

    // MARK: legacy migration (review fix — pre-scope data must not vanish)

    /// Pre-scoping releases stored works.json + leaf dirs at root; the
    /// first signed-in scope must adopt them so pre-update downloads
    /// stay visible and readable.
    func testLegacyDataMigratesIntoScope() throws {
        let leaf = "w/ch1"
        var cs = CharacterSet.alphanumerics
        cs.insert(charactersIn: "-_.")
        let enc = leaf.addingPercentEncoding(withAllowedCharacters: cs)!
        let legacyDir = root.appendingPathComponent(enc, isDirectory: true)
        try FileManager.default.createDirectory(
            at: legacyDir, withIntermediateDirectories: true)
        try Data([1]).write(to: legacyDir.appendingPathComponent("p1.jpg"))
        let rec = OfflineStore.LeafRecord(
            folder: leaf, pageCount: 1, cachedCount: 1, bytes: 1,
            thumb: nil, names: ["p1.jpg"], addedAt: 1, lastReadAt: 1)
        try JSONEncoder().encode([leaf: rec])
            .write(to: root.appendingPathComponent("works.json"))
        // A foreign scope dir must NOT be swallowed by the migration.
        let foreign = root.appendingPathComponent("h2%7Cb%40x", isDirectory: true)
        try FileManager.default.createDirectory(
            at: foreign, withIntermediateDirectories: true)

        let s = OfflineStore(apiProvider: { nil },
                             scopeProvider: { "h1|a@x" }, root: root)
        XCTAssertEqual(s.records[leaf]?.cachedCount, 1)
        XCTAssertNotNil(s.localFileURL(folder: leaf, name: "p1.jpg"))
        XCTAssertFalse(FileManager.default.fileExists(
            atPath: root.appendingPathComponent("works.json").path))
        XCTAssertTrue(FileManager.default.fileExists(atPath: foreign.path))
    }

    /// The anonymous scope must not claim a signed-in library — a
    /// logged-out boot leaves legacy data in place until the real
    /// account arrives.
    func testLegacyNotMigratedForAnonScope() throws {
        try FileManager.default.createDirectory(
            at: root, withIntermediateDirectories: true)
        let rec = OfflineStore.LeafRecord(
            folder: "w", pageCount: 1, cachedCount: 1, bytes: 1,
            thumb: nil, names: ["p1.jpg"], addedAt: 1, lastReadAt: 1)
        try JSONEncoder().encode(["w": rec])
            .write(to: root.appendingPathComponent("works.json"))
        _ = OfflineStore(apiProvider: { nil },
                         scopeProvider: { "h1|anon" }, root: root)
        XCTAssertTrue(FileManager.default.fileExists(
            atPath: root.appendingPathComponent("works.json").path))
    }

    /// Review fix — a delete issued just before a scope switch must
    /// remove THIS scope's files only; the next scope's same-named work
    /// and its record survive.
    func testDeleteDuringScopeSwitchKeepsNewScopeFiles() async throws {
        var scope = "h|a@x"
        let s = OfflineStore(apiProvider: { nil },
                             scopeProvider: { scope }, root: root)
        let leaf = "w"
        try FileManager.default.createDirectory(
            at: s.dirURL(for: leaf), withIntermediateDirectories: true)
        try Data([1]).write(to: s.fileURL(folder: leaf, name: "p1.jpg"))
        s.testPut(OfflineStore.LeafRecord(
            folder: leaf, pageCount: 1, cachedCount: 1, bytes: 1,
            thumb: nil, names: ["p1.jpg"], addedAt: 1, lastReadAt: 1))
        let dirA = s.dirURL(for: leaf)
        s.delete(path: leaf, leaves: [leaf])
        // Switch before the delete task's filesystem pass runs.
        scope = "h|b@x"
        s.scopeChanged()
        let dirB = s.dirURL(for: leaf)
        try FileManager.default.createDirectory(
            at: dirB, withIntermediateDirectories: true)
        try Data([2]).write(to: dirB.appendingPathComponent("p1.jpg"))
        s.testPut(OfflineStore.LeafRecord(
            folder: leaf, pageCount: 1, cachedCount: 1, bytes: 1,
            thumb: nil, names: ["p1.jpg"], addedAt: 1, lastReadAt: 1))
        try await Task.sleep(nanoseconds: 300_000_000)
        XCTAssertFalse(FileManager.default.fileExists(atPath: dirA.path))
        XCTAssertTrue(FileManager.default.fileExists(
            atPath: dirB.appendingPathComponent("p1.jpg").path))
        XCTAssertNotNil(s.records[leaf])
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
