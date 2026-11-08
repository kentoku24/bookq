"""Dual-reader pairing UI (S08 / #83) — mock-controller-driven DOM asserts.

`app/static/viewer/pairUI.js` exposes `window.PairUI`, driven by an injected
controller interface::

    { create(), join(code), approve(), reject(), leave(), swapSides(),
      subscribe(cb) -> unsubscribe, state }

`state` is the controller-owned snapshot the UI renders::

    { status, role, code, joinRequest, side, transport, error }
    status: idle | connecting | connected | reconnecting | peer-away |
            loading | error | ended
    role/side/transport: 'leader'|'follower' / 'right'|'left' /
            'server-ws'|'webrtc-dc' / null

Tests inject a mock controller via ``add_init_script`` (``window.PairController``
+ a ``window.__pairEmit(patch)`` state driver) — production paths stay
untouched; the real sync client wiring lands in S09 (#84).

Run:
    uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_reader_pairing_ui.py -q
"""

from playwright.sync_api import expect

from viewer_touch import open_book, tap, touch_session

MOCK_JS = """
(() => {
  const subs = [];
  window.__pairCalls = [];
  const ctl = {
    state: {status:'idle', role:null, code:null, joinRequest:false,
            side:null, transport:null, error:null},
    subscribe(cb) {
      subs.push(cb);
      return () => { const i = subs.indexOf(cb); if (i >= 0) subs.splice(i, 1); };
    },
    create() { window.__pairCalls.push('create'); },
    join(code) { window.__pairCalls.push('join:' + code); },
    approve() { window.__pairCalls.push('approve'); },
    reject() { window.__pairCalls.push('reject'); },
    leave() { window.__pairCalls.push('leave'); },
    swapSides() { window.__pairCalls.push('swapSides'); },
  };
  window.__pairEmit = (patch) => {
    ctl.state = Object.assign({}, ctl.state, patch);
    subs.slice().forEach(cb => cb(ctl.state));
  };
  window.PairController = ctl;
})();
"""

# Two windows in the same context share this BroadcastChannel link: a state
# emitted on one page is relayed to the other's controller, so the test can
# observe whether a local UI action ever leaks onto the peer.
LINKED_MOCK_JS = """
(() => {
  const ch = new BroadcastChannel('pair-ui-test-link');
  const subs = [];
  window.__pairCalls = [];
  window.__pairRx = [];
  const ctl = {
    state: {status:'idle', role:null, code:null, joinRequest:false,
            side:null, transport:null, error:null},
    subscribe(cb) {
      subs.push(cb);
      return () => { const i = subs.indexOf(cb); if (i >= 0) subs.splice(i, 1); };
    },
    create() { window.__pairCalls.push('create'); },
    join(code) { window.__pairCalls.push('join:' + code); },
    approve() { window.__pairCalls.push('approve'); },
    reject() { window.__pairCalls.push('reject'); },
    leave() { window.__pairCalls.push('leave'); },
    swapSides() { window.__pairCalls.push('swapSides'); },
  };
  const apply = (patch) => {
    ctl.state = Object.assign({}, ctl.state, patch);
    subs.slice().forEach(cb => cb(ctl.state));
  };
  window.__pairEmit = (patch) => { apply(patch); ch.postMessage(patch); };
  ch.onmessage = (ev) => { window.__pairRx.push(ev.data); apply(ev.data); };
  window.PairController = ctl;
})();
"""


def _with_mock(page, js=MOCK_JS):
    """Install the mock controller and reload so it exists before page scripts."""
    page.add_init_script(js)
    page.reload()
    page.wait_for_selector("#pair-btn")


def _ensure_sidebar_open(page):
    """The pair launcher lives in the sidebar header — the sidebar must be
    open to reach it. classList-only (not toggleSidebar): the real toggle
    also navigates back to browse, which would invalidate viewer-state
    assertions in tests that have a book loaded."""
    page.evaluate(
        """var s = document.getElementById('sidebar');
           if (!s.classList.contains('open')) {
             s.classList.add('open');
             document.getElementById('viewer').classList.add('sidebar-open');
             if (window.syncSidebarA11y) window.syncSidebarA11y();
           }""")


def _open_ui(page):
    """Open the sidebar (the launcher lives in its header) and open the
    pairing dialog."""
    page.wait_for_function("typeof window.closeSidebar === 'function'")
    _ensure_sidebar_open(page)
    page.click("#pair-btn")
    expect(page.locator("#pair-overlay")).to_be_visible()


def _emit(page, patch):
    page.evaluate(f"window.__pairEmit({patch})")


# Every render shows exactly one section — everything else is `hidden`.
PAIR_SECTIONS = (
    "#pair-sec-choice #pair-sec-join #pair-sec-leader "
    "#pair-sec-waiting #pair-sec-session #pair-sec-error #pair-sec-picker"
).split()


def _only_section_visible(page, sel):
    for s in PAIR_SECTIONS:
        if s == sel:
            expect(page.locator(s)).to_be_visible()
        else:
            expect(page.locator(s)).to_be_hidden()


