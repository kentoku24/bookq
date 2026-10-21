"""Browse URL-state behavior tests (Todo 1: browse hash writers + ancestor-pop stack).

T1 covers the writer half of the browse URL-state feature: drilling into a
folder writes `#/browse/<path>` (one pushState per level), the breadcrumb ↩
goes UP one tree level — popping to an ancestor entry in the tracked stack
(history.go(-k)) instead of pushing a duplicate — and identical-URL writes are
no-ops, so the history never grows from browse navigation.

The reader half (deep links, reload restore, Back/Forward re-sync, dead-path
fallback) is wired in Todo 2 and covered by tests added there.

Run:
    uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_browse_url_state.py -q
"""

import re

from playwright.sync_api import expect


def test_drill_down_sets_browse_hash(page):
    # Tile mode: drilling into a folder writes #/browse/<path>.
    page.locator('.tile[data-path="series"]').click()
    expect(page).to_have_url(re.compile(r"#/browse/series$"))
    # Back to root via the breadcrumb, switch to list mode, drill again.
    page.locator("#browse-header").click()
    page.locator("#footer-refresh-btn").click()  # tile → list (one click)
    page.locator('.item[data-path="group"]').click()
    # The view mode (v=list) carries in the URL across folder navigation
    # (Task 2, PR 4) — only the default "tile" mode omits the param.
    expect(page).to_have_url(re.compile(r"#/browse/group\?v=list$"))


def test_breadcrumb_updates_hash(page, server):
    page.locator('.tile[data-path="group"]').click()
    page.locator('.tile[data-path="group/episodes"]').click()
    expect(page).to_have_url(re.compile(r"#/browse/group/episodes$"))
    expect(page.locator("#folder-count")).to_have_text("4 件")
    # Up one level: group.
    page.locator("#browse-header").click()
    expect(page).to_have_url(re.compile(r"#/browse/group$"))
    expect(page.locator("#folder-count")).to_have_text("4 件")
    expect(page.locator("#browse-header")).to_have_text("↩ group")
    # Up to root: canonical no-hash URL.
    # Root count includes the parent-level images-tile ("mixed").
    page.locator("#browse-header").click()
    expect(page).to_have_url(re.compile(r"^" + re.escape(server) + r"/?$"))
    expect(page.locator("#folder-count")).to_have_text("18 件")
    expect(page.locator("#browse-header")).to_have_text("📁 フォルダ")


def test_breadcrumb_pop_does_not_duplicate_entries(page, server):
    page.locator('.tile[data-path="group"]').click()
    page.locator('.tile[data-path="group/episodes"]').click()
    before = page.evaluate("history.length")  # 3: initial load + group + episodes
    page.locator("#browse-header").click()  # pop to group, not a duplicate push
    expect(page).to_have_url(re.compile(r"#/browse/group$"))
    after = page.evaluate("history.length")
    assert after == before, f"breadcrumb click grew history: {before} -> {after}"
    page.go_back()
    # Back lands on root (NOT episodes): proves the pop happened instead of a
    # duplicate push — a duplicate push would make go_back land on episodes.
    # Root-grid rendering on a no-hash arrival is T2's Oracle-F1 behavior (test_back_walks_hierarchy final leg).
    expect(page).to_have_url(re.compile(r"^" + re.escape(server) + r"/?$"))


def test_breadcrumb_up_does_not_grow_history(page):
    baseline = page.evaluate("history.length")
    page.locator('.tile[data-path="group"]').click()           # +1 entry
    page.locator('.tile[data-path="group/episodes"]').click()  # +1 entry
    page.locator("#browse-header").click()  # pop to group — no growth
    page.locator("#browse-header").click()  # pop to root — no growth
    assert page.evaluate("history.length") == baseline + 2


def test_drill_into_flattened_wrapper_sets_hash(page):
    # "wrap" is a root tile (single-child flattening renames its CHILDREN to
    # wrap/inner/vol1..2 — the root tile itself is plain "wrap").
    page.locator('.tile[data-path="wrap"]').click()
    expect(page).to_have_url(re.compile(r"#/browse/wrap$"))
    expect(page.locator("#folder-count")).to_have_text("2 件")
    expect(page.locator("#browse-header")).to_have_text("↩ wrap")


