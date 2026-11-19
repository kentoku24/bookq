"""H5 (contrast) and H9 (touch targets) remediation tests.

Values come from .omo/reports/ui-ux-audit.md §4:
- H5: exact color targets for low-contrast text/non-text elements.
- H9: minimum 44x44 touch targets (WCAG 2.5.5).

These assert the *remediated* values, so they fail (RED) against the pre-fix
style.css and pass (GREEN) after web/static/style.css is updated.
"""

# H5 exact target colors from the audit report
FOOTER_COLOR = "rgb(154, 154, 154)"  # #9a9a9a (was #555)
EMPTY_COLOR = "rgb(154, 154, 154)"  # #9a9a9a (was #555)
LABEL_COLOR = "rgb(166, 166, 166)"  # #a6a6a6 (was #666)
PLACEHOLDER_COLOR = "rgb(143, 143, 143)"  # #8f8f8f (was #444)

MIN_TARGET = 44  # H9 minimum touch target in px (both dimensions)


def _computed_color(page, selector, probe_html=None):
    """Computed color for `selector`.

    Some selectors only exist in the DOM under conditions the fixture tree
    does not produce (e.g. .card-meta .meta-row .label needs metadata, and
    .tile-placeholder only renders for thumbless tiles). When the element is
    absent, inject a minimal probe with the same class structure — CSS rules
    match globally, so the computed value is the real rule's value either way.
    """
    has = page.evaluate("sel => document.querySelectorAll(sel).length > 0", selector)
    if not has:
        assert probe_html is not None, f"no element matches {selector!r} in the DOM"
        page.evaluate(
            """html => {
                const d = document.createElement('div');
                d.innerHTML = html;
                document.body.appendChild(d.firstElementChild);
            }""",
            probe_html,
        )
    return page.evaluate(
        "sel => getComputedStyle(document.querySelector(sel)).color", selector
    )


def _rect(page, selector):
    """getBoundingClientRect dimensions for `selector`, or None when absent."""
    return page.evaluate(
        """sel => {
            const el = document.querySelector(sel);
            if (!el) return null;
            const r = el.getBoundingClientRect();
            return {w: r.width, h: r.height};
        }""",
        selector,
    )


def test_low_contrast_colors_updated(page):
    # Browse root is loaded with tiles; #sidebar-footer and #empty are always
    # in the DOM (template ids).
    assert _computed_color(page, "#sidebar-footer") == FOOTER_COLOR
    assert _computed_color(page, "#empty") == EMPTY_COLOR
    assert _computed_color(
        page,
        ".card-meta .meta-row .label",
        '<div class="card-meta"><div class="meta-row"><span class="label">x</span></div></div>',
    ) == LABEL_COLOR
    assert _computed_color(
        page, ".tile-placeholder", '<div class="tile-placeholder"></div>'
    ) == PLACEHOLDER_COLOR


def test_header_menu_scrollable_in_short_landscape(browser, server):
    """667x375 landscape: the ⋯menu must fit the viewport and scroll so the
    last item (logout) is reachable — the fixed page can't scroll for it."""
    context = browser.new_context(
        viewport={"width": 667, "height": 375}, is_mobile=True, has_touch=True
    )
    pg = context.new_page()
    try:
        pg.goto(server)
        pg.wait_for_selector(".tile")
        pg.click("#header-menu-btn")
        pg.wait_for_selector("#header-menu:not([hidden])")
        menu = pg.evaluate(
            """() => {
                const m = document.getElementById('header-menu');
                const r = m.getBoundingClientRect();
                return {bottom: r.bottom, overflowY: getComputedStyle(m).overflowY};
            }"""
        )
        assert menu["bottom"] <= 375, (
            f"menu bottom {menu['bottom']}px overflows 375px viewport"
        )
        assert menu["overflowY"] in ("auto", "scroll"), (
            f"#header-menu overflowY is {menu['overflowY']!r} — needs scroll"
        )
        # The menu's own scroll reaches the last item.
        pg.evaluate(
            "const m = document.getElementById('header-menu');"
            "m.scrollTop = m.scrollHeight"
        )
        logout_bottom = pg.evaluate(
            "document.getElementById('logout-btn').getBoundingClientRect().bottom"
        )
        assert 0 < logout_bottom <= 375, (
            f"#logout-btn bottom {logout_bottom}px not reachable at 375px"
        )
    finally:
        context.close()