def _visible_control_boxes(page):
    """Bounding boxes of every rendered control inside the pairing dialog."""
    return page.evaluate(
        """() => [...document.querySelectorAll(
               '#pair-panel button, #pair-panel input')]
            .filter(el => {
              const r = el.getBoundingClientRect();
              return r.width > 0 && r.height > 0;
            })
            .map(el => {
              const r = el.getBoundingClientRect();
              return {id: el.id || el.className,
                      x: r.x, y: r.y, w: r.width, h: r.height,
                      right: r.right, bottom: r.bottom};
            })"""
    )


# ── entry point ──


def test_entry_point_exists_and_offers_start_join(page, server):
    _with_mock(page)
    btn = page.locator("#pair-btn")
    expect(btn).to_be_visible()
    assert btn.get_attribute("aria-label") == "2台で見開き"
    _open_ui(page)
    expect(page.locator("#pair-create")).to_be_visible()
    expect(page.locator("#pair-join-open")).to_be_visible()


# ── code input ──


def test_code_input_numeric_mode_and_leading_zero(page, server):
    _with_mock(page)
    _open_ui(page)
    page.click("#pair-join-open")
    code = page.locator("#pair-code-input")
    expect(code).to_be_visible()
    assert code.get_attribute("inputmode") == "numeric"
    # leading zeros must survive as the input VALUE (string, not a number)
    code.click()
    page.keyboard.type("012345")
    assert code.input_value() == "012345"
    assert page.locator("#pair-join-submit").is_enabled()


def test_code_input_paste_accepts_six_digits(page, server):
    _with_mock(page)
    _open_ui(page)
    page.click("#pair-join-open")
    page.evaluate(
        """() => {
          const el = document.getElementById('pair-code-input');
          const dt = new DataTransfer();
          dt.setData('text/plain', '012345');
          el.dispatchEvent(new ClipboardEvent('paste',
            {clipboardData: dt, bubbles: true, cancelable: true}));
        }"""
    )
    assert page.locator("#pair-code-input").input_value() == "012345"
    assert page.locator("#pair-join-submit").is_enabled()


def test_code_input_sanitizes_non_digits(page, server):
    _with_mock(page)
    _open_ui(page)
    page.click("#pair-join-open")
    code = page.locator("#pair-code-input")
    code.click()
    page.keyboard.type("12ab")
    assert code.input_value() == "12"
    assert page.locator("#pair-join-submit").is_disabled()


def test_code_input_enter_submits(page, server):
    _with_mock(page)
    _open_ui(page)
    page.click("#pair-join-open")
    code = page.locator("#pair-code-input")
    code.fill("314159")
    code.press("Enter")
    assert page.evaluate("window.__pairCalls") == ["join:314159"]


# ── leader flow ──


def test_leader_join_request_auto_approved_no_buttons(page, server):
    """#114: a join request is auto-approved by the controller — the leader
    UI surfaces no approve/reject controls while it waits."""
    _with_mock(page)
    _open_ui(page)
    page.click("#pair-create")
    assert page.evaluate("window.__pairCalls") == ["create"]
    _emit(page, "{status:'connecting', role:'leader', code:'042917'}")
    expect(page.locator("#pair-code-value")).to_have_text("042917")
    _only_section_visible(page, "#pair-sec-leader")
    # a join request arrives → no manual approval step exists
    _emit(page, "{joinRequest:true}")
    expect(page.locator("#pair-approve")).to_have_count(0)
    expect(page.locator("#pair-reject")).to_have_count(0)
    expect(page.locator("#pair-join-request")).to_have_count(0)
    _emit(page, "{status:'connected', role:'leader', side:'left', transport:'server-ws'}")
    # approval completes pairing → the modal gets out of the way
    expect(page.locator("#pair-overlay")).to_be_hidden()
    expect(page.locator("#pair-status")).to_contain_text("接続済")


def test_leader_join_request_keeps_code_and_cancel(page, server):
    """Pending approval still shows the code screen — the peer may be
    mid-handshake, and キャンセル remains the way out."""
    _with_mock(page)
    _open_ui(page)
    page.click("#pair-create")
    _emit(page, "{status:'connecting', role:'leader', code:'123456'}")
    _emit(page, "{joinRequest:true}")
    expect(page.locator("#pair-code-value")).to_have_text("123456")
    expect(page.locator("#pair-leader-cancel")).to_be_visible()
    _emit(page, "{joinRequest:false}")
    expect(page.locator("#pair-code-value")).to_have_text("123456")


# ── follower flow ──


def test_follower_join_waits_then_established(page, server):
    _with_mock(page)
    _open_ui(page)
    page.click("#pair-join-open")
    page.locator("#pair-code-input").fill("777001")
    page.click("#pair-join-submit")
    assert page.evaluate("window.__pairCalls") == ["join:777001"]
    _emit(page, "{status:'connecting', role:'follower'}")
    _only_section_visible(page, "#pair-sec-waiting")
    _emit(page, "{status:'connected', role:'follower', side:'left', transport:'server-ws'}")
    # approval resolves the wait → the modal steps aside
    expect(page.locator("#pair-overlay")).to_be_hidden()
    expect(page.locator("#pair-status")).to_contain_text("接続済")
    expect(page.locator("#pair-status")).to_contain_text("follower")


