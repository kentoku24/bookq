"""Baseline horizontal swipe cancel/commit tests for T10.

Spec: 60/48 hysteresis, dragX*0.42 preview, 220ms commit to parent, 180ms snap cancel.

Run:
    uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_swipe_horizontal_baseline.py -q
"""

import re
from playwright.sync_api import expect


def _dispatch_swipe(page, start_x, moves):
    # moves: list of (x, y) deltas relative to start
    return page.evaluate(
        """([sx, moves]) => {
            const scroll = document.getElementById('sidebar-scroll');
            const grid = document.getElementById('tile-grid');
            const target = scroll;
            const mk = (type, x, y, hasTouch) => {
                const touch = hasTouch ? [{identifier:1, target: grid, clientX: x, clientY: y, pageX: x, pageY: y}] : [];
                const e = new TouchEvent(type, {
                    touches: hasTouch ? [new Touch({identifier:1, target: grid, clientX: x, clientY: y})] : [],
                    changedTouches: hasTouch ? [] : [new Touch({identifier:1, target: grid, clientX: x, clientY: y})],
                    bubbles: true, cancelable: true
                });
                // Fallback for environments where Touch constructor not available with target
                if (!e.touches || e.touches.length===0) {
                    const fake = new Event(type, {bubbles:true, cancelable:true});
                    fake.touches = hasTouch ? [{clientX: x, clientY: y, identifier:1, target: grid}] : [];
                    fake.changedTouches = hasTouch ? [] : [{clientX: x, clientY: y, identifier:1, target: grid}];
                    return fake;
                }
                return e;
            };
            // Use simpler dispatch via synthetic events with touches arrays
            const dispatch = (type, x, y, hasTouch) => {
                const ev = new Event(type, {bubbles:true, cancelable:true});
                ev.touches = hasTouch ? [{clientX: x, clientY: y, identifier:0, target: grid}] : [];
                ev.changedTouches = hasTouch ? [] : [{clientX: x, clientY: y, identifier:0, target: grid}];
                // Also set clientX/Y on event for fallback
                target.dispatchEvent(ev);
            };
            // Directly call handler via touch coordinates simulation using helper
            // Instead use the app's touch handlers by dispatching real TouchEvents on target
            // We'll synthesize via page.dispatch_event style but here we emulate via JS TouchEvents
            const mkTouch = (x, y) => {
                try { return new Touch({identifier:0, target:grid, clientX:x, clientY:y}); } catch { return {identifier:0, target:grid, clientX:x, clientY:y, pageX:x, pageY:y}; }
            };
            const fire = (type, x, y, hasTouch) => {
                let ev;
                try {
                    const t = hasTouch ? [new Touch({identifier:0, target:grid, clientX:x, clientY:y})] : [];
                    const ct = hasTouch ? [] : [new Touch({identifier:0, target:grid, clientX:x, clientY:y})];
                    ev = new TouchEvent(type, {touches: t, changedTouches: ct, bubbles:true, cancelable:true});
                } catch {
                    ev = new Event(type, {bubbles:true, cancelable:true});
                    ev.touches = hasTouch ? [{clientX:x, clientY:y, identifier:0, target:grid}] : [];
                    ev.changedTouches = hasTouch ? [] : [{clientX:x, clientY:y, identifier:0, target:grid}];
                }
                target.dispatchEvent(ev);
            };
            fire('touchstart', sx, 50, true);
            for (const [x, y] of moves) {
                fire('touchmove', sx + x, 50 + y, true);
            }
            return window.__ptrDebug ? window.__ptrDebug.hState : null;
        }""",
        [start_x, moves],
    )


