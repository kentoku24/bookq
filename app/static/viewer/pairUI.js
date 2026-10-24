/* dual-reader pairing UI — code connect / approve / swap-sides / leave
 * (#83 / S08). Plain global script; exposes window.PairUI.
 *
 * Driven by an INJECTED controller — real sync wiring lands in S09 (#84).
 * Controller interface (parent issue #75 §3.3/§3.5):
 *   { create(), join(code), approve(), reject(), leave(), swapSides(),
 *     subscribe(cb) -> unsubscribe, state }
 * state shape this UI renders (all fields optional except status):
 *   status : 'idle' | 'connecting' | 'connected' | 'reconnecting'
 *          | 'peer-away' | 'loading' | 'error' | 'ended'
 *   role   : 'leader' | 'follower' | null
 *   code   : 6-digit string the leader shows while waiting for a join
 *   joinRequest : truthy while a follower waits for leader approval
 *   side   : 'right' | 'left' | null — the half this device renders
 *   transport : 'server-ws' | 'webrtc-dc' | null — data-plane kind
 *   error  : message shown in the error view
 *
 * Injection: set window.PairController before this script evaluates
 * (defer order), or call window.PairUI.init({controller}) / .attach(c).
 * With no controller the UI still opens and shows 未接続; controller calls
 * no-op. The UI NEVER calls the controller on open/close alone — only on
 * explicit button activation — and pairing-UI gestures are stopped before
 * they can reach the viewer's page-turn / toolbar / keyboard paths.
 */