# ── state → indicator mapping ──

STATE_CASES = [
    ("{status:'idle'}", "未接続"),
    ("{status:'connecting', role:'leader', code:'111111'}", "接続中"),
    ("{status:'connected', role:'leader', side:'right', transport:'server-ws'}", "接続済"),
    ("{status:'connected', role:'leader', side:'right', transport:'server-ws'}", "leader"),
    ("{status:'connected', role:'follower', side:'left', transport:'server-ws'}", "follower"),
    ("{status:'reconnecting', role:'leader', side:'right', transport:'server-ws'}", "再接続中"),
    ("{status:'peer-away', role:'leader', side:'right', transport:'server-ws'}", "相手不在"),
    ("{status:'loading', role:'leader', side:'right', transport:'server-ws'}", "読み込み中"),
    ("{status:'error', error:'boom'}", "エラー"),
    ("{status:'ended'}", "解除済"),
]


def test_state_indicator_mapping(page, server):
    _with_mock(page)
    for patch, needle in STATE_CASES:
        _emit(page, patch)
        expect(page.locator("#pair-status")).to_contain_text(needle)


def test_state_indicator_data_status_attr(page, server):
    """Each state also carries a distinguishable class hook: data-status."""
    _with_mock(page)
    for patch, key in [
        ("{status:'idle'}", "idle"),
        ("{status:'connecting'}", "connecting"),
        ("{status:'connected'}", "connected"),
        ("{status:'reconnecting'}", "reconnecting"),
        ("{status:'peer-away'}", "peer-away"),
        ("{status:'loading'}", "loading"),
        ("{status:'error'}", "error"),
        ("{status:'ended'}", "ended"),
    ]:
        _emit(page, patch)
        assert (
            page.locator("#pair-status").get_attribute("data-status") == key
        ), patch


def test_side_indicator_left_right(page, server):
    _with_mock(page)
    _emit(page, "{status:'connected', role:'leader', side:'right', transport:'server-ws'}")
    expect(page.locator("#pair-side")).to_have_text("右側担当")
    _emit(page, "{side:'left'}")
    expect(page.locator("#pair-side")).to_have_text("左側担当")


def test_transport_indicator_switches(page, server):
    _with_mock(page)
    _emit(page, "{status:'connected', role:'leader', side:'right', transport:'server-ws'}")
    expect(page.locator("#pair-transport")).to_have_text("サーバー経由")
    _emit(page, "{transport:'webrtc-dc'}")
    expect(page.locator("#pair-transport")).to_have_text("端末間直接")


# ── controls by role ──


def test_swap_sides_operable_from_both_roles(page, server):
    _with_mock(page)
    _open_ui(page)
    _emit(page, "{status:'connected', role:'leader', side:'right', transport:'server-ws'}")
    swap = page.locator("#pair-swap")
    expect(swap).to_be_visible()
    assert swap.is_enabled()
    swap.click()
    assert page.evaluate("window.__pairCalls") == ["swapSides"]
    _emit(page, "{side:'left'}")
    _emit(page, "{status:'connected', role:'follower', side:'right', transport:'server-ws'}")
    swap = page.locator("#pair-swap")
    expect(swap).to_be_visible()
    assert swap.is_enabled()
    swap.click()
    assert page.evaluate("window.__pairCalls") == ["swapSides", "swapSides"]


def test_work_change_hidden_for_follower(page, server):
    _with_mock(page)
    _open_ui(page)
    _emit(page, "{status:'connected', role:'leader', side:'right', transport:'server-ws'}")
    cw = page.locator("#pair-change-work")
    expect(cw).to_be_visible()
    assert cw.is_enabled()
    _emit(page, "{status:'connected', role:'follower', side:'left', transport:'server-ws'}")
    # hidden or disabled are both acceptable per spec; ours hides it
    assert (
        not page.locator("#pair-change-work").is_visible()
        or not page.locator("#pair-change-work").is_enabled()
    )


def test_leave_calls_controller_and_shows_ended(page, server):
    _with_mock(page)
    _open_ui(page)
    _emit(page, "{status:'connected', role:'follower', side:'left', transport:'server-ws'}")
    page.click("#pair-leave")
    assert page.evaluate("window.__pairCalls") == ["leave"]
    _emit(page, "{status:'ended'}")
    expect(page.locator("#pair-status")).to_contain_text("解除済")
    # back to the start/join choice — re-pairing is possible immediately
    _only_section_visible(page, "#pair-sec-choice")


