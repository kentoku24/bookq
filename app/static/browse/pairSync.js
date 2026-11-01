/* bookq browse/pairSync — ペアモード中のブラウズ一覧ミラー (#75 系)。
 * Plain global script. index.html で pairController.js の後、app.js の前に読む
 * （実行時はすべて window.* 経由の遅延解決なので、DOMContentLoaded で init する）。
 *
 * 何をするか:
 *   leader がブラウズ一覧（#sidebar-scroll / #tile-grid）を開くと pair の phase が
 *   'selecting' になる（pairController の sidebar observer → SET_ACTIVITY）。
 *   armed = live pair && phase==='selecting' の間、自分の {path, scrollTop,
 *   clientHeight, scrollHeight, viewMode} を ephemeral frame UI_SYNC で
 *   peer へ中継する。受信側は不変式「peer のスクロール進捗 = 自分の
 *   スクロール進捗」で scrollTop を導く（双方向とも同じ式）:
 *
 *     peerMax   = max(0, scrollH - viewH)
 *     prog      = peerMax > 0 ? min(1, top / peerMax) : 0
 *     scrollTop = prog * max(0, 自分の scrollHeight - clientHeight)
 *
 *   tile グリッドの列数は端末幅で変わるため、px ではなく進捗率で写す —
 *   画面の大きい peer（タブレット等）でも同じ割合位置に収まる。
 *   進捗写像は「自分も scrollH を送れる」デプロイでのみ有効
 *   （_canRatio: pairClient.js が立てる BOOKQ_UI_SYNC_V2）。SWR で
 *   新 pairSync + 旧 pairClient が混ざった端末では送信に scrollH が
 *   載らず peer 側は legacy を使うため、受信も legacy に倒して両方向を
 *   対称に保つ（進捗/px が方向で混ざると切替のたびにずれが積む）。
 *   scrollH を持たない旧クライアントの frame も同じ legacy px 変換
 *   （followerTop = top + viewH / leaderTop = top - clientHeight）。
 *   なお scrollHeight は content-visibility の記憶高さを含み得る近似値
 *   （分母に入るため位置の誤差になるが、端末間で同傾向の誤差）。
 *
 *   path / mode が違えば先にローカル遷移（scrollPositions 保存 →
 *   currentBrowsePath + viewMode + renderView({scrollAnchor:null}) +
 *   writeBrowseStateReplace — URL hash も実階層に追従させるが replaceState
 *   限定: push/history.go は hashchange→applyUrlState の二重適用になる）。
 *   結果、スクロール・フォルダ潜り・戻る・表示モード変更は双方向で伝搬する。
 *
 *   tile受信時のスクロール適用は、受信毎の scrollTop 書き換えではなく
 *   「追従ドライブ」（後述）が行う: 目標 = 直近実測位置 or 送信側が実
 *   scrollイベントからフィットしたフリング終端予測 pred、表示 = 目標への
 *   2段一次遅れフィルタ — 受信間を毎フレーム連続描画し、静的目標に
 *   オーバーシュートしない（補正由来の巻き戻し・再加速が構造上出ない）。
 *   list/card・階層遷移・初回適用は従来どおり即時適用。
 *
 *   UI_SYNC は committed reader state ではない（revision/apply_log/dedup を
 *   汚さない SIGNAL 同型の ephemeral 中継）。searchMode 中は送受信とも停止
 *   （検索結果の同期はスコープ外）。順序は単一 WS link が保証するので
 *   seq は持たない。
 *
 *   follower のミラー sidebar は最初の有効 frame で自動的に開き、disarm
 *   （phase 離脱 / session 終了）で自分が開いた分だけ畳む。armed 中に
 *   ユーザーが閉じた場合はその arm 期間中は開き直さない（_userClosed）。
 *
 *   echo/逆変換の保護:
 *   - remote 適用直後 SUPPRESS_MS 間は scroll send を窓明けへ defer する
 *     （drop しない — 窓内のローカル操作が失われるのを防ぐ）。
 *   - _appliedHold: 適用直後の位置と同じ path/top/mode に居る間は送信しない。
 *     clamp で目標に届かなかった位置（例: 末尾で maxTop に貼り付く）を再送
 *     すると peer が逆変換で戻されるのを防ぐ。ユーザーが実際に
 *     動かした時点（top が変わる）で解除される。
 */
