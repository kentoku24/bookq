"""Helpers for the browse header ⋯menu (browse-header-ux change).

The header's overflow menu holds 絞り込み / ライブラリ編集 / view-mode radios /
再読み込み / ハーネス / ログアウト inside #header-menu, which stays `hidden`
until #header-menu-btn opens it.

- Playwright .click() needs a visible target → open the menu first.
- JS-level pokes (page.evaluate / eval_on_selector el.click()) work while the
  menu is hidden because listeners sit on the items themselves.
"""

from playwright.sync_api import Page


def open_header_menu(page: Page) -> None:
    page.locator("#header-menu-btn").click()
    page.wait_for_selector("#header-menu:not([hidden])")


def set_view_mode(page: Page, mode: str) -> None:
    """Pick tile|list|card from the ⋯menu's radio items (real clicks)."""
    assert mode in ("tile", "list", "card")
    open_header_menu(page)
    page.locator(f"#menu-view-{mode}").click()
    page.wait_for_selector("#header-menu", state="hidden")