def test_touch_targets_min_44px(page, landscape_page):
    # Sidebar / toolbar controls (portrait viewport).
    for sel in ["#menu-btn", "#header-menu-btn", "#pair-btn", "#search-input", "#offline-status"]:
        rect = _rect(page, sel)
        assert rect is not None, f"{sel} not found in the DOM"
        assert rect["w"] >= MIN_TARGET and rect["h"] >= MIN_TARGET, (
            f"{sel} is {rect['w']:.1f}x{rect['h']:.1f} (< {MIN_TARGET}px)"
        )

    # ⋯menu items are full-width rows — measured with the menu open.
    page.locator("#header-menu-btn").click()
    page.wait_for_selector("#header-menu:not([hidden])")
    for sel in ["#filter-btn", "#lib-edit-btn", "#menu-view-list", "#menu-reload", "#harness-link", "#logout-btn"]:
        rect = _rect(page, sel)
        assert rect is not None, f"{sel} not found in the DOM"
        assert rect["w"] >= MIN_TARGET and rect["h"] >= MIN_TARGET, (
            f"{sel} is {rect['w']:.1f}x{rect['h']:.1f} (< {MIN_TARGET}px)"
        )
    page.keyboard.press("Escape")

    # Overlay spread controls (landscape viewport; default 2P mode shows both).
    for sel in ["#spread-toggle", "#spread-shift"]:
        rect = _rect(landscape_page, sel)
        assert rect is not None, f"{sel} not found in the DOM"
        assert rect["w"] >= MIN_TARGET and rect["h"] >= MIN_TARGET, (
            f"{sel} is {rect['w']:.1f}x{rect['h']:.1f} (< {MIN_TARGET}px)"
        )

    # #resume-btn only renders visibly when a stored reading position exists;
    # on a fresh session it is display:none (.hidden) — skip when hidden.
    resume = page.locator("#resume-btn")
    if resume.is_visible():
        rect = resume.bounding_box()
        assert rect is not None
        assert rect["width"] >= MIN_TARGET and rect["height"] >= MIN_TARGET, (
            f"#resume-btn is {rect['width']:.1f}x{rect['height']:.1f} (< {MIN_TARGET}px)"
        )

    # .chip-remove needs the filter sheet with facets (metadata), which the
    # fixture tree has none of — skipped here. Its 44px hit area comes from
    # the same remediation as the targets above and is enforced by the CSS
    # rule .chip .chip-remove (width/height 44px), to be covered in the
    # integration phase with a metadata fixture.


def test_resume_btn_clears_footer(page):
    """Resume pill must clear the sidebar footer (RED on bottom:16px).

    Regression for the #resume-btn / #sidebar-footer overlap: with the old
    CSS (`bottom:16px` at web/static/style.css:248) the fixed-position
    resume pill sits on top of the footer; the fix (`bottom:76px` = ~60px
    footer + 16px gap) leaves an 8px+ vertical gap.

    RED pre-fix (gap assertion fails at 16px), GREEN post-fix (76px).
    """
    # Seed reading progress (shape per web/static/app.js:403-408
    # {page, total, ts}); simple_manga is a leaf in the conftest fixture
    # tree (tests/conftest.py library_tree) and in
    # tests/fixtures/uiux_tree.py, so no dynamic resolution is needed.
    page.evaluate("localStorage.setItem(window.OfflineCore.progressKey(), JSON.stringify({'simple_manga':{page:4,total:10,ts:Date.now()}}))")
    page.reload()
    page.wait_for_selector(".tile")

    resume = page.locator("#resume-btn")
    resume.wait_for(state="visible", timeout=5000)
    assert page.evaluate(
        "document.getElementById('sidebar').classList.contains('open')"
    ), "sidebar should be open after reload so the resume pill shows"

    resume_box = resume.bounding_box()
    footer_box = page.locator("#sidebar-footer").bounding_box()
    assert resume_box is not None, "#resume-btn has no bounding box"
    assert footer_box is not None, "#sidebar-footer has no bounding box"
    assert resume_box["y"] + resume_box["height"] <= footer_box["y"] - 8, (
        f"#resume-btn bottom {resume_box['y'] + resume_box['height']:.1f}px "
        f"overlaps #sidebar-footer top {footer_box['y']:.1f}px (need 8px+ gap)"
    )

    # Clickability proof 1: header ⋯menu center click opens the menu and a
    # view pick still toggles the grid (tile -> list); sidebar stays open.
    menu_btn_box = page.locator("#header-menu-btn").bounding_box()
    assert menu_btn_box is not None, "#header-menu-btn has no bounding box"
    page.mouse.click(
        menu_btn_box["x"] + menu_btn_box["width"] / 2,
        menu_btn_box["y"] + menu_btn_box["height"] / 2,
    )
    page.wait_for_selector("#header-menu:not([hidden])", timeout=5000)
    list_box = page.locator("#menu-view-list").bounding_box()
    assert list_box is not None, "#menu-view-list has no bounding box"
    page.mouse.click(
        list_box["x"] + list_box["width"] / 2,
        list_box["y"] + list_box["height"] / 2,
    )
    page.wait_for_selector("#tile-grid.list-mode", timeout=5000)

    # Clickability proof 2: resume center click opens the viewer for the
    # leaf folder (sidebar closes, folder name shows).
    resume_box = resume.bounding_box()
    assert resume_box is not None, "#resume-btn lost its bounding box"
    page.mouse.click(
        resume_box["x"] + resume_box["width"] / 2,
        resume_box["y"] + resume_box["height"] / 2,
    )
    page.wait_for_function(
        "() => !document.getElementById('sidebar').classList.contains('open')",
        timeout=5000,
    )
    assert page.locator("#folder-name").text_content() == "simple_manga"