# ── T2: browse restore (deep links, Back from a book, reload) ─────────────────
# The reader half: applyUrlState gains a #/browse/ branch that restores the
# folder level (teardownViewer + renderView + scroll), dead/empty paths
# normalize to canonical root (no hash), and a no-hash arrival while a browse
# level or book is showing resets to root browse (Oracle F1).


def test_deep_link_browse_restores_level(page, server):
    page.goto(server + "/#/browse/group/episodes")
    expect(page.locator("#folder-count")).to_have_text("4 件")
    expect(page.locator("#browse-header")).to_have_text("↩ episodes")
    paths = [t.get_attribute("data-path") for t in page.locator(".tile").all()]
    assert paths == [
        "group/episodes/ch-1",
        "group/episodes/ch-2",
        "group/episodes/ch-10",
        "group/episodes/ch-11",
    ]


def test_deep_link_browse_root_normalizes(page, server):
    page.goto(server + "/#/browse/")
    expect(page.locator("#folder-count")).to_have_text("18 件")
    expect(page.locator("#browse-header")).to_have_text("📁 フォルダ")
    expect(page).to_have_url(re.compile(r"^" + re.escape(server) + r"/?$"))


def test_dead_browse_path_falls_back_to_root(page, server):
    page.goto(server + "/#/browse/nonexistent")
    expect(page.locator("#folder-count")).to_have_text("18 件")
    expect(page).to_have_url(re.compile(r"^" + re.escape(server) + r"/?$"))


def test_back_from_book_returns_to_browse_level(page, server):
    page.locator('.tile[data-path="group"]').click()
    page.locator('.tile[data-path="group/deep_series"]').click()
    expect(page).to_have_url(re.compile(r"#/browse/group/deep_series$"))
    page.locator('.tile[data-path="group/deep_series/vol1"]').click()
    expect(page.locator("#counter")).to_have_text("1 / 2")
    expect(page).to_have_url(re.compile(r"#/view/group/deep_series/vol1\?p=0$"))
    page.go_back()
    expect(page).to_have_url(re.compile(r"#/browse/group/deep_series$"))
    expect(page.locator("#folder-count")).to_have_text("2 件")
    expect(page.locator("#browse-header")).to_have_text("↩ deep_series")


def test_sidebar_toggle_from_reader_writes_browse_url(page, server):
    # Opening the ☰ sidebar from a reader returns to the browse level and
    # writes #/browse/<level> — the hash must match the browse grid on screen,
    # not stay a stale #/view/<book> while the reader is gone.
    page.locator('.tile[data-path="group"]').click()
    page.locator('.tile[data-path="group/deep_series"]').click()
    page.locator('.tile[data-path="group/deep_series/vol1"]').click()
    expect(page).to_have_url(re.compile(r"#/view/group/deep_series/vol1\?p=0$"))
    page.locator("#menu-btn").click()
    expect(page).to_have_url(re.compile(r"#/browse/group/deep_series$"))
    expect(page.locator("#folder-count")).to_have_text("2 件")
    expect(page.locator("#browse-header")).to_have_text("↩ deep_series")


def test_back_walks_hierarchy(page, server):
    page.locator('.tile[data-path="group"]').click()
    page.locator('.tile[data-path="group/episodes"]').click()
    page.locator('.tile[data-path="group/episodes/ch-1"]').click()
    expect(page).to_have_url(re.compile(r"#/view/group/episodes/ch-1\?p=0$"))
    page.go_back()
    expect(page).to_have_url(re.compile(r"#/browse/group/episodes$"))
    expect(page.locator("#folder-count")).to_have_text("4 件")
    expect(page.locator("#browse-header")).to_have_text("↩ episodes")
    page.go_back()
    expect(page).to_have_url(re.compile(r"#/browse/group$"))
    expect(page.locator("#folder-count")).to_have_text("4 件")
    page.go_back()
    expect(page).to_have_url(re.compile(r"^" + re.escape(server) + r"/?$"))
    expect(page.locator("#folder-count")).to_have_text("18 件")
    expect(page.locator("#browse-header")).to_have_text("📁 フォルダ")


