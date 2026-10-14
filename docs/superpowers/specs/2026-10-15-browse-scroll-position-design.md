# フォルダ一覧の表示位置（スクロールアンカー）設計

対象: サイドバーのフォルダ一覧（`#sidebar-scroll` 内の `#tile-grid`）
関連: `app/static/browse/hash.js`, `app/static/browse/render.js`, `app/static/app.js`, `app/static/browse/scroll.js`（新規）

## 1. 目的

フォルダを開いたときの一覧スクロール位置を決定的にする。現状は「親階層のスクロール位置がそのまま持ち越される」ため、
開いた直後にリストの途中（ときに最後のタイルが上端で切れた状態）が表示されることがある。

決定済みの振る舞い（優先順位）:

| 優先 | 状況 | 表示位置 |
| --- | --- | --- |
| 1 | **ビューア復帰**（☰ / フォルダ名タップ / ブラウザBack） | ビューアで表示していたページのあるエピソードを上端に合わせる |
| 2 | その階層の**保存位置がある**（`↩`/Back で戻ってきた） | 従来どおり保存位置を復元（アンカーは適用しない） |
| 3 | **最大既読**: 上記以外でフォルダを開いた（タイルtap のドリルダウン / ディープリンク初回 / 保存位置の無い `restoreBrowse`） | 直下の子のうち **`ReadingProgress` があるものの一覧順で最後**（＝最大番号）を上端に |
| 4 | **上端**: 該当なし | `scrollTop = 0` |

決定済みの付帯条件（ユーザー確認済み）:

- 「読まれた」= `ReadingProgress` にエントリがあること（読了は問わない）
- 「最も大きい数字」= 一覧の並び順（サーバの自然順ソート。`1 < 2 < 10`、漢数字も正規化済み）で最後のもの。名前から数字を抜き出さない
- 位置合わせは**上端合わせ**（俯瞰グリッドの「上から 1/3」方式は採らない）
- 適用範囲は「新しく開くとき＋ビューア復帰」。`↩`/Back の保存位置復元は従来のまま
- 対象は**直下の子のみ**（孫に既読があっても、開いた階層に既読の子が無ければ上端）
- **ルート一覧（`currentBrowsePath === null`）ではビューア復帰・最大既読を適用しない**（作品一覧の「続きから」は既存の `#resume-btn` の役割。ルートの保存位置復元は従来どおり）

## 2. 背景（実測で確定した前提）

一時プローブを実ブラウザ（Chromium / tests/conftest の fixture tree）で走らせて確認した事実:

- **スクロール容器は `#sidebar-scroll` のみ**。`#tile-grid` は `scrollHeight == clientHeight` で自分はスクロールしない
  （390x844 / 1200x800 の両方。`ss.scrollTop=300` → 300、`tg.scrollTop=300` → 0）
- タイル高さは描画直後に確定する（390 幅で 297px、多サムネ/プレースホルダ含め同値）。画像ロード後のレイアウトシフトは無い
- `offsetTop(tile) - offsetTop(#sidebar-scroll)` と rect 差分は一致する（±0.1px）
- 現状の持ち越しは実在する: root を 500 までスクロール → `zoom_series` を開くと `scrollTop = 373`（新コンテンツの最大値にクランプ）。3 枚目のタイル上端が -3px で切れる
- **ビューア復帰（☰）では `restoreBrowse` が 2 回走る**: 1 回目は `returnToBrowse`（hash.js:362）、2 回目は `pushBrowseUrl` の `history.go(-1)` が起こす `hashchange` → `applyUrlState`（hash.js:397）
- `restoreBrowse` は先頭で `_teardownViewer()` するため、`renderView` の時点で `currentFolder`/`chapterList` は空。**章パスは teardown 前に捕捉する必要がある**
- URL の `p=` は 200ms デバウンスで古くなる（連続ページ送り中は特に）。**章の特定に URL を使ってはいけない**
- 章パスは `currentViewerState().folder` で正確に取れる。`ReadingProgress` のキー・タイルの `data-path`・章の `fullPath` は（scanner の flatten 越しでも）一致する
- `scanner.py:355-360` の flatten（親に画像なし＋子 1 つで子名に親名を前置）により、`zoom_series` は直接 ep-1..3 を持ち、パスは `zoom_series/series/ep-N`

