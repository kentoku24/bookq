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
        // Padding contract (#441, web parity): pad=(0-1+2)%2=1 → cover is
        // NOT solo in cross-chapter mode; chapters start on pair
        // boundaries instead.
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

    func testCrossChapterThreeChaptersCumulativePadsOffset0() {
        // #441: cumulative pads (3/3/2 pages, offset 0) — the old web
        // double-count dropped the second spacer (boundaries [0,4,7]).
        // Contract: spacers before c1 and c2 → boundaries [0,4,8].
        let w = ReaderWork.assemble(clickedPath: "w/01",
                                    chapters: [chapter("01", "w/01", 3),
                                               chapter("02", "w/02", 3),
                                               chapter("03", "w/03", 2)],
                                    pageMode: .spread, spreadOffset: 0)
        XCTAssertEqual(w.boundaries, [0, 4, 8])
        XCTAssertTrue(w.images[3].isSpacer)
        XCTAssertTrue(w.images[7].isSpacer)
        XCTAssertEqual(w.images.count, 10)
        XCTAssertEqual(w.spreads, [[0, 1], [2], [4, 5], [6], [8, 9]])
    }

    func testCrossChapterThreeChaptersCumulativePadsOffset1() {
        // #441: cumulative pads (3/3/2 pages, offset 1) — leading spacer
        // plus spacers before c1 and c2 → boundaries [1,5,9].
        let w = ReaderWork.assemble(clickedPath: "w/01",
                                    chapters: [chapter("01", "w/01", 3),
                                               chapter("02", "w/02", 3),
                                               chapter("03", "w/03", 2)],
                                    pageMode: .spread, spreadOffset: 1)
        XCTAssertEqual(w.boundaries, [1, 5, 9])
        XCTAssertTrue(w.images[0].isSpacer)
        XCTAssertTrue(w.images[4].isSpacer)
        XCTAssertTrue(w.images[8].isSpacer)
        XCTAssertEqual(w.images.count, 11)
        XCTAssertEqual(w.spreads, [[1, 2], [3], [5, 6], [7], [9, 10]])
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
        // parent "g/w" holds leaf folders 01/02 plus a subfolder.
        // Depth >= 2 required (SIBLING_AUTOLOAD_MIN_DEPTH parity, #274):
        // the same shape at depth 1 ("w/01") returns nil — see
        // testShallowDepthHasNoSiblings.
        let tree = [node("g", [
            node("w", [
                node("01", [img("p1.png"), img("p2.png")]),
                node("02", [img("p1.png")]),
                node("sub", [node("x", [img("a.png")])]),
            ]),
        ])]
        let sibs = ReaderWork.siblingLeaves(of: "g/w/01", in: tree)
        XCTAssertEqual(sibs?.map(\.path), ["g/w/01", "g/w/02", "g/w/sub"])
        let (list, cross) = ReaderWork.chapters(for: "g/w/01", in: tree)
        XCTAssertTrue(cross)
        XCTAssertEqual(list.map(\.fullPath), ["g/w/01", "g/w/02"])
        XCTAssertEqual(list[0].images.count, 2)
    }

    func testShallowDepthHasNoSiblings() {
        // Raw depth < 2 never concatenates (web/server parity, #274) even
        // when leaf siblings exist side by side.
        let tree = [node("w", [
            node("01", [img("p1.png"), img("p2.png")]),
            node("02", [img("p1.png")]),
        ])]
        XCTAssertNil(ReaderWork.siblingLeaves(of: "w/01", in: tree))
        let (list, cross) = ReaderWork.chapters(for: "w/01", in: tree)
        XCTAssertFalse(cross)
        XCTAssertEqual(list.map(\.fullPath), ["w/01"])
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

    // MARK: - chapter spread range (getChapterSpreadRange)

    func testChapterSpreadRangeSingleChapterIsFullBook() {
        let w = ReaderWork.assemble(clickedPath: "a",
                                    chapters: [chapter("a", "a", 5)],
                                    pageMode: .spread, spreadOffset: 0)
        // boundaries.count <= 1 → the whole book is the clamp range.
        XCTAssertEqual(w.chapterSpreadRange(0).lo, 0)
        XCTAssertEqual(w.chapterSpreadRange(0).hi, 2)
    }

    func testChapterSpreadRangeCrossChapter() {
        // ch0: 3 imgs / ch1: 4 imgs, offset0 → 1 spacer before ch1.
        // spreads [[0,1],[2],[4,5],[6,7]] — ch0 owns spreads 0-1,
        // ch1 owns 2-3 (spreads never mix chapters).
        let w = ReaderWork.assemble(clickedPath: "w/01",
                                    chapters: [chapter("01", "w/01", 3),
                                               chapter("02", "w/02", 4)],
                                    pageMode: .spread, spreadOffset: 0)
        let r0 = w.chapterSpreadRange(0)
        XCTAssertEqual(r0.lo, 0)
        XCTAssertEqual(r0.hi, 1)
        let r1 = w.chapterSpreadRange(1)
        XCTAssertEqual(r1.lo, 2)
        XCTAssertEqual(r1.hi, 3)
    }

    func testChapterSpreadRangeLeadingSpacerOffset1() {
        // offset1 cross-chapter quirk: leading spacer + boundary spacer.
        // spreads [[1,2],[3],[5,6]] — ch0 = 0-1, ch1 = 2 only.
        let w = ReaderWork.assemble(clickedPath: "w/01",
                                    chapters: [chapter("01", "w/01", 3),
                                               chapter("02", "w/02", 2)],
                                    pageMode: .spread, spreadOffset: 1)
        let r0 = w.chapterSpreadRange(0)
        XCTAssertEqual(r0.lo, 0)
        XCTAssertEqual(r0.hi, 1)
        let r1 = w.chapterSpreadRange(1)
        XCTAssertEqual(r1.lo, 2)
        XCTAssertEqual(r1.hi, 2)
    }

    // MARK: - chapter-local slider (web syncChapterSlider parity)

    func testChapterSliderSingleChapterFullBook() {
        // Single chapter: the slider spans the whole book, as before.
        // spreads [[0,1],[2,3],[4]] → span 2.
        let w = ReaderWork.assemble(clickedPath: "a",
                                    chapters: [chapter("a", "a", 5)],
                                    pageMode: .spread, spreadOffset: 0)
        XCTAssertEqual(w.chapterSliderMax(at: 0), 2)
        XCTAssertEqual(w.idxToChapterSlider(0), 0)
        XCTAssertEqual(w.idxToChapterSlider(4), 2)
        XCTAssertEqual(w.chapterSliderToIdx(0, at: 0), 0)
        XCTAssertEqual(w.chapterSliderToIdx(1, at: 0), 2)
        XCTAssertEqual(w.chapterSliderToIdx(2, at: 0), 4)
    }

    func testChapterSliderCrossChapterScoped() {
        // ch0: 3 imgs / ch1: 4 imgs, offset0 → 1 spacer before ch1.
        // spreads [[0,1],[2],[4,5],[6,7]] — ch0 owns spreads 0-1,
        // ch1 owns 2-3 (boundaries [0,4]).
        let w = ReaderWork.assemble(clickedPath: "w/01",
                                    chapters: [chapter("01", "w/01", 3),
                                               chapter("02", "w/02", 4)],
                                    pageMode: .spread, spreadOffset: 0)
        // ch0 side: span covers only its 2 spreads.
        XCTAssertEqual(w.chapterSliderMax(at: 2), 1)
        XCTAssertEqual(w.idxToChapterSlider(0), 0)
        XCTAssertEqual(w.idxToChapterSlider(2), 1)
        XCTAssertEqual(w.chapterSliderToIdx(0, at: 2), 0)
        XCTAssertEqual(w.chapterSliderToIdx(1, at: 2), 2)
        // ch1 side: global idx 5 → chapter1 local spread 0, idx 6 → 1.
        XCTAssertEqual(w.chapterSliderMax(at: 5), 1)
        XCTAssertEqual(w.idxToChapterSlider(5), 0)
        XCTAssertEqual(w.idxToChapterSlider(6), 1)
        XCTAssertEqual(w.chapterSliderToIdx(0, at: 5), 4)
        XCTAssertEqual(w.chapterSliderToIdx(1, at: 5), 6)
    }

    func testChapterSliderToIdxClampsToChapterNotBook() {
        // The whole point of the change: a slider value can never resolve
        // into a sibling chapter — overflow clamps to the chapter's own
        // hi, underflow to its own lo.
        let w = ReaderWork.assemble(clickedPath: "w/01",
                                    chapters: [chapter("01", "w/01", 3),
                                               chapter("02", "w/02", 4)],
                                    pageMode: .spread, spreadOffset: 0)
        // at ch0 (idx 2): val 99 → ch0's last spread first page (2),
        // never ch1's first page (4).
        XCTAssertEqual(w.chapterSliderToIdx(99, at: 2), 2)
        XCTAssertEqual(w.chapterSliderToIdx(-9, at: 5), 4)
        // idxToChapterSlider on a spacer global index stays in range.
        XCTAssertEqual(w.idxToChapterSlider(3), 0)
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

    // MARK: - page-turn drag math (custom ZStack pager)

    func testDragDirectionWaitsForThreshold() {
        XCTAssertNil(PageTurnMath.dragDirection(dx: 5, dy: 0))
        XCTAssertNil(PageTurnMath.dragDirection(dx: -5, dy: 0))
        XCTAssertNil(PageTurnMath.dragDirection(dx: PageTurnMath.decideDistance,
                                                dy: 0))
    }

    func testDragDirectionRejectsVerticalDominant() {
        // |dx| > decideDistance but |dy| larger → still undecided.
        XCTAssertNil(PageTurnMath.dragDirection(dx: 20, dy: 30))
        XCTAssertNil(PageTurnMath.dragDirection(dx: -20, dy: -40))
    }

    func testDragDirectionRightIsForward() {
        // Manga/RTL: drag right = next spread (current slides right).
        XCTAssertEqual(PageTurnMath.dragDirection(dx: 20, dy: 5), 1)
        XCTAssertEqual(PageTurnMath.dragDirection(dx: -20, dy: 5), -1)
    }

    func testDragDeltaLinearToViewport() {
        XCTAssertEqual(PageTurnMath.dragDelta(dx: 100, viewWidth: 400), 0.25,
                       accuracy: 0.0001)
        XCTAssertEqual(PageTurnMath.dragDelta(dx: 400, viewWidth: 400), 1.0,
                       accuracy: 0.0001)
        XCTAssertEqual(PageTurnMath.dragDelta(dx: 0, viewWidth: 400), 0)
    }

    func testDragDeltaSignedBothDirections() {
        // Odd-symmetric: left drags retreat pos (backward turn).
        XCTAssertEqual(PageTurnMath.dragDelta(dx: -100, viewWidth: 400), -0.25,
                       accuracy: 0.0001)
        XCTAssertEqual(PageTurnMath.dragDelta(dx: -400, viewWidth: 400), -1.0,
                       accuracy: 0.0001)
    }

    func testDragDeltaDampsOvershoot() {
        // Past one width the delta keeps growing but at 30% rate.
        let d = PageTurnMath.dragDelta(dx: 800, viewWidth: 400)
        XCTAssertEqual(d, 1 + (2 - 1) * PageTurnMath.overshootDamping,
                       accuracy: 0.0001)
        XCTAssertGreaterThan(d, 1)
        // Symmetric on the backward side.
        XCTAssertEqual(PageTurnMath.dragDelta(dx: -800, viewWidth: 400), -d,
                       accuracy: 0.0001)
    }

    func testRangeClampBookEdges() {
        // Book bounds are just rangeClamp(0, count-1) — the single-
        // chapter case of the chapter-range clamp.
        XCTAssertEqual(PageTurnMath.rangeClamp(2.5, lo: 0, hi: 5), 2.5)
        XCTAssertEqual(PageTurnMath.rangeClamp(-1, lo: 0, hi: 5),
                       -PageTurnMath.edgeRubber, accuracy: 0.0001)
        XCTAssertEqual(PageTurnMath.rangeClamp(6, lo: 0, hi: 5),
                       5 + PageTurnMath.edgeRubber, accuracy: 0.0001)
        XCTAssertEqual(PageTurnMath.rangeClamp(-0.5, lo: 0, hi: 5),
                       -0.5 * PageTurnMath.edgeRubber, accuracy: 0.0001)
    }

    func testRangeClampArbitraryBounds() {
        // Chapter-bound rubber band: inside [lo,hi] passthrough, overshoot
        // damped toward the bound (web dragPx *= 0.35 at chapter edge).
        XCTAssertEqual(PageTurnMath.rangeClamp(3.5, lo: 2, hi: 5), 3.5)
        XCTAssertEqual(PageTurnMath.rangeClamp(1.0, lo: 2, hi: 5),
                       2 - PageTurnMath.edgeRubber, accuracy: 0.0001)
        XCTAssertEqual(PageTurnMath.rangeClamp(6.0, lo: 2, hi: 5),
                       5 + PageTurnMath.edgeRubber, accuracy: 0.0001)
        // Inverted/empty ranges collapse to a point; overshoot still
        // rubber-bands on both sides of it.
        XCTAssertEqual(PageTurnMath.rangeClamp(3.0, lo: 4, hi: 2),
                       4 - PageTurnMath.edgeRubber, accuracy: 0.0001)
        XCTAssertEqual(PageTurnMath.rangeClamp(5.0, lo: 4, hi: 2),
                       4 + PageTurnMath.edgeRubber, accuracy: 0.0001)
    }

    func testCommitDirectionOnFlick() {
        // Predicted end ≥ half the viewport commits even on short travel.
        XCTAssertEqual(PageTurnMath.commitDirection(translation: 10,
                                                    predicted: 200,
                                                    viewWidth: 400), 1)
        XCTAssertEqual(PageTurnMath.commitDirection(translation: -10,
                                                    predicted: -200,
                                                    viewWidth: 400), -1)
    }

    func testCommitDirectionOnDistance() {
        // Deliberate drag past the web touchend threshold
        // `min(10% width, 70)` commits even without a flick.
        XCTAssertEqual(PageTurnMath.commitDirection(translation: 140,
                                                    predicted: 100,
                                                    viewWidth: 400), 1)
        XCTAssertEqual(PageTurnMath.commitDirection(translation: -140,
                                                    predicted: -100,
                                                    viewWidth: 400), -1)
        XCTAssertNil(PageTurnMath.commitDirection(translation: 30,
                                                  predicted: 30,
                                                  viewWidth: 400))
    }

    func testCommitDirectionThresholdMatchesWeb() {
        // web touchend: min(innerWidth * 0.10, 70) — 10% below 700pt,
        // capped at 70pt above it.
        XCTAssertEqual(PageTurnMath.commitDirection(translation: 40,
                                                    predicted: 0,
                                                    viewWidth: 400), 1)
        XCTAssertNil(PageTurnMath.commitDirection(translation: 39,
                                                  predicted: 0,
                                                  viewWidth: 400))
        XCTAssertEqual(PageTurnMath.commitDirection(translation: 70,
                                                    predicted: 0,
                                                    viewWidth: 800), 1)
        XCTAssertNil(PageTurnMath.commitDirection(translation: 69,
                                                  predicted: 0,
                                                  viewWidth: 800))
    }

    func testCommitDirectionRejectsZeroWidth() {
        XCTAssertNil(PageTurnMath.commitDirection(translation: 100,
                                                  predicted: 500,
                                                  viewWidth: 0))
    }

    // MARK: - depth scrim (coveredFraction)

    func testCoveredFractionZeroAtRest() {
        // Integer pos = settled spread — nothing is covered.
        XCTAssertEqual(PageTurnMath.coveredFraction(0, count: 6), 0)
        XCTAssertEqual(PageTurnMath.coveredFraction(2, count: 6), 0)
        XCTAssertEqual(PageTurnMath.coveredFraction(5, count: 6), 0)
    }

    func testCoveredFractionForwardTurn() {
        // pos = i+0.3 forward: the revealed next spread is still 70%
        // covered → scrim fraction 0.7, brightening toward 0 at i+1.
        XCTAssertEqual(PageTurnMath.coveredFraction(2.3, count: 6), 0.7,
                       accuracy: 0.0001)
        XCTAssertEqual(PageTurnMath.coveredFraction(2.9, count: 6), 0.1,
                       accuracy: 0.0001)
    }

    func testCoveredFractionBackwardTurn() {
        // pos = i-0.3 written as 2.7 (floor 2 sliding in on top):
        // the current spread is 30% covered → fraction 0.3.
        XCTAssertEqual(PageTurnMath.coveredFraction(2.7, count: 6), 0.3,
                       accuracy: 0.0001)
        XCTAssertEqual(PageTurnMath.coveredFraction(2.1, count: 6), 0.9,
                       accuracy: 0.0001)
    }

    func testCoveredFractionEdgesAndOvershoot() {
        // No covered page at the book edges or while rubber-banding past.
        XCTAssertEqual(PageTurnMath.coveredFraction(-0.4, count: 6), 0)
        XCTAssertEqual(PageTurnMath.coveredFraction(0.5, count: 1), 0)
        XCTAssertEqual(PageTurnMath.coveredFraction(5.5, count: 6), 0)
        XCTAssertEqual(PageTurnMath.coveredFraction(6.2, count: 6), 0)
    }
}
