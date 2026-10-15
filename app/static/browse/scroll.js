/* bookq browse/scroll — フォルダ一覧（#tile-grid）のスクロール位置の決定と適用。
 * Plain global script. index.html で browse/render.js の後、search/facets.js の前に読む。
 * 決定（decide）は DOM 計測をしない（単体テスト可能）。適用（apply）だけが幾何を触る。
 *
 * 決定表（上から順に評価）:
 *   cause==='top'                        -> top（上端）
 *   level===null（ルートの作品一覧）      -> hasSaved ? saved : top
 *   cause==='open'                       -> progress / top（決定を intent として残す）
 *   intent が level 一致                  -> intent（viewer / progress / top）
 *   hasSaved                             -> saved（呼び出し側の従来の rAF 復元に任せる）
 *   直下に既読がある                       -> progress（一覧順で最後＝最大番号）
 *   それ以外                              -> top
 *
 * 位置の intent: 「この階層の位置は決定済み」という level 紐づきの one-shot の印
 * （{level, anchor, via}）。決定が 2 回来る経路で 2 回目が古い保存位置に負けないように
 * する — ☰/Back の復帰は restoreBrowse が 2 回走り（returnToBrowse と、pushBrowseUrl の
 * history.go(-1) が起こす hashchange。2 回目にはビューアは teardown 済みで章パスを live
 * 状態から取れない）、新規オープン直後も同じ history.go(-1) → hashchange で
 * restoreBrowse が来る。level が一致する間は冪等に同じアンカーを返し（一覧が跳ねない）、
 * level が変わった決定 / cause==='top' / loadImages / resetToTop で破棄される。
 * URL の p= は 200ms デバウンスで古いので章の特定には使わない。
 */
