/* bookq browse/matchSort — 現在フォルダ直下の子要素をクエリで relevance ソート。
 * Plain global script: defines window.BrowseMatchSort. Depends on
 * window.currentBrowsePath (app.js composition root) at call-time.
 *
 * 検索入力は「ライブラリ全体の /api/v1/search 結果ビュー」ではなく、今いる
 * フォルダの一覧そのものを一致度順に並べ替える。全要素を残し (フィルタしない)、
 * スコア 0 のものは既存ツリー順のまま末尾に並ぶ。クエリはフォルダ移動
 * (updateBrowseHeader → syncLevel) でクリアされる。
 */
// @ts-check
(function () {
  /** @type {{ level: string | null, text: string }} */
  var _state = { level: null, text: "" };

  function _normLevel(path) { return path || null; }

  function _meta(node) {
    var m = /** @type {any} */ (node).metadata;
    return m && typeof m === "object" ? m : null;
  }

  function _anyContains(list, w) {
    if (!Array.isArray(list)) return false;
    for (var i = 0; i < list.length; i++) {
      if (typeof list[i] === "string" && list[i].toLowerCase().indexOf(w) !== -1) return true;
    }
    return false;
  }

  // フィールド重み: name=10 / title=10 / artist=8 / tag=3 (サーバー
  // rust/viewer-rs/src/search.rs の relevance を踏襲し name を追加)。
  // name のみ一致位置ボーナス: 完全一致 +6 / 前方一致 +3 — 「vol」クエリで
  // "vol1" (前方一致) が "Evolution" (途中一致) を上回るようにする。
  function _scoreNode(node, words) {
    var name = String(/** @type {any} */ (node).name || "").toLowerCase();
    var meta = _meta(node);
    var title = meta
      ? (String(meta.title_jp || "") + " " + String(meta.title_en || "")).toLowerCase()
      : "";
    var score = 0;
    var hitName = false, hitTitle = false, hitArtist = false, hitTag = false;
    for (var i = 0; i < words.length; i++) {
      var w = words[i];
      if (name.indexOf(w) !== -1) {
        hitName = true;
        score += 10;
        if (name === w) score += 6;
        else if (name.indexOf(w) === 0) score += 3;
      }
      if (title.indexOf(w) !== -1) { hitTitle = true; score += 10; }
      if (meta && _anyContains(meta.artist, w)) { hitArtist = true; score += 8; }
      if (meta && _anyContains(meta.tags, w)) { hitTag = true; score += 3; }
    }
    var fields = (hitName ? 1 : 0) + (hitTitle ? 1 : 0) + (hitArtist ? 1 : 0) + (hitTag ? 1 : 0);
    if (fields > 1) score += (fields - 1) * 2;
    return score;
  }

  function setQuery(text) {
    _state.text = String(text == null ? "" : text).trim();
    // クエリが効くのは「入力した時点のフォルダ」だけ。ナビゲーションで
    // currentBrowsePath が変わると syncLevel → clear が走る。
    _state.level = _normLevel(/** @type {any} */ (window).currentBrowsePath);
  }

  function clear() {
    _state.level = null;
    _state.text = "";
    try {
      var si = /** @type {any} */ (document).getElementById("search-input");
      if (si) si.value = "";
    } catch (e) { /* DOM 不在の harness では state だけ戻す */ }
  }

  // フォルダ移動の確定点 (updateBrowseHeader) から呼ぶ。同一レベルの
  // 再描画・ツリー再検証では level が変わらないのでクエリは維持される。
  function syncLevel(path) {
    if (_state.text && _state.level !== _normLevel(path)) clear();
  }

  function isActive() { return !!_state.text; }

  // getBrowseChildrenAt の return 直前から呼ぶ。表示中レベルとクエリの
  // level が一致するときだけソートしたコピーを返す — underlay 等の別
  // レベル描画には触れない。children は呼び出し側の新規配列だが、公開
  // API として slice してから sort し引数を変更しない。
  function apply(children, path) {
    if (!_state.text || _state.level !== _normLevel(path)) return children;
    var words = _state.text.toLowerCase().split(/\s+/).filter(function (w) { return w.length > 0; });
    if (!words.length) return children;
    var list = /** @type {any[]} */ (children).slice();
    var decorated = list.map(function (n) {
      return { n: n, s: _scoreNode(n, words) };
    });
    // Array.prototype.sort は安定 — 同スコアは入力順 (ツリーの自然順) を維持。
    decorated.sort(function (a, b) { return b.s - a.s; });
    return decorated.map(function (d) { return d.n; });
  }

  window.BrowseMatchSort = {
    setQuery: setQuery,
    clear: clear,
    syncLevel: syncLevel,
    apply: apply,
    isActive: isActive,
    // テスト用: スコア単体を露出 (apply の並び規則と同一実装)。
    scoreNode: function (node, words) { return _scoreNode(node, words); },
    _state: _state,
  };
})();