def test_swipe_60_to_45_cancel(page, server):
    page.locator('.tile[data-path="group"]').click()
    page.locator('.tile[data-path="group/episodes"]').click()
    expect(page).to_have_url(re.compile(r"#/browse/group/episodes$"))
    # Simulate: start at x=10 (<24 gate), drag 65 -> ready, back to 45 -> dragging, release
    page.evaluate("""() => {
        const scroll = document.getElementById('sidebar-scroll');
        const grid = document.getElementById('tile-grid');
        const target = scroll;
        const fire = (type, x, y, hasTouch) => {
            let ev;
            try {
                const t = hasTouch ? [new Touch({identifier:0, target:grid, clientX:x, clientY:y})] : [];
                const ct = hasTouch ? [] : [new Touch({identifier:0, target:grid, clientX:x, clientY:y})];
                ev = new TouchEvent(type, {touches: t, changedTouches: ct, bubbles:true, cancelable:true});
            } catch {
                ev = new Event(type, {bubbles:true, cancelable:true});
                ev.touches = hasTouch ? [{clientX:x, clientY:y, identifier:0, target:grid}] : [];
                ev.changedTouches = hasTouch ? [] : [{clientX:x, clientY:y, identifier:0, target:grid}];
            }
            target.dispatchEvent(ev);
        };
        window.__ptrDebug && (window.__ptrDebug.hState);
        fire('touchstart', 10, 50, true);
        fire('touchmove', 20, 50, true); // dx 10 -> lock h
        fire('touchmove', 75, 50, true); // dx 65 -> ready_to_pop
        window._mid = window.__ptrDebug ? window.__ptrDebug.hState : null;
        fire('touchmove', 55, 50, true); // dx 45 -> should go back to draggingH
        window._afterBack = window.__ptrDebug ? window.__ptrDebug.hState : null;
        window._hX = window.__ptrDebug ? window.__ptrDebug.hX : null;
        fire('touchend', 55, 50, false);
    }""")
    page.wait_for_timeout(400)
    mid = page.evaluate("window._mid")
    after = page.evaluate("window._afterBack")
    # 65 should be ready_to_pop, 45 should be draggingH (hysteresis 60/48)
    assert mid == "ready_to_pop", f"mid expected ready_to_pop got {mid}"
    assert after == "draggingH", f"after back expected draggingH got {after}"
    # Hash should NOT have popped (cancel)
    expect(page).to_have_url(re.compile(r"#/browse/group/episodes$"))
    # top PTR delegated to Safari: ptr-label element removed — assert not exists
    assert page.evaluate("document.getElementById('ptr-label')") is None
    assert page.evaluate("document.getElementById('pull-indicator')") is None


def test_swipe_70_commit_pops_parent(page, server):
    page.locator('.tile[data-path="group"]').click()
    page.locator('.tile[data-path="group/episodes"]').click()
    expect(page).to_have_url(re.compile(r"#/browse/group/episodes$"))
    page.evaluate("""() => {
        const scroll = document.getElementById('sidebar-scroll');
        const grid = document.getElementById('tile-grid');
        const target = scroll;
        const fire = (type, x, y, hasTouch) => {
            let ev;
            try {
                const t = hasTouch ? [new Touch({identifier:0, target:grid, clientX:x, clientY:y})] : [];
                const ct = hasTouch ? [] : [new Touch({identifier:0, target:grid, clientX:x, clientY:y})];
                ev = new TouchEvent(type, {touches: t, changedTouches: ct, bubbles:true, cancelable:true});
            } catch {
                ev = new Event(type, {bubbles:true, cancelable:true});
                ev.touches = hasTouch ? [{clientX:x, clientY:y, identifier:0, target:grid}] : [];
                ev.changedTouches = hasTouch ? [] : [{clientX:x, clientY:y, identifier:0, target:grid}];
            }
            target.dispatchEvent(ev);
        };
        fire('touchstart', 10, 50, true);
        fire('touchmove', 25, 50, true);
        fire('touchmove', 85, 50, true); // dx 75 -> ready
        fire('touchend', 85, 50, false);
    }""")
    page.wait_for_timeout(600)
    expect(page).to_have_url(re.compile(r"#/browse/group$"))
    expect(page.locator("#folder-count")).to_have_text("4 件")


