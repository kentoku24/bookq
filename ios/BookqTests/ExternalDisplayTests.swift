import XCTest
@testable import Bookq

/// External-display projection: activation state machine, best-mode
/// selection, and the shared spread-fit/pageMode helpers.
@MainActor
final class ExternalDisplayTests: XCTestCase {
    private func makePresentation() -> ExternalDisplayStore.Presentation {
        let work = ReaderWork.assemble(
            clickedPath: "w",
            chapters: [ReaderChapter(name: "w", fullPath: "w",
                                     images: [PageRef(name: "1.jpg", folder: "w")])],
            pageMode: .single, spreadOffset: 0)
        return .init(work: work, currentIdx: 0, pageAR: [:],
                     spreadOffset: 0, landscapeSpread: true, urlFor: { _ in nil })
    }

    private func makeStore(screens: Int) -> ExternalDisplayStore {
        let s = ExternalDisplayStore()
        s.externalScreenCount = { screens }
        s.activate = {}
        s.deactivate = { _ in }
        s.refreshAvailability()
        return s
    }

    // MARK: - bestMode

    func testBestModePicksMostPixels() {
        let modes = [
            ExternalDisplayStore.ModeRef(size: CGSize(width: 1920, height: 1080),
                                         pixelAspectRatio: 1),
            ExternalDisplayStore.ModeRef(size: CGSize(width: 3840, height: 2160),
                                         pixelAspectRatio: 1),
            ExternalDisplayStore.ModeRef(size: CGSize(width: 2560, height: 1440),
                                         pixelAspectRatio: 1),
        ]
        XCTAssertEqual(ExternalDisplayStore.bestModeIndex(modes), 1)
    }

    func testBestModeAccountsForPixelAspectRatio() {
        // A 1920×1080pt @2x mode (3840×2160px) beats a 3000×2000pt @1x mode.
        let modes = [
            ExternalDisplayStore.ModeRef(size: CGSize(width: 1920, height: 1080),
                                         pixelAspectRatio: 2),
            ExternalDisplayStore.ModeRef(size: CGSize(width: 3000, height: 2000),
                                         pixelAspectRatio: 1),
        ]
        XCTAssertEqual(ExternalDisplayStore.bestModeIndex(modes), 0)
    }

    func testBestModeEmpty() {
        XCTAssertNil(ExternalDisplayStore.bestModeIndex([]))
    }

    // MARK: - Activation state machine

    func testPresentationWithScreenActivatesOnce() {
        let s = makeStore(screens: 1)
        var a = 0
        s.activate = { a += 1 }
        s.updatePresentation(makePresentation())
        s.updatePresentation(makePresentation())   // republish — still pending
        XCTAssertEqual(a, 1)
        XCTAssertFalse(s.connected)
    }

    func testNoScreenDefersActivationUntilConnect() {
        let s = makeStore(screens: 0)
        var a = 0
        s.activate = { a += 1 }
        s.updatePresentation(makePresentation())
        XCTAssertEqual(a, 0)
        XCTAssertFalse(s.available)
        s.externalScreenCount = { 1 }
        s.refreshAvailability()
        XCTAssertTrue(s.available)
        XCTAssertEqual(a, 1)
    }

    func testNoPresentationNeverActivates() {
        let s = makeStore(screens: 1)
        var a = 0
        s.activate = { a += 1 }
        s.refreshAvailability()
        XCTAssertEqual(a, 0)
    }

    func testEndPresentationDestroysSession() {
        let s = makeStore(screens: 1)
        var a = 0, d = 0
        s.activate = { a += 1 }
        s.deactivate = { _ in d += 1 }
        s.updatePresentation(makePresentation())
        s.sessionRef = NSObject()
        s.markConnected()
        s.endPresentation()
        XCTAssertEqual(d, 1)
        s.markDisconnected()
        XCTAssertNil(s.sessionRef)
        XCTAssertFalse(s.connected)
        // No bounce-back: presentation is gone, so availability alone must
        // not re-activate even though the screen is still attached.
        XCTAssertEqual(a, 1)
    }

    func testUserDisableDestroysThenReenableReactivates() {
        let s = makeStore(screens: 1)
        var a = 0, d = 0
        s.activate = { a += 1 }
        s.deactivate = { _ in d += 1 }
        s.updatePresentation(makePresentation())
        s.sessionRef = NSObject()
        s.markConnected()
        s.userEnabled = false
        XCTAssertEqual(d, 1)
        s.markDisconnected()
        s.userEnabled = true
        XCTAssertEqual(a, 2)
    }

    func testReactivationAfterDisconnectWhilePresenting() {
        let s = makeStore(screens: 1)
        var a = 0
        s.activate = { a += 1 }
        s.updatePresentation(makePresentation())
        s.sessionRef = NSObject()
        s.markConnected()
        s.updatePresentation(makePresentation())  // connected: no re-request
        XCTAssertEqual(a, 1)
        // Session drops (display sleep/yank) → while still presenting,
        // the next availability pass activates a fresh session.
        s.markDisconnected()
        XCTAssertEqual(a, 2)
    }

    // MARK: - Shared layout helpers

    func testPageModeRule() {
        XCTAssertEqual(ReaderWork.pageMode(for: CGSize(width: 3840, height: 2160),
                                           landscapeSpread: true), .spread)
        XCTAssertEqual(ReaderWork.pageMode(for: CGSize(width: 2160, height: 3840),
                                           landscapeSpread: true), .single)
        XCTAssertEqual(ReaderWork.pageMode(for: CGSize(width: 3840, height: 2160),
                                           landscapeSpread: false), .single)
    }

    func testSpreadFitHeightBound() {
        // Two AR-0.7 pages on a 4K canvas: height-bound at 2160.
        let fit = ReaderWork.spreadFit(ars: [0.7, 0.7], gap: 5,
                                       in: CGSize(width: 3840, height: 2160))
        XCTAssertEqual(fit.height, 2160, accuracy: 0.001)
        XCTAssertEqual(fit.width, 0.7 * 2160 * 2 + 5, accuracy: 0.001)
    }

    func testSpreadFitWidthClamp() {
        // Wide pages clamp to viewport width and shrink the height.
        let fit = ReaderWork.spreadFit(ars: [1.5, 1.5], gap: 5,
                                       in: CGSize(width: 1920, height: 1080))
        XCTAssertEqual(fit.width, 1920, accuracy: 0.001)
        XCTAssertEqual(fit.height, (1920 - 5) / 3.0, accuracy: 0.001)
    }
}
