/* dual-reader shared-spread projection — pure, transport-free (#76 / S01).
 * Contract: parent issue #75 §3.2. The browser NEVER recomputes the pairing;
 * it only projects the server-provided spread table onto its own side. */
(function () {
  'use strict';

  function isPageRef(x) {
    return !!x && typeof x === 'object' &&
      typeof x.folder === 'string' && typeof x.name === 'string';
  }

  function _err(code, msg) {
    const e = new Error(code + ': ' + msg);
    e.code = code;
    return e;
  }

  function _checkSpreads(spreads) {
    if (!Array.isArray(spreads)) throw _err('ERR_MALFORMED', 'spreads must be an array');
  }

  function clampIndex(spreads, index) {
    _checkSpreads(spreads);
    if (!Number.isInteger(index)) throw _err('ERR_MALFORMED', 'index must be int');
    if (spreads.length === 0) return -1;
    return Math.max(0, Math.min(index, spreads.length - 1));
  }

  function spreadIndexById(spreads, id) {
    _checkSpreads(spreads);
    if (typeof id !== 'string') throw _err('ERR_MALFORMED', 'id must be str');
    for (let i = 0; i < spreads.length; i++) {
      if (spreads[i] && spreads[i].id === id) return i;
    }
    return -1;
  }

  function _sameRef(a, b) {
    return isPageRef(a) && isPageRef(b) && a.folder === b.folder && a.name === b.name;
  }

  function spreadIndexForPage(spreads, ref) {
    _checkSpreads(spreads);
    if (!isPageRef(ref)) throw _err('ERR_MALFORMED', 'ref must be a PageRef');
    for (let i = 0; i < spreads.length; i++) {
      const s = spreads[i];
      if (!s) continue;
      if (_sameRef(s.right, ref) || _sameRef(s.left, ref)) return i;
    }
    return -1;
  }

  function turnIndex(spreads, index, delta, crossChapter) {
    _checkSpreads(spreads);
    if (spreads.length === 0) return -1;
    if (!Number.isInteger(index) || index < 0 || index >= spreads.length) {
      throw _err('ERR_RANGE', 'index out of range: ' + index);
    }
    if (delta !== 1 && delta !== -1) throw _err('ERR_RANGE', 'delta must be ±1');
    const target = index + delta;
    if (crossChapter) return Math.max(0, Math.min(target, spreads.length - 1));
    const chapter = spreads[index].chapterFolder;
    let lo = index, hi = index;
    while (lo > 0 && spreads[lo - 1].chapterFolder === chapter) lo--;
    while (hi + 1 < spreads.length && spreads[hi + 1].chapterFolder === chapter) hi++;
    return Math.max(lo, Math.min(target, hi));
  }

  /* Canonical projection (§3.2): leader renders leaderSide; follower the
   * opposite. Blank side -> {side, ref:null}. */
  function projectSpread(spread, role, leaderSide) {
    if (!spread || typeof spread !== 'object') throw _err('ERR_MALFORMED', 'spread must be an object');
    if (leaderSide !== 'right' && leaderSide !== 'left') {
      throw _err('ERR_RANGE', 'leaderSide must be right|left');
    }
    let side;
    if (role === 'leader') side = leaderSide;
    else if (role === 'follower') side = leaderSide === 'right' ? 'left' : 'right';
    else throw _err('ERR_MALFORMED', 'role must be leader|follower');
    const ref = side === 'right' ? (spread.right || null) : (spread.left || null);
    return { side: side, ref: ref };
  }

  globalThis.PairModel = {
    PROTOCOL_VERSION: 1,
    isPageRef: isPageRef,
    clampIndex: clampIndex,
    spreadIndexById: spreadIndexById,
    spreadIndexForPage: spreadIndexForPage,
    turnIndex: turnIndex,
    projectSpread: projectSpread,
  };
})();