## 3. 判定の一元化（新モジュール `app/static/browse/scroll.js`）

判定（どこに合わせるか）と適用（幾何）を分離する。判定は DOM 計測をしない＝単体でテストできる。

```js
window.BrowseScroll = {
  // 決定: {via, anchor} を返す。via は 'viewer'|'saved'|'progress'|'top'
  //   cause: 'open'    = 新規オープン（navigateToFolder / 保存位置の無い restoreBrowse）
  //          'restore' = restoreBrowse（保存位置復元と競合しうる）
  //          'top'     = resetToTop
  decide(opts),            // opts: {level, cause, hasSaved}

  // ビューア復帰マーカー（レベルに紐づく one-shot。詳細は 3.2）
  noteViewerReturn(level, chapterPath),
  peekViewerReturn(),

  scroller(),              // 実行時に解決（#sidebar-scroll。スクロール不能なら #tile-grid）
  apply(anchorPath),       // 同期で scrollTop を書く（null = 上端）
};
```

`decide` の規則（上から順に評価）:

```
cause === 'top'                                   -> {via:'top',      anchor:null}
level === null（ルート）                            -> hasSaved ? {via:'saved', anchor:null} : {via:'top', anchor:null}
cause === 'restore' && marker.level === level
     && marker.chapterPath がこの階層の表示子に存在 -> {via:'viewer',   anchor:chapterPath}
cause === 'restore' && hasSaved                     -> {via:'saved',    anchor:null}
非ルート && 既読の直下子がある                        -> {via:'progress', anchor:最後の既読子}
それ以外                                             -> {via:'top',      anchor:null}
```

- `via:'saved'` のとき呼び出し側は従来の保存位置復元を行い、アンカーは適用しない（＝1 つの階層への書き込みは 1 系統だけ）
- 「表示子に存在」の判定は `window.getBrowseChildren()` の `_fullPath` 集合で行う（絞り込みで消えている場合・階層違いをここで吸収する）
- `decide` はマーカーを **level 一致で参照し、level が違えば破棄**する（期限切れの一元管理）

### 3.1 呼び出し元（変更点）

| 場所 | 変更 |
| --- | --- |
| `app.js:navigateToFolder()` | `currentBrowsePath` 更新後 `decide({level:path, cause:'open', hasSaved:false})` → `renderView({scrollAnchor: anchor})` |
| `hash.js:restoreBrowse()` | 先頭（teardown 前）で `currentViewerState()` があれば `noteViewerReturn(path, st.folder)`。保存位置キーの有無を `hasSaved` として `decide({level:path, cause:'restore', hasSaved})`。`via==='viewer'` なら**既存の rAF 復元ブロックをスキップ**して `renderView({scrollAnchor: anchor})`、`via==='saved'` なら従来の rAF 復元のみ（アンカー無し） |
| `hash.js:resetToTop()` | `decide({level:null, cause:'top'})` → `renderView({scrollAnchor: null})` |
| `app.js:loadImages()` | `BrowseScroll` のマーカーを破棄（別の本を開いた＝ビューア復帰の文脈が切れる）。アンカーは渡さない |
| `app.js:updateBrowseHeader()` の `↩` onclick | **変更なし**（保存位置復元のみ。ビューアを開いたまま親階層へ移る操作なので対象外） |
| `browse/render.js:renderBrowse()` | `opts.scrollAnchor` を受ける。`undefined` = 触らない / `null` = 上端 / path = そのタイル上端。各 `render*` の `innerHTML` 置換直後に `BrowseScroll.apply()` |
| `renderView({scrollAnchor})` | 上記を `renderBrowse` へ受け渡す（未指定は `undefined` のまま＝再描画で勝手に動かない） |

### 3.2 ビューア復帰マーカー（二重描画対策）

☰ 復帰は `restoreBrowse` が 2 回走る（§2）。2 回目はビューアが既に死んでいるため、live 状態からも
`currentViewerState()` からも章を取れない。**マーカーが唯一の正しい情報源**になる。