(function () {
  // この階層の位置は決定済み、という印（level に紐づく one-shot）。
  // 値は {level, anchor, via}。via は 'viewer' | 'progress' | 'top'。
  // 有効なのは level が一致する間だけで、level が変わった決定 / cause==='top' /
  // loadImages / resetToTop で破棄される（呼び出し元の責務も含む）。
  var anchorIntent = null;

  // 決定を印として残す（新規オープン / ビューア復帰の両方から使う）
  function noteIntent(level, anchor, via) {
    if (!level) return; // ルートはアンカー対象外
    anchorIntent = { level: level, anchor: anchor, via: via };
  }
  // ビューア復帰の章を印として残す（hash.js から teardown 前に呼ばれる公開 API）
  function noteViewerReturn(level, chapterPath) {
    if (!level || !chapterPath) return;
    anchorIntent = { level: level, anchor: chapterPath, via: 'viewer' };
  }
  // テスト/デバッグ用: ビューア復帰の印が生きているときだけ返す
  function peekViewerReturn() {
    return (anchorIntent && anchorIntent.via === 'viewer') ? anchorIntent : null;
  }
  function clearViewerReturn() { anchorIntent = null; }

  // 表示中の階層の子（＝renderTiles が描くフォルダタイル）のパス一覧。
  // 絞り込み適用後なので「一覧に無いものはアンカーにしない」判定に使える。
  function _visibleChildPaths() {
    var out = [];
    try {
      var kids = window.getBrowseChildren ? window.getBrowseChildren() : [];
      for (var i = 0; i < kids.length; i++) out.push(kids[i]._fullPath);
    } catch (e) { console.warn(e); }
    return out;
  }

  function _has(list, v) { return list.indexOf(v) !== -1; }

  // 一覧順（＝サーバの自然順ソート）で最後の既読子。ReadingProgress は 1 回だけ
  // parse する（タイルごとに get() しない、browse/render.js と同じ方針）。
  function _lastReadChild(paths) {
    var all = {};
    try {
      var RP = window.ReadingProgress;
      if (RP && typeof RP.getAllSafe === 'function') all = RP.getAllSafe() || {};
      else if (RP && typeof RP.getAll === 'function') all = RP.getAll() || {};
    } catch (e) { console.warn(e); }
    var found = null;
    for (var i = 0; i < paths.length; i++) if (all[paths[i]]) found = paths[i];
    return found;
  }

  // 契約: opts.level は「表示中の階層」と一致していること。_visibleChildPaths は
  // window.getBrowseChildren()（＝表示中階層の子）を読むため、level を切り替える前に
  // 呼ぶと別階層の子で判定してしまう（呼び出し側は currentBrowsePath 更新後に呼ぶ）。
  // opts.hasSaved は呼び出し側の申告を信頼する（'open' は常に false を渡す）。
  function decide(opts) {
    opts = opts || {};
    var level = (opts.level === undefined) ? null : opts.level;
    var cause = opts.cause || 'restore';
    var hasSaved = !!opts.hasSaved;

    if (cause === 'top') { clearViewerReturn(); return { via: 'top', anchor: null }; }
    // 別 level の判定に来た時点で印は期限切れ
    if (anchorIntent && anchorIntent.level !== level) clearViewerReturn();
    if (!level) return hasSaved ? { via: 'saved', anchor: null } : { via: 'top', anchor: null };

    var paths = _visibleChildPaths();

    if (cause === 'open') {
      // 決定を印として残す。直後に pushBrowseUrl の history.go(-1) → hashchange →
      // restoreBrowse が同じ階層へ来ても、古い保存位置で上書きされないようにする。
      var last = _lastReadChild(paths);
      noteIntent(level, last || null, last ? 'progress' : 'top');
      return { via: last ? 'progress' : 'top', anchor: last || null };
    }

    // この階層の位置が決定済み（ビューア復帰 / 直前の新規オープン）→ 冪等に同じ位置を返す。
    // 消費はしない: ☰ 復帰は restoreBrowse が 2 回走るため、2 回目も同じ答えが要る。
    if (anchorIntent && anchorIntent.level === level) {
      var a = anchorIntent.anchor;
      if (a === null || _has(paths, a)) return { via: anchorIntent.via, anchor: a };
      // 章/アンカーが一覧に無い（絞り込み等）→ 破棄して保存位置 / 最大既読 / 上端へ落とす
      clearViewerReturn();
    }
    if (hasSaved) return { via: 'saved', anchor: null };
    var fallback = _lastReadChild(paths);
    if (fallback) return { via: 'progress', anchor: fallback };
    return { via: 'top', anchor: null };
  }

  // 実際にスクロールする要素。実測では #sidebar-scroll（#tile-grid は
  // scrollHeight === clientHeight で自分では動かない）。CSS が変わった場合の保険として
  // スクロール可能な方を選ぶ。
  function scroller() {
    var ss = document.getElementById('sidebar-scroll');
    if (ss && ss.scrollHeight > ss.clientHeight) return ss;
    var tg = document.getElementById('tile-grid');
    if (tg && tg.scrollHeight > tg.clientHeight) return tg;
    return ss || tg;
  }

  function _tileFor(path) {
    var grid = document.getElementById('tile-grid');
    if (!grid) return null;
    var els = grid.querySelectorAll('[data-path]');
    for (var i = 0; i < els.length; i++) {
      // セレクタ文字列にパスを埋めない（引用符・空白を含む名前のため）
      if (els[i].dataset.path === path) return els[i];
    }
    return null;
  }

  // innerHTML 置換と同一タスク内で呼ぶ（同期）。anchorPath が null/'' なら上端。
  // 容器の解決（scrollHeight/clientHeight の比較）は null 経路でも 1 回入るが、
  // cold なトップ表示は renderView が anchor を渡さないので apply 自体が呼ばれない。
  function apply(anchorPath) {
    var sc = scroller();
    if (!sc) return;
    var target = 0;
    if (anchorPath) {
      var tile = _tileFor(anchorPath);
      // 一覧に無い（絞り込み・空リスト）→ 上端のまま
      if (tile) target = Math.max(0, Math.round(tile.offsetTop - sc.offsetTop));
    }
    sc.scrollTop = target;
  }

  window.BrowseScroll = {
    decide: decide,
    noteViewerReturn: noteViewerReturn,
    peekViewerReturn: peekViewerReturn,
    clearViewerReturn: clearViewerReturn,
    scroller: scroller,
    apply: apply,
  };
})();
