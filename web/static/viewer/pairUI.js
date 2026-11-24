/* dual-reader pairing UI — code connect / auto-approve / swap-sides / leave
 * (#83 / S08, #114). Plain global script; exposes window.PairUI.
 *
 * Driven by an INJECTED controller — real sync wiring lands in S09 (#84).
 * Controller interface (parent issue #75 §3.3/§3.5):
 *   { create(), join(code), connect(pick?), approve(), reject(), leave(),
 *     swapSides(), subscribe(cb) -> unsubscribe, state }
 * connect() is the one-tap MATCH op: 「近くの相手とつながる」 binds this
 * device as follower of a fresh waiting session or as a new waiting
 * leader. It resolves {ambiguous, candidates} when several sessions wait
 * (picker → connect(candidate.id)) and {unsupported} on a pre-MATCH
 * server (the choice demotes to the classic 開始/参加 pair). The idle
 * launcher tap only opens the panel — MATCH and code-join are explicit
 * choices there (#160); controllers without connect() always get the
 * legacy 開始/参加 choice.
 * state shape this UI renders (all fields optional except status):
 *   status : 'idle' | 'connecting' | 'connected' | 'reconnecting'
 *          | 'peer-away' | 'loading' | 'error' | 'ended'
 *   role   : 'leader' | 'follower' | null
 *   code   : 6-digit string the leader shows while waiting for a join
 *   joinRequest : truthy while a follower waits — approved automatically
 *     by the controller (#114); this UI no longer surfaces the request
 *   side   : 'right' | 'left' | null — the half this device renders
 *   transport : 'server-ws' | 'webrtc-dc' | null — data-plane kind
 *   error  : message shown in the error view
 *
 * Injection: set window.PairController before this script evaluates
 * (defer order), or call window.PairUI.init({controller}) / .attach(c).
 * With no controller the UI still opens and shows 未接続; controller calls
 * no-op. Open/close alone never calls the controller — the pairing
 * gesture is the explicit つながる/参加 choice inside the panel — and
 * pairing-UI gestures are stopped before they can reach the viewer's
 * page-turn / toolbar / keyboard paths.
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
  // Wire-stable reason codes → user-facing text (#158). The protocol
  // vocabulary (sessions.py REJECT reasons, state.py sequencer rejects,
  // END reasons, manifest ERROR codes, local client errors) never reaches
  // the UI raw — unknown codes fall back to a generic message and are
  // logged for debugging instead.
  const REASON_TEXT = {
    bad_code: 'コードが正しくありません',
    code_consumed: 'このコードは既に使われています',
    code_expired: 'コードの有効期限が切れました',
    expired_code: 'コードの有効期限が切れました',
    bad_resume: '再接続情報が無効です',
    resume_expired: '再接続の有効期限が切れました',
    resume_in_use: 'この端末は別の画面で接続中です',
    credentials_lost: '接続情報が失われました。もう一度ペアリングしてください',
    peer_offline: '相手がオフラインです',
    not_paired: 'ペアが成立していません',
    not_leader: 'leader のみ実行できます',
    no_pending: '参加リクエストがありません',
    already_bound: '既に接続中です',
    no_session: 'セッションが見つかりません',
    wrong_session: 'セッションが一致しません',
    malformed: 'リクエストが不正です',
    rejected: '参加が拒否されました',
    left: '相手が解除しました',
    session_lost: '接続が切れました',
    shutdown: 'サーバーが再起動しました',
    incompatible: 'アプリのバージョンが一致しません。更新してください',
    forbidden: 'アクセスが拒否されました',
    not_found: '作品が見つかりません',
    empty_work: '作品にページがありません',
    broken_archive: '作品ファイルを読み込めません',
    page_not_found: 'ページが見つかりません',
    no_initial_page: '開始ページがありません',
    range: 'ページ指定が範囲外です',
    size: '作品が大きすぎます',
    timeout: '読み込みがタイムアウトしました',
    apply_failed: '状態の適用に失敗しました',
    manifest_error: '作品情報の読み込みに失敗しました',
    error: 'エラーが発生しました',
  };
  function errorTextFor(reason) {
    if (reason && REASON_TEXT[reason]) return REASON_TEXT[reason];
    if (reason) console.info('[pairUI] unmapped error reason:', reason);
    return 'エラーが発生しました';
  }
  // States in which the session section (swap / change-work / leave) and the
  // side+transport indicators are meaningful.
  const SESSION_STATES = ['connected', 'reconnecting', 'peer-away', 'loading'];
  // One-tap MATCH eligibility window — must mirror MATCH_WINDOW_SEC in
  // app/reader_sync/sessions.py (120). Past it, a waiting leader can only
  // be reached via the code/QR it still shows.
  const MATCH_WINDOW_MS = 120000;
  // How long the strip stays after 'ended' so a reader notices the pair
  // dropped (the strip is the only in-viewer surface).
  const END_FLASH_MS = 3000;

  let ctl = null;
  let unsubscribe = null;
  let open_ = false;
  let joinView = false;   // local UI state: the code-entry form is showing
  let joinPending = false; // a join() is in flight from the join view
  let pickList = null;    // ambiguous MATCH candidates awaiting a pick
  let scanStream = null;  // live camera stream while the join view is open
  let scanRAF = 0;
  let scanCanvas = null;
  let qrDrawnFor = null;  // last code painted into #pair-qr
  let lastStatus = null;  // status from the previous render (auto-close edge)
  let leaderSince = 0;    // when the leader-waiting view first showed
  let leaderNoteTimer = null;
  let endFlashUntil = 0;  // strip stays visible on 'ended' until this
  let endFlashTimer = null;
  let lastFocus = null;
  let inertSnapshot = null; // [[el, hadInertAttr]]
  let built = false;
  let els = {};
  // #158: a failed join's reason survives the error→ended transition so
  // the code form can show it inline instead of losing it on 解除.
  let lastJoinError = null;   // mapped message from the latest error state
  let joinNoticeShown = false; // one-shot: already surfaced in the join form
  let openedAt = 0;            // backdrop dismiss is muted this long (#160)
  let connectUnsupported = false; // connect() answered {unsupported} →
                                  // the choice falls back to 開始/参加
  let connectPending = false;   // a connect() call is in flight — the
                                // つながる pick must not double-fire
  /* A rapid second launcher tap lands on the freshly-shown overlay as a
   * backdrop click and instantly closes the panel while the session keeps
   * connecting (#160-A). Swallow backdrop dismissals for a short grace —
   * a deliberate tap outside the panel still closes it after the window. */
  const DISMISS_GRACE_MS = 350;

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
          '<button id="pair-create" type="button" class="pair-action">' +
            '<span id="pair-create-label">開始</span>' +
            '<span class="pair-sub" id="pair-create-sub">' +
              'この端末が leader になりコードを発行</span></button>' +
          '<button id="pair-guest-invite" type="button" class="pair-action">' +
            '<span>ゲストを招待</span>' +
            '<span class="pair-sub">QR で未ログイン端末を招待（この端末が leader）</span></button>' +
          '<button id="pair-join-open" type="button" class="pair-action">' +
            '<span id="pair-join-open-label">参加</span>' +
            '<span class="pair-sub" id="pair-join-open-sub">' +
              '相手のコードを入力</span></button>' +
          '<p id="pair-guest-note" class="pair-note" hidden>' +
            'ゲストは招待リンク（QR の URL）からのみ参加できます</p>' +
        '</div>' +
        '<div id="pair-sec-join" class="pair-sec" hidden>' +
          '<video id="pair-scan-video" playsinline muted autoplay hidden></video>' +
          '<label id="pair-code-label" for="pair-code-input">6桁のコードを入力</label>' +
          '<input id="pair-code-input" type="text" inputmode="numeric" ' +
            'pattern="[0-9]*" maxlength="6" autocomplete="off" autocorrect="off" ' +
            'spellcheck="false" aria-labelledby="pair-code-label">' +
          '<p id="pair-join-err" class="pair-note pair-err" hidden></p>' +
          '<div class="pair-row">' +
            '<button id="pair-join-back" type="button">戻る</button>' +
            '<button id="pair-join-submit" type="button" class="pair-primary" disabled>参加する</button>' +
          '</div>' +
        '</div>' +
        '<div id="pair-sec-leader" class="pair-sec" hidden>' +
          '<p id="pair-leader-note" class="pair-note"></p>' +
          '<div id="pair-code-value" aria-label="ペアリングコード">------</div>' +
          '<canvas id="pair-qr" width="168" height="168" aria-label="ペアリングQRコード" hidden></canvas>' +
          '<button id="pair-leader-join" type="button" class="pair-link">コードで参加する</button>' +
          '<button id="pair-leader-cancel" type="button">キャンセル</button>' +
        '</div>' +
        '<div id="pair-sec-picker" class="pair-sec" hidden>' +
          '<p class="pair-note">接続先を選んでください</p>' +
          '<div id="pair-picker-list"></div>' +
          '<button id="pair-picker-back" type="button">キャンセル</button>' +
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
          '<div class="pair-row">' +
            '<button id="pair-error-close" type="button" ' +
              'aria-label="エラー表示を閉じる (接続の試行は保持されます)">あとで</button>' +
            '<button id="pair-error-leave" type="button" class="pair-danger" ' +
              'aria-label="接続を破棄して最初からやり直す">やり直す</button>' +
          '</div>' +
        '</div>' +
      '</div>' +
    '</div>';

  function buildDom() {
    if (built) return;
    built = true;

    // Launcher lives statically in the tile/sidebar header (index.html);
    // fall back to a JS twin when the shell predates it.
    let btn = _el('pair-btn');
    if (!btn) {
      btn = document.createElement('button');
      btn.id = 'pair-btn';
      btn.type = 'button';
      btn.title = '2台で見開き';
      btn.setAttribute('aria-label', '2台で見開き');
      btn.textContent = '🔗';
      (_el('sidebar-header') || document.body).appendChild(btn);
    }

    // Persistent status chip — THE state indicator. The wrapper stays the
    // aria-live region (role=status, polite/atomic) while the inner
    // <button> is the operable control: Tab reaches it and Enter/Space
    // open the panel natively, with a 44px target (#159).
    const status = document.createElement('div');
    status.id = 'pair-status';
    status.dataset.status = 'idle';
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    status.setAttribute('aria-atomic', 'true');
    status.innerHTML =
      '<button id="pair-status-btn" type="button" aria-haspopup="dialog" ' +
        'aria-expanded="false" aria-controls="pair-panel">' +
        '<span id="pair-status-text">未接続</span>' +
        '<span id="pair-side" hidden></span>' +
        '<span id="pair-transport" hidden></span>' +
      '</button>';
    // Inside #toolbar the strip rides existing chrome and can never float
    // over page content (#159); the fixed overlay is only the fallback
    // for toolbar-less shells.
    (_el('toolbar') || document.body).appendChild(status);

    const overlay = document.createElement('div');
    overlay.id = 'pair-overlay';
    overlay.className = 'hidden';
    overlay.innerHTML = PANEL_HTML;
    document.body.appendChild(overlay);

    els = {
      btn: btn,
      status: status,
      statusBtn: _el('pair-status-btn'),
      statusText: _el('pair-status-text'),
      side: _el('pair-side'),
      transport: _el('pair-transport'),
      overlay: overlay,
      panel: _el('pair-panel'),
      close: _el('pair-close'),
      secChoice: _el('pair-sec-choice'),
      createLabel: _el('pair-create-label'),
      createSub: _el('pair-create-sub'),
      joinOpenLabel: _el('pair-join-open-label'),
      joinOpenSub: _el('pair-join-open-sub'),
      guestInvite: _el('pair-guest-invite'),
      guestNote: _el('pair-guest-note'),
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
      joinErr: _el('pair-join-err'),
      scanVideo: _el('pair-scan-video'),
      codeValue: _el('pair-code-value'),
      qr: _el('pair-qr'),
      leaderNote: _el('pair-leader-note'),
      leaderJoin: _el('pair-leader-join'),
      leaderCancel: _el('pair-leader-cancel'),
      secPicker: _el('pair-sec-picker'),
      pickerList: _el('pair-picker-list'),
      pickerBack: _el('pair-picker-back'),
      waitingText: _el('pair-waiting-text'),
      waitingCancel: _el('pair-waiting-cancel'),
      sessionInfo: _el('pair-session-info'),
      swap: _el('pair-swap'),
      changeWork: _el('pair-change-work'),
      leave: _el('pair-leave'),
      errorText: _el('pair-error-text'),
      errorClose: _el('pair-error-close'),
      errorLeave: _el('pair-error-leave'),
    };
    wireEvents();
    syncStripA11y();
    watchStripA11y();
  }

  /* The strip must leave the a11y tree whenever chrome hides it — the
   * .hidden fade, the [hidden] collapse and the overview-open veil all
   * leave an invisible-but-focusable control behind otherwise (#159).
   * Ancestors are checked, not only the strip's own flags: a strip
   * (re)built while the toolbar is already .hidden, or a hide path that
   * skips the app.js mirror, must not resurrect the trap. */
  let hideObs = null;
  function syncStripA11y() {
    if (!els.statusBtn) return;
    // !closest('#toolbar'): the toolbar-less fallback appends the strip to
    // body where CSS display:none's it — it must leave the a11y tree too,
    // or an invisible button stays tabbable (#586).
    const gone = !!els.status.closest('.hidden, [hidden]') ||
      !els.status.closest('#toolbar') ||
      document.body.classList.contains('overview-open');
    els.status.setAttribute('aria-hidden', gone ? 'true' : 'false');
    els.statusBtn.tabIndex = gone ? -1 : 0;
    // Focus may not stay on a control that just left the a11y tree.
    const ae = document.activeElement;
    if (gone && ae && els.status.contains(ae)) ae.blur();
  }
  function watchStripA11y() {
    if (hideObs || typeof MutationObserver !== 'function') return;
    hideObs = new MutationObserver(syncStripA11y);
    hideObs.observe(els.status,
      {attributes: true, attributeFilter: ['class', 'hidden']});
    if (els.status.parentElement) {
      // the toolbar's own .hidden — mirrored or not
      hideObs.observe(els.status.parentElement,
        {attributes: true, attributeFilter: ['class', 'hidden']});
    }
    hideObs.observe(document.body,
      {attributes: true, attributeFilter: ['class']});
  }

  // ── render: state → indicators + section visibility ──

  function render() {
    if (!built) return;
    const s = st();
    const status = s.status || 'idle';
    const role = s.role || null;
    const inSession = SESSION_STATES.indexOf(status) !== -1;
    // A join submit is in flight only while the client is connecting;
    // any settled state (error / idle / ended / connected) re-arms the
    // form so a failed or cancelled join can be retried.
    if (status !== 'connecting') joinPending = false;
    // Flash the strip on 'ended' so a reader mid-book sees the pair
    // dropped — status transitions to 'ended' leave no surface otherwise.
    if (status === 'ended' && lastStatus && lastStatus !== 'idle' &&
        lastStatus !== 'ended') {
      endFlashUntil = Date.now() + END_FLASH_MS;
    }
    const flashing = status === 'ended' && Date.now() < endFlashUntil;
    if (flashing && !endFlashTimer) {
      endFlashTimer = setTimeout(() => {
        endFlashTimer = null;
        render();
      }, endFlashUntil - Date.now() + 20);
    }

    let label = STATUS_LABEL[status] || status;
    if (status === 'connected' && role) label += '（' + role + '）';
    els.status.dataset.status = status;
    // The strip only exists while a session is (or is becoming) real —
    // solo reading never sees it. Tap → the session panel.
    els.status.hidden = status === 'idle' || (status === 'ended' && !flashing);
    els.statusText.textContent = label;
    if (els.btn) {
      els.btn.dataset.status = status;
      els.btn.dataset.role = role || '';
    }

    const sideText = SIDE_LABEL[s.side] || '';
    els.side.textContent = sideText;
    els.side.hidden = !(sideText && inSession);
    const trText = TRANSPORT_LABEL[s.transport] || '';
    els.transport.textContent = trText;
    els.transport.hidden = !(trText && inSession);

    /* The idle choice follows controller capability (#160): a MATCH-aware
     * client offers 「近くの相手とつながる / コードで参加」; a legacy
     * client — or one whose connect() answered {unsupported} — keeps the
     * classic 開始/参加 pair. A guest caller can never lead — CREATE and
     * MATCH are rejected `not_leader` on a guest link — and their JOIN
     * path is the invite URL, never a 6-digit code (the server rejects
     * guest JOIN{code} outright), so every code surface hides for them:
     * leader button, join form entry, and the guest-invite action itself.
     * Only the explanatory note remains. */
    const isGuest = window.__BOOKQ_GUEST__ === 'true';
    const matchable = !isGuest &&
                      !!(ctl && typeof ctl.connect === 'function' &&
                         !connectUnsupported);
    if (els.create) show(els.create, !isGuest);
    if (els.joinOpen) show(els.joinOpen, !isGuest);
    if (els.guestInvite) show(els.guestInvite, !isGuest);
    if (els.guestNote) show(els.guestNote, isGuest);
    els.createLabel.textContent = matchable ? '近くの相手とつながる' : '開始';
    els.createSub.textContent = matchable
      ? '自動でマッチング（なければ待機開始）'
      : 'この端末が leader になりコードを発行';
    els.joinOpenLabel.textContent = matchable ? 'コードで参加' : '参加';
    els.joinOpenSub.textContent = matchable
      ? '相手の6桁コードを入力' : '相手のコードを入力';

    let sec;
    if (status === 'idle' || status === 'ended') {
      // joinView wins: a slow {ambiguous} resolving while the code form
      // is open must not steal the section mid-entry — the picker waits
      // for the user to back out (#160).
      sec = joinView ? els.secJoin
                     : (pickList ? els.secPicker : els.secChoice);
    } else if (status === 'connecting') {
      sec = role === 'leader' ? els.secLeader : els.secWaiting;
    } else if (status === 'error') {
      sec = els.secError;
    } else {
      sec = els.secSession;
    }
    const secs = [els.secChoice, els.secJoin, els.secLeader,
                  els.secWaiting, els.secSession, els.secError, els.secPicker];
    for (const e of secs) show(e, e === sec);
    if (sec !== els.secJoin) stopScan();

    if (sec === els.secPicker) {
      els.pickerList.innerHTML = '';
      for (const c of pickList || []) {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'pair-pick';
        // Candidates carry an opaque server token, never the 6-digit
        // code — a picked row re-MATCHes with connect(id) so the human
        // code stays private to the waiting leader's own screen.
        b.textContent = (c.leaderName || '待機中の端末') +
          (typeof c.ageSec === 'number' ? ' — ' + c.ageSec + '秒前' : '');
        b.addEventListener('click', () => {
          pickList = null;
          if (ctl) ctl.connect(c.id);
        });
        els.pickerList.appendChild(b);
      }
    }

    if (sec === els.secLeader) {
      els.codeValue.textContent = s.code || '------';
      drawQR(s);
      // Past the match window nobody can find this session via 🔗 —
      // say so instead of promising a join that can no longer happen.
      if (!leaderSince) leaderSince = Date.now();
      const left = MATCH_WINDOW_MS - (Date.now() - leaderSince);
      els.leaderNote.textContent = left > 0
        ? 'もう1台が接続ボタンを押すと自動でつながります'
        : '自動接続の受付は終了しました。コードまたはQRで参加できます';
      if (leaderNoteTimer) { clearTimeout(leaderNoteTimer); leaderNoteTimer = null; }
      if (left > 0) {
        leaderNoteTimer = setTimeout(() => {
          leaderNoteTimer = null;
          render();
        }, left + 20);
      }
    } else {
      leaderSince = 0;
      if (leaderNoteTimer) { clearTimeout(leaderNoteTimer); leaderNoteTimer = null; }
      if (sec === els.secWaiting) {
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
        const msg = errorTextFor(s.error);
        els.errorText.textContent = msg;
        // Carry the reason into the join form when 解除 returns there —
        // only join-flow errors (joinView was up when the error hit).
        // joinNoticeShown re-arms so a fresh failure surfaces again.
        lastJoinError = joinView ? msg : null;
        joinNoticeShown = false;
      } else if (sec === els.secJoin) {
        if (lastJoinError && !joinNoticeShown) {
          // One-shot carry-over (#158): show the failed join's reason
          // inline and reset the field so the same wrong code isn't
          // resubmitted blindly. Consumed once — later renders leave
          // the user's typing alone.
          joinNoticeShown = true;
          joinError(lastJoinError);
          els.codeInput.value = '';
          els.joinSubmit.disabled = true;
        } else {
          els.joinSubmit.disabled = els.codeInput.value.length !== 6;
        }
      }
    }

    // A pending→connected transition means pairing just succeeded (or a
    // reconnect healed) — the modal's job is done, get out of the way.
    // Reopening while already connected stays open (swap / leave UI).
    const became = status === 'connected' &&
      ['connecting', 'loading', 'reconnecting', 'peer-away'].indexOf(lastStatus) !== -1;
    lastStatus = status;
    if (became && open_) close();
  }

  // ── open / close with inert + focus management ──

  function firstControl() {
    const secs = [els.secJoin, els.secLeader, els.secWaiting,
                  els.secSession, els.secError, els.secPicker, els.secChoice];
    const sec = secs.find(e => e && !e.hidden);
    const t = sec && sec.querySelector('input, button:not([disabled])');
    return t || els.close;
  }

  function open() {
    if (open_) return;
    open_ = true;
    openedAt = Date.now();
    lastFocus = document.activeElement;
    if (els.statusBtn) els.statusBtn.setAttribute('aria-expanded', 'true');
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

  /* A focus-restore candidate must be somewhere a user can actually
   * see and reach (#586): mounted, not inert/[hidden]/.hidden-chrome,
   * not under the overview veil, not flung off-canvas (the closed
   * sidebar slides translateX(-100%) — only a rect check catches that,
   * no class marks it), and really painted inside the viewport. BODY
   * itself is never a valid landing spot: focusing it is a no-op that
   * strands focus exactly where #159 found it. */
  function focusable(el) {
    if (!el || el === document.body || el === document.documentElement) {
      return false;
    }
    if (!document.contains(el) || typeof el.focus !== 'function') {
      return false;
    }
    if (el.closest('[inert]') || el.closest('[hidden]') ||
        el.closest('.hidden')) return false;
    /* overview-open veils chrome with opacity:0 — #toolbar/#spread-controls
     * are also inerted (covered above) and the sidebar launcher stays
     * visible, so only the veil-without-inert set needs an explicit check
     * (#586 review: a blanket body.overview-open reject stranded focus on
     * BODY even when #pair-btn was plainly on screen). */
    if (document.body.classList.contains('overview-open') &&
        el.closest('#tapmode-controls, #hint, #zone-prev, #zone-next,' +
                   ' #zone-toggle, #img-retry')) return false;
    if (typeof el.checkVisibility === 'function' &&
        !el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) {
      return false;
    }
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 &&
           r.bottom > 0 && r.right > 0 &&
           r.top < window.innerHeight && r.left < window.innerWidth;
  }

  function close() {
    if (!open_) return;
    open_ = false;
    joinView = false;
    pickList = null;
    stopScan();
    els.overlay.classList.add('hidden');
    if (els.statusBtn) els.statusBtn.setAttribute('aria-expanded', 'false');
    if (inertSnapshot) {
      for (const [el, was] of inertSnapshot) {
        if (!was) el.removeAttribute('inert');
      }
      inertSnapshot = null;
    }
    // Restore focus to where it came from; if that element is gone or
    // not actually on screen, land on a real control — the visible
    // strip first, then the launcher, then the menu button — never
    // BODY (#159; off-canvas/hidden-aware since #586).
    let back = lastFocus;
    lastFocus = null;
    if (!focusable(back)) {
      back = focusable(els.statusBtn) ? els.statusBtn : null;
    }
    if (!focusable(back)) back = els.btn;
    if (!focusable(back)) back = _el('menu-btn');
    if (focusable(back)) back.focus();
  }

  // ── code input ──

  function sanitizeCode() {
    const v = els.codeInput.value.replace(/\D/g, '').slice(0, 6);
    if (v !== els.codeInput.value) els.codeInput.value = v;
    els.joinSubmit.disabled = v.length !== 6;
    if (v.length === 6) submitJoin();   // no extra tap: 6th digit submits
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
    // Guests join via the invite URL only — the server rejects guest
    // JOIN{code}, so don't even offer the attempt.
    if (!/^\d{6}$/.test(v) || !ctl || joinPending ||
        window.__BOOKQ_GUEST__ === 'true') return;
    joinPending = true;   // cleared when the join view re-opens
    // A fresh attempt retires the carried-over failure notice (#158).
    lastJoinError = null;
    joinNoticeShown = false;
    joinError('');
    ctl.join(v);
  }

  // ── QR display (leader) ──

  function drawQR(s) {
    if (!els.qr) return;
    // One QR serves both readers: the `?gt=` guest token rides the
    // `/#/join/<code>` deep link, so an unlogged-in browser redeems the
    // guest grant while native apps and member scans still extract the
    // plain 6-digit code (they can't use — and don't need — the grant).
    const url = (s && s.code && s.guestToken)
      ? location.origin + '/#/join/' + s.code + '?gt=' + s.guestToken
      : (s && s.code ? location.origin + '/#/join/' + s.code : null);
    if (!url || typeof window.qrcode !== 'function') {
      els.qr.hidden = true;
      els.qr.dataset.qrUrl = '';
      qrDrawnFor = null;
      return;
    }
    // Expose the encoded URL on the canvas: the browser e2e drives the
    // real 「ゲストを招待」 button and needs the actual link a scanner
    // would read — re-deriving it in the test would miss format drift.
    els.qr.dataset.qrUrl = url;
    if (qrDrawnFor === url) { els.qr.hidden = false; return; }
    // 'L' keeps module count low for the longer URL payload so cells
    // stay scannable on a ~170px canvas.
    const qr = window.qrcode(0, 'L');
    // URL-QR: any camera app deep-links straight into join — the token is
    // in the fragment so it never reaches access logs or cache keys.
    qr.addData(url);
    qr.make();
    const n = qr.getModuleCount();
    const cell = Math.max(1, Math.floor(els.qr.width / (n + 8)));  // +quiet zone
    const off = Math.floor((els.qr.width - cell * n) / 2);
    const c = els.qr.getContext('2d');
    c.fillStyle = '#fff';
    c.fillRect(0, 0, els.qr.width, els.qr.height);
    c.fillStyle = '#000';
    for (let r = 0; r < n; r++) {
      for (let col = 0; col < n; col++) {
        if (qr.isDark(r, col)) {
          c.fillRect(off + col * cell, off + r * cell, cell, cell);
        }
      }
    }
    els.qr.hidden = false;
    qrDrawnFor = url;
  }

  // ── QR scan (follower) ──

  function joinError(msg) {
    if (!els.joinErr) return;
    els.joinErr.textContent = msg || '';
    els.joinErr.hidden = !msg;
  }

  function startScan() {
    joinError('');
    if (scanStream) return;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia ||
        typeof window.jsQR !== 'function') {
      joinError('この端末ではカメラ読み取りを使えません');
      return;
    }
    navigator.mediaDevices.getUserMedia({
      video: {facingMode: 'environment'}, audio: false,
    }).then(stream => {
      if (!joinView) { stream.getTracks().forEach(t => t.stop()); return; }
      scanStream = stream;
      els.scanVideo.srcObject = stream;
      els.scanVideo.hidden = false;
      els.scanVideo.play().catch(() => {});
      scanRAF = requestAnimationFrame(scanTick);
    }).catch(() => {
      // A carried-over join-failure notice (#158) outranks the camera
      // error — the getUserMedia rejection can land after it was shown.
      if (!joinNoticeShown) joinError('カメラを起動できませんでした');
    });
  }

  function scanTick() {
    scanRAF = 0;
    if (!scanStream) return;
    const v = els.scanVideo;
    if (v.readyState >= 2 && v.videoWidth && v.videoHeight) {
      if (!scanCanvas) scanCanvas = document.createElement('canvas');
      scanCanvas.width = v.videoWidth;
      scanCanvas.height = v.videoHeight;
      const cx = scanCanvas.getContext('2d', {willReadFrequently: true});
      cx.drawImage(v, 0, 0);
      try {
        const img = cx.getImageData(0, 0, scanCanvas.width, scanCanvas.height);
        const res = window.jsQR(img.data, img.width, img.height);
        if (res) {
          // accept a bare 6-digit code and the #/join/<code> deep-link
          // (the QR's `?gt=` guest token is for unlogged-in browsers —
          // a logged-in member always joins via the code itself)
          const code = /^\d{6}$/.test(res.data) ? res.data
            : (res.data.match(/#\/join\/(\d{6})/) || [])[1];
          if (code) {
            stopScan();
            els.codeInput.value = code;
            sanitizeCode();   // auto-submits at 6 digits
            return;
          }
        }
      } catch (e) { /* decode errors are per-frame — keep scanning */ }
    }
    scanRAF = requestAnimationFrame(scanTick);
  }

  function stopScan() {
    if (scanRAF) { cancelAnimationFrame(scanRAF); scanRAF = 0; }
    if (scanStream) {
      scanStream.getTracks().forEach(t => t.stop());
      scanStream = null;
    }
    if (els.scanVideo) {
      els.scanVideo.srcObject = null;
      els.scanVideo.hidden = true;
    }
  }

  // ── events ──

  function wireEvents() {
    /* The idle launcher tap used to fire connect() itself — a quick
     * second tap then closed the panel mid-MATCH, and anyone holding a
     * 6-digit code had to become a waiting leader first (#160). The tap
     * now only opens the panel; the choice section offers 「近くの相手と
     * つながる」 (connect) and 「コードで参加」 (join form) explicitly. */
    els.btn.addEventListener('click', e => {
      e.stopPropagation();
      open();
    });
    els.status.addEventListener('click', e => { e.stopPropagation(); open(); });
    els.close.addEventListener('click', close);
    els.errorClose.addEventListener('click', close);
    els.errorLeave.addEventListener('click', () => { if (ctl) ctl.leave(); });
    // Backdrop tap dismisses like the other sheets do; a tap that lands on
    // the panel itself never reaches this branch. Right after open() the
    // double-tap grace swallows the stray second launcher tap (#160).
    els.overlay.addEventListener('click', e => {
      if (e.target === els.overlay &&
          Date.now() - openedAt > DISMISS_GRACE_MS) close();
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

    /* 「近くの相手とつながる」 — the explicit MATCH trigger (#160-B).
     * connect() joins a nearby waiting session or becomes the waiting
     * leader; {ambiguous} lists candidates, {unsupported} demotes the
     * choice back to the legacy 開始/参加 pair. */
    function fireConnect() {
      if (!ctl || typeof ctl.connect !== 'function' || connectPending) {
        return;
      }
      let res;
      try { res = ctl.connect(); } catch (err) { res = null; }
      if (res && typeof res.then === 'function') {
        connectPending = true;
        res.then(r => {
          connectPending = false;
          if (r && r.ambiguous && Array.isArray(r.candidates) &&
              r.candidates.length) {
            pickList = r.candidates;
            if (open_) render(); else open();
          } else {
            if (r && r.unsupported) connectUnsupported = true;
            if (open_) render();
          }
        }).catch(() => { connectPending = false; });
      }
    }
    els.create.addEventListener('click', () => {
      if (ctl && typeof ctl.connect === 'function' && !connectUnsupported) {
        fireConnect();
      } else if (ctl) {
        ctl.create();
      }
    });
    /* 「ゲストを招待」 — explicit CREATE (never MATCH): only a CREATE ACK
     * mints the guestToken the composite `/#/join/<code>?gt=` QR needs,
     * so the invite action must not go through connect()'s match path. */
    els.guestInvite.addEventListener('click', () => {
      if (ctl && typeof ctl.create === 'function') ctl.create();
    });
    els.joinOpen.addEventListener('click', () => {
      joinView = true;
      joinPending = false;
      lastJoinError = null;
      joinNoticeShown = false;
      render();
      // Camera is the primary entry path here; no autofocus so the soft
      // keyboard stays down over the live preview.
      startScan();
    });
    els.joinBack.addEventListener('click', () => {
      joinView = false;
      els.codeInput.value = '';
      stopScan();
      render();
      firstControl().focus();
    });
    els.joinSubmit.addEventListener('click', submitJoin);
    // The waiting leader can still join someone else's code — leave the
    // own waiting session first so it does not linger orphaned.
    els.leaderJoin.addEventListener('click', () => {
      if (ctl) ctl.leave();
      joinView = true;
      joinPending = false;
      lastJoinError = null;
      joinNoticeShown = false;
      render();
      startScan();
    });
    els.pickerBack.addEventListener('click', () => {
      pickList = null;
      render();
    });
    els.codeInput.addEventListener('input', sanitizeCode);
    els.codeInput.addEventListener('paste', onCodePaste);
    els.codeInput.addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); submitJoin(); }
    });
    els.leaderCancel.addEventListener('click', () => { if (ctl) ctl.leave(); });
    els.waitingCancel.addEventListener('click', () => { if (ctl) ctl.leave(); });
    els.swap.addEventListener('click', () => { if (ctl) ctl.swapSides(); });
    els.leave.addEventListener('click', () => { if (ctl) ctl.leave(); });
    // Optional extension point — S09 wires the real work picker; the mock
    // may or may not implement it. A successful call opens the browse list
    // under this dialog — close so the lid does not block the picker.
    els.changeWork.addEventListener('click', () => {
      if (ctl && typeof ctl.changeWork === 'function' &&
          ctl.changeWork() !== false) close();
    });
  }

  // ── controller binding / public API ──

  function attach(controller) {
    if (unsubscribe) {
      try { unsubscribe(); } catch (_) { /* controller cleanup is best-effort */ }
      unsubscribe = null;
    }
    ctl = controller || null;
    connectUnsupported = false;
    connectPending = false;
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
