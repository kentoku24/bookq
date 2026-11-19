import XCTest

/// Reader UX tests — open a leaf work from the browse grid, turn pages
/// via the tap zones (web `zoneActionAt` parity: edge 30% zones follow
/// `tapForwardLeft`, default left = forward), then close back to browse.
final class ReaderUITests: XCTestCase {
    var app: XCUIApplication!

    override func setUpWithError() throws {
        continueAfterFailure = false
        app = XCUIApplication()
        UITestSupport.launch(app)
    }

    private func tile(_ path: String) -> XCUIElement {
        app.buttons["tile:\(path)"]
    }

    /// Series → Series/vol1 → reader cover. Waits for the page counter
    /// so callers start from a settled reader.
    @discardableResult
    private func openVol1Reader() -> XCUIElement {
        app.assertExists(tile("Series"), within: 60)
        tile("Series").tap()
        app.assertExists(tile("Series/vol1"), within: 30)
        tile("Series/vol1").tap()
        let counter = app.staticTexts["readerCounter"]
        app.assertExists(counter, within: 60,
                         "reader never opened (counter missing)")
        return counter
    }

    /// Poll `element.label` until it equals `text` — counter updates are
    /// async (page turn animation + @Published), so a bare XCTAssertEqual
    /// races the render.
    private func expectLabel(_ element: XCUIElement, _ text: String,
                             timeout: TimeInterval = 15,
                             file: StaticString = #filePath,
                             line: UInt = #line) {
        let pred = NSPredicate(format: "label == %@", text)
        let exp = XCTNSPredicateExpectation(predicate: pred,
                                            object: element)
        let result = XCTWaiter.wait(for: [exp], timeout: timeout)
        XCTAssertEqual(result, .completed,
                       "label never became '\(text)' (was '\(element.label)')",
                       file: file, line: line)
    }

    func testReaderOpensAndTurnsPages() throws {
        let counter = openVol1Reader()
        expectLabel(counter, "1 / 3")

        // Left 30% zone = forward (tapForwardLeft default) → page 2.
        app.coordinate(withNormalizedOffset: CGVector(dx: 0.15, dy: 0.5))
            .tap()
        expectLabel(counter, "2 / 3")

        // Right 30% zone = back → page 1. Proves both directions work.
        app.coordinate(withNormalizedOffset: CGVector(dx: 0.85, dy: 0.5))
            .tap()
        expectLabel(counter, "1 / 3")
    }

    func testReaderCloseReturnsToBrowse() throws {
        _ = openVol1Reader()
        app.buttons["readerClose"].tap()
        // Dismiss is cover-local — no refetch — so a stuck cover and a
        // missing level are different bugs. Check them separately, and
        // give the restore the same headroom the other waits get.
        app.assertGone(app.buttons["readerClose"], within: 10,
                       "reader cover did not dismiss")
        app.assertExists(tile("Series/vol1"), within: 30,
                         "browse level did not restore after reader close")
    }
}
