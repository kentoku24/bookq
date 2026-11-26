(function () {
  /* Shared guest-access helpers, loaded in BOTH shells (login.js on the
   * unauthed side, browse/hash.js on the viewer side) plus used by the
   * pair/offline modules for the `__BOOKQ_GUEST__` shell marker.
   * Guest deep links ride the member `/#/join/<code>` fragment as `?gt=`
   * so native apps still parse the code; browsers take the grant path. */
  var GUEST_HASH_RE = /^#\/join\/\d{6}\?gt=([A-Za-z0-9_-]{16,128})$/;

  /* The `__BOOKQ_GUEST__` shell marker — `globalThis` so SW/module tests
   * can read it too. */
  function isGuest() {
    return globalThis.__BOOKQ_GUEST__ === "true";
  }

  /* `#/join/<code>?gt=<token>` → token, or null. */
  function parseHash(hash) {
    var m = (hash || "").match(GUEST_HASH_RE);
    return m ? m[1] : null;
  }

  /* POST the invite token — resolves to the fetch Response (the callers
   * own their ok/toast handling: login.js reloads, hash.js joins). */
  function redeem(token) {
    return fetch(globalThis.BookqApiEndpoints.guestRedeem, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token: token}),
    });
  }

  globalThis.BookqGuest = {
    isGuest: isGuest,
    parseHash: parseHash,
    redeem: redeem,
  };
})();