def test_leader_waiting_cancel_calls_leave(page, server):
    _with_mock(page)
    _open_ui(page)
    page.click("#pair-create")
    _emit(page, "{status:'connecting', role:'leader', code:'654321'}")
    _only_section_visible(page, "#pair-sec-leader")
    page.click("#pair-leader-cancel")
    assert page.evaluate("window.__pairCalls") == ["create", "leave"]
    _emit(page, "{status:'ended'}")
    _only_section_visible(page, "#pair-sec-choice")


def test_follower_waiting_cancel_calls_leave(page, server):
    _with_mock(page)
    _open_ui(page)
    page.click("#pair-join-open")
    page.locator("#pair-code-input").fill("777001")
    page.click("#pair-join-submit")
    _emit(page, "{status:'connecting', role:'follower'}")
    _only_section_visible(page, "#pair-sec-waiting")
    page.click("#pair-waiting-cancel")
    assert page.evaluate("window.__pairCalls") == ["join:777001", "leave"]
    _emit(page, "{status:'ended'}")
    # joinView stays set — the follower returns to the code-entry form
    _only_section_visible(page, "#pair-sec-join")


# ── QR pairing (#106) ──


def test_primary_action_buttons_on_the_right(page, server):
    _with_mock(page)
    _open_ui(page)
    page.click("#pair-join-open")
    back = page.locator("#pair-join-back").bounding_box()
    submit = page.locator("#pair-join-submit").bounding_box()
    assert back["x"] < submit["x"]      # 戻る left, 参加する right


def test_leader_code_renders_qr_canvas(page, server):
    _with_mock(page)
    _open_ui(page)
    _emit(page, "{status:'connecting', role:'leader', code:'654321'}")
    expect(page.locator("#pair-qr")).to_be_visible()
    dark = page.evaluate(
        """() => {
          const c = document.getElementById('pair-qr');
          const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
          let n = 0;
          for (let i = 0; i < d.length; i += 4) if (d[i] < 64) n++;
          return n;
        }""")
    assert dark > 100, "QR canvas must contain dark modules"


def test_qr_decode_roundtrip(page, server):
    """The shipped vendor libs interoperate: qrcode-generator output must be
    readable by jsQR — the exact produce/scan pair used on real devices."""
    _with_mock(page)
    _open_ui(page)
    _emit(page, "{status:'connecting', role:'leader', code:'042917'}")
    expect(page.locator("#pair-qr")).to_be_visible()
    decoded = page.evaluate(
        """() => {
          const c = document.getElementById('pair-qr');
          const img = c.getContext('2d').getImageData(0, 0, c.width, c.height);
          const r = window.jsQR(img.data, img.width, img.height);
          return r ? r.data : null;
        }""")
    # URL-QR: the code rides a #/join/ deep link so any camera app pairs
    assert decoded == f"{server}/#/join/042917"


def test_join_view_autostarts_camera_scan(page, server):
    _with_mock(page)
    # Fake camera: an empty MediaStream keeps the scanner up without frames.
    page.add_init_script(
        "navigator.mediaDevices = navigator.mediaDevices || {};"
        "navigator.mediaDevices.getUserMedia = () =>"
        " Promise.resolve(new MediaStream());")
    page.reload()
    page.wait_for_selector("#pair-btn")
    _open_ui(page)
    page.click("#pair-join-open")
    _only_section_visible(page, "#pair-sec-join")
    # camera preview goes live inside the join view with no extra button
    expect(page.locator("#pair-scan-video")).to_be_visible()
    assert page.evaluate(
        "document.getElementById('pair-scan-video').srcObject instanceof MediaStream"
    )
    # back tears the stream down
    page.click("#pair-join-back")
    assert page.evaluate(
        "document.getElementById('pair-scan-video').srcObject") is None
    expect(page.locator("#pair-scan-video")).to_be_hidden()
    _only_section_visible(page, "#pair-sec-choice")
    assert page.evaluate("window.__pairCalls") == []


def test_join_scan_unavailable_shows_error(page, server):
    _with_mock(page)
    page.add_init_script(
        "navigator.mediaDevices = navigator.mediaDevices || {};"
        "navigator.mediaDevices.getUserMedia ="
        " () => Promise.reject(new DOMException('denied', 'NotAllowedError'));")
    page.reload()
    page.wait_for_selector("#pair-btn")
    _open_ui(page)
    page.click("#pair-join-open")
    # camera fails → manual entry stays usable with an inline note
    expect(page.locator("#pair-join-err")).to_be_visible()
    expect(page.locator("#pair-scan-video")).to_be_hidden()
    _only_section_visible(page, "#pair-sec-join")


def test_modal_auto_closes_when_connected(page, server):
    _with_mock(page)
    _open_ui(page)
    _emit(page, "{status:'connecting', role:'follower'}")
    _only_section_visible(page, "#pair-sec-waiting")
    _emit(page, "{status:'connected', role:'follower', side:'right', transport:'server-ws'}")
    expect(page.locator("#pair-overlay")).to_be_hidden()
    # reopening while connected is the management UI — must NOT auto-close
    page.click("#pair-btn")
    expect(page.locator("#pair-overlay")).to_be_visible()
    _only_section_visible(page, "#pair-sec-session")


