import XCTest
@testable import Bookq

/// PairModel.turnIndex / project — direct unit coverage of the shared
/// projection math (test_reader_sync_model parity: chapter clamp vs
/// crossChapter, boundary clamps, role/side/blank projection).
final class PairModelTests: XCTestCase {

    private func spread(_ id: String, _ ch: String) -> PairSpread {
        PairSpread(id: id, chapterFolder: ch,
                   right: PairPageRef(folder: ch, name: "\(id)r.png"),
                   left: PairPageRef(folder: ch, name: "\(id)l.png"))
    }

    private var table: [PairSpread] {
        [spread("c0s0", "W/ch1"), spread("c0s1", "W/ch1"),
         spread("c1s0", "W/ch2"), spread("c1s1", "W/ch2")]
    }

    // MARK: - turnIndex

    /// crossChapter walks across chapter boundaries.
    func testCrossChapterWalksBoundaries() {
        XCTAssertEqual(PairModel.turnIndex(in: table, index: 1,
                                           delta: 1, crossChapter: true), 2)
        XCTAssertEqual(PairModel.turnIndex(in: table, index: 2,
                                           delta: -1, crossChapter: true), 1)
    }

    /// !crossChapter clamps to the current chapter run — chapter edges
    /// hold position instead of bleeding into the sibling chapter.
    func testChapterClampHoldsAtBoundary() {
        // ch1 last spread +next → stays at index 1 (clamped, not 2)
        XCTAssertEqual(PairModel.turnIndex(in: table, index: 1,
                                           delta: 1, crossChapter: false), 1)
        // ch2 first spread +prev → stays at index 2
        XCTAssertEqual(PairModel.turnIndex(in: table, index: 2,
                                           delta: -1, crossChapter: false), 2)
        // in-chapter moves still walk
        XCTAssertEqual(PairModel.turnIndex(in: table, index: 0,
                                           delta: 1, crossChapter: false), 1)
        XCTAssertEqual(PairModel.turnIndex(in: table, index: 3,
                                           delta: -1, crossChapter: false), 2)
    }

    /// Table edges clamp regardless of mode (web boundary turns).
    func testTableEdgesClamp() {
        XCTAssertEqual(PairModel.turnIndex(in: table, index: 0,
                                           delta: -1, crossChapter: true), 0)
        XCTAssertEqual(PairModel.turnIndex(in: table, index: 3,
                                           delta: 1, crossChapter: true), 3)
    }

    /// Invalid inputs rejected — empty table, out-of-range index,
    /// non-±1 delta (mirrors web request_malformed rejection).
    func testInvalidTurnInputs() {
        XCTAssertNil(PairModel.turnIndex(in: [], index: 0, delta: 1,
                                         crossChapter: true))
        XCTAssertNil(PairModel.turnIndex(in: table, index: 9, delta: 1,
                                         crossChapter: true))
        XCTAssertNil(PairModel.turnIndex(in: table, index: 0, delta: 2,
                                         crossChapter: true))
        XCTAssertNil(PairModel.turnIndex(in: table, index: 0, delta: 0,
                                         crossChapter: true))
    }

    // MARK: - chapterRange (chapter-local slider bounds)

    /// chapterRange returns the contiguous chapterFolder run around
    /// index — the same bounds turnIndex clamps to when !crossChapter.
    func testChapterRangeFindsRuns() {
        // table: c0s0,c0s1 @ "W/ch1" ; c1s0,c1s1 @ "W/ch2"
        for i in [0, 1] {
            let r = PairModel.chapterRange(in: table, index: i)
            XCTAssertEqual(r?.lo, 0)
            XCTAssertEqual(r?.hi, 1)
        }
        for i in [2, 3] {
            let r = PairModel.chapterRange(in: table, index: i)
            XCTAssertEqual(r?.lo, 2)
            XCTAssertEqual(r?.hi, 3)
        }
    }

