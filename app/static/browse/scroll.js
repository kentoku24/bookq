/* bookq browse/scroll — フォルダ一覧（#tile-grid）のスクロール位置の決定と適用。
 * Plain global script. index.html で browse/render.js の後、search/facets.js の前に読む。
 * 決定（decide）は DOM 計測をしない（単体テスト可能）。適用（apply）だけが幾何を触る。
 *
 * 決定表（上から順に評価）:
 *   cause==='top'                        -> top（上端）
 *   level===null（ルートの作品一覧）      -> hasSaved ? saved : top
 *   cause==='restore' && マーカーが一致   -> viewer（章がこの階層の表示子にあるときだけ）
 *   cause==='restore' && hasSaved         -> saved（呼び出し側の従来の rAF 復元に任せる）
 *   直下に既読がある                       -> progress（一覧順で最後＝最大番号）
 *   それ以外                              -> top
 *
 * ビューア復帰マーカー: ☰/Back の復帰は restoreBrowse が 2 回走る（returnToBrowse と、
 * pushBrowseUrl の history.go(-1) が起こす hashchange）。2 回目にはビューアは teardown
 * 済みで章パスを live 状態から取れないため、1 回目の teardown 前に確定した値をここに
 * 保持し、level が一致する間は冪等に同じアンカーを返す（一覧が跳ねない）。
 * URL の p= は 200ms デバウンスで古いので章の特定には使わない。
 */
(function () {
  var viewerReturn = null; // {level: string, chapterPath: string}

  function noteViewerReturn(level, chapterPath) {
    // ルート（level が空）はビューア復帰の対象外（作品一覧は #resume-btn の役割）
    if (!level || !chapterPath) return;
    viewerReturn = { level: level, chapterPath: chapterPath };
  }
  function peekViewerReturn() { return viewerReturn; }
  function clearViewerReturn() { viewerReturn = null; }

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
    // 別 level の判定に来た時点でマーカーは期限切れ
    if (viewerReturn && viewerReturn.level !== level) clearViewerReturn();
    if (!level) return hasSaved ? { via: 'saved', anchor: null } : { via: 'top', anchor: null };
    // 明示的にフォルダを開いた＝ビューア復帰の文脈は切れた
    if (cause === 'open') clearViewerReturn();

    var paths = _visibleChildPaths();
    if (viewerReturn && viewerReturn.level === level && _has(paths, viewerReturn.chapterPath)) {
      return { via: 'viewer', anchor: viewerReturn.chapterPath };
    }
    if (hasSaved) return { via: 'saved', anchor: null };
    var last = _lastReadChild(paths);
    if (last) return { via: 'progress', anchor: last };
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