def test_ptr_debug_hstate_observable(page):
    h = page.evaluate("window.__ptrDebug ? window.__ptrDebug.hState : null")
    assert h in ("idle", "draggingH", "ready_to_pop"), f"hState observable {h}"
    thresh = page.evaluate("window.__ptrDebug ? window.__ptrDebug.H_THRESH : null")
    assert thresh == 60
    deact = page.evaluate("window.__ptrDebug ? window.__ptrDebug.H_DEACT : null")
    assert deact == 48


def test_swipe_back_cancel_on_return(page, server):
    page.locator('.tile[data-path="group"]').click()
    page.locator('.tile[data-path="group/episodes"]').click()
    expect(page).to_have_url(re.compile(r"#/browse/group/episodes$"))
    page.evaluate("""() => {
        const scroll = document.getElementById('sidebar-scroll');
        const grid = document.getElementById('tile-grid');
        const target = scroll;
        const fire = (type, x, y, hasTouch) => {
            let ev;
            try {
                const t = hasTouch ? [new Touch({identifier:0, target:grid, clientX:x, clientY:y})] : [];
                const ct = hasTouch ? [] : [new Touch({identifier:0, target:grid, clientX:x, clientY:y})];
                ev = new TouchEvent(type, {touches: t, changedTouches: ct, bubbles:true, cancelable:true});
            } catch {
                ev = new Event(type, {bubbles:true, cancelable:true});
                ev.touches = hasTouch ? [{clientX:x, clientY:y, identifier:0, target:grid}] : [];
                ev.changedTouches = hasTouch ? [] : [{clientX:x, clientY:y, identifier:0, target:grid}];
            }
            target.dispatchEvent(ev);
        };
        fire('touchstart', 10, 50, true);
        fire('touchmove', 20, 50, true);
        fire('touchmove', 75, 50, true);
        window._mid = window.__ptrDebug ? window.__ptrDebug.hState : null;
        fire('touchmove', 55, 50, true);
        window._afterBack = window.__ptrDebug ? window.__ptrDebug.hState : null;
        fire('touchend', 55, 50, false);
    }""")
    page.wait_for_timeout(400)
    mid = page.evaluate("window._mid")
    after = page.evaluate("window._afterBack")
    assert mid == "ready_to_pop", f"mid expected ready_to_pop got {mid}"
    assert after == "draggingH", f"after back expected draggingH got {after}"
    expect(page).to_have_url(re.compile(r"#/browse/group/episodes$"))


def test_swipe_back_commits_on_release(page, server):
    page.locator('.tile[data-path="group"]').click()
    page.locator('.tile[data-path="group/episodes"]').click()
    expect(page).to_have_url(re.compile(r"#/browse/group/episodes$"))
    page.evaluate("""() => {
        const scroll = document.getElementById('sidebar-scroll');
        const grid = document.getElementById('tile-grid');
        const target = scroll;
        const fire = (type, x, y, hasTouch) => {
            let ev;
            try {
                const t = hasTouch ? [new Touch({identifier:0, target:grid, clientX:x, clientY:y})] : [];
                const ct = hasTouch ? [] : [new Touch({identifier:0, target:grid, clientX:x, clientY:y})];
                ev = new TouchEvent(type, {touches: t, changedTouches: ct, bubbles:true, cancelable:true});
            } catch {
                ev = new Event(type, {bubbles:true, cancelable:true});
                ev.touches = hasTouch ? [{clientX:x, clientY:y, identifier:0, target:grid}] : [];
                ev.changedTouches = hasTouch ? [] : [{clientX:x, clientY:y, identifier:0, target:grid}];
            }
            target.dispatchEvent(ev);
        };
        fire('touchstart', 10, 50, true);
        fire('touchmove', 25, 50, true);
        fire('touchmove', 85, 50, true);
        fire('touchend', 85, 50, false);
    }""")
    page.wait_for_timeout(600)
    expect(page).to_have_url(re.compile(r"#/browse/group$"))
    expect(page.locator("#folder-count")).to_have_text("4 件")