def test_error_section_offers_leave(page, server):
    _with_mock(page)
    _open_ui(page)
    _emit(page, "{status:'error', error:'apply_failed'}")
    _only_section_visible(page, "#pair-sec-error")
    # #158: wire reason codes map to Japanese — the raw code stays out of
    # the UI (it is still available via console for debugging).
    expect(page.locator("#pair-error-text")).to_contain_text("状態の適用に失敗しました")
    expect(page.locator("#pair-error-text")).not_to_contain_text("apply_failed")
    page.click("#pair-error-leave")
    assert page.evaluate("window.__pairCalls") == ["leave"]
    _emit(page, "{status:'ended'}")
    _only_section_visible(page, "#pair-sec-choice")


def test_error_reason_maps_known_codes(page, server):
    _with_mock(page)
    _open_ui(page)
    _emit(page, "{status:'error', error:'bad_code'}")
    expect(page.locator("#pair-error-text")).to_contain_text("コードが正しくありません")
    expect(page.locator("#pair-error-text")).not_to_contain_text("bad_code")
    _emit(page, "{status:'error', error:'code_expired'}")
    expect(page.locator("#pair-error-text")).to_contain_text("コードの有効期限が切れました")


def test_error_reason_unknown_shows_generic(page, server):
    _with_mock(page)
    _open_ui(page)
    _emit(page, "{status:'error', error:'some_future_reason'}")
    expect(page.locator("#pair-error-text")).to_contain_text("エラーが発生しました")
    expect(page.locator("#pair-error-text")).not_to_contain_text("some_future_reason")


def test_failed_join_returns_to_form_with_reason(page, server):
    """#158: 解除 after a failed code join lands back on the join form —
    the reason stays visible inline and the field is reset."""
    _with_mock(page)
    _open_ui(page)
    page.click("#pair-join-open")
    page.fill("#pair-code-input", "000000")
    assert page.evaluate("window.__pairCalls") == ["join:000000"]
    _emit(page, "{status:'connecting'}")
    _emit(page, "{status:'error', error:'bad_code'}")
    _only_section_visible(page, "#pair-sec-error")
    expect(page.locator("#pair-error-text")).to_contain_text("コードが正しくありません")
    page.click("#pair-error-leave")
    assert "leave" in page.evaluate("window.__pairCalls")
    _emit(page, "{status:'ended'}")
    _only_section_visible(page, "#pair-sec-join")
    expect(page.locator("#pair-join-err")).to_contain_text("コードが正しくありません")
    assert page.locator("#pair-code-input").input_value() == ""
    # A fresh submit clears the carried-over notice.
    page.fill("#pair-code-input", "123456")
    page.click("#pair-join-submit")
    expect(page.locator("#pair-join-err")).to_be_hidden()


def test_error_without_join_view_does_not_leak_into_form(page, server):
    """#158: an error that did not come from the join flow must not
    surface inside the code form later."""
    _with_mock(page)
    _open_ui(page)
    _emit(page, "{status:'error', error:'bad_code'}")
    page.click("#pair-error-leave")
    _emit(page, "{status:'ended'}")
    _only_section_visible(page, "#pair-sec-choice")
    page.click("#pair-join-open")
    _only_section_visible(page, "#pair-sec-join")
    # joinOpen is a fresh intent — no stale reason, no forced clearing of
    # whatever the camera-error notice holds.
    assert page.locator("#pair-code-input").input_value() == ""


def test_pair_status_hides_with_toolbar(page, server):
    _with_mock(page)
    _emit(page, "{status:'connected', role:'leader', side:'right', transport:'server-ws'}")
    cls = page.locator("#pair-status").get_attribute("class") or ""
    assert "hidden" not in cls
    page.evaluate("() => window.__toggleToolbarZone()")
    assert "hidden" in page.locator("#toolbar").get_attribute("class")
    cls = page.locator("#pair-status").get_attribute("class") or ""
    assert "hidden" in cls
    page.evaluate("() => window.__toggleToolbarZone()")
    cls = page.locator("#pair-status").get_attribute("class") or ""
    assert "hidden" not in cls


# ── accessibility ──