    /// Out-of-range → nil; a single-chapter table is one full run;
    /// repeated folder names in non-adjacent runs stay separate.
    func testChapterRangeEdges() {
        XCTAssertNil(PairModel.chapterRange(in: table, index: -1))
        XCTAssertNil(PairModel.chapterRange(in: table, index: 4))
        XCTAssertNil(PairModel.chapterRange(in: [], index: 0))
        let single = [spread("s0", "W/ch1"), spread("s1", "W/ch1")]
        XCTAssertEqual(PairModel.chapterRange(in: single, index: 0)?.hi, 1)
        XCTAssertEqual(PairModel.chapterRange(in: single, index: 1)?.lo, 0)
        // ch1 / ch2 / ch1 — the two ch1 runs must not merge.
        let split = [spread("a", "W/ch1"), spread("b", "W/ch2"),
                     spread("c", "W/ch1")]
        XCTAssertEqual(PairModel.chapterRange(in: split, index: 0)?.hi, 0)
        XCTAssertEqual(PairModel.chapterRange(in: split, index: 2)?.lo, 2)
    }

    // MARK: - project

    /// Leader gets leaderSide, follower the opposite side; ref == nil
    /// renders as a blank (pairedView.js parity).
    func testProjectRolesAndSides() {
        let s = spread("s0", "W/ch1")
        let lead = PairModel.project(spread: s, role: .leader,
                                     leaderSide: .right)
        XCTAssertEqual(lead.side, PairSide.right)
        XCTAssertEqual(lead.ref, s.right)
        let fol = PairModel.project(spread: s, role: .follower,
                                    leaderSide: .right)
        XCTAssertEqual(fol.side, PairSide.left)
        XCTAssertEqual(lead.ref, s.right)
        XCTAssertEqual(fol.ref, s.left)
    }

    /// leaderSide=.left flips both assignments.
    func testProjectSwappedLeaderSide() {
        let s = spread("s0", "W/ch1")
        XCTAssertEqual(PairModel.project(spread: s, role: .leader,
                                         leaderSide: .left).side, PairSide.left)
        XCTAssertEqual(PairModel.project(spread: s, role: .follower,
                                         leaderSide: .left).side, PairSide.right)
    }

    /// A nil left ref projects as blank for the follower (odd trailing
    /// spread — web 'no stale neighbour page' case).
    func testProjectBlankSide() {
        var s = spread("s0", "W/ch1")
        s.left = nil
        let fol = PairModel.project(spread: s, role: .follower,
                                    leaderSide: .right)
        XCTAssertNil(fol.ref)
        XCTAssertEqual(fol.side, PairSide.left)
    }

    // MARK: - PairReaderGate (#216)

    /// The cover must survive transient phases — loading (SET_OFFSET /
    /// OPEN_WORK manifest reload) and paused — so the leader's onDisappear
    /// cannot mistake a phase-driven dismiss for a browse intent.
    func testReaderGatePresentsTransientPhases() {
        XCTAssertTrue(PairReaderGate.presents(.reading))
        XCTAssertTrue(PairReaderGate.presents(.loading))
        XCTAssertTrue(PairReaderGate.presents(.paused))
        XCTAssertTrue(PairReaderGate.presents(.selecting))
        XCTAssertFalse(PairReaderGate.presents(.waiting))
        XCTAssertFalse(PairReaderGate.presents(nil))
    }

    /// Only a close while `reading` is the leader's browse intent; closing
    /// during loading/paused/selecting must not emit SET_ACTIVITY selecting.
    func testReaderGateCloseSignalsSelectingOnlyWhileReading() {
        XCTAssertTrue(PairReaderGate.closeSignalsSelecting(.reading))
        for ph in [PairPhase.loading, .paused, .selecting, .waiting] {
            XCTAssertFalse(PairReaderGate.closeSignalsSelecting(ph), "\(ph)")
        }
        XCTAssertFalse(PairReaderGate.closeSignalsSelecting(nil))
    }
}