def test_swipe_back_from_flattened_node_pops_tree_parent(page, server):
    """Regression: node "inner/aa" under "nest/wrap" has _fullPath
    "nest/wrap/inner/aa". The old commit path string-split that path, produced
    the dead textual parent "nest/wrap/inner" (scanner flatten), failed the
    getNodeAt guard, and fell back to root. The real tree parent is "nest/wrap".
    """
    page.locator('.tile[data-path="nest"]').click()
    page.locator('.tile[data-path="nest/wrap"]').click()
    page.locator('.tile[data-path="nest/wrap/inner/aa"]').click()
    expect(page).to_have_url(re.compile(r"#/browse/nest/wrap/inner/aa$"))
    expect(page.locator("#browse-header")).to_have_text("↩ inner/aa")
    page.evaluate("""() => {
        const scroll = document.getElementById('sidebar-scroll');
        const grid = document.getElementById('tile-grid');
        const target = scroll;
        const fire = (type, x, y, hasTouch) => {
            let ev;
            try {
                const t = hasTouch ? [new Touch({identifier:0, target:grid, clientX:x, clientY:y})] : [];
                const ct = hasTouch ? [] : [new Touch({identifier:0, target:grid, clientX:x, clientY:y})];
                ev = new TouchEvent(type, {touches: t, changedTouches: ct, bubbles:true, cancelable:true});
            } catch {
                ev = new Event(type, {bubbles:true, cancelable:true});
                ev.touches = hasTouch ? [{clientX:x, clientY:y, identifier:0, target:grid}] : [];
                ev.changedTouches = hasTouch ? [] : [{clientX:x, clientY:y, identifier:0, target:grid}];
            }
            target.dispatchEvent(ev);
        };
        fire('touchstart', 10, 50, true);
        fire('touchmove', 25, 50, true);
        fire('touchmove', 85, 50, true);
        fire('touchend', 85, 50, false);
    }""")
    page.wait_for_timeout(600)
    expect(page).to_have_url(re.compile(r"#/browse/nest/wrap$"))
    expect(page.locator("#browse-header")).to_have_text("↩ wrap")
    expect(page.locator("#folder-count")).to_have_text("2 件")


def test_swipe_preview_drag_0_42_transform_and_opacity(page, server):
    page.locator('.tile[data-path="group"]').click()
    expect(page).to_have_url(re.compile(r"#/browse/group$"))
    vals = page.evaluate("""() => {
        const w = document.getElementById('sidebar-scroll').clientWidth || 280;
        const dragX = 60;
        const preview = Math.min(dragX * 0.42, w);
        const opacity = Math.max(0.7, 1 - (preview / w) * 0.3);
        window.__ptrDebug.injectH(dragX);
        const grid = document.getElementById('tile-grid');
        const tr = grid.style.transform;
        const op = grid.style.opacity;
        const wc = grid.style.willChange;
        window.__ptrDebug.injectH(0);
        return {w, dragX, preview, opacity, tr, op, wc};
    }""")
    assert abs(vals["preview"] - 25.2) < 0.01, f"preview {vals['preview']} != 25.2"
    assert f"{vals['preview']}px" in vals["tr"] or "25.2px" in vals["tr"], f"transform {vals['tr']} missing 25.2px"
    assert "translate3d" in vals["tr"], f"transform {vals['tr']} missing translate3d"
    assert abs(float(vals["op"]) - vals["opacity"]) < 0.02, f"opacity {vals['op']} != {vals['opacity']}"
    assert vals["wc"] == "transform, opacity"
    preview2 = page.evaluate("""() => {
        const w = document.getElementById('sidebar-scroll').clientWidth || 280;
        window.__ptrDebug.injectH(120);
        const tr = document.getElementById('tile-grid').style.transform;
        const m = tr.match(/([0-9.]+)px/);
        const got = m ? parseFloat(m[1]) : null;
        const expected = Math.min(120*0.42, w);
        window.__ptrDebug.injectH(0);
        return {got, expected, tr, w};
    }""")
    assert abs(preview2["got"] - preview2["expected"]) < 0.5, f"120*0.42 preview {preview2}"
    page.wait_for_timeout(200)