(function () {
  'use strict';

  var SEND_MIN_MS = 60;      // scroll 送信の最小間隔（leading edge）
  var SEND_TRAIL_MS = 120;   // 最終位置を必ず送る trailing デバウンス
  var SUPPRESS_MS = 400;     // remote 適用直後の echo 抑止窓
  var PENDING_TTL_MS = 2000; // 未適用 frame の賞味期限（古い stash は捨てる）
  var STALE_MS = 180;
  var TARGET_TAU_MS = 55;
  var FLING_HIST_MS = 300;
  var FLING_TAU_DEF_MS = 250;
  var FLING_TAU_MIN_MS = 80;
  var FLING_TAU_MAX_MS = 800;
  var POSITION_EPS_PX = 1;
  var EST_MIN_MS = 30;
  var EST_MAX_MS = 200;
  var LOCAL_SETTLE_MS = 400;

  var _armed = false;        // ミラー有効（live pair && phase==='selecting'）
  var _unlocked = false;     // 送信解禁 — leader は arm 即時、follower は初適用後
  var _lastSendTs = -1e9;
  var _lastSentSig = null;
  var _appliedHold = null;   // 直近に適用した remote 由来 state {path,top,mode}
  var _trailT = null;
  var _suppressUntil = 0;    // echo 抑止の期限（Date.now ベース）
  var _pendingMsg = null;    // 未適用の最新 remote {msg, at}（未 arm/searchMode 等）
  var _mirrorOpened = false; // このモジュールが開いた sidebar か（disarm で畳む対象）
  var _userClosed = false;   // armed 中にユーザーがミラー sidebar を閉じた
  var _prevPresenceSig = '';
  var _lastRemoteMsg = null; // 最後に受理した remote frame（resize 時の再写像用）
  var _inited = false;
  var _subs = [];            // PairController から外した購読（解除用）
  var _follow = null;
  var _followCtx = null;
  var _rafId = 0;
  var _touchActive = false;
  var _pointerActive = false;
  var _lastLocalMove = -1e9;
  var _lastLocalIntent = -1e9;
  var _sndHist = [];
  var _trace = null;

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
  /* このデプロイの sendUiSync は scrollH をワイヤに載せるか。
   * 旧 pairClient.js との SWR 混在では false — その場合は送信も受信
   * 適用も legacy px に揃え、方向による写像の食い違いを防ぐ。 */
  function _canRatio() {
    try { return _w().BOOKQ_UI_SYNC_V2 === true; }
    catch (e) { return false; }
  }

  /* ── pending（未適用 frame の短期 stash）── */

  function _stashPending(msg, at) {
    _pendingMsg = {msg: msg, at: at || Date.now()};
  }
  /* 取り出して消費。TTL 切れは捨てる — 古い位置（別 arm・別フォルダ由来）を
   * 後の arm-edge で適用してユーザー位置を強制移動させないため。 */
  function _takePending() {
    var p = _pendingMsg; _pendingMsg = null;
    if (!p) return null;
    return (Date.now() - p.at <= PENDING_TTL_MS) ? p : null;
  }

  /* ── arm / disarm ── */

  function _onCtlState(st) {
    var armed = _live(st) && _paired(st) && st.phase === 'selecting';
    if (armed && !_armed) {
      _armed = true;
      _prevPresenceSig = _presenceSig(st);
      // arm 前に届いた frame（relay が STATE 反映を追い越す競合）をここで
      // 適用する — 捨てると leader の arm-edge 一発を逃して follower が
      // 永久に開かない。TTL 切れの stash は捨てる。
      var pm = _takePending();
      if (pm) _applyRemote(pm.msg, pm.at);
      // leader は開いている側 — すぐ共有を始める。follower は remote を待つ
      // （まだ stale 位置を送ると leader を巻き込む）。
      if (st.role === 'leader') {
        _unlocked = true;
        _lastSentSig = null;
        _requestSend();
      }
      return;
    }
    if (!armed && _armed) {
      _armed = false;
      _unlocked = false;
      _lastSentSig = null;
      _appliedHold = null;
      _pendingMsg = null;
      _userClosed = false;
      _suppressUntil = 0;
      _lastRemoteMsg = null;
      _resetFollow();
      _releaseInput();
      _lastLocalMove = -1e9;
      _lastLocalIntent = -1e9;
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
      if (_followCtx && (_followCtx.sid !== st.sessionId ||
          _followCtx.role !== st.role || st.status === 'reconnecting')) {
        _resetFollow();
      }
      // presence 変化（peer の復帰等）は両方向で現在値を再送する —
      // 内容不変でも送る必要があるので dedup を解除してから要求する。
      var sig = _presenceSig(st);
      if (sig !== _prevPresenceSig) {
        _prevPresenceSig = sig;
        _lastSentSig = null;
        _requestSend();
      }
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
      scrollH: _canRatio() ? sc.scrollHeight : undefined,
      mode: _w().viewMode || 'tile',
    };
  }

  function _sigOf(p) {
    return (p.path || '') + '|' + Math.round(p.top) + '|' +
      Math.round(p.viewH) + '|' + Math.round(p.scrollH || 0) + '|' + p.mode;
  }

  /* remote 適用直後の位置に留まっている間は送信しない。clamp で目標に届か
   * なかった値を peer に返すと、逆変換で peer を戻してしまうため。viewH
   * は比較しない — resize（iOS ツールバー伸縮等）で clamp 値が漏れない
   * ようにする。 */
  function _holdMatch(p) {
    var h = _appliedHold;
    return !!h && (p.path || null) === h.path &&
      Math.abs(p.top - h.top) <= 1 && (p.mode || 'tile') === h.mode;
  }

  /* 直近のローカル scroll event 列から指数減衰をフィットし、フリング中は
   * 予測停止点を返す。減速が一貫しない区間（ドラッグ中・加速・段差）は
   * null — その場合 pred は送らず受信側は実測追従のみ行う。指が触れて
   * いる間（ドラッグ）と、最後の scroll event から間が空いた送信（trailing
   * 等 = 既に停止）も null — 古い速度から外挿すると受信側が行き過ぎる。 */
  function _flingPred() {
    if (_touchActive || _pointerActive) return null;
    var h = _sndHist, t = _perfNow();
    while (h.length && t - h[0].t > FLING_HIST_MS) h.shift();
    var n = h.length;
    if (n < 3) return null;
    var p0 = h[n - 3], p1 = h[n - 2], p2 = h[n - 1];
    if (t - p2.t > 100) return null;
    if (p0.path !== p2.path || p1.path !== p2.path ||
        p0.mode !== p2.mode || p1.mode !== p2.mode) return null;
    var dt1 = p1.t - p0.t, dt2 = p2.t - p1.t;
    if (dt1 < 8 || dt2 < 8 || dt1 > 100 || dt2 > 100) return null;
    var v1 = (p1.top - p0.top) / dt1, v2 = (p2.top - p1.top) / dt2;
    if (v1 * v2 <= 0) return null;
    var r = v2 / v1;
    if (r <= 0 || r > 1) return null;
    var tau = FLING_TAU_DEF_MS;
    if (r < 0.95 && r > 0.3) {
      tau = -((dt1 + dt2) / 2) / Math.log(r);
      if (tau < FLING_TAU_MIN_MS) tau = FLING_TAU_MIN_MS;
      else if (tau > FLING_TAU_MAX_MS) tau = FLING_TAU_MAX_MS;
    }
    var sc = _scroller();
    var pred = p2.top + v2 * tau;
    return Math.round(sc ? _clampTop(sc, pred) : pred);
  }

  function _sendNow() {
    if (!_sendGate()) return;
    var now = Date.now();
    if (_suppressUntil > now) {
      // 抑止窓内の send は「捨てる」ではなく窓の終わりへ defer する —
      // remote 適用直後のローカル操作（nav/scroll）が永久に失われるのを防ぐ。
      // 内容が適用値のままなら _appliedHold が窓明けで止めるので echo もない。
      _deferSend(_suppressUntil - now + 20);
      return;
    }
    var p = _payload();
    var sig = _sigOf(p);
    if (_holdMatch(p)) return;
    _appliedHold = null;              // 適用位置から実際に動いた — hold 解除
    if (sig === _lastSentSig) return;
    _lastSendTs = now;
    var pred = _flingPred();
    if (pred !== null) p.pred = pred;
    var ok = false;
    try { ok = _pc().sendUiSync(p) !== false; }
    catch (e) { console.warn(e); }
    // 未送信（リンク不通等）で _lastSentSig は進めない — 次のイベントで
    // 同じ内容を再送できるようにする。
    if (ok) _lastSentSig = sig;
  }

  function _deferSend(ms) {
    if (_trailT) clearTimeout(_trailT);
    _trailT = setTimeout(function () {
      _trailT = null;
      _requestSend();
    }, ms);
  }

  /* scroll/observer 起点: 連続スクロールは SEND_MIN_MS 間隔 + 最後に必ず
   * trailing 送信。抑止窓中は窓明けへ defer（drop しない）。 */
  function _requestSend() {
    if (!_sendGate()) return;
    if (_holdMatch(_payload())) return;
    var now = Date.now();
    if (_suppressUntil <= now && now - _lastSendTs >= SEND_MIN_MS) {
      _sendNow();
      return;
    }
    _deferSend(Math.max(SEND_TRAIL_MS, _suppressUntil - now + 20));
  }

  /* ── 受信適用 ── */

  function _perfNow() {
    try { return performance.now(); }
    catch (e) { return Date.now(); }
  }
  function _tracePush(o) {
    if (_trace) {
      _trace.push(o);
      if (_trace.length > 240) _trace.shift();
    }
  }
  function _maxTop(sc) {
    return Math.max(0, sc.scrollHeight - sc.clientHeight);
  }
  function _clampTop(sc, t) {
    return Math.min(Math.max(0, t), _maxTop(sc));
  }
  /* peer の scrollTop をローカル座標に写す。自分も scrollH を送れる
   * デプロイ（_canRatio）かつ frame が scrollH を持つ場合はスクロール
   * 進捗 top/(scrollH-viewH) を自側の maxTop へ写す（双方向同じ式 —
   * 逆変換にすれば元の位置へ戻る自己逆写像）。それ以外は従来の
   * 1画面分 px オフセットにフォールバック。 */
  function _remoteTop(sc, top, viewH, scrollH) {
    var t;
    if (_canRatio() &&
        typeof scrollH === 'number' && isFinite(scrollH) && scrollH > 0) {
      var peerMax = Math.max(0, scrollH - viewH);
      var prog = peerMax > 0 ? Math.min(1, top / peerMax) : 0;
      t = prog * _maxTop(sc);
    } else {
      t = (_role() === 'follower') ? top + viewH : top - sc.clientHeight;
    }
    return _clampTop(sc, t);
  }
  function _reducedMotion() {
    try {
      return !!(window.matchMedia &&
        window.matchMedia('(prefers-reduced-motion: reduce)').matches);
    } catch (e) { return false; }
  }

  function _localBusy() {
    if (_touchActive || _pointerActive) return true;
    var n = Date.now();
    return n - _lastLocalMove < LOCAL_SETTLE_MS ||
      n - _lastLocalIntent < LOCAL_SETTLE_MS;
  }
  function _releaseInput() {
    _touchActive = false;
    _pointerActive = false;
  }
  function _beginLocalInput() {
    if (!_armed || !_sidebarOpen()) return;
    _lastLocalIntent = Date.now();
    _cancelFollowKeepCtx();
  }

  function _cancelFollowKeepCtx() {
    if (_rafId) { cancelAnimationFrame(_rafId); _rafId = 0; }
    _follow = null;
  }
  function _resetFollow() {
    _cancelFollowKeepCtx();
    _followCtx = null;
  }

  function _ctxOf(sc, msg) {
    var st = _pc() && _pc().state;
    return {path: _w().currentBrowsePath || null,
            mode: _w().viewMode || 'tile',
            viewH: msg && msg.viewH || 0,
            scrollH: msg && msg.scrollH || 0,
            sid: st ? st.sessionId : null,
            role: _role(),
            sc: sc};
  }
  function _sameFollowCtx(ctx) {
    var c = _followCtx;
    return !!c && c.path === ctx.path && c.mode === ctx.mode &&
      c.viewH === ctx.viewH && c.scrollH === ctx.scrollH &&
      c.sid === ctx.sid && c.role === ctx.role && c.sc === ctx.sc;
  }

  /* ── 追従ドライブ ──
   * tile受信時の scrollTop を段階書き換えせず、受信ストリームから作る
   * 移動目標へ2段一次遅れフィルタで毎フレーム追従する。層は:
   *   計測 _followEstimate  → 受理 _followSample（stream更新）
   *   目標 _targetAt        → 実測 or 送信側終端予測 pred（単調hold付）
   *   追従 _integrate       → 厳密積分 F/X — 位置・速度とも連続で、
   *                           静的目標にオーバーシュートしない
   *   書き込み _writeFollow → DOM + 出所記録（echo防止・端re-anchor）
   * 駆動は _followTick（rAF）、継続条件は _followEligible。 */

  function _newFollow(sc, top, t) {
    return {sc: sc, hist: [],
      stream: {t0: t, top: top, vel: 0, dir: 0, pred: null},
      s1: top, pos: top, lastTick: t, tMax: -1e18, tMin: 1e18,
      active: false};
  }
  function _seedFollow(sc, ctx) {
    _follow = _newFollow(sc, sc.scrollTop, _perfNow());
    _followCtx = ctx;
  }
  function _applyImmediate(sc, target, ctx) {
    _resetFollow();
    sc.scrollTop = target;
    var actual = sc.scrollTop;
    _suppressUntil = Date.now() + SUPPRESS_MS;
    // 適用後の local state を記録 — この位置（clamp 後の値を含む）に居る間の
    // 送信は抑止し、peer を逆変換で戻さない。
    _appliedHold = {path: _w().currentBrowsePath || null, top: actual,
                    mode: _w().viewMode || 'tile'};
    var t = _perfNow();
    _followCtx = ctx;
    _follow = _newFollow(sc, actual, t);
    _follow.hist.push({t: t, top: actual});
  }

  /* 計測 — 直近3サンプルから速度・進行方向を推定する。
   * ジッターの混じる到着間隔に対し、減速が一貫する場合のみ前回速度を
   * 1/4 混ぜて平滑化する。EST_MIN_MS 未満に詰まった到着は直前サンプルへ
   * 合流させる — 履歴を捨てると方向を失い、pred と単調holdが外れて目標が
   * 実測へ逆走する。 */
  function _followEstimate(f, top) {
    var t = _perfNow();
    var h = f.hist, last = h.length ? h[h.length - 1] : null;
    var v = 0, stop = false;
    if (last && t - last.t < EST_MIN_MS) {
      last.top = top;
    } else if (last && t - last.t > EST_MAX_MS) {
      h.length = 0;
      h.push({t: t, top: top});
    } else {
      if (last && Math.abs(top - last.top) <= POSITION_EPS_PX) stop = true;
      h.push({t: t, top: top});
      if (h.length > 3) h.shift();
    }
    var n = h.length;
    if (!stop && n >= 2) {
      var p2 = h[n - 1], p1 = h[n - 2];
      var dt2 = p2.t - p1.t;
      if (dt2 > 0) {
        var v2 = (p2.top - p1.top) / dt2;
        v = v2;
        if (n >= 3) {
          var p0 = h[n - 3];
          var dt1 = p1.t - p0.t, v1 = (p1.top - p0.top) / dt1;
          var r = dt1 > 0 ? dt2 / dt1 : 0;
          if (dt1 > 0 && v1 * v2 > 0 &&
              Math.abs(v2) >= 0.5 * Math.abs(v1) &&
              r >= 0.5 && r <= 2) {
            v = 0.75 * v2 + 0.25 * v1;
          }
        }
      }
    }
    if (stop) v = 0;
    return {t: t, v: v};
  }

  function _validPred(f, top, dir, pred) {
    if (typeof pred !== 'number' || dir === 0) return null;
    if ((pred - top) * dir <= 0) return null;
    if (Math.abs(pred - top) > 6 * f.sc.clientHeight) return null;
    return pred;
  }

  /* 受理 — 新しい実測でストリーム状態を更新する。フィルタ状態（s1/pos）
   * は触れない — 表示は連続したまま新しい目標へ向き直る。 */
  function _followSample(top, pred) {
    var f = _follow;
    var est = _followEstimate(f, top);
    var t = est.t, v = est.v;
    var dir = v > 0 ? 1 : (v < 0 ? -1 : 0);
    if (dir !== f.stream.dir) { f.tMax = -1e18; f.tMin = 1e18; }
    var P = _validPred(f, top, dir, pred);
    f.stream = {t0: t, top: top, vel: v, dir: dir, pred: P};
    // 起動時だけ時計を合わせる — rAF 稼働中に上書きすると前フレームから
    // 受信までの経過が積分から落ち、rAF の時刻が受信より前なら dt=0 で
    // そのフレームが止まる（受信周期のカクつき）。
    if (!f.active) f.lastTick = t;
    _tracePush({t: t, k: 'r', recv: top, v: v, pred: P});
    f.active = true;
    _ensureRaf();
  }

  /* 目標決定 — ストリームが新鮮な間は pred（あれば）を採用し、
   * 進行方向への単調holdで目標の逆走を抑える。stale後は最後の
   * 実測位置へ戻して収束させる。 */
  function _targetAt(f, t) {
    var m = f.stream;
    var T = m.top;
    if (t - m.t0 < STALE_MS) {
      if (m.pred !== null && m.pred !== undefined) T = m.pred;
      if (m.dir > 0) { if (T > f.tMax) f.tMax = T; T = f.tMax; }
      else if (m.dir < 0) { if (T < f.tMin) f.tMin = T; T = f.tMin; }
    }
    return T;
  }

  /* 追従 — 同一τの2段一次遅れを閉形式で積分する。
   * X(t) = T - (B + A·s/τ)·e^{-s/τ}（A=T-F0, B=T-X0）。
   * s1 は X より先に動く第1段 — 表示位置 pos が目標 T を超えて進むことは
   * なく、速度 (s1-pos)/τ も常に連続。 */
  function _integrate(f, T, dt) {
    var e = Math.exp(-dt / TARGET_TAU_MS);
    var A = T - f.s1, B = T - f.pos;
    f.s1 = T - A * e;
    f.pos = T - (B + A * dt / TARGET_TAU_MS) * e;
  }

  function _writeFollow(pos) {
    var f = _follow, sc = f.sc;
    sc.scrollTop = pos;
    var actual = sc.scrollTop;
    _appliedHold = {path: _w().currentBrowsePath || null, top: actual,
                    mode: _w().viewMode || 'tile'};
    if (Math.abs(actual - pos) > 1) {
      f.pos = actual;
      f.s1 = actual;
      if ((pos - actual) * (f.stream.top - actual) > 0) {
        f.stream.top = actual;
      }
    }
    return actual;
  }

  /* 継続条件 — 追従の文脈（session/role/path/mode/scroller/表示状態）が
   * 生成時と一致している間だけ描画する。 */
  function _followEligible(f) {
    var c = _followCtx;
    var st = _pc() && _pc().state;
    return !!(f.sc && c && f.sc === _scroller() && _armed && st &&
      st.phase === 'selecting' && st.sessionId === c.sid &&
      _role() === c.role && !_searchMode() &&
      !document.hidden && _sidebarOpen() && !_reducedMotion() &&
      (_w().viewMode || 'tile') === c.mode &&
      (_w().currentBrowsePath || null) === c.path);
  }

  function _ensureRaf() {
    if (_rafId || !_follow || !_follow.active) return;
    _rafId = requestAnimationFrame(_followTick);
  }
  function _followTick(t) {
    _rafId = 0;
    var f = _follow;
    if (!f || !f.active || !_followEligible(f)) { _resetFollow(); return; }
    var ts = (typeof t === 'number') ? t : _perfNow();
    var dt = ts - f.lastTick;
    f.lastTick = ts;
    if (dt < 0) dt = 0;
    if (dt > 500) dt = 500;
    var T = _targetAt(f, ts);
    _integrate(f, T, dt);
    var x = f.pos;
    var w = _writeFollow(x);
    if (_trace) _tracePush({t: ts, k: 'w', x: x, w: w, T: T});
    var s = f.stream;
    if ((s.vel === 0 || ts - s.t0 >= STALE_MS) &&
        Math.abs(f.pos - s.top) < POSITION_EPS_PX &&
        Math.abs(f.s1 - f.pos) < POSITION_EPS_PX) {
      f.pos = s.top;
      f.s1 = s.top;
      _writeFollow(s.top);
      f.active = false;
      return;
    }
    _ensureRaf();
  }

  function _onScrollEvent() {
    var sc = _scroller();
    var p = sc && {
      path: _w().currentBrowsePath || null,
      top: sc.scrollTop,
      mode: _w().viewMode || 'tile',
    };
    // 自分の remote 由来の書き込み（_applyImmediate/_writeFollow が
    // _appliedHold に記録）が起こした event は送らない。
    if (p && _holdMatch(p)) return;
    _tracePush({t: _perfNow(), k: 's', top: p ? p.top : 0});
    if (p) {
      _sndHist.push({t: _perfNow(), top: p.top,
                     path: p.path, mode: p.mode});
      if (_sndHist.length > 4) _sndHist.shift();
    }
    if (_appliedHold) _appliedHold = null;
    if (_armed) {
      _suppressUntil = 0;
      _lastLocalMove = Date.now();
      _cancelFollowKeepCtx();
    }
    _requestSend();
  }

  function _ensureSidebarOpen() {
    if (_userClosed || _role() === 'leader' || _sidebarOpen()) return;
    try {
      // 正規の open 経路（app.js toggleSidebar と同じ visual open）を共有 —
      // 手書き class 付与だと tile-full 等のレイアウトが欠けて scrollHeight
      // が peer と食い違う。
      if (_w().openSidebar) { _w().openSidebar(); _mirrorOpened = true; return; }
    } catch (e) { console.warn(e); }
    var s = _sidebar();
    var v = document.getElementById('viewer');
    if (!s) return;
    s.classList.add('open');
    if (v) v.classList.add('sidebar-open');
    try { if (_w().syncSidebarA11y) _w().syncSidebarA11y(); }
    catch (e2) { console.warn(e2); }
    _mirrorOpened = true;
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

  function _applyRemote(msg, at) {
    if (!msg || msg.view !== 'browse') return;
    // 通常モードで非表示 root 配下への remote path は拒否 — pending に
    // stash しない（禁止対象を再適用可能にしない）し currentBrowsePath にも
    // 代入しない。同期を解除してトップへ戻す。表示設定やモードは
    // UI_SYNC へ混ぜない（§8: モードは端末ローカル）。
    var FV = _w().FolderVisibility;
    if (FV && FV.readyState === 'error' && !FV.hasSnapshot){
      // 判定不能（error・snapshot 無し）— 反映も解除もしない。未適用と
      // して stash し、次 frame・grid 変化・復旧後の再判定に委ねる。
      _stashPending(msg, at);
      return;
    }
    if (FV && FV.readyState !== 'loading' && !FV.showAll &&
        msg.path && typeof FV.isHidden === 'function' &&
        FV.isHidden(msg.path) &&
        typeof _w().redirectHiddenToTop === 'function') {
      _w().redirectHiddenToTop();
      return;
    }
    if (!_armed) {
      // relay が STATE(phase=selecting) の反映を追い越す競合では未 arm —
      // 捨てずに短期保持し、_onCtlState の arm-edge で適用する。
      _stashPending(msg, at);
      return;
    }
    if (_searchMode() || !_pathResolvable(msg.path)) {
      // 最新のみ保持 — searchMode 解除後 / tree 到着後に observer・次 msg で再適用。
      _resetFollow();
      _stashPending(msg, at);
      return;
    }
    _pendingMsg = null;

    var sc = _scroller();
    if (!sc) return;
    _ensureSidebarOpen();

    var myTop = sc.scrollTop;
    var targetPath = (msg.path === undefined) ? null : msg.path;
    var navNeeded = (targetPath || null) !== (_w().currentBrowsePath || null);
    var modeNeeded = !!msg.mode && _w().viewMode !== msg.mode;

    if (navNeeded || modeNeeded) {
      try {
        // 離れる階層の位置保存は navigateToFolder と同じく — disarm 後の
        // ローカル復帰が正しい位置に戻れるようにする。
        var gk = _w().getScrollKey, sps = _w().scrollPositions;
        if (gk && sps) sps[gk(_w().currentBrowsePath)] = myTop;
        if (navNeeded) _w().currentBrowsePath = targetPath || null;
        if (modeNeeded) _w().viewMode = msg.mode;
        if (_w().updateBrowseHeader) _w().updateBrowseHeader();
        // scrollAnchor:null は「位置を決める」render（上端）— 直後に remote
        // 値で上書きする。saved/decide 経路は経由しない（決定的）。
        if (_w().renderView) _w().renderView({scrollAnchor: null});
        // URL hash は実階層に追従させる — replaceState 限定。push すると
        // remote 操作がこちらの履歴に積まれ、Back/history.go 由来の
        // hashchange→applyUrlState が二重に走る。browse path/mode は
        // ローカル状態（ペア情報を URL に書かない §3.6 とは別）。
        if (_w().writeBrowseStateReplace) _w().writeBrowseStateReplace();
      } catch (e) { console.warn(e); }
      sc = _scroller();          // render で参照が変わっていても再解決
      if (!sc) return;
    }

    var target = _remoteTop(sc, msg.top, msg.viewH, msg.scrollH);
    var predT = null;
    if (typeof msg.pred === 'number' && isFinite(msg.pred)) {
      predT = _remoteTop(sc, msg.pred, msg.viewH, msg.scrollH);
    }

    var ctx = _ctxOf(sc, msg);
    // resize 時の再写像用に最後の frame を保持（適用コンテキストの
    // path/mode と一緒に）。busy で skip した frame も peer の真値なので
    // 上書きしてよい。
    _lastRemoteMsg = {path: ctx.path, mode: ctx.mode,
                      top: msg.top, viewH: msg.viewH, scrollH: msg.scrollH};
    // 未解禁 follower の初回 frame はローカル操作中でも適用する — 捨てると
    // _unlocked が立たず、leader が動くまで自分の操作も共有されない。
    var firstApply = _role() === 'follower' && !_unlocked;
    if (navNeeded || modeNeeded) {
      _applyImmediate(sc, target, ctx);
    } else if (!firstApply && ctx.mode === 'tile' && _sidebarOpen() &&
               !document.hidden && _localBusy()) {
      return;
    } else if (ctx.mode !== 'tile' || _reducedMotion() ||
               !_sidebarOpen() || document.hidden) {
      _applyImmediate(sc, target, ctx);
    } else if (!_sameFollowCtx(ctx)) {
      _applyImmediate(sc, target, ctx);
    } else {
      if (!_follow) _seedFollow(sc, ctx);
      _suppressUntil = Date.now() + SUPPRESS_MS;
      _followSample(target, predT);
    }
    if (_role() === 'follower' && !_unlocked) _unlocked = true;
  }

  /* tile-grid の childList 変化 = renderView 系の全再描画（潜り/戻る/
   * mode/件数更新）。ローカル nav は live 値が送られるのでここで拾えば
   * 新規リスナを増やさずに済む。pending のリトライも兼ねる。 */
  function _onGridMutated() {
    _resetFollow();
    _releaseInput();
    var pm = _takePending();
    if (pm) _applyRemote(pm.msg, pm.at);
    _requestSend();
  }

  /* ローカル resize で自側の maxTop が変わると、同じ px でも進捗が
   * ずれる（ツールバー伸縮・回転・分割表示）。適用位置が remote 由来の
   * まま動いていなければ（_appliedHold が生きている = ユーザーがまだ
   * 触っていない）、最後の frame を自側の新しい幾何へ写し直す。
   * ユーザーがスクロール済みなら hold が解けているので何もしない —
   * その位置はローカルの真値として送信される。 */
  function _reapplyLastRemote() {
    var sc = _scroller(), m = _lastRemoteMsg;
    if (!_armed || !sc || !m) return;
    if (_searchMode() || _localBusy() || !_sidebarOpen()) return;
    if ((m.path || null) !== (_w().currentBrowsePath || null) ||
        (m.mode || 'tile') !== (_w().viewMode || 'tile')) return;
    if (!_holdMatch(_payload())) return;
    var t = _remoteTop(sc, m.top, m.viewH, m.scrollH);
    if (Math.abs(t - sc.scrollTop) <= 1) return;
    sc.scrollTop = t;
    _suppressUntil = Date.now() + SUPPRESS_MS;
    _appliedHold = {path: _w().currentBrowsePath || null,
                    top: sc.scrollTop,
                    mode: _w().viewMode || 'tile'};
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
        _onScrollEvent(); // 抑止/defer の判定は _requestSend 側で一元化
      }, {passive: true});
      ss.addEventListener('pointerdown', function (e) {
        if (!_armed || (e && e.isPrimary === false)) return;
        if (!_pointerActive) {
          _pointerActive = true;
          _beginLocalInput();
        }
      }, {passive: true});
      ss.addEventListener('touchstart', function () {
        if (!_armed) return;
        if (!_touchActive) {
          _touchActive = true;
          _beginLocalInput();
        }
      }, {passive: true});
      var touchEnd = function (e) {
        if (!_touchActive) return;
        _touchActive = !!(e && e.touches && e.touches.length > 0);
        if (!_touchActive) _lastLocalIntent = Date.now();
      };
      ss.addEventListener('touchend', touchEnd, {passive: true});
      ss.addEventListener('touchcancel', touchEnd, {passive: true});
      ss.addEventListener('wheel', function () {
        _beginLocalInput();
      }, {passive: true});
    }
    var pointerEnd = function () {
      if (_pointerActive) {
        _pointerActive = false;
        _lastLocalIntent = Date.now();
      }
    };
    document.addEventListener('pointerup', pointerEnd,
                            {passive: true, capture: true});
    document.addEventListener('pointercancel', pointerEnd,
                            {passive: true, capture: true});
    var scrollKeys = {ArrowUp: 1, ArrowDown: 1, PageUp: 1, PageDown: 1,
                      Home: 1, End: 1, ' ': 1};
    document.addEventListener('keydown', function (e) {
      if (!_armed || !_sidebarOpen()) return;
      var t = e && e.target;
      var sb = _sidebar();
      if (!sb || !t || !sb.contains(t)) return;
      if (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' ||
          t.tagName === 'SELECT' || t.isContentEditable) return;
      // フォーカスされた button 上の Space は押下（.offline-btn 等）でスクロールしない
      if (e.key === ' ' && t.tagName === 'BUTTON') return;
      if (scrollKeys[e.key]) _beginLocalInput();
    }, {passive: true});
    window.addEventListener('blur', function () {
      _releaseInput();
    }, {passive: true});
    window.addEventListener('pagehide', function () {
      _releaseInput();
      _resetFollow();
    });
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) {
        _releaseInput();
        _resetFollow();
      }
    });
    if (typeof MutationObserver === 'function') {
      var tg = document.getElementById('tile-grid');
      if (tg) {
        try {
          new MutationObserver(_onGridMutated).observe(tg, {childList: true});
        } catch (e) { console.warn(e); }
      }
      var sb = _sidebar();
      if (sb) {
        // armed 中にミラー sidebar が閉じられた = ユーザーが自分で閉じた
        // （このモジュールが閉じるのは disarm のみで、その時点では
        // _armed=false）。次の受信 frame で開き直すとちらつくので、その
        // arm 期間中は開き直さない。
        var wasOpen = sb.classList.contains('open');
        try {
          new MutationObserver(function () {
            var open = sb.classList.contains('open');
            if (open === wasOpen) return;
            wasOpen = open;
            if (!_armed) return;
            _userClosed = !open;
            if (!open) {
              _resetFollow();
              _releaseInput();
            }
          }).observe(sb, {attributes: true, attributeFilter: ['class']});
        } catch (e) { console.warn(e); }
      }
    }
    window.addEventListener('resize', function () {
      _resetFollow();
      _reapplyLastRemote();
      _requestSend();
    }, {passive: true});
    var ctlOk = typeof pc.subscribe === 'function' &&
      typeof pc.onEvent === 'function';
    if (ctlOk) {
      _subs.push(pc.subscribe(_onCtlState));
      _subs.push(pc.onEvent(_onCtlEvent));
      // 健全マーカー: index.html の stale-shell watchdog が「このデプロイの
      // 機能チェーンが揃っているか」を見る。旧 pairController には onEvent が
      // 無いので、その混在では flag は立たず watchdog が修復に入る。
      // _onCtlState より先に立てる — 中の例外でマーカー未設定誤診を防ぐ。
      try { window.BOOKQ_BROWSE_SYNC = true; } catch (e) {}
    }
    // subscribe は即時通知しない — 起動時点の状態で arm 判定する
    // （ペア中のリロードが selecting の途中で戻るケース）。
    _onCtlState(pc.state);
  }

  if (window.__BOOKQ_TEST_MODE__ === 'true') {
    _trace = [];
    window.__pairBrowseSync = {
      get armed() { return _armed; },
      get unlocked() { return _unlocked; },
      get lastSentSig() { return _lastSentSig; },
      get appliedHold() { return _appliedHold; },
      get userClosed() { return _userClosed; },
      get suppressUntil() { return _suppressUntil; },
      get pending() { return _pendingMsg; },
      get smoothing() {
        var f = _follow;
        var t = _perfNow();
        var s = f && f.stream;
        return {
          active: !!(f && f.active),
          displayTop: f ? f.pos : 0,
          displayVel: f ? (f.s1 - f.pos) / TARGET_TAU_MS : 0,
          modelTop: f ? _targetAt(f, t) : 0,
          modelVel: s ? s.vel : 0,
          history: f ? f.hist.length : 0,
          local: _localBusy(),
        };
      },
      get trace() { return _trace ? _trace.slice() : []; },
      get fps() {
        var w = (_trace || []).filter(function (e) { return e.k === 'w'; });
        if (w.length < 2) return null;
        var min = 1e9, max = 0, sum = 0, n = w.length - 1;
        for (var i = 1; i < w.length; i++) {
          var d = w[i].t - w[i - 1].t;
          sum += d;
          if (d < min) min = d;
          if (d > max) max = d;
        }
        var avg = sum / n;
        return {writes: w.length, avgMs: avg, minMs: min, maxMs: max,
                avgFps: 1000 / avg, worstFps: 1000 / max};
      },
      flush: function () {
        if (_trailT) { clearTimeout(_trailT); _trailT = null; }
        _suppressUntil = 0;
        var pm = _takePending();
        if (pm) _applyRemote(pm.msg, pm.at);
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

  // #142 — reader-sync verification hook (test envs only): with
  // ?pair_report=<cid> in the URL, POST the raw PairController/PairedView
  // state to the same-origin /api/v1/pair-report endpoint on every change.
  // The runner reads it back over HTTP — the only structured-state channel
  // available on iOS Simulator Safari / PWA webclips, which have no
  // JS-eval automation path. No-op in production (param absent, test flag
  // off, or the endpoint 404s).
  (function () {
    try {
      if (window.__BOOKQ_TEST_MODE__ !== 'true') return;
      var cid = new URLSearchParams(location.search).get('pair_report');
      if (!cid) return;
      var last = '';
      setInterval(function () {
        var pc = null, pv = null;
        try {
          pc = window.PairController ? window.PairController.state : null;
          pv = window.PairedView ? window.PairedView.state() : null;
        } catch (e) { return; }
        var body;
        try {
          body = JSON.stringify({id: cid, pc: pc, pv: pv,
                                 solo: !(pv && pv.active)});
        } catch (e) { return; }
        if (body === last) return;
        last = body;
        fetch('/api/v1/pair-report', {
          method: 'POST', cache: 'no-store', keepalive: true,
          headers: {'Content-Type': 'application/json'},
          body: body,
        }).catch(function () { last = ''; });
      }, 400);
    } catch (e) {}
  })();
})();
