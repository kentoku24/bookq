import XCTest
@testable import Bookq

/// Mirrors the spread/chapter semantics of app.js + spread.js.
final class ReaderModelTests: XCTestCase {

    private func pages(_ folder: String, _ names: [String]) -> [PageRef] {
        names.map { PageRef(name: $0, folder: folder) }
    }

    private func chapter(_ name: String, _ path: String, _ n: Int) -> ReaderChapter {
        ReaderChapter(name: name, fullPath: path,
                      images: pages(path, (1...n).map { "p\($0).png" }))
    }

    private func node(_ name: String, _ children: [TreeNode] = []) -> TreeNode {
        TreeNode(name: name, type: "folder", children: children)
    }

    private func img(_ name: String) -> TreeNode {
        TreeNode(name: name, type: "image", children: nil)
    }

    // MARK: - spreads

    func testSingleModeOnePagePerSpread() {
        let w = ReaderWork.assemble(clickedPath: "a",
                                    chapters: [chapter("a", "a", 5)],
                                    pageMode: .single, spreadOffset: 0)
        XCTAssertEqual(w.spreads, [[0], [1], [2], [3], [4]])
        XCTAssertEqual(w.realCount, 5)
    }

    func testSpreadModeOffset0Pairs() {
        let w = ReaderWork.assemble(clickedPath: "a",
                                    chapters: [chapter("a", "a", 5)],
                                    pageMode: .spread, spreadOffset: 0)
        XCTAssertEqual(w.spreads, [[0, 1], [2, 3], [4]])
    }

    func testSpreadModeOffset1CoverSolo() {
        let w = ReaderWork.assemble(clickedPath: "a",
                                    chapters: [chapter("a", "a", 5)],
                                    pageMode: .spread, spreadOffset: 1)
        XCTAssertEqual(w.spreads, [[0], [1, 2], [3, 4]])
    }

    func testSpreadOfAndNavigation() {
        let w = ReaderWork.assemble(clickedPath: "a",
                                    chapters: [chapter("a", "a", 5)],
                                    pageMode: .spread, spreadOffset: 0)
        XCTAssertEqual(w.spreadOf(3), 1)
        XCTAssertEqual(w.nextSpreadFirstPage(after: 1), 2)
        XCTAssertEqual(w.prevSpreadFirstPage(before: 2), 0)
        XCTAssertNil(w.nextSpreadFirstPage(after: 4))
        XCTAssertNil(w.prevSpreadFirstPage(before: 0))
    }

    // MARK: - chapters + spacers

    func testCrossChapterBoundariesEvenNoSpacers() {
        // chapter0 has 4 images → chapter1 already aligned (even) in offset0
        let w = ReaderWork.assemble(clickedPath: "w/01",
                                    chapters: [chapter("01", "w/01", 4),
                                               chapter("02", "w/02", 3)],
                                    pageMode: .spread, spreadOffset: 0)
        XCTAssertEqual(w.boundaries, [0, 4])
        XCTAssertEqual(w.images.count, 7)
        XCTAssertEqual(w.images.filter(\.isSpacer).count, 0)
        XCTAssertEqual(w.spreads, [[0, 1], [2, 3], [4, 5], [6]])
    }

    func testCrossChapterSpacerPadsOddBoundary() {
        // chapter0 has 3 → chapter1 starts at idx 3 (odd) → 1 spacer pad
        let w = ReaderWork.assemble(clickedPath: "w/01",
                                    chapters: [chapter("01", "w/01", 3),
                                               chapter("02", "w/02", 4)],
                                    pageMode: .spread, spreadOffset: 0)
        XCTAssertEqual(w.images.count, 8)          // +1 spacer
        XCTAssertTrue(w.images[3].isSpacer)
        XCTAssertEqual(w.boundaries, [0, 4])
        XCTAssertEqual(w.spreads, [[0, 1], [2], [4, 5], [6, 7]])
        XCTAssertEqual(w.realCount, 7)
        XCTAssertEqual(w.realIndex(at: 4), 3)
    }

    func testCrossChapterOffset1Chapter0GetsLeadingSpacer() {
        // web quirk preserved: pad=(0-1+2)%2=1 → cover is NOT solo in
        // cross-chapter mode; chapters start on pair boundaries instead.
        let w = ReaderWork.assemble(clickedPath: "w/01",
                                    chapters: [chapter("01", "w/01", 3),
                                               chapter("02", "w/02", 2)],
                                    pageMode: .spread, spreadOffset: 1)
        // c0: startIdx=0 → pad 1; c1: startIdx=4 → pad (4-1+2)%2=1
        // images: [S, r0, r1, r2, S, s0, s1]
        XCTAssertTrue(w.images[0].isSpacer)
        XCTAssertTrue(w.images[4].isSpacer)
        XCTAssertEqual(w.boundaries, [1, 5])
        // groups: [0]=spacer dropped, (1,2), (3,4)→4 spacer→[3], (5,6)
        XCTAssertEqual(w.spreads, [[1, 2], [3], [5, 6]])
    }