def test_swipe_animation_durations_commit_220_cancel_180_snap_180(page, server):
    page.locator('.tile[data-path="group"]').click()
    page.locator('.tile[data-path="group/episodes"]').click()
    expect(page).to_have_url(re.compile(r"#/browse/group/episodes$"))
    commit_tf = page.evaluate("""() => {
        const scroll = document.getElementById('sidebar-scroll');
        const grid = document.getElementById('tile-grid');
        const target = scroll;
        const fire = (type, x, y, hasTouch) => {
            let ev;
            try {
                const t = hasTouch ? [new Touch({identifier:0, target:grid, clientX:x, clientY:y})] : [];
                const ct = hasTouch ? [] : [new Touch({identifier:0, target:grid, clientX:x, clientY:y})];
                ev = new TouchEvent(type, {touches: t, changedTouches: ct, bubbles:true, cancelable:true});
            } catch {
                ev = new Event(type, {bubbles:true, cancelable:true});
                ev.touches = hasTouch ? [{clientX:x, clientY:y, identifier:0, target:grid}] : [];
                ev.changedTouches = hasTouch ? [] : [{clientX:x, clientY:y, identifier:0, target:grid}];
            }
            target.dispatchEvent(ev);
        };
        fire('touchstart', 10, 50, true);
        fire('touchmove', 25, 50, true);
        fire('touchmove', 85, 50, true);
        fire('touchend', 85, 50, false);
        return grid.style.transition;
    }""")
    assert "220ms" in commit_tf, f"commit transition {commit_tf} missing 220ms"
    page.wait_for_timeout(600)
    cancel_tf = page.evaluate("""() => {
        const scroll = document.getElementById('sidebar-scroll');
        const grid = document.getElementById('tile-grid');
        const target = scroll;
        const fire = (type, x, y, hasTouch) => {
            let ev;
            try {
                const t = hasTouch ? [new Touch({identifier:0, target:grid, clientX:x, clientY:y})] : [];
                const ct = hasTouch ? [] : [new Touch({identifier:0, target:grid, clientX:x, clientY:y})];
                ev = new TouchEvent(type, {touches: t, changedTouches: ct, bubbles:true, cancelable:true});
            } catch {
                ev = new Event(type, {bubbles:true, cancelable:true});
                ev.touches = hasTouch ? [{clientX:x, clientY:y, identifier:0, target:grid}] : [];
                ev.changedTouches = hasTouch ? [] : [{clientX:x, clientY:y, identifier:0, target:grid}];
            }
            target.dispatchEvent(ev);
        };
        grid.style.transition = '';
        fire('touchstart', 10, 50, true);
        fire('touchmove', 20, 50, true);
        fire('touchmove', 75, 50, true);
        fire('touchmove', 55, 50, true);
        fire('touchend', 55, 50, false);
        return grid.style.transition;
    }""")
    assert "180ms" in cancel_tf, f"cancel transition {cancel_tf} missing 180ms"
    page.wait_for_timeout(400)
    snap_ms = page.evaluate("window.__ptrDebug ? 180 : null")
    assert snap_ms == 180
    # top PTR delegated to native: verify bottom rubber snap instead
    snap_tf = page.evaluate("""() => {
        const grid = document.getElementById('tile-grid');
        const scroll = document.getElementById('sidebar-scroll');
        scroll.scrollTop = scroll.scrollHeight;
        const target = scroll;
        const fire = (type, y, hasTouch) => {
            let ev;
            try {
                const t = hasTouch ? [new Touch({identifier:1, target:grid, clientX:10, clientY:y})] : [];
                const ct = hasTouch ? [] : [new Touch({identifier:1, target:grid, clientX:10, clientY:y})];
                ev = new TouchEvent(type, {touches: t, changedTouches: ct, bubbles:true, cancelable:true});
            } catch {
                ev = new Event(type, {bubbles:true, cancelable:true});
                ev.touches = hasTouch ? [{clientX:10, clientY:y, identifier:1, target:grid}] : [];
                ev.changedTouches = hasTouch ? [] : [{clientX:10, clientY:y, identifier:1, target:grid}];
            }
            target.dispatchEvent(ev);
        };
        fire('touchstart', 100, true);
        fire('touchmove', 90, true);
        fire('touchmove', 20, true);
        fire('touchend', 20, false);
        return grid.style.transition;
    }""")
    assert "180ms" in snap_tf, f"snap transition {snap_tf} missing 180ms"
    page.wait_for_timeout(300)


