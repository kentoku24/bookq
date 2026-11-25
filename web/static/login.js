(function () {
  // Guest deep links ride the member `/#/join/<code>` fragment as `?gt=`
  // so native apps still parse the code; browsers take the grant path.
  var token = globalThis.BookqGuest ? BookqGuest.parseHash(location.hash) : null;
  // Guest deep links are consumed by the redeem flow below — saving them
  // into bookq_return_hash would re-trigger a join after a real login.
  if (location.hash && !token) {
    sessionStorage.setItem("bookq_return_hash", JSON.stringify({hash: location.hash, ts: Date.now()}));
  }
  if (!token) return;
  function fail(msg) {
    var err = document.getElementById('login-err');
    if (err) {
      err.textContent = msg;
      err.hidden = false;
    }
  }
  BookqGuest.redeem(token).then(function (res) {
    if (res.ok) {
      // Cookie is set — reload into the viewer shell. The #/join/…?gt=
      // fragment survives reload(), so hash.js can bind the follower
      // pair on the next document. (location.replace to the same URL
      // would be a no-op and strand us on the login page.)
      location.reload();
    } else {
      fail('ゲストリンクが無効か期限切れです');
    }
  }).catch(function () {
    // Network failure — the invite itself may be fine; the hash survives
    // so a reload retries the redeem.
    fail('ネットワークエラーです。もう一度お試しください');
  });
})();