- `noteViewerReturn(level, chapterPath)`: 1 回目（`returnToBrowse` → `restoreBrowse`）の teardown 前に設定。値は live な `currentViewerState().folder`（URL の `p=` は使わない＝§2 のデバウンス問題を回避）。`level === null`（ルート）のときは設定しない（ルートはビューア復帰の対象外）
- 1 回目・2 回目とも同じ level なので、同じ anchor が冪等に適用され、**一覧は跳ねない**
- 破棄条件: `decide` が異なる level で呼ばれたとき / `cause === 'open'`（ユーザーが明示的にフォルダを開いた）/ `loadImages` / `resetToTop`
- セッション内メモリのみ（リロードで消える。リロードは「保存位置の無い restoreBrowse」なので最大既読（または上端）に落ちる）

### 3.3 適用（幾何）

- 容器は実行時解決。`#sidebar-scroll` が `scrollHeight > clientHeight` ならそれを使い、そうでなければ `#tile-grid`
- 対象タイルは `#tile-grid` 内の要素を `el.dataset.path === path` で線形走査（セレクタ文字列にパスを埋め込まない。`夏 の 本` のような名前・引用符対策）
- `scrollTop = Math.round(offsetTop(tile) - offsetTop(scroller))`、下限 0（上限はブラウザの clamp）
- `innerHTML` 置換と**同一タスク内**で同期適用する。誤った位置が 1 フレーム見えることがなく、既存の rAF 復元と書き込みが競合しない（テストも決定的になる）
- アンカーが無い場合（`null`）は計測せず `scrollTop = 0` のみ。cold なトップページ表示（＝ルート初回）はこの経路なので、表示完了予算に計測コストを足さない

## 4. エッジケース

- 絞り込み（作者/タグ）でアンカーが一覧に無い → `decide` の「表示子に存在」判定で落ち、保存位置 → 最大既読 → 上端 の順にフォールバック
- 一覧が空（`フォルダが見つかりません`）→ 上端（no-op）
- 表示モード切替・ツリー再取得・サムネ追加読み込み・オフラインマップ更新による再描画 → `renderView()` に anchor を渡さないので**触らない**
- 検索モード → 検索タイルは常にビューアを開く（`facets.js:128-148`）ので対象外。検索結果描画にも anchor は渡さない
- `#resume-btn`（続きから）→ 対象外（parent を描画して 100ms 後に `loadImages`。アンカーは渡さない）
- 浅いツリー（`_shallowMode`）→ ドリルダウンはキューされ、フル到着後の `navigateToFolder` で anchor が適用される
- アーカイブ/PDF/EPUB の子も同様に `data-path` で解決される（パスは flatten 済みの生パス）
- ルートの loose images（`data-path=""` の self タイル）→ `level === null` は上端固定のため対象外

## 5. テスト（新規 `tests/test_browse_scroll_target.py`）

**ビューポートは 390x430**（390x844 では fixture のほとんどの階層がスクロール不能で空振りする。実測: `group` は `scrollHeight == clientHeight == 728`）。
各テストの冒頭で `#sidebar-scroll` の `scrollHeight > clientHeight` を**前提として assert** し、空振りを構造的に排除する。
容器は `#sidebar-scroll` を直接読み書きする（既存 `test_sidebar_scroll.py` のメディアクエリ推測ヘルパは使わない）。

| # | テスト | 期待 |
| --- | --- | --- |
| 1 | 既読なしで `zoom_series` を開く（親を 500 までスクロール済み） | `scrollTop == 0`（現状 373 = RED） |
| 2 | `bookq_progress` に ep-2 と ep-3 を仕込んで `zoom_series` を開く | ep-3 のタイル上端（≈370）。ep-2 の位置（≈52）ではない |
| 3 | ep-2（行1）を開いてページ送り→ ☰ | ep-2 の位置（≈52）。最大既読 ep-3（≈370）に引っ張られない＝ビューア復帰 > 最大既読 |
| 4 | 3 と同じ操作で `window.renderView` の呼び出しを数える | 復帰時に 2 回呼ばれ、2 回目の後も `scrollTop` が変わらない（二重描画の冪等性） |
| 5 | ep-3（行2）を開いてページ送り→ ブラウザ Back | ep-3 の位置（≈370）＝Back 経路でもビューア復帰 |
| 6 | `group` をスクロール→ `deep_series` へドリル→ `↩` で戻る | 親の保存位置が復元される（既存挙動の回帰 pin） |
| 7 | `group` を 0 で `deep_series` へドリル→ ↩→ `other_work` を開いて ☰ | 保存位置（0）ではなく `other_work`（≈370）＝ビューア復帰 > 保存位置 |
| 8 | root をスクロール→ `simple_manga` を開く→ ブラウザ Back（bare root） | ルートは上端（`resetToTop`）＝ルートにはビューア復帰を適用しない |
| 9 | `window.BrowseScroll.decide` の直接呼び出し | `{level:null, hasSaved:false}`→`top` / `{cause:'restore', hasSaved:true}`→`saved` / 既読子あり（`cause:'open'`）→`progress`+期待パス / level 不一致でマーカー破棄 |
| 10 | 1〜3 を list / card モードでも | 同じ規則（モードは一覧順・幾何にのみ影響） |

