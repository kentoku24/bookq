import XCTest
@testable import Bookq

/// VisibilityStore state-machine and path-matching tests — mirrors
/// browse/visibility.js semantics (hiddenMatch / isVisible / guardPath /
/// blockedState / fail-closed / presentation gating).
@MainActor
final class VisibilityStoreTests: XCTestCase {
    private var dir: URL!
    private var store: VisibilityStore!

    override func setUp() async throws {
        dir = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        store = VisibilityStore(apiProvider: { nil }, cacheDir: dir)
    }

    override func tearDown() async throws {
        try? FileManager.default.removeItem(at: dir)
    }

    /// Test hook — apply a server-style snapshot.
    private func apply(owner: String = "o1", rev: Int = 1, roots: [String]) {
        store.testApply(owner: owner, revision: rev, roots: roots)
    }

    // MARK: fail-closed (no snapshot → nothing visible, grid blocked)

    func testNoSnapshotBlocksGrid() {
        XCTAssertEqual(store.blockedState, .loading)  // idle → 読込中…
        XCTAssertFalse(store.isVisible("work"))
        XCTAssertEqual(store.guardPath("work"), .pending)
    }

    func testSnapshotUnblocksAndFilters() {
        apply(roots: ["secret"])
        XCTAssertEqual(store.blockedState, .none)
        XCTAssertTrue(store.isVisible("work"))
        XCTAssertFalse(store.isVisible("secret"))
        XCTAssertFalse(store.isVisible("secret/ch1"))
        XCTAssertEqual(store.guardPath("secret"), .hidden)
        XCTAssertEqual(store.guardPath("work"), .allow)
    }

    // MARK: hiddenMatch — covering longest / ancestor shortest, NFC+case

    func testHiddenMatchSelfDescendant() {
        apply(roots: ["A", "A/B"])
        let m1 = store.hiddenMatch("A")
        XCTAssertEqual(m1?.kind, .self_)
        let m2 = store.hiddenMatch("A/B/c")
        XCTAssertEqual(m2?.kind, .descendant)
        XCTAssertEqual(m2?.key, "A/B")        // longest covering key wins
    }

    func testHiddenMatchAncestor() {
        apply(roots: ["wrap/inner/work"])
        let m = store.hiddenMatch("wrap")
        XCTAssertEqual(m?.kind, .ancestor)
        XCTAssertEqual(m?.key, "wrap/inner/work")
        // ancestor match also not visible in normal mode
        XCTAssertFalse(store.isVisible("wrap"))
        // but isHidden (deny) does NOT cover ancestors
        XCTAssertFalse(store.isHidden("wrap"))
    }

    func testHiddenMatchCaseAndNFC() {
        apply(roots: ["Hide_Me", "é"])       // "é" NFC
        XCTAssertTrue(store.isHidden("hide_me"))
        XCTAssertTrue(store.isHidden("HIDE_ME/x"))
        // NFD form matches NFC key
        XCTAssertTrue(store.isHidden("e\u{301}"))
    }

    func testDotDotEscapesLibraryRoot() {
        apply(roots: ["a"])
        XCTAssertFalse(store.isHidden("../a"))
        XCTAssertNil(store.hiddenMatch("../a"))
    }

    // MARK: showAll

    func testShowAllRevealsEverything() {
        apply(roots: ["secret"])
        store.showAll = true
        XCTAssertTrue(store.isVisible("secret"))
        XCTAssertEqual(store.guardPath("secret"), .allow)
        // isHidden predicate still reports state (badges use it)
        XCTAssertTrue(store.isHidden("secret"))
    }

    // MARK: presentation — showAll only, editable rules

    func testPresentationHiddenInNormalMode() {
        apply(roots: ["secret"])
        XCTAssertNil(store.presentation("secret"))   // normal mode → null
    }

    func testPresentationShowAll() {
        apply(roots: ["A/B"])
        store.showAll = true
        let p1 = store.presentation("A/B", editable: true)
        XCTAssertEqual(p1?.badge, "非表示")
        XCTAssertTrue(p1?.dimmed == true)
        XCTAssertTrue(p1?.toggle == true)
        let p2 = store.presentation("A/B/c", editable: true)
        XCTAssertEqual(p2?.badge, "上位フォルダが非表示")
        let p3 = store.presentation("A", editable: true)
        XCTAssertEqual(p3?.badge, "配下に非表示")
        XCTAssertFalse(p3?.toggle ?? true)    // ancestors never editable
    }

    // MARK: snapshot adoption — monotonic revision, owner switch reset

    func testDownrevRejected() {
        apply(rev: 5, roots: ["a"])
        store.testApply(owner: "o1", revision: 3, roots: ["b"])
        XCTAssertTrue(store.isHidden("a"))
        XCTAssertFalse(store.isHidden("b"))
        store.testApply(owner: "o1", revision: 3, roots: ["b"], allowDownrev: true)
        XCTAssertTrue(store.isHidden("b"))
        XCTAssertFalse(store.isHidden("a"))
    }

    func testOwnerSwitchResetsMode() {
        apply(owner: "o1", roots: ["a"])
        store.showAll = true
        store.testApply(owner: "o2", revision: 0, roots: [])
        XCTAssertFalse(store.showAll)
        XCTAssertTrue(store.isVisible("a"))
    }

    // MARK: account switch (review fix — no cross-account leakage)

    /// accountChanged() drops the previous owner's entire snapshot —
    /// hidden roots, revision, showAll — and returns to fail-closed
    /// loading until the new account's first refresh.
    func testAccountChangedClearsPriorOwner() {
        apply(owner: "o1", rev: 3, roots: ["secret", "work/b"])
        store.showAll = true
        store.accountChanged()
        XCTAssertFalse(store.showAll)
        XCTAssertTrue(store.readOnly == false)
        XCTAssertEqual(store.status, .loading)
        // Old hidden roots must not hide (or expose) anything in the
        // pending state — guardPath reports pending, not hidden/allow.
        XCTAssertEqual(store.guardPath("secret"), .pending)
        XCTAssertFalse(store.isVisible("work"))
    }

    // MARK: cached restore

    func testCacheRestoreMarksReadonly() {
        store.testApply(owner: "o1", revision: 2, roots: ["x"])   // writes cache
        let s2 = VisibilityStore(apiProvider: { nil }, cacheDir: dir)
        XCTAssertEqual(s2.status, .readonly)
        XCTAssertTrue(s2.readOnly)
        XCTAssertTrue(s2.isHidden("x"))
        XCTAssertTrue(s2.hasSnapshot)
    }
}