def test_controls_min_44px_and_accessible_names(page, server):
    _with_mock(page)
    _open_ui(page)
    # pair launcher itself
    r = page.locator("#pair-btn").bounding_box()
    assert r["width"] >= 44 and r["height"] >= 44
    # walk the sections so every control gets measured: choice → join →
    # leader(code+request) → session
    for b in _visible_control_boxes(page):
        assert b["w"] >= 44 and b["h"] >= 44, b
    page.click("#pair-join-open")
    for b in _visible_control_boxes(page):
        assert b["w"] >= 44 and b["h"] >= 44, b
    page.click("#pair-join-back")
    _emit(page, "{status:'connecting', role:'leader', code:'123456', joinRequest:true}")
    for b in _visible_control_boxes(page):
        assert b["w"] >= 44 and b["h"] >= 44, b
    _emit(page, "{status:'connected', role:'leader', side:'right', transport:'server-ws'}")
    # pending→connected auto-closes; reopen the management UI to measure it
    page.click("#pair-btn")
    for b in _visible_control_boxes(page):
        assert b["w"] >= 44 and b["h"] >= 44, b
    # every control (dialog + launcher) exposes an accessible name
    names = page.evaluate(
        """() => [...document.querySelectorAll(
               '#pair-panel button, #pair-panel input, #pair-btn')]
            .filter(el => {
              const r = el.getBoundingClientRect();
              return r.width > 0 && r.height > 0;
            })
            .map(el => el.getAttribute('aria-label')
                || (el.getAttribute('aria-labelledby')
                    ? document.getElementById(el.getAttribute('aria-labelledby')).textContent
                    : '')
                || (el.textContent || '').trim())"""
    )
    assert names and all(n for n in names)


def test_focus_moves_in_on_open_and_back_on_close(page, server):
    _with_mock(page)
    _open_ui(page)
    assert page.evaluate(
        "!!document.activeElement.closest('#pair-panel')"
    ), "focus must move into the dialog on open"
    page.click("#pair-close")
    expect(page.locator("#pair-overlay")).to_be_hidden()
    assert page.evaluate("document.activeElement.id") == "pair-btn"


def test_esc_closes_dialog(page, server):
    _with_mock(page)
    _open_ui(page)
    page.keyboard.press("Escape")
    expect(page.locator("#pair-overlay")).to_be_hidden()


def test_background_inert_while_open(page, server):
    _with_mock(page)
    pre = page.evaluate(
        """() => ({
            viewer: document.getElementById('viewer').hasAttribute('inert'),
            sidebar: document.getElementById('sidebar').hasAttribute('inert')})"""
    )
    page.click("#pair-btn")
    expect(page.locator("#pair-overlay")).to_be_visible()
    for el in ("viewer", "sidebar"):
        assert page.evaluate(
            f"document.getElementById('{el}').hasAttribute('inert')"
        ), f"#{el} must be inert while the dialog is open"
    page.keyboard.press("Escape")
    post = page.evaluate(
        """() => ({
            viewer: document.getElementById('viewer').hasAttribute('inert'),
            sidebar: document.getElementById('sidebar').hasAttribute('inert')})"""
    )
    assert post == pre, "inert must be restored to the pre-open state"


def test_state_changes_emit_to_aria_live_region(page, server):
    _with_mock(page)
    assert page.locator("#pair-status").get_attribute("role") == "status"
    assert page.locator("#pair-status").get_attribute("aria-live") == "polite"
    before = page.locator("#pair-status").text_content()
    _emit(page, "{status:'connected', role:'leader', side:'right', transport:'server-ws'}")
    expect(page.locator("#pair-status")).not_to_have_text(before.strip())
    assert "接続済" in page.locator("#pair-status").text_content()


def test_dialog_semantics(page, server):
    _with_mock(page)
    _open_ui(page)
    panel = page.locator("#pair-panel")
    assert panel.get_attribute("role") == "dialog"
    assert panel.get_attribute("aria-modal") == "true"
    assert panel.get_attribute("aria-labelledby")


# ── 320px layout ──


def test_controls_fit_320px_no_overlap(browser, server):
    ctx = browser.new_context(viewport={"width": 320, "height": 568})
    page = ctx.new_page()
    try:
        page.add_init_script(MOCK_JS)
        page.goto(server)
        page.wait_for_selector("#pair-btn")
        _open_ui(page)
        # walk a couple of sections so more controls get measured
        _emit(page, "{status:'connected', role:'leader', side:'right', transport:'server-ws'}")
        for b in _visible_control_boxes(page):
            assert b["x"] >= 0 and b["right"] <= 320, b
            assert b["y"] >= 0 and b["bottom"] <= 568, b
        # persistent affordance must not cover existing chrome
        overlaps = page.evaluate(
            """() => {
              const inter = (a, b) => a && b && a.width > 0 && b.width > 0 &&
                !(a.right <= b.left || b.right <= a.left ||
                  a.bottom <= b.top || b.bottom <= a.top);
              const rect = sel => {
                const el = document.querySelector(sel);
                return el ? el.getBoundingClientRect() : null;
              };
              const status = rect('#pair-status');
              const covered = [];
              for (const sel of ['#toolbar', '#spread-controls',
                                 '#tapmode-controls', '#overview-close',
                                 '#menu-btn', '#overview-btn'])
                if (inter(status, rect(sel))) covered.push(sel);
              const btn = rect('#pair-btn');
              for (const sel of ['#menu-btn', '#overview-btn', '#counter'])
                if (inter(btn, rect(sel))) covered.push('btn:' + sel);
              return covered;
            }"""
        )
        assert overlaps == [], f"pair UI overlaps existing chrome: {overlaps}"
    finally:
        ctx.close()


# ── taps never reach the page-turn path ──