既存テストへの影響: `test_sidebar_scroll.py`（↩ 復元）は無変更で緑を維持する。`#sidebar-scroll` のスクロールを前提にした
`test_browse_url_state.py` / `test_search_e2e.py` / `test_browse_polish.py` も、アンカーが働くのは
「フォルダを開く」「ビューア復帰」「保存位置なしの restore」だけなので影響しない見込み（実装時にフルスイートで確認）。

## 6. 変更ファイル一覧

- 新規 `app/static/browse/scroll.js`（`window.BrowseScroll`）
- `app/static/browse/hash.js` — `restoreBrowse` に章の捕捉・`hasSaved` 判定・`decide`・`via:'saved'` 分岐、`resetToTop` に `cause:'top'`
- `app/static/browse/render.js` — `renderBrowse({scrollAnchor})` と、各 `render*` の `innerHTML` 置換直後の `apply`
- `app/static/app.js` — `renderView({scrollAnchor})` の受け渡し、`navigateToFolder` の anchor 計算、`loadImages` のマーカー破棄
- `app/templates/index.html` — `<script src="/static/browse/scroll.js" defer>`（`browse/render.js` の後、`app.js` の前）
- `app/static/sw.js` — `SHELL_ASSETS` に追加（`tests/test_sw_offline_contract.py` の件数 19 → 20）
- `CLAUDE.md` / `AGENTS.md` — モジュール一覧（`AGENTS.md` の一覧は gesture/zoom/overview が抜けて古いので同時に修正）
- `docs/core-features.md` — サイドバーのスクロール位置の項に「フォルダを開いたときの位置（上端 / 最大既読 / ビューア復帰）」を追記

## 7. スコープ外（記録のみ）

- `tests/test_sidebar_scroll.py` のデスクトップ分岐は `#tile-grid.scrollTop` を設定する**空振り**（実測: 実際のスクローラは `#sidebar-scroll`）。「デスクトップでは検索ボックスが固定」という前提も CSS 上は成立していない（検索ボックスはスクローラの内側。fixture が短いため目立っていない）
- URL の `p=` が 200ms デバウンスで古い（章の特定に URL を使わない、という本設計の前提で回避済み）
- `ReadingProgress` の保存が 500ms デバウンスで、`teardownViewer` で flush していない（ビューア復帰は記録に依存しないので本件の成立には不要。別 issue 候補）

## 8. リスクと対策

| リスク | 対策 |
| --- | --- |
| 二重描画で判定が上書きされる | 3.2 のマーカー（level 一致 + one-shot + 冪等適用） |
| 保存位置復元との二重書き込み | `decide` が `via:'saved'` を返し、書き込み系統を 1 本化 |
| 章の取り違え（URL デバウンス） | 章は live 状態（`currentViewerState().folder`）から取得 |
| 空振りテスト | 小ビューポート + `scrollHeight > clientHeight` の前提 assert |
| 再描画で勝手にスクロールする | anchor は `undefined`（触らない）を既定にし、明示的に渡した描画だけで適用 |
| 計測コスト（forced layout） | アンカーがある場合のみ計測。ルート初回＝アンカー無しは `scrollTop=0` だけ |

## 9. 未解決（実装時に実測して決める）

- `scroller()` の解決順は実測で `#sidebar-scroll` に確定している（390x844 / 1200x800）。テストで使う 390x430 と 900x600 でも同じであることをテスト冒頭の前提 assert で固定する（`#tile-grid` へのフォールバックは将来 CSS が変わったときの保険）
- 360 度回転・リサイズ直後（`updateOrientation` 後）のアンカー適用は今回のスコープ外（フォルダを開く操作ではない）。必要になれば `renderView` に anchor を渡すだけで拡張できる
