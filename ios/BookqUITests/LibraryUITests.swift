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
        app.assertExists(field, within: 15)
        return field
    }

    func testTopLevelFoldersRender() throws {
        app.assertExists(tile("Series"), within: 60,
                         "top-level 'Series' tile never appeared")
        app.assertExists(tile("Other"), within: 15,
                         "top-level 'Other' tile never appeared")
        app.assertExists(app.staticTexts["2 件"], within: 15,
                         "folder count label '2 件' never appeared")
    }

    func testDrillDownShowsVolumeTiles() throws {
        app.assertExists(tile("Series"), within: 60)
        tile("Series").tap()
        app.assertExists(tile("Series/vol1"), within: 30,
                         "drilled level did not show Series/vol1")
        app.assertExists(tile("Series/vol2"), within: 15,
                         "drilled level did not show Series/vol2")
    }

    func testSearchModeToggles() throws {
        app.assertExists(tile("Series"), within: 60)
        let field = openSearchField()
        app.typeTextReliably("Series", into: field)

        // Fixture folders carry no comic metadata, so the global index
        // returns zero hits — the important UX signal is that the browse
        // grid is REPLACED by the search surface (empty state text).
        app.assertExists(
            app.staticTexts["一致する結果がありません"],
            within: 30,
            "search mode never engaged (empty-results text missing)")
        XCTAssertFalse(tile("Series").exists,
                       "browse tile still present while search is active")

        // Exit search mode by emptying the query — SearchModel's
        // empty-query path sets active=false and the browse grid returns.
        app.clearTextInput(field)
        app.assertExists(tile("Series"), within: 30,
                         "browse grid did not return after search exit")
    }

    func testLayoutModeSwitch() throws {
        app.assertExists(tile("Series"), within: 60)
        app.buttons["layoutMenuBtn"].tap()
        let listItem = app.buttons["リスト"]
        app.assertExists(listItem, within: 10,
                         "layout menu did not offer リスト")
        listItem.tap()
        // Row layout re-renders the same tiles — identifier must still
        // resolve (guards the LazyVStack re-layout path).
        app.assertExists(tile("Series"), within: 15)
        app.assertExists(tile("Other"), within: 10)
    }

    func testTrailingToolbarSurvivesDrillDown() throws {
        app.assertExists(tile("Series"), within: 60)
        tile("Series").tap()
        app.assertExists(tile("Series/vol1"), within: 30)
        // pair / settings / ⋯menu must stay on pushed levels too —
        // Android and the web client keep the same cluster visible at
        // every depth (pre-fix it was root-only).
        app.assertExists(app.buttons["pairBtn"], within: 10,
                         "pairBtn missing on drilled level")
        app.assertExists(app.buttons["settingsBtn"], within: 10,
                         "settingsBtn missing on drilled level")
        app.assertExists(app.buttons["layoutMenuBtn"], within: 10,
                         "layoutMenuBtn missing on drilled level")
        // And the menu still functions there — the 表示 picker opens.
        app.buttons["layoutMenuBtn"].tap()
        app.assertExists(app.buttons["リスト"], within: 10,
                         "layout menu did not open on drilled level")
    }

    func testOfflineButtonPresent() throws {
        app.assertExists(tile("Series"), within: 60)
        app.assertExists(
            app.buttons["offlineBtn:Series"], within: 15,
            "offline download button missing on folder tile")
        app.assertExists(
            app.buttons["offlineBtn:Other"], within: 10,
            "offline download button missing on image-only tile")
    }
}
