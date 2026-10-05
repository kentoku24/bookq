"""Accessibility-focused UI tests (H4 keyboard/dialog semantics, H6 pinch zoom).

H4 (ui-ux-audit findings S-F3 / B-N4 / B-N3 / L-F6):
- (a) browse items (.tile/.card/.item, facet .chip) are keyboard-reachable:
  focusable and open on Enter/Space.
- (b) off-screen controls are inert, so they leave both the focus order and
  the accessibility tree: the closed filter #bottom-sheet, the closed
  #sidebar, and the hidden #resume-btn.
- (c) the filter sheet is a real dialog: role=dialog aria-modal=true
  aria-labelledby.
- (d) icon-only buttons carry aria-labels.

H6: the viewer's meta viewport must not disable user scaling.
`user-scalable=no` breaks pinch zoom on mobile browsers (WCAG 1.4.4 Resize
Text / 1.4.10 Reflow-adjacent), so its removal is asserted here to keep it
from coming back.

Run:
    uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_ui_a11y.py -q
"""


def test_viewport_allows_zoom(page):
    # `page` fixture: logged-in test-mode browser, browse root loaded.
    viewport_content = page.evaluate(
        'document.querySelector(\'meta[name="viewport"]\').content'
    )
    assert "user-scalable=no" not in viewport_content
    assert "initial-scale=1" in viewport_content


def test_keyboard_can_open_tile(page):
    """H4(a): browse tiles are in the tab order and open the viewer on Enter.

    Tabs until the focus lands on a known leaf tile (simple_manga holds only
    images, so Enter must open the viewer), then presses Enter and waits for
    the page counter — i.e. a real viewer load, not a drill-down.
    """
    page.wait_for_selector('.tile[data-path="simple_manga"]')
    reached = False
    for _ in range(30):
        page.keyboard.press("Tab")
        reached = page.evaluate(
            "() => {"
            "  const el = document.activeElement;"
            "  return !!el && el.matches('.tile') && el.dataset.path === 'simple_manga';"
            "}"
        )
        if reached:
            break
    assert reached, "Tab never focused a .tile — browse items are not keyboard-focusable"
    page.keyboard.press("Enter")
    page.wait_for_function(
        'document.getElementById("counter").textContent.trim() !== ""'
    )


def test_hidden_controls_not_focusable(page):
    """H4(b)+(c): the closed filter sheet is inert and marked as a dialog when open."""
    sheet_inert = "() => document.getElementById('bottom-sheet').hasAttribute('inert')"
    # Closed by default: off-screen sheet must be inert (out of tab order + a11y tree).
    assert page.evaluate(sheet_inert), "closed #bottom-sheet must be inert"
    # Open it via the filter button.
    page.click("#filter-btn")
    page.wait_for_function(
        "document.getElementById('bottom-sheet').classList.contains('open')"
    )
    assert not page.evaluate(sheet_inert), "open #bottom-sheet must not be inert"
    assert page.evaluate(
        "document.getElementById('bottom-sheet').getAttribute('role')"
    ) == "dialog"
    assert page.evaluate(
        "document.getElementById('bottom-sheet').getAttribute('aria-modal')"
    ) == "true"
    # Close it again: inert must come back.
    page.click("#bottom-sheet-close")
    assert page.evaluate(sheet_inert), "closed #bottom-sheet must be inert again"


def test_sidebar_inert_when_closed(page):
    """H4(b): a closed sidebar is inert + aria-hidden; reopening removes both."""
    # Open a book — the sidebar closes itself.
    page.locator('.tile[data-path="simple_manga"]').click()
    page.wait_for_function(
        'document.getElementById("counter").textContent.trim() !== ""'
    )
    sidebar_inert = "() => document.getElementById('sidebar').hasAttribute('inert')"
    assert page.evaluate(sidebar_inert), "closed #sidebar must be inert"
    assert page.evaluate(
        "document.getElementById('sidebar').getAttribute('aria-hidden')"
    ) == "true"
    # Reopen via the menu button.
    page.click("#menu-btn")
    assert not page.evaluate(sidebar_inert), "open #sidebar must not be inert"
    assert page.evaluate(
        "document.getElementById('sidebar').hasAttribute('aria-hidden')"
    ) is False


def test_icon_buttons_have_aria_labels(page):
    """H4(d): icon-only controls expose an aria-label."""
    for sel in (
        "#menu-btn",
        "#filter-btn",
        "#footer-refresh-btn",
        "#logout-btn",
        "#spread-toggle",
        "#spread-shift",
        "#bottom-sheet-close",
    ):
        label = page.get_attribute(sel, "aria-label")
        assert label, f"{sel} lacks an aria-label"