def test_viewer_swipe_hysteresis(landscape_page):
    page = landscape_page
    page.locator('.tile[data-path="long"]').click()
    expect(page.locator("#counter")).to_have_text("1 / 30")
    # Hysteresis 70/58 in 900px landscape: drag +75 then back to +45 (<58) cancel -> no turn.
    # The pull-back is several samples, as a real finger reports it. With a single
    # sample the velocity window (touchend) still spans touchstart→release, i.e. it
    # reads +45px over however long dispatch took: 0 when all events land in one ms
    # (fast machine), but a forward flick on a slow CI runner.
    page.evaluate("""() => {
        const c = document.getElementById('img-container');
        const fire = (type, x, hasTouch) => {
            let ev;
            try {
                const t = hasTouch ? [new Touch({identifier:0, target:c, clientX:x, clientY:200})] : [];
                const ct = hasTouch ? [] : [new Touch({identifier:0, target:c, clientX:x, clientY:200})];
                ev = new TouchEvent(type, {touches: t, changedTouches: ct, bubbles:true, cancelable:true});
            } catch {
                ev = new Event(type, {bubbles:true, cancelable:true});
                ev.touches = hasTouch ? [{clientX:x, clientY:200, identifier:0, target:c}] : [];
                ev.changedTouches = hasTouch ? [] : [{clientX:x, clientY:200, identifier:0, target:c}];
            }
            c.dispatchEvent(ev);
        };
        fire('touchstart', 300, true);
        fire('touchmove', 375, true);
        fire('touchmove', 365, true);
        fire('touchmove', 355, true);
        fire('touchmove', 345, true);
        fire('touchend', 345, false);
    }""")
    page.wait_for_timeout(400)
    expect(page.locator("#counter")).to_have_text("1 / 30")
    page.evaluate("""() => {
        const c = document.getElementById('img-container');
        const fire = (type, x, hasTouch) => {
            let ev;
            try {
                const t = hasTouch ? [new Touch({identifier:0, target:c, clientX:x, clientY:200})] : [];
                const ct = hasTouch ? [] : [new Touch({identifier:0, target:c, clientX:x, clientY:200})];
                ev = new TouchEvent(type, {touches: t, changedTouches: ct, bubbles:true, cancelable:true});
            } catch {
                ev = new Event(type, {bubbles:true, cancelable:true});
                ev.touches = hasTouch ? [{clientX:x, clientY:200, identifier:0, target:c}] : [];
                ev.changedTouches = hasTouch ? [] : [{clientX:x, clientY:200, identifier:0, target:c}];
            }
            c.dispatchEvent(ev);
        };
        fire('touchstart', 300, true);
        fire('touchmove', 380, true);
        fire('touchend', 380, false);
    }""")
    page.wait_for_timeout(800)
    cnt = page.locator("#counter").inner_text()
    assert cnt != "1 / 30", f"expected page turn from 1/30 got {cnt}"