def test_back_forward_then_breadcrumb_does_not_duplicate(page, server):
    # Regression: after Back→Forward, the breadcrumb must still POP
    # (history.go) to the ancestor instead of pushing a duplicate entry. This
    # broke when reconcile popped the urlStack on Back — discarding the prefix
    # Forward later re-enters, so the breadcrumb fell back to pushState.
    page.locator('.tile[data-path="group"]').click()
    page.locator('.tile[data-path="group/episodes"]').click()
    expect(page).to_have_url(re.compile(r"#/browse/group/episodes$"))
    page.go_back()
    expect(page).to_have_url(re.compile(r"#/browse/group$"))
    page.go_back()
    expect(page).to_have_url(re.compile(r"^" + re.escape(server) + r"/?$"))
    page.go_forward()
    expect(page).to_have_url(re.compile(r"#/browse/group$"))
    page.go_forward()
    expect(page).to_have_url(re.compile(r"#/browse/group/episodes$"))
    before = page.evaluate("history.length")
    page.locator("#browse-header").click()  # pop to group — no growth
    expect(page).to_have_url(re.compile(r"#/browse/group$"))
    after = page.evaluate("history.length")
    assert after == before, f"breadcrumb after Back→Forward grew history: {before} -> {after}"


def test_deep_link_flattened_child_up_returns_to_tree_parent(page, server):
    page.goto(server + "/#/browse/wrap/inner/vol1")
    expect(page.locator("#tile-grid")).to_contain_text("フォルダが見つかりません")
    expect(page.locator("#browse-header")).to_have_text("↩ inner/vol1")
    page.locator("#browse-header").click()
    expect(page).to_have_url(re.compile(r"#/browse/wrap$"))
    expect(page.locator("#folder-count")).to_have_text("2 件")
    expect(page.locator("#browse-header")).to_have_text("↩ wrap")


def test_deep_link_childless_folder_keeps_browse_level(page, server):
    page.goto(server + "/#/browse/empty_folder")
    expect(page.locator("#tile-grid")).to_contain_text("フォルダが見つかりません")
    expect(page.locator("#browse-header")).to_have_text("↩ empty_folder")
    expect(page).to_have_url(re.compile(r"#/browse/empty_folder$"))


def test_reload_then_up_falls_back_to_push(page, server):
    page.goto(server + "/#/browse/group/episodes")
    expect(page.locator("#folder-count")).to_have_text("4 件")
    page.locator("#browse-header").click()
    expect(page).to_have_url(re.compile(r"#/browse/group$"))
    expect(page.locator("#folder-count")).to_have_text("4 件")
    expect(page.locator("#browse-header")).to_have_text("↩ group")
    page.go_back()
    expect(page).to_have_url(re.compile(r"#/browse/group/episodes$"))


def test_back_into_browse_clears_active_search(page):
    page.locator("#search-input").fill("manga")
    page.wait_for_timeout(600)  # debounce 300ms + API roundtrip
    page.evaluate("location.hash = '#/browse/group'")
    expect(page.locator("#folder-count")).to_have_text("4 件")
    expect(page.locator("#browse-header")).to_have_text("↩ group")
    expect(page.locator("#search-input")).to_have_value("")
    expect(page).to_have_url(re.compile(r"#/browse/group$"))


def test_page_turn_after_browse_push_does_not_grow_history(page):
    page.locator('.tile[data-path="series"]').click()
    expect(page).to_have_url(re.compile(r"#/browse/series$"))
    page.locator('.tile[data-path="series/vol1"]').click()
    expect(page).to_have_url(re.compile(r"#/view/series/vol1\?p=0$"))
    before = page.evaluate("history.length")
    page.locator("#zone-prev").click()
    expect(page.locator("#counter")).to_have_text("2 / 2")
    expect(page).to_have_url(re.compile(r"#/view/series/vol1\?p=1$"))
    after = page.evaluate("history.length")
    assert after == before, f"page turn grew history: {before} -> {after}"


