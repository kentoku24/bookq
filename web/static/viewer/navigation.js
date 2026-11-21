/* dual-reader navigation port — request intents vs committed display
 * state (#77 / S02). Contract: parent issue #75 §3.6 —
 * 「1回のユーザー移動と1回の表示反映を区別できるようにする。」
 *
 *   - Every user movement request is a typed intent handed to
 *     ViewerNavigation.request(intent) — the single send-side port.
 *   - ViewerNavigation.use(handler) installs THE current request handler
 *     and returns its unregister function; with no handler installed the
 *     default handler (registered by app.js) reproduces the existing solo
 *     behaviour exactly.
 *   - ViewerNavigation.applyCommittedState(snapshot, manifest, options)
 *     is the separate receive-only channel for remote committed state: it
 *     never calls request(), and it bumps the committed-state generation
 *     so stale deferred display finalizers (e.g. the swipe-commit snapTo)
 *     drop themselves instead of overwriting the newer committed anchor.
 *
 * Intents carry PageRef {folder, name} values only — never DOM nodes,
 * never local globalIdx values. `via` tags the input path (tap, key,
 * swipe, edge, slider, overview, spread-shift, tile, search, resume,
 * sidebar); it is local metadata a comm layer drops when mapping intents
 * to the §3.2 wire frames (TURN / GOTO / SET_OFFSET / OPEN_WORK).
 */
(function () {
  'use strict';

  var INTENT_TYPES = {
    turn: true,      // {delta:+1|-1, crossChapter:bool, fromChapter:str|null, via}
    goto: true,      // {page:PageRef, via, animate?:bool}
    setOffset: true, // {offset:0|1, via}
    openWork: true,  // {folder:str, page?:PageRef, via}
    browse: true     // {via}
  };

  function _err(code, msg) {
    var e = new Error(code + ': ' + msg);
    e.code = code;
    return e;
  }

  function isPageRef(x) {
    return !!x && typeof x === 'object' &&
      typeof x.folder === 'string' && typeof x.name === 'string';
  }

  /* Port-boundary validation: catches malformed intents where they are
   * produced, not deep inside a handler. Mirrors pairModel.js's ERR_*
   * convention. */
  function _validate(intent) {
    if (!intent || typeof intent !== 'object' || typeof intent.type !== 'string') {
      throw _err('ERR_MALFORMED', 'intent must be an object with a type');
    }
    if (!INTENT_TYPES[intent.type]) throw _err('ERR_TYPE', 'unknown intent type: ' + intent.type);
    switch (intent.type) {
      case 'turn':
        if (intent.delta !== 1 && intent.delta !== -1) {
          throw _err('ERR_RANGE', 'turn.delta must be ±1');
        }
        if (typeof intent.crossChapter !== 'boolean') {
          throw _err('ERR_MALFORMED', 'turn.crossChapter must be bool');
        }
        if (intent.fromChapter != null && typeof intent.fromChapter !== 'string') {
          throw _err('ERR_MALFORMED', 'turn.fromChapter must be str|null');
        }
        break;
      case 'goto':
        if (!isPageRef(intent.page)) throw _err('ERR_MALFORMED', 'goto.page must be a PageRef');
        break;
      case 'setOffset':
        if (intent.offset !== 0 && intent.offset !== 1) {
          throw _err('ERR_RANGE', 'setOffset.offset must be 0|1');
        }
        break;
      case 'openWork':
        // "" is legal: the root self-tile opens the root level's loose images.
        if (typeof intent.folder !== 'string') {
          throw _err('ERR_MALFORMED', 'openWork.folder must be a str');
        }
        if (intent.page != null && !isPageRef(intent.page)) {
          throw _err('ERR_MALFORMED', 'openWork.page must be a PageRef|null');
        }
        break;
    }
    return intent;
  }

  var _handler = null;       // installed by use(); null → default below
  var _default = null;       // registered by app.js: the existing solo behaviour
  /* Committed-state epoch. Deferred display finalizers capture
   * generation() when they are reserved and drop themselves when it has
   * moved — a remote commit supersedes any local visual work in flight. */
  var _commitGen = 0;
  var _commitApplier = null;

  /* The single send-side port for user movement requests. */
  function request(intent) {
    _validate(intent);
    var h = _handler || _default;
    if (!h) return undefined;  // app.js has not installed its handler yet
    return h(intent);
  }

  /* Install the current request handler; returns an unregister function
   * that restores the handler it displaced (use() stacks, so a newer
   * unregister leaves an older handler intact). */
  function use(handler) {
    if (typeof handler !== 'function') throw _err('ERR_MALFORMED', 'handler must be a function');
    var prev = _handler;
    _handler = handler;
    return function unregister() { if (_handler === handler) _handler = prev; };
  }

  /* app.js registers the solo handler once at boot: what used to run
   * inline at each input site now runs as the default request handler,
   * so an installed comm/pairing handler receives the identical intents
   * a solo session would have applied locally. */
  function setDefaultHandler(handler) {
    if (typeof handler !== 'function') throw _err('ERR_MALFORMED', 'handler must be a function');
    _default = handler;
  }

  /* Receive-only channel for committed remote state. Bumps the
   * generation BEFORE delegating so a finalizer that fires during or
   * after the apply already reads itself as stale. Never calls request(). */
  function applyCommittedState(snapshot, manifest, options) {
    _commitGen++;
    if (_commitApplier) _commitApplier(snapshot, manifest, options);
    return _commitGen;
  }

  /* app.js registers the local committed-state applier (spreadId → local
   * anchor resolution + snapTo). Returns an unregister function. */
  function onCommittedState(fn) {
    if (typeof fn !== 'function') throw _err('ERR_MALFORMED', 'applier must be a function');
    var prev = _commitApplier;
    _commitApplier = fn;
    return function unregister() { if (_commitApplier === fn) _commitApplier = prev; };
  }

  globalThis.ViewerNavigation = {
    INTENT_TYPES: Object.keys(INTENT_TYPES),
    isPageRef: isPageRef,
    request: request,
    use: use,
    setDefaultHandler: setDefaultHandler,
    applyCommittedState: applyCommittedState,
    onCommittedState: onCommittedState,
    generation: function () { return _commitGen; },
  };
})();
