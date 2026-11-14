/* bookq viewer/pairedView — dual-reader per-device projection state (S06, #81).
 * Plain global script: defines window.PairedView. DOM-free and transport-free:
 * this module only decides WHICH PageRef this device renders for a committed
 * shared spread; app.js owns the strip that draws it.
 *
 *   - Entry: ViewerNavigation.applyCommittedState -> (app.js) -> PairedView.apply()
 *     returns a typed descriptor {kind:'paired'|'leave'|'solo'|'error'}. app.js
 *     performs all DOM work; nothing here ever emits an intent.
 *   - Side resolution is delegated to PairModel.projectSpread — role and
 *     physical side are separate concepts: leader gets snapshot.leaderSide,
 *     follower gets the opposite. SWAP changes leaderSide only; the shared
 *     spreadId stays put.
 *   - LEAVE-equivalent: PairedView.leave(), apply() with {paired:false}, or a
 *     snapshot whose phase is 'ended' all end paired rendering so app.js can
 *     restore the pre-pair solo layout.
 */
(function () {
  'use strict';

  function _err(code, msg){
    var e = new Error(code + ': ' + msg);
    e.code = code;
    return e;
  }

  function _fresh(){
    return {
      active: false,
      role: null,
      side: null,          // 'right' | 'left' — the side THIS device renders
      leaderSide: null,    // 'right' | 'left' — shared-session property (SWAP)
      spreadId: null,
      spreadIndex: -1,
      revision: 0,
      workEpoch: 0,
      sessionId: null,
      manifestVersion: 0,
      manifest: null,      // last applied manifest object (identity compare)
      ref: null,           // this device's assigned PageRef — null = blank side
      anchorRef: null,     // spread anchor PageRef (first real page)
      spreadTotal: 0,      // committed manifest's shared spread-table length
    };
  }

  var st = _fresh();
  var hooks = {};          // {leave: fn} — app.js registers the DOM restore

  function isActive(){ return st.active; }

  function clear(){ st = _fresh(); }

  function setHooks(h){
    if (!h || typeof h !== 'object') throw _err('ERR_MALFORMED', 'hooks must be an object');
    for (var k in h) hooks[k] = h[k];
  }

  // Public LEAVE: clears the projection state and lets app.js restore solo.
  function leave(){
    var was = st.active;
    clear();
    if (was && typeof hooks.leave === 'function'){
      try { hooks.leave(); } catch (e) { console.warn(e); }
    }
    return was;
  }

  function state(){
    return {
      active: st.active,
      role: st.role,
      side: st.side,
      leaderSide: st.leaderSide,
      spreadId: st.spreadId,
      spreadIndex: st.spreadIndex,
      revision: st.revision,
      workEpoch: st.workEpoch,
      sessionId: st.sessionId,
      manifestVersion: st.manifestVersion,
      ref: st.ref,
      anchorRef: st.anchorRef,
      spreadTotal: st.spreadTotal,
      blank: !!(st.active && st.ref === null),
    };
  }

  // The PageRef this device renders for a wire spread {right, left|null}.
  function ownRefFor(spread){
    if (!st.active || !spread) return null;
    return st.side === 'right' ? (spread.right || null) : (spread.left || null);
  }

  function _tableOf(manifest){
    if (!manifest) return null;
    var t = manifest.spreadTable || manifest.spreads;
    return Array.isArray(t) ? t : null;
  }

  // Apply a committed shared state. Pure decision + projection — no DOM.
  //   apply(snapshot, manifest, {role, memberId, paired})
  // kind 'paired': {spread, spreadIndex, side, ref, anchorRef, manifestChanged}
  // kind 'leave':  paired session ended — app.js restores the solo layout
  // kind 'solo':   no role anywhere — app.js runs the normal solo path
  // kind 'error':  malformed commit — gen already bumped, nothing applied
  function apply(snapshot, manifest, options){
    options = options || {};
    var leaving = options.paired === false || (snapshot && snapshot.phase === 'ended');
    if (leaving){
      var wasLeaving = st.active;
      clear();
      return { kind: wasLeaving ? 'leave' : 'solo' };
    }
    var role = options.role;
    if (role == null && options.memberId && snapshot && snapshot.presence){
      var me = snapshot.presence[options.memberId];
      if (me && typeof me === 'object') role = me.role;
    }
    if (role == null) role = st.role;   // a commit keeps the session's role
    if (role !== 'leader' && role !== 'follower') return { kind: 'solo' };
    var leaderSide = snapshot && snapshot.leaderSide;
    if (leaderSide !== 'right' && leaderSide !== 'left'){
      return { kind: 'error', code: 'ERR_MALFORMED' };
    }
    var sid = snapshot && snapshot.spreadId;
    var table = _tableOf(manifest);
    if (!sid || !table) return { kind: 'error', code: 'ERR_MALFORMED' };
    var si = -1;
    for (var i = 0; i < table.length; i++){
      if (table[i] && table[i].id === sid) { si = i; break; }
    }
    if (si < 0) return { kind: 'error', code: 'ERR_RANGE' };
    var spread = table[si];
    var proj;
    try {
      proj = window.PairModel.projectSpread(spread, role, leaderSide);
    } catch (e) {
      return { kind: 'error', code: (e && e.code) || 'ERR_MALFORMED' };
    }
    var anchor = spread.right || spread.left || null;
    if (!anchor) return { kind: 'error', code: 'ERR_RANGE' };
    var manifestChanged = manifest !== st.manifest;
    st.active = true;
    st.role = role;
    st.side = proj.side;
    st.leaderSide = leaderSide;
    st.spreadId = sid;
    st.spreadIndex = si;
    st.revision = snapshot.revision;
    st.workEpoch = snapshot.workEpoch;
    st.sessionId = snapshot.sessionId;
    st.manifestVersion = snapshot.manifestVersion;
    st.manifest = manifest;
    st.ref = proj.ref;
    st.anchorRef = anchor;
    st.spreadTotal = table.length;
    return {
      kind: 'paired',
      spread: spread,
      spreadIndex: si,
      side: proj.side,
      ref: proj.ref,
      anchorRef: anchor,
      manifestChanged: manifestChanged,
    };
  }

  window.PairedView = {
    isActive: isActive,
    apply: apply,
    leave: leave,
    clear: clear,
    state: state,
    ownRefFor: ownRefFor,
    setHooks: setHooks,
  };
})();