    func testChapterInfoLocalIndex() {
        let w = ReaderWork.assemble(clickedPath: "w/01",
                                    chapters: [chapter("01", "w/01", 3),
                                               chapter("02", "w/02", 4)],
                                    pageMode: .spread, spreadOffset: 0)
        // boundaries [0,4] (1 spacer); global 5 → chapter1 local 1
        let info = w.chapterInfo(at: 5)
        XCTAssertEqual(info.chapterIdx, 1)
        XCTAssertEqual(info.name, "02")
        XCTAssertEqual(info.localIdx, 1)
        // global 2 → chapter0 local 2
        XCTAssertEqual(w.chapterInfo(at: 2).localIdx, 2)
    }

    func testResumeGlobalIndexChapterLocal() {
        let w = ReaderWork.assemble(clickedPath: "w/01",
                                    chapters: [chapter("01", "w/01", 3),
                                               chapter("02", "w/02", 4)],
                                    pageMode: .spread, spreadOffset: 0)
        // saved.page=2 on chapter1 → boundaries[1]+2 = 6
        XCTAssertEqual(w.resumeGlobalIndex(chapterIdx: 1, localIdx: 2), 6)
        // out-of-range clamps to chapter start
        XCTAssertEqual(w.resumeGlobalIndex(chapterIdx: 1, localIdx: 99), 4)
        XCTAssertEqual(w.resumeGlobalIndex(chapterIdx: 0, localIdx: -1), 0)
    }

    // MARK: - siblingLeaves + chapters

    func testSiblingLeavesFindsParentChildren() {
        // parent "w" holds leaf folders 01/02 plus a subfolder
        let tree = [node("w", [
            node("01", [img("p1.png"), img("p2.png")]),
            node("02", [img("p1.png")]),
            node("sub", [node("x", [img("a.png")])]),
        ])]
        let sibs = ReaderWork.siblingLeaves(of: "w/01", in: tree)
        XCTAssertEqual(sibs?.map(\.path), ["w/01", "w/02", "w/sub"])
        let (list, cross) = ReaderWork.chapters(for: "w/01", in: tree)
        XCTAssertTrue(cross)
        XCTAssertEqual(list.map(\.fullPath), ["w/01", "w/02"])
        XCTAssertEqual(list[0].images.count, 2)
    }

    func testTopLevelNodeHasNoSiblings() {
        let tree = [node("a", [img("p.png")]), node("b", [img("q.png")])]
        XCTAssertNil(ReaderWork.siblingLeaves(of: "a", in: tree))
        let (list, cross) = ReaderWork.chapters(for: "a", in: tree)
        XCTAssertFalse(cross)
        XCTAssertEqual(list.count, 1)
        XCTAssertEqual(list[0].images.count, 1)
    }

    func testMixedSelfFolderDisablesCross() {
        // "w/01" holds an image AND a subfolder → mixed → single chapter
        let tree = [node("w", [
            node("01", [img("p1.png"), node("extra", [img("e.png")])]),
            node("02", [img("p1.png")]),
        ])]
        let (list, cross) = ReaderWork.chapters(for: "w/01", in: tree)
        XCTAssertFalse(cross)
        XCTAssertEqual(list[0].images.count, 1)   // direct images only
    }

    // MARK: - tap zones (test_viewer_tapmode: L/R modes + center chrome)

    func testZoneActionLeftMode() {
        XCTAssertEqual(zoneAction(frac: 0.10, tapForwardLeft: true), .turn(1))
        XCTAssertEqual(zoneAction(frac: 0.90, tapForwardLeft: true), .turn(-1))
    }

    func testZoneActionRightMode() {
        XCTAssertEqual(zoneAction(frac: 0.10, tapForwardLeft: false), .turn(-1))
        XCTAssertEqual(zoneAction(frac: 0.90, tapForwardLeft: false), .turn(1))
    }

    func testZoneActionCenterTogglesChrome() {
        XCTAssertEqual(zoneAction(frac: 0.50, tapForwardLeft: true), .toggleChrome)
        XCTAssertEqual(zoneAction(frac: 0.50, tapForwardLeft: false), .toggleChrome)
    }

    /// Boundary taps land on the declared 30%/70% edges — a tap exactly on
    /// the line goes to the outer side of the band it belongs to.
    func testZoneActionBoundaries() {
        XCTAssertEqual(zoneAction(frac: 0.29, tapForwardLeft: true), .turn(1))
        XCTAssertEqual(zoneAction(frac: 0.71, tapForwardLeft: true), .turn(-1))
        XCTAssertEqual(zoneAction(frac: 0.30, tapForwardLeft: true), .toggleChrome)
        XCTAssertEqual(zoneAction(frac: 0.70, tapForwardLeft: true), .toggleChrome)
    }
}