def test_pair_ui_taps_do_not_turn_pages(ios_page, server):
    page = ios_page
    _with_mock(page)
    open_book(page, server, "long", 0, 30)
    page.wait_for_selector("#pair-btn")
    page.evaluate(
        """() => {
          window.__turnSpy = 0;
          const wrap = (name) => {
            const orig = window[name];
            window[name] = (...a) => { window.__turnSpy++; return orig(...a); };
          };
          wrap('viewerGoNext'); wrap('viewerGoPrev'); wrap('viewerSnapTo');
        }"""
    )
    _ensure_sidebar_open(page)
    page.click("#pair-btn")
    expect(page.locator("#pair-overlay")).to_be_visible()
    s = touch_session(page)
    # real CDP taps on dialog controls must not classify as page turns
    for sel in ("#pair-create", "#pair-join-open", "#pair-join-back",
                "#pair-close"):
        box = page.locator(sel).bounding_box()
        tap(s, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.wait_for_timeout(30)
    expect(page.locator("#pair-overlay")).to_be_hidden()  # ✕ closed it
    # reopen, then a backdrop tap (outside the panel, on the overlay)
    page.click("#pair-btn")
    expect(page.locator("#pair-overlay")).to_be_visible()
    tap(s, 10, 400)
    s.detach()
    page.wait_for_timeout(30)
    assert page.evaluate("window.__turnSpy") == 0
    assert page.evaluate("window.__lastTouchEndVia") in (None, "tap")
    assert page.evaluate("window.currentIdx") == 0
    expect(page.locator("#pair-overlay")).to_be_hidden()  # backdrop tap closed it


def test_pair_ui_clicks_do_not_turn_pages(page, server):
    _with_mock(page)
    open_book(page, server, "long", 0, 30)
    page.wait_for_selector("#pair-btn")
    page.evaluate(
        """() => {
          window.__turnSpy = 0;
          const wrap = (name) => {
            const orig = window[name];
            window[name] = (...a) => { window.__turnSpy++; return orig(...a); };
          };
          wrap('viewerGoNext'); wrap('viewerGoPrev'); wrap('viewerSnapTo');
        }"""
    )
    tb_before = page.locator("#toolbar").get_attribute("class")
    _open_ui(page)
    page.click("#pair-join-open")
    page.click("#pair-join-back")
    page.click("#pair-create")
    page.click("#pair-close")
    assert page.evaluate("window.__turnSpy") == 0
    assert page.evaluate("window.currentIdx") == 0
    # opening/closing the pair UI must not toggle the toolbar either
    assert page.locator("#toolbar").get_attribute("class") == tb_before


# ── open/close is local-only (peer zoom/toolbar untouched) ──


def test_open_close_has_no_peer_side_effects(browser, server):
    ctx = browser.new_context(viewport={"width": 390, "height": 844})
    pa, pb = ctx.new_page(), ctx.new_page()
    try:
        for p in (pa, pb):
            p.add_init_script(LINKED_MOCK_JS)
            p.goto(server)
            p.wait_for_selector("#pair-btn")
        for p in (pa, pb):
            p.goto(f"{server}/#/view/long?p=0")
            p.wait_for_function(
                "document.getElementById('counter').textContent.trim() !== ''"
            )
        # pair both ends (relay carries the emit across contexts)
        pa.evaluate(
            "window.__pairEmit({status:'connected', role:'leader', side:'right', transport:'server-ws'})"
        )
        pb.wait_for_function(
            "document.getElementById('pair-status').dataset.status === 'connected'"
        )
        rx_before = pb.evaluate("window.__pairRx.length")

        def peer_snapshot(p):
            return p.evaluate(
                """() => ({
                    toolbar: document.getElementById('toolbar').className,
                    zoom: window.Zoom ? window.Zoom.state() : null,
                    idx: window.currentIdx,
                    overview: document.getElementById('overview').hidden})"""
            )

        before = peer_snapshot(pb)
        # open + close on A must not emit anything to the peer nor touch B's UI
        _ensure_sidebar_open(pa)
        pa.click("#pair-btn")
        expect(pa.locator("#pair-overlay")).to_be_visible()
        pa.keyboard.press("Escape")
        expect(pa.locator("#pair-overlay")).to_be_hidden()
        pa.wait_for_timeout(50)  # let any (wrongly) relayed messages land
        assert peer_snapshot(pb) == before
        assert pa.evaluate("window.__pairCalls") == [], \
            "open/close alone must not invoke the controller"
        assert pb.evaluate("window.__pairRx.length") == rx_before
    finally:
        ctx.close()


# ── MATCH (one-tap connect) ────────────────────────────────────────

# Mock variant whose controller implements connect() — the one-tap MATCH
# path. __pairConnectResult steers its resolved value per test.
CONNECT_JS = MOCK_JS.replace(
    "create() { window.__pairCalls.push('create'); },",
    """connect(pick) {
      window.__pairCalls.push(pick ? 'connect:' + pick : 'connect');
      return Promise.resolve(window.__pairConnectResult || null);
    },
    create() { window.__pairCalls.push('create'); },""")


def test_idle_tap_fires_connect_when_supported(page, server):
    """The launcher IS the pairing gesture: one idle tap → connect()."""
    _with_mock(page, js=CONNECT_JS)
    page.evaluate("window.__pairConnectResult = "
                  "{sessionId:'S1', code:'042917', role:'leader'}")
    _ensure_sidebar_open(page)
    page.click("#pair-btn")
    expect(page.locator("#pair-overlay")).to_be_visible()
    assert page.evaluate("window.__pairCalls") == ["connect"]
    _emit(page, "{status:'connecting', role:'leader', code:'042917'}")
    _only_section_visible(page, "#pair-sec-leader")
    expect(page.locator("#pair-code-value")).to_have_text("042917")


def test_ambiguous_match_shows_picker_then_picks_token(page, server):
    """Candidates carry an opaque token (never the 6-digit code); picking
    one re-MATCHes with connect(id), so the human code stays private."""
    _with_mock(page, js=CONNECT_JS)
    page.evaluate(
        """window.__pairConnectResult = {ambiguous:true, candidates:[
             {id:'tok-a', leaderName:'A', ageSec:3},
             {id:'tok-b', leaderName:'B', ageSec:7}]}""")
    _ensure_sidebar_open(page)
    page.click("#pair-btn")
    _only_section_visible(page, "#pair-sec-picker")
    picks = page.locator(".pair-pick")
    assert picks.count() == 2
    # the waiter's age disambiguates same-name candidates
    assert "3秒前" in picks.nth(0).text_content()
    picks.nth(0).click()
    assert page.evaluate("window.__pairCalls") == ["connect", "connect:tok-a"]


def test_unsupported_match_keeps_classic_choice(page, server):
    """A pre-MATCH server resolves {unsupported} — 開始/参加 stays usable."""
    _with_mock(page, js=CONNECT_JS)
    page.evaluate("window.__pairConnectResult = {unsupported:'malformed'}")
    _ensure_sidebar_open(page)
    page.click("#pair-btn")
    _only_section_visible(page, "#pair-sec-choice")
    page.click("#pair-create")
    assert page.evaluate("window.__pairCalls") == ["connect", "create"]


def test_join_deep_link_consumes_hash_and_joins(page, server):
    """#/join/<6桁> is a one-shot input: it joins, then leaves the URL."""
    _with_mock(page)
    page.goto(f"{server}/#/join/314159")
    page.wait_for_function("location.hash === ''")
    page.wait_for_function(
        "(window.__pairCalls || []).indexOf('join:314159') !== -1")


def test_join_deep_link_never_hijacks_an_active_pair(page, server):
    """#/join/<code> while paired only surfaces the session panel — a
    blind join() would drop the link without LEAVE and strand the
    partner in 'peer-away' for the whole resume window."""
    _with_mock(page)
    _emit(page, "{status:'connected', role:'follower'}")
    page.evaluate("location.hash = '#/join/314159'")
    page.wait_for_function("location.hash === ''")
    expect(page.locator("#pair-overlay")).to_be_visible()
    _only_section_visible(page, "#pair-sec-session")
    assert page.evaluate("window.__pairCalls") == []


def test_join_retry_after_cancel_is_possible(page, server):
    """joinPending must not strand the form: a cancelled join returns to
    the code entry, and typing a new code submits again."""
    _with_mock(page)
    _open_ui(page)
    page.click("#pair-join-open")
    page.locator("#pair-code-input").fill("777001")
    _emit(page, "{status:'connecting', role:'follower'}")
    page.click("#pair-waiting-cancel")
    _emit(page, "{status:'ended'}")
    page.locator("#pair-code-input").fill("111222")
    assert page.evaluate("window.__pairCalls") == [
        "join:777001", "leave", "join:111222"]


def test_ended_flashes_the_strip_before_hiding(page, server):
    """A pair ending while the reader is up flashes '解除済' briefly —
    otherwise nothing in the viewer surface tells the reader."""
    _with_mock(page)
    strip = page.locator("#pair-status")
    _emit(page, "{status:'connected', role:'leader', side:'right', transport:'server-ws'}")
    expect(strip).to_be_visible()
    _emit(page, "{status:'ended'}")
    expect(strip).to_be_visible()
    assert "解除済" in strip.text_content()
    page.wait_for_timeout(3200)
    expect(strip).to_be_hidden()


def test_session_strip_hidden_at_idle_and_tappable(page, server):
    _with_mock(page)
    strip = page.locator("#pair-status")
    expect(strip).to_be_hidden()                      # solo: no noise
    _emit(page, "{status:'connected', role:'leader', side:'right', transport:'server-ws'}")
    expect(strip).to_be_visible()
    assert "接続済" in strip.text_content()
    assert "右側担当" in strip.text_content()
    page.evaluate("window.closeSidebar()")   # strip lives on the reader face
    strip.click()
    expect(page.locator("#pair-overlay")).to_be_visible()
    _only_section_visible(page, "#pair-sec-session")
