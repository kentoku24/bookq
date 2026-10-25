/* bookq browse/pairSync — ペアモード中のブラウズ一覧ミラー (#75 系)。
 * Plain global script. index.html で pairController.js の後、app.js の前に読む
 * （実行時はすべて window.* 経由の遅延解決なので、DOMContentLoaded で init する）。
 *
 * 何をするか:
 *   leader がブラウズ一覧（#sidebar-scroll / #tile-grid）を開くと pair の phase が
 *   'selecting' になる（pairController の sidebar observer → SET_ACTIVITY）。
 *   armed = paired && phase==='selecting' の間、自分の {path, scrollTop, viewH,
 *   viewMode} を ephemeral frame UI_SYNC で peer へ中継する。受信側は不変式
 *   「followerTop = leaderTop + leaderClientHeight」で自分の scrollTop を導く:
 *
 *     follower <- leader : scrollTop = msg.top + msg.viewH     （1画面分下）
 *     leader   <- follower: scrollTop = msg.top - 自分の clientHeight
 *
 *   path / mode が違えば先にローカル遷移（currentBrowsePath + viewMode +
 *   renderView({scrollAnchor:null}) — BrowseScroll.decide をバイパスして決定的）。
 *   結果、スクロール・フォルダ潜り・戻る・表示モード変更はすべて双方向で伝搬する。
 *   follower のサイドバーは最初の有効 frame で自動的に開き、disarm（phase 離脱 /
 *   unpaired）で閉じる — 下には既に適用済みの共有見開きがある。
 *
 *   UI_SYNC は committed reader state ではない（revision/apply_log/dedup を汚さない
 *   SIGNAL 同型の ephemeral 中継）。URL hash は書かない（§3.6: ペア情報はURLへ
 *   書かない）。searchMode 中は送受信とも停止（検索結果の同期はスコープ外）。
 *   remote 適用が起こす scroll/observer イベントは _suppressUntil の窓で echo
 *   しない。follower は最初の remote 適用まで送信解禁しない（stale 位置で
 *   leader を巻き込まない）。
 */