# ── T3: consistency — empty/failure branches, resume hop, pull-refresh ────────
# Every path that clears/rewrites the hash to no-hash must also reset the
# browse state (currentBrowsePath = null, urlStack = [], searchMode cleared)
# so the screen matches the canonical root URL (stale-stack hazard, Oracle F4).


def test_empty_viewer_reset_renders_root_browse(page, server):
    page.goto(server + "/#/view/empty_folder?p=0")
    expect(page.locator("#empty")).to_have_text("📭 このフォルダに画像はありません")
    expect(page).to_have_url(re.compile(r"^" + re.escape(server) + r"/?$"))
    expect(page.locator("#folder-count")).to_have_text("18 件")
    expect(page.locator("#sidebar")).to_have_class(re.compile(r"open"))


def test_resume_hop_keeps_url_consistent(page, server):
    # group/episodes is a non-leaf (folder children only), so the hop renders
    # group browse, then loadImages("group/episodes") returns [] → empty branch
    # → root browse + no-hash URL.
    page.evaluate(
        "localStorage.setItem('bookq_progress', "
        "JSON.stringify({'group/episodes': {page: 0, total: 1, ts: Date.now()}}))"
    )
    page.reload()
    page.locator("#resume-btn").click()
    expect(page).to_have_url(re.compile(r"^" + re.escape(server) + r"/?$"))
    expect(page.locator("#folder-count")).to_have_text("18 件")
    expect(page.locator("#sidebar")).to_have_class(re.compile(r"open"))


def test_resume_leaf_opens_book(page, server):
    page.evaluate(
        "localStorage.setItem('bookq_progress', "
        "JSON.stringify({'group/deep_series/vol1': {page: 0, total: 2, ts: Date.now()}}))"
    )
    page.reload()
    page.locator("#resume-btn").click()
    expect(page).to_have_url(re.compile(r"#/view/group/deep_series/vol1\?p=0$"))
    expect(page.locator("#folder-name")).to_contain_text("vol1")


def test_pull_refresh_preserves_browse_hash(page, server):
    page.locator('.tile[data-path="series"]').click()
    expect(page).to_have_url(re.compile(r"#/browse/series$"))
    page.evaluate("""() => {
      const grid = document.getElementById('tile-grid');
      const scroll = document.getElementById('sidebar-scroll');
      scroll.scrollTop = 0;
      const mk = (type, y, hasTouch) => {
        let ev;
        try {
          ev = new TouchEvent(type, {
            touches: hasTouch ? [new Touch({identifier: 1, target: grid, clientX: 10, clientY: y})] : [],
            changedTouches: hasTouch ? [] : [new Touch({identifier: 1, target: grid, clientX: 10, clientY: y})],
            bubbles: true, cancelable: true
          });
        } catch {
          ev = new Event(type, {bubbles:true, cancelable:true});
          ev.touches = hasTouch ? [{clientX:10, clientY:y, identifier:1, target:grid}] : [];
          ev.changedTouches = hasTouch ? [] : [{clientX:10, clientY:y, identifier:1, target:grid}];
        }
        scroll.dispatchEvent(ev);
      };
      mk('touchstart', 20, true);
      mk('touchmove', 40, true);
      mk('touchmove', 60, true);
      mk('touchmove', 80, true);
      mk('touchmove', 100, true);
      mk('touchend', 100, false);
    }""")
    page.wait_for_timeout(900)
    expect(page).to_have_url(re.compile(r"#/browse/series$"))
    expect(page.locator("#folder-count")).to_have_text("2 件")