(function () {
  'use strict';

  const STATUS_LABEL = {
    idle: '未接続',
    connecting: '接続中',
    connected: '接続済',
    reconnecting: '再接続中',
    'peer-away': '相手不在',
    loading: '読み込み中',
    error: 'エラー',
    ended: '解除済',
  };
  const TRANSPORT_LABEL = {
    'server-ws': 'サーバー経由',
    'webrtc-dc': '端末間直接',
  };
  const SIDE_LABEL = { right: '右側担当', left: '左側担当' };
  // States in which the session section (swap / change-work / leave) and the
  // side+transport indicators are meaningful.
  const SESSION_STATES = ['connected', 'reconnecting', 'peer-away', 'loading'];

  let ctl = null;
  let unsubscribe = null;
  let open_ = false;
  let joinView = false;   // local UI state: the code-entry form is showing
  let lastFocus = null;
  let inertSnapshot = null; // [[el, hadInertAttr]]
  let built = false;
  let els = {};

  function st() {
    return (ctl && ctl.state) || { status: 'idle' };
  }

  function _el(id) { return document.getElementById(id); }
  function show(el, on) { if (el) el.hidden = !on; }

  const PANEL_HTML =
    '<div id="pair-panel" role="dialog" aria-modal="true" aria-labelledby="pair-title">' +
      '<div class="pair-head">' +
        '<span id="pair-title">2台で見開き</span>' +
        '<button id="pair-close" type="button" aria-label="閉じる">✕</button>' +
      '</div>' +
      '<div class="pair-body">' +
        '<div id="pair-sec-choice" class="pair-sec">' +
          '<button id="pair-create" type="button" class="pair-action">開始' +
            '<span class="pair-sub">この端末が leader になりコードを発行</span></button>' +
          '<button id="pair-join-open" type="button" class="pair-action">参加' +
            '<span class="pair-sub">相手のコードを入力</span></button>' +
        '</div>' +
        '<div id="pair-sec-join" class="pair-sec" hidden>' +
          '<label id="pair-code-label" for="pair-code-input">6桁のコードを入力</label>' +
          '<input id="pair-code-input" type="text" inputmode="numeric" ' +
            'pattern="[0-9]*" maxlength="6" autocomplete="off" autocorrect="off" ' +
            'spellcheck="false" aria-labelledby="pair-code-label">' +
          '<div class="pair-row">' +
            '<button id="pair-join-submit" type="button" class="pair-primary" disabled>参加する</button>' +
            '<button id="pair-join-back" type="button">戻る</button>' +
          '</div>' +
        '</div>' +
        '<div id="pair-sec-leader" class="pair-sec" hidden>' +
          '<p class="pair-note">相手の端末でこのコードを入力してください</p>' +
          '<div id="pair-code-value" aria-label="ペアリングコード">------</div>' +
          '<div id="pair-join-request" hidden>' +
            '<p class="pair-note">参加要求があります</p>' +
            '<div class="pair-row">' +
              '<button id="pair-approve" type="button" class="pair-primary">承認</button>' +
              '<button id="pair-reject" type="button" class="pair-danger">拒否</button>' +
            '</div>' +
          '</div>' +
          '<button id="pair-leader-cancel" type="button">キャンセル</button>' +
        '</div>' +
        '<div id="pair-sec-waiting" class="pair-sec" hidden>' +
          '<p id="pair-waiting-text" class="pair-note">承認を待っています…</p>' +
          '<button id="pair-waiting-cancel" type="button">キャンセル</button>' +
        '</div>' +
        '<div id="pair-sec-session" class="pair-sec" hidden>' +
          '<p id="pair-session-info" class="pair-note"></p>' +
          '<button id="pair-swap" type="button">左右交換</button>' +
          '<button id="pair-change-work" type="button">作品変更</button>' +
          '<button id="pair-leave" type="button" class="pair-danger">解除</button>' +
        '</div>' +
        '<div id="pair-sec-error" class="pair-sec" hidden>' +
          '<p id="pair-error-text" class="pair-note"></p>' +
          '<button id="pair-error-close" type="button">閉じる</button>' +
        '</div>' +
      '</div>' +
    '</div>';

  function buildDom() {
    if (built) return;
    built = true;

    // Launcher lives in the viewer toolbar — same affordance family as ☰/⊞.
    const btn = document.createElement('button');
    btn.id = 'pair-btn';
    btn.type = 'button';
    btn.title = '2台で見開き';
    btn.setAttribute('aria-label', '2台で見開き');
    btn.textContent = '🔗';
    const toolbar = _el('toolbar');
    (toolbar || document.body).appendChild(btn);

    // Persistent status chip — THE state indicator. role=status makes it the
    // aria-live region for every state change (polite, atomic).
    const status = document.createElement('div');
    status.id = 'pair-status';
    status.dataset.status = 'idle';
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    status.setAttribute('aria-atomic', 'true');
    status.innerHTML =
      '<span id="pair-status-text">未接続</span>' +
      '<span id="pair-side" hidden></span>' +
      '<span id="pair-transport" hidden></span>';
    document.body.appendChild(status);

    const overlay = document.createElement('div');
    overlay.id = 'pair-overlay';
    overlay.className = 'hidden';
    overlay.innerHTML = PANEL_HTML;
    document.body.appendChild(overlay);

    els = {
      btn: btn,
      status: status,
      statusText: _el('pair-status-text'),
      side: _el('pair-side'),
      transport: _el('pair-transport'),
      overlay: overlay,
      panel: _el('pair-panel'),
      close: _el('pair-close'),
      secChoice: _el('pair-sec-choice'),
      secJoin: _el('pair-sec-join'),
      secLeader: _el('pair-sec-leader'),
      secWaiting: _el('pair-sec-waiting'),
      secSession: _el('pair-sec-session'),
      secError: _el('pair-sec-error'),
      create: _el('pair-create'),
      joinOpen: _el('pair-join-open'),
      codeInput: _el('pair-code-input'),
      joinSubmit: _el('pair-join-submit'),
      joinBack: _el('pair-join-back'),
      codeValue: _el('pair-code-value'),
      joinRequest: _el('pair-join-request'),
      approve: _el('pair-approve'),
      reject: _el('pair-reject'),
      leaderCancel: _el('pair-leader-cancel'),
      waitingText: _el('pair-waiting-text'),
      waitingCancel: _el('pair-waiting-cancel'),
      sessionInfo: _el('pair-session-info'),
      swap: _el('pair-swap'),
      changeWork: _el('pair-change-work'),
      leave: _el('pair-leave'),
      errorText: _el('pair-error-text'),
      errorClose: _el('pair-error-close'),
    };
    wireEvents();
  }

  // ── render: state → indicators + section visibility ──

  function render() {
    if (!built) return;
    const s = st();
    const status = s.status || 'idle';
    const role = s.role || null;
    const inSession = SESSION_STATES.indexOf(status) !== -1;

    let label = STATUS_LABEL[status] || status;
    if (status === 'connected' && role) label += '（' + role + '）';
    els.status.dataset.status = status;
    els.statusText.textContent = label;

    const sideText = SIDE_LABEL[s.side] || '';
    els.side.textContent = sideText;
    els.side.hidden = !(sideText && inSession);
    const trText = TRANSPORT_LABEL[s.transport] || '';
    els.transport.textContent = trText;
    els.transport.hidden = !(trText && inSession);

    let sec;
    if (status === 'idle' || status === 'ended') {
      sec = joinView ? els.secJoin : els.secChoice;
    } else if (status === 'connecting') {
      sec = role === 'leader' ? els.secLeader : els.secWaiting;
    } else if (status === 'error') {
      sec = els.secError;
    } else {
      sec = els.secSession;
    }
    const secs = [els.secChoice, els.secJoin, els.secLeader,
                  els.secWaiting, els.secSession, els.secError];
    for (const e of secs) show(e, e === sec);

    if (sec === els.secLeader) {
      els.codeValue.textContent = s.code || '------';
      els.joinRequest.hidden = !s.joinRequest;
    } else if (sec === els.secWaiting) {
      els.waitingText.textContent =
        role === 'follower' ? '承認を待っています…' : '接続中…';
    } else if (sec === els.secSession) {
      const bits = [];
      if (role) bits.push(role);
      if (sideText) bits.push(sideText);
      if (trText) bits.push(trText);
      els.sessionInfo.textContent = bits.join(' · ');
      // 作品変更 is a leader privilege (#75 §3.3: OPEN_WORK is leader-only);
      // followers never see an enabled control for it.
      els.changeWork.hidden = role !== 'leader';
      els.changeWork.disabled = role !== 'leader';
    } else if (sec === els.secError) {
      els.errorText.textContent = s.error || 'エラーが発生しました';
    } else if (sec === els.secJoin) {
      els.joinSubmit.disabled = els.codeInput.value.length !== 6;
    }
  }

  // ── open / close with inert + focus management ──

  function firstControl() {
    const secs = [els.secJoin, els.secLeader, els.secWaiting,
                  els.secSession, els.secError, els.secChoice];
    const sec = secs.find(e => e && !e.hidden);
    const t = sec && sec.querySelector('input, button:not([disabled])');
    return t || els.close;
  }

  function open() {
    if (open_) return;
    open_ = true;
    lastFocus = document.activeElement;
    els.overlay.classList.remove('hidden');
    // aria-modal promises the background is unreachable: put the two other
    // top-level regions inert (snapshot their prior state for restore).
    inertSnapshot = [];
    for (const el of [_el('viewer'), _el('sidebar')]) {
      if (!el) continue;
      inertSnapshot.push([el, el.hasAttribute('inert')]);
      el.setAttribute('inert', '');
    }
    render();
    firstControl().focus();
  }

  function close() {
    if (!open_) return;
    open_ = false;
    joinView = false;
    els.overlay.classList.add('hidden');
    if (inertSnapshot) {
      for (const [el, was] of inertSnapshot) {
        if (!was) el.removeAttribute('inert');
      }
      inertSnapshot = null;
    }
    // Restore focus to where it came from; if that element is gone or sits
    // in an inert subtree (e.g. a now-closed sidebar), the launcher is the
    // safe landing spot.
    let back = lastFocus;
    lastFocus = null;
    if (!back || !document.contains(back) || back.closest('[inert]')) {
      back = els.btn;
    }
    if (back && typeof back.focus === 'function') back.focus();
  }

  // ── code input ──

  function sanitizeCode() {
    const v = els.codeInput.value.replace(/\D/g, '').slice(0, 6);
    if (v !== els.codeInput.value) els.codeInput.value = v;
    els.joinSubmit.disabled = v.length !== 6;
  }

  function onCodePaste(e) {
    const cd = e.clipboardData || window.clipboardData;
    if (!cd) return;
    const text = cd.getData('text');
    if (text == null) return;
    e.preventDefault();
    els.codeInput.value = String(text).replace(/\D/g, '').slice(0, 6);
    sanitizeCode();
  }

  function submitJoin() {
    const v = els.codeInput.value;
    if (!/^\d{6}$/.test(v) || !ctl) return;
    ctl.join(v);
  }

  // ── events ──

  function wireEvents() {
    els.btn.addEventListener('click', e => { e.stopPropagation(); open(); });
    els.close.addEventListener('click', close);
    els.errorClose.addEventListener('click', close);
    // Backdrop tap dismisses like the other sheets do; a tap that lands on
    // the panel itself never reaches this branch.
    els.overlay.addEventListener('click', e => {
      if (e.target === els.overlay) close();
    });
    // Pairing-UI gestures must never reach the viewer's page-turn paths
    // (zone clicks live on sibling elements, touch handlers on #img-container,
    // keydown on document). Belt-and-braces: stop everything at the overlay.
    for (const t of ['click', 'pointerdown', 'pointermove', 'pointerup',
                     'mousedown', 'mouseup', 'touchstart', 'touchmove',
                     'touchend', 'touchcancel', 'contextmenu', 'wheel']) {
      els.overlay.addEventListener(t, e => e.stopPropagation());
    }
    // Keys originating inside the dialog never reach the document-level
    // viewer bindings (arrows / f / Home / End / app.js's own Esc handler).
    els.panel.addEventListener('keydown', e => {
      e.stopPropagation();
      if (e.key === 'Escape') { e.preventDefault(); close(); }
    });
    // Stray keys while the dialog is open (focus somehow outside it) must
    // not reach the viewer either; Esc closes from anywhere. Keys whose
    // target is inside the panel are already stopped by the panel-level
    // handler — letting them through here keeps input keydown listeners
    // (e.g. Enter→submit on the code field) working. stopPropagation only:
    // Enter/Space/Tab default actions still work inside the dialog.
    document.addEventListener('keydown', e => {
      if (!open_) return;
      if (els.panel.contains(e.target)) return;
      e.stopPropagation();
      if (e.key === 'Escape') { e.preventDefault(); close(); }
    }, true);

    els.create.addEventListener('click', () => { if (ctl) ctl.create(); });
    els.joinOpen.addEventListener('click', () => {
      joinView = true;
      render();
      els.codeInput.focus();
    });
    els.joinBack.addEventListener('click', () => {
      joinView = false;
      els.codeInput.value = '';
      render();
      firstControl().focus();
    });
    els.joinSubmit.addEventListener('click', submitJoin);
    els.codeInput.addEventListener('input', sanitizeCode);
    els.codeInput.addEventListener('paste', onCodePaste);
    els.codeInput.addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); submitJoin(); }
    });
    els.approve.addEventListener('click', () => { if (ctl) ctl.approve(); });
    els.reject.addEventListener('click', () => { if (ctl) ctl.reject(); });
    els.leaderCancel.addEventListener('click', () => { if (ctl) ctl.leave(); });
    els.waitingCancel.addEventListener('click', () => { if (ctl) ctl.leave(); });
    els.swap.addEventListener('click', () => { if (ctl) ctl.swapSides(); });
    els.leave.addEventListener('click', () => { if (ctl) ctl.leave(); });
    // Optional extension point — S09 wires the real work picker; the mock
    // may or may not implement it.
    els.changeWork.addEventListener('click', () => {
      if (ctl && typeof ctl.changeWork === 'function') ctl.changeWork();
    });
  }

  // ── controller binding / public API ──

  function attach(controller) {
    if (unsubscribe) {
      try { unsubscribe(); } catch (_) { /* controller cleanup is best-effort */ }
      unsubscribe = null;
    }
    ctl = controller || null;
    if (ctl && typeof ctl.subscribe === 'function') {
      unsubscribe = ctl.subscribe(() => render());
    }
    render();
  }

  function init(opts) {
    opts = opts || {};
    buildDom();
    attach('controller' in opts ? opts.controller
                                : (window.PairController || null));
    return api;
  }

  const api = {
    init: init,
    attach: attach,
    open: open,
    close: close,
    isOpen: () => open_,
    state: st,
  };
  window.PairUI = api;

  function boot() {
    buildDom();
    attach(window.PairController || null);
  }
  // Defer scripts evaluate after parsing but before DOMContentLoaded, so a
  // controller created by a later script (S09 wiring) is picked up twice:
  // once if it already exists, once more at DOMContentLoaded.
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
  document.addEventListener('DOMContentLoaded', () => {
    if (!ctl && window.PairController) attach(window.PairController);
  });
})();