(function () {
  'use strict';

  var SEND_MIN_MS = 60;      // scroll 送信の最小間隔（leading edge）
  var SEND_TRAIL_MS = 120;   // 最終位置を必ず送る trailing デバウンス
  var SUPPRESS_MS = 400;     // remote 適用直後の echo 抑止窓

  var _armed = false;        // ミラー有効（live pair && phase==='selecting'）
  var _unlocked = false;     // 送信解禁 — leader は arm 即時、follower は初適用後
  var _seq = 0;
  var _lastSendTs = -1e9;
  var _lastSentSig = null;
  var _appliedSig = null;    // 直近に remote 適用した local state の sig（echo 検出用）
  var _trailT = null;
  var _suppressUntil = 0;    // echo 抑止の期限（Date.now ベース）
  var _pendingMsg = null;    // 未適用の最新 remote msg（未 arm/tree 未ロード/searchMode 等）
  var _mirrorOpened = false; // このモジュールが開いた sidebar か（disarm で畳む対象）
  var _prevPresenceSig = '';
  var _inited = false;
  var _subs = [];            // PairController から外した購読（解除用）

  function _w() { return window; }
  function _pc() { return _w().PairController || null; }
  function _scroller() {
    return (_w().BrowseScroll && _w().BrowseScroll.scroller)
      ? _w().BrowseScroll.scroller()
      : document.getElementById('sidebar-scroll');
  }
  function _sidebar() { return document.getElementById('sidebar'); }
  function _sidebarOpen() {
    var s = _sidebar();
    return !!(s && s.classList && s.classList.contains('open'));
  }
  function _searchMode() { return !!_w().searchMode; }
  function _paired(st) {
    var p = st && st.presence;
    return !!p && Object.keys(p).length >= 2;
  }
  /* 'ended'/'error'/'idle' は presence が残っていてもミラー対象外 —
   * controller._memberBound と同じ live 判定。 */
  function _live(st) {
    return !!st && !!st.sessionId && st.status !== 'idle' &&
      st.status !== 'ended' && st.status !== 'error';
  }
  function _presenceSig(st) {
    var p = st && st.presence;
    if (!p) return '';
    var keys = Object.keys(p).sort();
    var out = [];
    for (var i = 0; i < keys.length; i++) {
      var m = p[keys[i]] || {};
      out.push(keys[i] + ':' + m.status + ':' + m.role);
    }
    return out.join(',');
  }
  function _role() {
    var pc = _pc();
    var st = pc && pc.state;
    return st ? st.role : null;
  }

  /* ── arm / disarm ── */

  function _onCtlState(st) {
    var armed = _live(st) && _paired(st) && st.phase === 'selecting';
    if (armed && !_armed) {
      _armed = true;
      _prevPresenceSig = _presenceSig(st);
      // arm 前に届いた frame（relay が STATE 反映を追い越す競合）をここで
      // 適用する — 捨てると leader の arm-edge 一発を逃して follower が
      // 永久に開かない。
      if (_pendingMsg) {
        var m = _pendingMsg; _pendingMsg = null;
        _applyRemote(m);
      }
      // leader は開いている側 — すぐ共有を始める。follower は remote を待つ
      // （まだ stale 位置を送ると leader を巻き込む）。
      if (st.role === 'leader') {
        _unlocked = true;
        _requestSend();
      }
      return;
    }
    if (!armed && _armed) {
      _armed = false;
      _unlocked = false;
      _lastSentSig = null;
      _appliedSig = null;
      _pendingMsg = null;
      _suppressUntil = 0;
      if (_trailT) { clearTimeout(_trailT); _trailT = null; }
      // 自分が開いたミラー用サイドバーだけ畳む — role を見ると 'ended' で
      // null 化した後に leader の手動 sidebar まで畳んでしまう。
      if (_mirrorOpened && _sidebarOpen()) {
        try { _w().closeSidebar && _w().closeSidebar(); }
        catch (e) { console.warn(e); }
      }
      _mirrorOpened = false;
      return;
    }
    if (armed) {
      // presence 変化（peer の復帰等）で leader は現在値を再送する。
      var sig = _presenceSig(st);
      if (sig !== _prevPresenceSig) {
        _prevPresenceSig = sig;
        if (st.role === 'leader') _requestSend();
      }
      // 未適用 msg を新しい state でもリトライしない（retry は描画/次 msg で）。
    }
  }

  function _onCtlEvent(e) {
    if (e && e.type === 'ui-sync') _applyRemote(e.payload);
  }

  /* ── 送信 ── */

  function _sendGate() {
    return _armed && _unlocked && _sidebarOpen() && !_searchMode() &&
      !!_scroller() && !!_pc();
  }

  function _payload() {
    var sc = _scroller();
    return {
      view: 'browse',
      path: _w().currentBrowsePath || null,
      top: sc.scrollTop,
      viewH: sc.clientHeight,
      mode: _w().viewMode || 'tile',
      seq: ++_seq,
    };
  }

  function _sigOf(p) {
    return (p.path || '') + '|' + Math.round(p.top) + '|' +
      Math.round(p.viewH) + '|' + p.mode;
  }

  function _deferSend(ms) {
    if (_trailT) clearTimeout(_trailT);
    _trailT = setTimeout(function () {
      _trailT = null;
      _requestSend();
    }, ms);
  }

  function _sendNow() {
    if (!_sendGate()) return;
    var now = Date.now();
    if (_suppressUntil > now) {
      // 抑止窓内の send は「捨てる」ではなく窓の終わりへ defer する —
      // remote 適用直後のローカル操作（nav/scroll）が永久に失われるのを防ぐ。
      // 内容が適用値と同じなら _appliedSig が窓明けで skip するので echo もない。
      _deferSend(_suppressUntil - now + 20);
      return;
    }
    var p = _payload();
    var sig = _sigOf(p);
    if (sig === _lastSentSig) return;
    if (_appliedSig) {
      var isEcho = (sig === _appliedSig);
      _appliedSig = null;          // one-shot: echo 一回分だけ skip
      if (isEcho) { _lastSendTs = now; return; }
    }
    _lastSentSig = sig;
    _lastSendTs = now;
    try { _pc().sendUiSync(p); } catch (e) { console.warn(e); }
  }

  /* scroll/observer 起点: 連続スクロールは SEND_MIN_MS 間隔 + 最後に必ず
   * trailing 送信。抑止窓中は窓明けへ defer（drop しない）。 */
  function _requestSend() {
    if (!_sendGate()) return;
    var now = Date.now();
    if (_suppressUntil <= now && now - _lastSendTs >= SEND_MIN_MS) {
      _sendNow();
      return;
    }
    _deferSend(Math.max(SEND_TRAIL_MS, _suppressUntil - now + 20));
  }

  /* ── 受信適用 ── */

  function _ensureSidebarOpen() {
    if (_role() === 'leader' || _sidebarOpen()) return;
    var s = _sidebar();
    var v = document.getElementById('viewer');
    if (!s) return;
    s.classList.add('open');
    _mirrorOpened = true;
    if (v) v.classList.add('sidebar-open');
    try { if (_w().syncSidebarA11y) _w().syncSidebarA11y(); }
    catch (e) { console.warn(e); }
    // 正規の open 経路と同じレイアウトに揃える — class 直書きだけだと
    // tile-full が付かずタイルが縮小描画され、scrollHeight が peer と
    // 食い違ってオフセット適用が maxTop に clamp される。
    try { if (_w().renderView) _w().renderView(); }
    catch (e) { console.warn(e); }
  }

  /* path がローカル tree で解決できるか。null=ルートは常に可。 */
  function _pathResolvable(path) {
    if (path === null || path === undefined) return true;
    try {
      return !!(_w().getNodeAt && _w().getNodeAt(path));
    } catch (e) {
      return false;
    }
  }

  function _applyRemote(msg) {
    if (!msg || msg.view !== 'browse') return;
    if (!_armed) {
      // relay が STATE(phase=selecting) の反映を追い越す競合では未 arm —
      // 捨てずに最新のみ保持し、_onCtlState の arm-edge で適用する。
      _pendingMsg = msg;
      return;
    }
    if (_searchMode() || !_pathResolvable(msg.path)) {
      // 最新のみ保持 — searchMode 解除後 / tree 到着後に observer・次 msg で再適用。
      _pendingMsg = msg;
      return;
    }
    _pendingMsg = null;
    _appliedSig = null;
    _suppressUntil = Date.now() + SUPPRESS_MS;

    var sc = _scroller();
    if (!sc) return;
    _ensureSidebarOpen();

    var target;
    var myTop = sc.scrollTop;
    var targetPath = (msg.path === undefined) ? null : msg.path;
    var navNeeded = (targetPath || null) !== (_w().currentBrowsePath || null);
    var modeNeeded = !!msg.mode && _w().viewMode !== msg.mode;

    if (navNeeded || modeNeeded) {
      try {
        if (navNeeded) _w().currentBrowsePath = targetPath || null;
        if (modeNeeded) _w().viewMode = msg.mode;
        if (_w().updateBrowseHeader) _w().updateBrowseHeader();
        // scrollAnchor:null は「位置を決める」render（上端）— 直後に remote
        // 値で上書きする。saved/decide 経路は経由しない（決定的）。
        if (_w().renderView) _w().renderView({scrollAnchor: null});
      } catch (e) { console.warn(e); }
      sc = _scroller();          // render で参照が変わっていても再解決
      if (!sc) return;
    }

    if (_role() === 'follower') target = msg.top + msg.viewH;
    else target = msg.top - sc.clientHeight;
    sc.scrollTop = Math.max(0, target);
    // 適用後の local state を記録 — 抑止窓明けの trailing send がこの sig と
    // 一致すれば echo（適用が起こした scroll event）と分かり skip できる。
    _appliedSig = _sigOf({
      path: _w().currentBrowsePath, top: sc.scrollTop,
      viewH: sc.clientHeight, mode: _w().viewMode,
    });
    if (_role() === 'follower' && !_unlocked) _unlocked = true;
  }

  /* tile-grid の childList 変化 = renderView 系の全再描画（潜り/戻る/
   * mode/件数更新）。ローカル nav は live 値が送られるのでここで拾えば
   * 新規リスナを増やさずに済む。pending のリトライも兼ねる。 */
  function _onGridMutated() {
    if (_pendingMsg) _applyRemote(_pendingMsg);
    _requestSend();
  }

  /* ── init ── */

  function _init() {
    if (_inited) return;
    _inited = true;
    var pc = _pc();
    if (!pc) return;                             // solo: 無タッチ
    var ss = document.getElementById('sidebar-scroll');
    if (ss) {
      ss.addEventListener('scroll', function () {
        _requestSend();   // 抑止/defer の判定は _requestSend 側で一元化
      }, {passive: true});
    }
    var tg = document.getElementById('tile-grid');
    if (tg && typeof MutationObserver === 'function') {
      try {
        new MutationObserver(_onGridMutated).observe(tg, {childList: true});
      } catch (e) { console.warn(e); }
    }
    window.addEventListener('resize', function () { _requestSend(); },
                            {passive: true});
    if (typeof pc.subscribe === 'function') _subs.push(pc.subscribe(_onCtlState));
    if (typeof pc.onEvent === 'function') _subs.push(pc.onEvent(_onCtlEvent));
    // subscribe は即時通知しない — 起動時点の状態で arm 判定する
    // （ペア中のリロードが selecting の途中で戻るケース）。
    _onCtlState(pc.state);
  }

  if (window.__BOOKQ_TEST_MODE__ === 'true') {
    window.__pairBrowseSync = {
      get armed() { return _armed; },
      get unlocked() { return _unlocked; },
      get lastSentSig() { return _lastSentSig; },
      get suppressUntil() { return _suppressUntil; },
      get pending() { return _pendingMsg; },
      get seq() { return _seq; },
      flush: function () {
        if (_trailT) { clearTimeout(_trailT); _trailT = null; }
        if (_pendingMsg) _applyRemote(_pendingMsg);
        _lastSendTs = -1e9;
        _sendNow();
      },
      applyRemote: _applyRemote,
      onState: _onCtlState,
    };
  }

  if (typeof document !== 'undefined' &&
      document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _init);
  } else if (typeof document !== 'undefined') {
    _init();
  }
})();
