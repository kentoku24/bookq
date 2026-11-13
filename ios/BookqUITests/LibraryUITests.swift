import XCTest

/// Browse-side UX tests against the TEST_MODE fixture tree:
///
///   Series/vol1 (3 pages), Series/vol2 (2 pages), Other (1 page)
///
/// Top level therefore shows two tiles — "Series" (drills down) and
/// "Other" (image-only, opens the reader directly). Tile identity is the
/// `tile:<path>` / `selfTile:<path>` accessibility identifier, matching
/// the Android `tile:<path>` testTag convention.
final class LibraryUITests: XCTestCase {
    var app: XCUIApplication!

    override func setUpWithError() throws {
        continueAfterFailure = false
        app = XCUIApplication()
        UITestSupport.launch(app)
    }

    private func tile(_ path: String) -> XCUIElement {
        app.buttons["tile:\(path)"]
    }

    /// `.searchable` fields can sit collapsed behind the nav-bar search
    /// control on iPhone — expand first when the field is not hittable.
    private func openSearchField() -> XCUIElement {
        let field = app.searchFields.firstMatch
        if field.waitForExistence(timeout: 15), field.isHittable {
            return field
        }
        // Collapsed state: the magnifying-glass control lives in the nav
        // bar (label is the localized "検索"/"Search").
        for i in 0..<app.navigationBars.buttons.count {
            let b = app.navigationBars.buttons.element(boundBy: i)
            if b.label.localizedCaseInsensitiveContains("検索")
                || b.label.localizedCaseInsensitiveContains("search") {
                b.tap()
                break
            }
        }
        XCTAssertTrue(field.waitForExistence(timeout: 15))
        return field
    }

    func testTopLevelFoldersRender() throws {
        XCTAssertTrue(tile("Series").waitForExistence(timeout: 60),
                      "top-level 'Series' tile never appeared")
        XCTAssertTrue(tile("Other").waitForExistence(timeout: 15),
                      "top-level 'Other' tile never appeared")
        XCTAssertTrue(app.staticTexts["2 件"].waitForExistence(timeout: 15),
                      "folder count label '2 件' never appeared")
    }

    func testDrillDownShowsVolumeTiles() throws {
        XCTAssertTrue(tile("Series").waitForExistence(timeout: 60))
        tile("Series").tap()
        XCTAssertTrue(tile("Series/vol1").waitForExistence(timeout: 30),
                      "drilled level did not show Series/vol1")
        XCTAssertTrue(tile("Series/vol2").waitForExistence(timeout: 15),
                      "drilled level did not show Series/vol2")
    }

    func testSearchModeToggles() throws {
        XCTAssertTrue(tile("Series").waitForExistence(timeout: 60))
        let field = openSearchField()
        field.tap()
        field.typeText("Series")

        // Fixture folders carry no comic metadata, so the global index
        // returns zero hits — the important UX signal is that the browse
        // grid is REPLACED by the search surface (empty state text).
        XCTAssertTrue(
            app.staticTexts["一致する結果がありません"]
                .waitForExistence(timeout: 30),
            "search mode never engaged (empty-results text missing)")
        XCTAssertFalse(tile("Series").exists,
                       "browse tile still present while search is active")

        // Exit search mode by emptying the query — SearchModel's
        // empty-query path sets active=false and the browse grid returns.
        // The clear-text button label is localized, so fall back to the
        // locale-independent keyboard delete key.
        var cleared = false
        for label in ["テキストを消去", "Clear text"] {
            let b = app.buttons[label]
            if b.exists, b.isHittable { b.tap(); cleared = true; break }
        }
        if !cleared {
            field.tap()
            for _ in 0..<40 where app.keyboards.keys["delete"].exists {
                app.keyboards.keys["delete"].tap()
            }
        }
        XCTAssertTrue(tile("Series").waitForExistence(timeout: 30),
                      "browse grid did not return after search exit")
    }

    func testLayoutModeSwitch() throws {
        XCTAssertTrue(tile("Series").waitForExistence(timeout: 60))
        app.buttons["layoutMenuBtn"].tap()
        let listItem = app.buttons["リスト"]
        XCTAssertTrue(listItem.waitForExistence(timeout: 10),
                      "layout menu did not offer リスト")
        listItem.tap()
        // Row layout re-renders the same tiles — identifier must still
        // resolve (guards the LazyVStack re-layout path).
        XCTAssertTrue(tile("Series").waitForExistence(timeout: 15))
        XCTAssertTrue(tile("Other").waitForExistence(timeout: 10))
    }

    func testOfflineButtonPresent() throws {
        XCTAssertTrue(tile("Series").waitForExistence(timeout: 60))
        XCTAssertTrue(
            app.buttons["offlineBtn:Series"].waitForExistence(timeout: 15),
            "offline download button missing on folder tile")
        XCTAssertTrue(
            app.buttons["offlineBtn:Other"].waitForExistence(timeout: 10),
            "offline download button missing on image-only tile")
    }
}