def test_pull_refresh_at_root_keeps_root(page, server):
    expect(page).to_have_url(re.compile(r"^" + re.escape(server) + r"/?$"))
    expect(page.locator("#folder-count")).to_have_text("18 件")
    page.evaluate("""() => {
      const grid = document.getElementById('tile-grid');
      const scroll = document.getElementById('sidebar-scroll');
      scroll.scrollTop = 0;
      const mk = (type, y, hasTouch) => {
        let ev;
        try {
          ev = new TouchEvent(type, {
            touches: hasTouch ? [new Touch({identifier: 1, target: grid, clientX: 10, clientY: y})] : [],
            changedTouches: hasTouch ? [] : [new Touch({identifier: 1, target: grid, clientX: 10, clientY: y})],
            bubbles: true, cancelable: true
          });
        } catch {
          ev = new Event(type, {bubbles:true, cancelable:true});
          ev.touches = hasTouch ? [{clientX:10, clientY:y, identifier:1, target:grid}] : [];
          ev.changedTouches = hasTouch ? [] : [{clientX:10, clientY:y, identifier:1, target:grid}];
        }
        scroll.dispatchEvent(ev);
      };
      mk('touchstart', 20, true);
      mk('touchmove', 40, true);
      mk('touchmove', 60, true);
      mk('touchmove', 80, true);
      mk('touchmove', 100, true);
      mk('touchend', 100, false);
    }""")
    page.wait_for_timeout(900)
    expect(page).to_have_url(re.compile(r"^" + re.escape(server) + r"/?$"))
    expect(page.locator("#folder-count")).to_have_text("18 件")


# ── T4: unicode round-trip + manual hash edit ─────────────────────────────────
# No browse-able level has a non-ASCII name in the fixture — unicode paths are
# exercised via deep links to unicode-named CHILD leaves (present-but-childless
# levels: 【第一部】 is an image-only leaf under kanji/series1).


def test_browse_unicode_child_round_trip(page, server):
    page.goto(server + "/#/browse/kanji/series1/%E3%80%90%E7%AC%AC%E4%B8%80%E9%83%A8%E3%80%91")
    expect(page.locator("#browse-header")).to_have_text("↩ 【第一部】", timeout=5000)
    expect(page.locator("#tile-grid")).to_contain_text("フォルダが見つかりません", timeout=5000)
    expect(page).to_have_url(re.compile(r"#/browse/kanji/series1/%E3%80%90%E7%AC%AC%E4%B8%80%E9%83%A8%E3%80%91$"))
    page.reload()
    page.wait_for_selector("#browse-header", timeout=5000)
    expect(page.locator("#browse-header")).to_have_text("↩ 【第一部】", timeout=5000)
    expect(page.locator("#tile-grid")).to_contain_text("フォルダが見つかりません", timeout=5000)
    expect(page).to_have_url(re.compile(r"#/browse/kanji/series1/%E3%80%90%E7%AC%AC%E4%B8%80%E9%83%A8%E3%80%91$"))


def test_manual_hash_edit_to_browse(page):
    page.evaluate("location.hash = '#/browse/group/deep_series'")
    expect(page.locator("#folder-count")).to_have_text("2 件")
    expect(page.locator("#browse-header")).to_have_text("↩ deep_series")


def test_manual_hash_edit_away_clears_to_root(page, server):
    page.locator('.tile[data-path="group"]').click()
    expect(page).to_have_url(re.compile(r"#/browse/group$"))
    page.evaluate("location.hash = ''")
    expect(page.locator("#folder-count")).to_have_text("18 件")
    expect(page).to_have_url(re.compile(r"^" + re.escape(server) + r"/?$"))


def test_stash_bridge_browse_hash_restored(page, server):
    page.add_init_script("sessionStorage.setItem('bookq_return_hash', JSON.stringify({hash: '#/browse/group/deep_series', ts: Date.now()}));")
    page.goto(server)
    expect(page.locator("#folder-count")).to_have_text("2 件", timeout=5000)
    expect(page.locator("#browse-header")).to_have_text("↩ deep_series", timeout=5000)
    expect(page).to_have_url(re.compile(r"#/browse/group/deep_series$"))


def test_malformed_browse_hash_falls_back_to_root(page, server):
    page.goto(server + "/#/browse/%zz")
    expect(page.locator("#folder-count")).to_have_text("18 件")
    expect(page).to_have_url(re.compile(r"^" + re.escape(server) + r"/?$"))
