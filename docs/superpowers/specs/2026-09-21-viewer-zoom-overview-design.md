# ビューア ピンチズーム + 俯瞰モード 設計

日付: 2026-09-21
状態: red team レビュー 3 巡（最終: Critical 0 / High 2 を反映、以降は実装中対処）を経た第 4 版。実装着手可。
センターピン: **快適な読書体験**。ズームも俯瞰も、読書の流れを止めない・誤操作で読者を迷子にしないことを最優先とする。

## 1. 目的

- 拡大: ピンチアウトでページを 1.0〜4.0x に拡大し、1 本指でパンできる。細かい書き文字を読む用途。
- 俯瞰: ピンチインで iOS 写真アプリのように全ページをグリッドで俯瞰し、タップで任意のページに飛ぶ。エピソードをまたいだ移動にも使う。
- 既存のページめくり（タップ／スワイプ／キー／スライダー、エピソード内遷移、末尾での次エピソード遷移）の挙動は **scale = 1.0 のとき** 一切変えない。

## 2. 背景と制約

- iOS Safari はレイアウト幅が画面幅に収まる状態からのズームアウトを許さず（`minimum-scale` を下げても不可、Simulator iOS 27 で実測）、スケール 1.0 でのピンチインを「タブ一覧を開く」ジェスチャとして横取りする。よって 2 本指ジェスチャは全てアプリが握る必要があり、その代償としてネイティブのピンチ拡大も自前実装になる。
- 参照実装: ブランチ `ai-overview2-minimal`（俯瞰モード2）。実機 iPhone で詰めた絶対ズーム状態機械・ネイティブズーム封じ・FLIP 入場を引き継ぐ。ただし参照実装が成立していた 2 つの前提は今回失われるので、明示的に再設計する。
  - (a) document 級のガードが `/viewer` 専用シェルに閉じていた → 今回は `/` に同居するので、ガードは `#viewer` 配下にゲートする（§4.1）。
  - (b) ズーム対象が現在ページの単一 `<img>` だった → 今回は `#img-track` を包むレイヤーなので、clamp の対象を「現在スプレッド」と定義する（§4.2）。
- 既存ビューアの不変条件（CLAUDE.md）: スプレッドが単位、anchorSpread 基準の位置、`snapTo` はアニメ後に rebase、`#img-track` の transition リスナは `isTrackSlideEnd()` で filter。ズームは親レイヤー `#zoom-layer` にのみ適用し、`#img-track` の `style.transition` / `transform` には一切触らない（レイヤーの `transitionend` は子の track リスナに届かないので干渉しない）。

## 3. 決定済みの要件

| 項目 | 決定 |
|---|---|
| トリガー | ピンチのみ。拡大も自前実装（ネイティブズームは封じる） |
| 俯瞰の範囲 | シリーズ全体（`images` 全チャプター連結）。チャプター境界に見出し行 |
| 並び順 | 右→左、上→下（ビューアと同じ RTL） |
| タイルタップ | そのページを開いて読書モードへ戻る |
| 拡大状態 | ページ送りで 1.0 にリセット |
| 俯瞰の入口（非タッチ） | ツールバーのグリッドボタン、`G` キー |
| URL/履歴 | 俯瞰・拡大は載せない（レイアウト設定と同じ扱い） |

## 4. 快適さの数値基準（QA と実装の合格条件）

- ピンチ追従: 2 本指の `touchmove` から次フレーム（16ms）で transform が更新される。`touchmove` ハンドラ内で DOM 実測（`getBoundingClientRect` / `offsetTop`）を行わない。
- 俯瞰入場: ピンチ開始から 0.5 到達までの間、同期処理は 1 フレーム 8ms 以内。グリッド構築の同期部分は 50ms 以内（先行構築はアイドル時間、§4.3）。
- 俯瞰スクロール: 300 ページの作品で 60fps を落とさない。セル画像はサムネイル（§4.3）。
- 誤発火ゼロ: 1 本指スワイプ／タップ／スライダー操作が、画面端 20px 起点で静止した 2 点目の接触（親指の付け根）で殺されない。**既存スワイプも生き残る**（§5.1 パーム除外。app.js 側の判定も差し替える）。
- 触覚: 俯瞰発火（0.5 到達）と端越えページ送りの**コミット閾値 80px 到達時に 1 回**、既存スワイプと同じ `navigator.vibrate(10)`（reduced-motion では鳴らさない）。clamp 端に触れただけでは鳴らさない。
- 行き止まりゼロ: 拡大中でも、どの向き・どのページモードでも「次ページへ進む」「1.0 に戻す」が片手で 1 操作で出来る（§5.2 中央ゾーン、端越えの一般化）。
- reduced-motion: ドリフト・FLIP・バウンス・ページ送り時の縮小アニメを全て即時に。`#zoom-layer` を style.css の既存 `prefers-reduced-motion` ブロックに追加。

## 5. アーキテクチャ（A 案: オーバーレイ方式）

読書ストリップ `#img-track` は無変更。拡大は `#img-track` を包む新ラッパー `#zoom-layer` への `transform`、俯瞰は別オーバーレイ `#overview`。新規 3 モジュールを `viewer/` に追加し、`app.js` には early return と reset 呼び出しを足すだけ。

```
index.html   #viewer > #img-container > #zoom-layer > #img-track      ← ズームはここ
                                       > #zone-prev/#zone-toggle/#zone-next  ← レイヤーの外（兄弟）。拡大しない
                     > #overview (hidden, z-index 12)
scripts      spread.js → gesture.js → zoom.js → overview.js → app.js
```

タップゾーンは `#zoom-layer` の**外**に置く。中に入れると 4x 時にゾーンごと拡大・移動して左 45% が画面のどこか分からなくなる。

### 5.1 `viewer/gesture.js` — ピンチ認識器 + ネイティブ封じ

公開: `window.ViewerGesture.attach(el, {onStart, onMove, onEnd})`。

**ピンチ成立条件（パーム除外）**: 接触点を「有効」と「パーム」に分類する。画面端 20px 以内で始まった接触はパームとして扱い、その接触が 10px 以上動いたら有効に昇格する。公開 `ViewerGesture.activeTouches(e)` は有効な接触だけを返す。ピンチは有効な接触が 2 点になり、**両方**が開始位置から 10px 以上動いた時点で成立し、そこで初めて `onStart({dist0, mid0})` を呼ぶ。

**既存スワイプを生き残らせる**: app.js の既存スワイプはスワイプ指を **`Touch.identifier` で追跡**する。`touchstart` で `activeTouches(e)` が 1 点のときその identifier を `dragTouchId` に記録し、`touchmove`/`touchend`/`touchcancel` は `e.changedTouches`/`e.touches` からその identifier の Touch を引いて座標を取る（`e.touches[0]` / `e.changedTouches[0]` の index 参照は全廃）。本数判定（`!== 1` で `dragActive=false` / return）は `activeTouches(e).length` で行う。パーム側だけが先に離れた `touchend` は identifier が一致しないので無視し、スワイプ指を離した時点で初めて確定する。これにより、端末下部を握った親指の付け根が触れたまま・離れても 1 本指スワイプはそのまま動く。有効 2 点になった瞬間は従来どおり `dragActive=false`（そこからはピンチ）。

- 成立後: 2 本指の `touchmove` で `onMove({ratio, mid})`。1 本指以下になったら `onEnd({ratio, remaining: Touch|null})`。`remaining` は残った指（パン原点の引き継ぎ用、§5.2）。
- **スナップ中断の回収**: `onStart` 時、`snapTo` のスライドが進行中なら `snapTo(currentIdx, false)` で即時確定してから基準を取る。1 本目の `touchstart` が `transition="none"; snapGen++` で進行中の rAF を無効化し、2 本目で `dragActive=false` になるため、これを入れないとストリップが 2 ページの中間で固まる。
- **ネイティブ封じ**（初期化時に一度張る。俯瞰モード2 で実機に必要と判明した全部。ただし全て `#viewer` 配下かつ `images.length > 0` にゲート）:
  1. `#img-container`, `#img-track *`, zones, toolbar 内の各要素, `#hint`, `#spread-controls *`, `#empty` に `touch-action:none`。`#page-slider` は `pan-x`（Simulator で つまみドラッグが壊れないことを実装時に確認する）。`#overview` は `pan-y`。サイドバーは無変更。
  2. document capture で `gesturestart/gesturechange/gestureend` を `preventDefault`（`#viewer.contains(e.target)` のみ）。
  3. `#viewer` に非 passive `touchmove`: `touches.length === 2` のときだけ `preventDefault`。
  4. document capture の非 passive `touchstart`: `touches.length >= 2` かつ `#viewer.contains(e.target)` かつ `images.length > 0` のときだけ `preventDefault`。サイドバーのタイル一覧スクロール中に 2 本目がかすってもスクロールが止まらない。
  5. viewport meta の `maximum-scale=1,user-scalable=no` は **Simulator で 1〜4 だけで封じられるか実測してから決める**。index.html:36 の meta はログイン済みシェル全体の唯一の meta なので、入れるとブラウズ UI のアクセシビリティズームも失う（standalone PWA では特に）。1〜4 で足りるなら入れない。足りない場合のみ入れ、副作用を CLAUDE.md に書く。

### 5.2 `viewer/zoom.js` — 絶対ズーム状態機械

状態: `zoom`（絶対、1.0 で rest）、`{scale, tx, ty}` を `#zoom-layer` に `transform-origin:0 0` で適用。`#zoom-layer` の CSS 既定は `transition:none`（`#img-track` の `.25s` を継承させない）。公開: `window.Zoom = {reset, isActive, state}`。**`Overview.isOpen()` の間は transform を書かない。**

- `target = startZoom × ratio`、`ZOOM_MIN=0.35, ZOOM_MAX=4`。
- **新ジェスチャ開始時**（`onStart`）: `layer.style.transition=''` とバウンスタイマーを必ず消す（消さないと 190ms 遅れの「水あめ」追従になる）。
- 帯域（絶対値）:
  - `target > 1.0`: 拡大。中点アンカー（開始中点直下の画像点が現在中点直下に留まる）。
  - `0.5 < target < 1.0`: 縮小帯。scale は指に追従、位置は指に追従せず、画像中心が俯瞰グリッド上の自セル中心へ `p=(1-target)/0.5` で線形ドリフト。**セル中心は算術で求める**（実インデックス + 見出し行数から行・列、セル幅・高は固定 px、`#overview` の `scrollTop` は「`.cur` が上から 1/3」で確定しているので既知）。DOM 実測はしない。reduced-motion ではドリフトなし。
  - `target <= 0.5`: 俯瞰発火（1 回ラッチ）+ `vibrate(10)`。指を離さず `> 0.7` で再アーム。`touchend` でラッチ解除。
- 離したとき: `> 1.15` は維持、`1.0 < z <= 1.15` は 1.0 にスナップ、`0.5 < z <= 1.0` は 190ms ease で rest へバウンス。
- **2P（見開き）モード**: 拡大の単位はスプレッド全体（`spreadMetrics(anchorSpread)` の矩形）。片ページ単位の拡大は行わない。clamp・端越えもスプレッド単位。
- **clamp の定義**: 「現在スプレッドの矩形 × scale がビューポートを**覆い続ける**範囲」。隣接スプレッドは `#zoom-layer` 内に居るが、この clamp によりビューポートに入ってこない。scale × スプレッドがビューポートより小さい軸（横持ち 1P の 0.7 比ページは横 574px < 1180px なので 2.06x まで横方向）は中央固定で、その軸は「パン余地なし」の状態。
- **パン（`zoom > 1`）**:
  - 既存 `touchstart/move/end` の先頭で `Zoom.isActive()` なら早期 return し、zoom.js 側のパンに回す。
  - `onEnd({remaining})` で 2 本→1 本になったら、残った指の位置を**そのままパン原点にする**（新しい `touchstart` は来ない）。拡大 → そのまま 1 本指でスライド、が途切れない。
  - パン終了（`touchend`）時に必ず `suppressClickFor(400)` を呼ぶ。
  - **タップゾーン**: `Zoom.isActive()` の間、`#zone-prev`/`#zone-next` は無効（`body.zoomed` で `pointer-events:none`）。拡大中に指を置き直しただけでページが飛ぶのを防ぐ。**中央 10% ゾーンは拡大中「1.0 に戻す」に意味が変わる**（ツールバーは拡大中どのみち隠れている）。片手で拡大を解除できる唯一の経路なので必須。
  - **端越えページ送り**: 「横方向にパン余地が無い状態から開始したドラッグ」で発火する。パン余地が無い = clamp 端に触れている、**または** その軸が中央固定（scale × スプレッド幅 < viewW）。これにより横持ち 1P で 1.5x に拡大しても行き止まりにならない。条件: 縦横優勢判定（`|dx| > |dy|`）を満たし、同方向に追従量 0.35 倍のラバーバンドで 80px 以上動いたら **80px 到達の瞬間に 1 回** `vibrate(10)` で予告し、離したら発火。パン継続中の惰性では発火しない（ドラッグ開始時点で余地があった場合は、そのドラッグ内で端に達しても発火しない）。方向は既存の符号規約と同じ: **左端に到達（または横に余地なし）で指を右へ 80px → `goNext({crossChapter:true})`**。右端で指を左へ → `goPrev`。（既存: 指が右 = `dragPx > 0` = 次ページ、app.js `advanced = 1`）
- `reset(animate)`: `snapTo` 先頭（`animate=true`: `snapTo` のスライドと同じ `transform .3s cubic-bezier(0.25,0.1,0.25,1.0)` で 1.0 へ縮む。瞬時だとタップの瞬間にガクッと縮んでからスライドする）、`_teardownViewer()`、俯瞰を開くとき、`updateOrientation` 経由（下記）。
- **`resize` の扱い**: 既存 `updateOrientation` は無条件で `snapTo(currentIdx,false)` に到達するので、そのままだと検索キーボードの開閉でも `snapTo` → `reset` が走る。`updateOrientation` の先頭に「前回の寸法と同じなら return」を入れる（§5.4 差分）。**前回値は別変数 `lastOrientW/lastOrientH` を番兵 `-1` で初期化して持つ。既存の `viewW`/`viewH` は宣言時に `window.innerWidth/Height` で初期化済みなので流用すると init の初回呼び出しが早期 return し、`body.portrait` も `pageMode` も付かない。** `spreadToggle`/`spreadShift` は `updateOrientation` を経由せず自前で `snapTo` するので、このガードは 1P/2P 切替に影響しない。これで幅・高さが実際に変わった `resize`/`orientationchange` だけが `snapTo` に届き、reset もそこで起きる。zoom.js 自身は `resize` を listen しない。
- 拡大中: `#hint` 非表示、`body.zoomed`（`#zoom-layer` の `will-change:transform` はこのクラスのときだけ付ける。常時だと `#img-track` と合成レイヤーが二重になる）。

### 5.3 `viewer/overview.js` — 俯瞰オーバーレイ

公開: `window.Overview = {open, close, isOpen, prebuild}`。

- **DOM / 重なり**: `#viewer` 直下に `#overview`（`position:absolute; inset:0; z-index:12; overflow-y:auto; touch-action:pan-y; hidden`）。`#toolbar`（z-index 5）と `#spread-controls`（z-index 10）より上。加えて `body.overview-open` で `#toolbar, #spread-controls, #hint, zones` を `pointer-events:none; opacity:0`（スライダーが背後で動かない、2P ボタンがタイルを隠さない）。
- **ヘッダ**: 上端は情報のみ（`position:sticky`）: フォルダ名、現在位置チップ。チップは既存カウンタ（チャプター内表示）と揃えて **`EP3 12/48 ・ 通算 140/312`**（スクロールに応じて「今画面中央にあるチャプター名」を表示。見出し行の sticky はこれで代替、下記）。**閉じるボタンは下端中央に 44px**（片手の親指が届く位置。上端の × は届かない）。
- **グリッド構造**: チャプターごとに `<section class="ov-chapter">` で区切り、各セクションが独立した `display:grid; direction:rtl; grid-template-columns:repeat(auto-fill, minmax(150px, 1fr)); gap:8px; padding:8px`。見出しはセクション直下の通常ブロック（`position:sticky; top:<ヘッダ高>`。含有ブロックがセクション全体になるので、そのチャプターをスクロールしている間は固定される。grid item に sticky を掛けても 1 行分しか効かないため）。iPhone 縦で 2 列、iPad 縦で 5 列、iPad 横で 7 列。密度切替 UI は付けない（スコープ外のまま）。RTL でも見出しは全幅なので左右反転しない。
- **セル**: `<button class="ov-cell" data-gidx>`。比率は**全セル 3:4 に固定**（`pageAR` は先行構築時点でプリフェッチ窓の 10 枚程度しか既知でなく、実比率にすると格子がギザギザになり、読み進めるたびにレイアウトが変わり、§5.2 の算術ドリフトの前提も崩れる）。画像は `object-fit:contain`（見開きスキャン 1 枚もの（比 1.4）でも切れない）。セル高は列幅から算出して px 固定。右下にチャプター内ページ番号。現在ページに `.cur` 枠線。`content-visibility:auto` で画面外セルのレイアウトコストを削る。
- **セル画像**: `OfflineCore.thumbUrl(folder, name, 400)`（`/thumb/?w=400`、ディスクキャッシュ済み）。`onerror` で `OfflineCore.imageUrl` にフォールバック（`downloadWork` は `/image/` しかプリキャッシュしないので、オフライン作品はフォールバック経路で表示される）。`loading="lazy" decoding="async"`。
- **内容**: `images` を走査し `isSpacer` は捨てる。`chapterList.length > 1` ならチャプターごとにセクション。**範囲は常にシリーズ全体**（決定済み要件）。600 ページ超では粗いウィンドウイング: 現在位置 ±200 ページのセルだけ `<img>` を持ち、残りは同じ高さのプレースホルダ（`<button>` は置くが `<img>` なし）。スクロールで窓を移動して `<img>` を出し入れする。全体のスクロール長は変わらないので「全体を俯瞰している」感覚を壊さない。
- **スクロール位置**: 開いたとき `.cur` が**上から 1/3** に来るようスクロール（上端だと読んできた分が全部画面外に消える）。
- **先行構築**: `loadImages` 完了後の `requestIdleCallback`（無ければ `setTimeout 300`）で構築する。状態は **`hidden` を外し** `opacity:0; pointer-events:none; contain:strict` で**レイアウト済み**にしておく（`hidden` のままだと初回レイアウトが入場時 = `touchmove` の中に戻る）。§4 の「同期 50ms 以内」はこのアイドル構築の予算。縮小帯に入った瞬間に構築しない。`_teardownViewer` / `loadImages` で破棄して再構築。
- **入場**: 現在ページ `<img>` のクローンを `position:fixed` で置き、220ms でセル矩形へ transform → 完了でグリッド表示。`transitionend` が来ない場合の `setTimeout(220+80)` フォールバック必須。reduced-motion / 実測失敗は即時表示。
- **俯瞰中の回転**: `ResizeObserver(#viewer)` でセル高を再計算し `.cur` へ再スクロール（`resize` イベントに頼らない）。
- **サイドバーとの排他**: 俯瞰が開いている間に `#menu-btn` が押されたら先に `Overview.close()` してから既存の `toggleSidebar` に渡す。`toggleSidebar` は `resize` を発火せず、`currentFolder` があると `returnToBrowse` に入るため、俯瞰を生かしたまま幅を変える経路を作らない。
- **退出（5 経路）**:
  1. セルの 8px 未満タップ。ただし**直近の `scroll` から 100ms 以内は「慣性を止める」だけで開かない**。開くときは `snapTo(gidx,false)` → 閉じる。タップしたセルの画像を `w=800` のサムネで `position:fixed` 前面に置き、本画像の `load` でクロスフェードして消す（200 ページ先はプリフェッチ窓の外なので、これが無いと黒画面になる）。**`load`・`error`・3 秒タイムアウトのいずれかで必ず除去**し、以後は既存のスピナー／⚠ 表示（`onCurError`）に委ねる。
  2. 下端の閉じるボタン（元のページに戻る）。
  3. `Esc`。**overview.js は app.js より前にロードされるので、自前の document `keydown` で先に受けて `stopImmediatePropagation`** する（storeAdapter と同じ作法。app.js 側に Esc 分岐を足しても後段になるので効かない）。
  4. 俯瞰内ピンチアウト。**`#overview` 上の `touchstart` から始まる新規ジェスチャ**のジェスチャ相対比が 1.3 を超えたとき。入場を起こしたジェスチャ（ターゲットは `#img-container` に固定されたまま）が継続中は、指を広げても退出しない（入退場の振動防止）。**着地は指の中点直下のセル**（写真アプリと同じ）。中点がセル外なら元のページ。
  5. `_teardownViewer()`（Back で別の本／トップに戻る経路。`resetToTop` と `restoreBrowse` は `loadImages` を通らない）。飛行クローンの回収も `close()` に含める。
- 退出後 400ms は `#img-container` の click を capture で握りつぶす（タップ由来の合成 click が背後のゾーンに落ちない）。
- **入口**: ツールバー右端 `#overview-btn`（`⊞`, `aria-label="ページ一覧"`）、`G` キー（`metaKey`/`ctrlKey` 付きは無視）。`images.length === 0` では no-op。

### 5.4 既存コードへの差分（全部）

- `index.html`: `#zoom-layer` ラッパー（ゾーンは外）、`#overview`、`#overview-btn`、script タグ 3 本。viewport meta は §5.1-5 の実測結果次第。
- `app.js`:
  - 既存 `touchstart/move/end` 先頭の `Zoom.isActive()` early return 3 行
  - 既存 `touchstart/move/end/cancel` の本数判定を `ViewerGesture.activeTouches(e).length` に、座標参照を `dragTouchId`（`Touch.identifier`）追跡に差し替え（パーム除外、§5.1）
  - `snapTo` 先頭に `Zoom.reset(true)`
  - `_teardownViewer` に `Zoom.reset(false); Overview.close()`
  - `loadImages` 完了後に `Overview.prebuild()`
  - `updateOrientation` 先頭に「`lastOrientW/H`（番兵 -1）と同じなら return」＋末尾で更新
  - `zoneToggle` の click: `Zoom.isActive()` なら `Zoom.reset(true)` してツールバーはトグルしない
  - `#menu-btn` の click: `Overview.isOpen()` なら先に `Overview.close()`
  - `keydown` に `G`（修飾キー除外）
  - `suppressClickFor` を `window` に公開（zoom.js / overview.js から呼ぶ）
- `style.css`: `#zoom-layer{position:absolute;inset:0;transition:none}`、`body.zoomed #zoom-layer{will-change:transform}`、`.ov-*`（`.ov-chapter` セクション、sticky 見出し、`content-visibility:auto` セル）、`body.zoomed` / `body.overview-open` の pointer-events 群、`touch-action` 群、reduced-motion ブロックへの追加。
- `sw.js`: SHELL_ASSETS に 3 ファイル。
- CLAUDE.md: フロントエンド節の script 数、ロード順契約（overview.js の Esc は app.js より前）、`Zoom.reset` は `snapTo` 先頭、俯瞰は URL に載せない、viewport meta の判断。

## 6. テスト

`tests/test_viewer_overview.py`（新規、Chromium）。CDP `Input.dispatchTouchEvent` を 2 点で発行するヘルパ（俯瞰モード2 から移植）。俯瞰関連は `ios_page`（390x844）と `ipad_landscape_page`（1180x820）の両方で回す。

- ピンチ成立: 画面端 20px 起点の 2 点目が静止したまま 1 本指を 100px 動かすと、`Zoom.state().scale === 1` のまま**ページが進む**（既存スワイプが生き残る）。画面中央起点で両方 10px 以上動くとピンチ成立 / **スワイプ中にパーム側だけを離してもページは進まず、スワイプ指を離した時点で確定する**
- 初回描画: 初回ロード時に `document.body.className` が `portrait`（`ios_page`）／`landscape`（`ipad_landscape_page`）になっている（`updateOrientation` ガードの退行検知）
- 発火帯: 0.5 で開く / 0.51 は 190ms 後 rest / 離さず 0.5→0.8→0.5 で 2 回発火 / 発火時に `vibrate` が呼ばれる（`navigator.vibrate` をスタブ）
- 拡大: 2.0x の中点アンカー / 2.0 から縮めて 1.0 を跨いでも俯瞰へ飛ばない / `resize`（幅変更あり）で 1.0、幅変更なしの `resize` では維持
- スナップ中断: 左ゾーンタップ直後（スライド中）にピンチ開始 → `Zoom` 終了後の `currentIdx` と `#img-track` の位置が整合（中途半端で止まらない）
- 拡大中のゾーン: 2.0x で左ゾーン座標をタップしてもページが進まない / **中央タップで `scale === 1` に戻り、ツールバーの表示状態は変わらない**
- パン: 2→1 本指で `touchstart` 無しにパンが始まる / clamp で現在スプレッドがビューポートを覆い続ける / 端に触れてから右へ 80px でラバーバンド後 `goNext`、惰性では発火しない / 左端で右 = 次、右端で左 = 前 / **`ipad_landscape_page` の 1P モードで 1.5x（横に余地なし）→ 右へ 80px で次ページに進む**
- 2P: `ipad_landscape_page` で拡大すると単位がスプレッド全体
- リセット: 拡大後にキー・スライダーで次ページ → 1.0（`snapTo` 経由）/ Back（`resetToTop`）で拡大も俯瞰も消える / **`viewW`/`viewH` が変わらない `resize` を dispatch しても scale が維持される**（`updateOrientation` のガード）
- グリッド: 3 チャプター構成で spacer がセルにならない / 見出し 2 本が**各セクション内で** sticky / DOM 順が RTL / `.cur` が上から 1/3 / iPad 横で 7 列 / **全セルが 3:4** / セル `src` が `/thumb/` で `onerror` 後に `/image/` / **700 ページの合成ツリーで `<img>` を持つセルが ±200 の窓に限られ、スクロール長は全ページ分**
- 退出: セルタップで `snapTo` + 閉じる + クロスフェード要素が `load` 後に消える / **本画像が `error` でも 3 秒以内に消える** / 慣性中（scroll 直後）のタップは開かない / 退出後 400ms のクリックが進めない / Esc（サイドバーが閉じない）/ 下端ボタン / `G` 往復（Cmd+G は無視）/ 俯瞰内ピンチアウトで中点直下のセルに着地 / **入場ジェスチャを離さず広げても閉じない**
- 重なり: 俯瞰中にスライダー座標をタップしても `currentIdx` が変わらない / **俯瞰中に `#menu-btn` を押すと俯瞰が閉じてからサイドバーが開く**
- 俯瞰中に `#viewer` の幅を変えるとセル高が再計算される / 入場アニメ中に `close()` してもクローンが残らない
- 非干渉: 1 本指スワイプ・既存テスト全部が変更なしで通る
- ガード: `#viewer` 内の 2 本指 `touchmove`/`touchstart` は `defaultPrevented`、1 本指は false、サイドバー内の 2 本指は false、`gesturestart` は `#viewer` 内のみ `defaultPrevented`
- `images.length === 0` で `G` が no-op

Simulator 手動 QA（`zoom_episodes/series`、iPhone 17 と iPad）: ピンチインでグリッド、EP 見出し 3 本が sticky、右上が EP1 P1、タップで戻る（黒画面なし）、拡大 → 左ゾーンをタップしても進まない、端越えで次ページ 1.0、親指の付け根を置いたままスワイプできる、Safari のタブ一覧が出ない、`#page-slider` のつまみがドラッグできる。

## 7. スコープ外

グリッド密度切替 UI（列数は auto-fill で自動）、俯瞰内の見開きペア表示、デスクトップのホイールズーム、ダブルタップズーム、俯瞰の URL/履歴同期、退出時の FLIP（クロスフェードのみ）、慣性、`?dbg=1` 診断、片ページ単位の 2P 拡大。

## 8. 実装中に対処する Medium 事項（red team 3 巡目より。spec の変更は不要）

- 拡大中は `#zone-toggle` を 20% に広げる（39px は 44px 基準を割る。片手での唯一の解除経路）。
- 拡大中のパン終了 `suppressClickFor(400)` は `#zone-toggle` の「1.0 に戻す」タップを塞がないよう、`zoneToggle` 側だけ抑止を無視する。
- `body.zoomed` はピンチ成立（`onStart`）時点で付ける（`will-change` の昇格を最初の追従フレームから外す）。
- ±200 ウィンドウイングの出し入れは `IntersectionObserver`（root=`#overview`、rootMargin で先読み）。`content-visibility:auto` と重複するので実測して片方に寄せてよい。
- `contain` の適用: セルは `content-visibility:auto` + 固定高、`#overview` は `contain:layout paint`（`size` はスクロールコンテナに乗せない）。
- `#menu-btn` 排他は `_teardownViewer` フックと重複するが害はない。どちらか一方が動けば足りることをコメントで示す。
- 算術ドリフト先の計算に「先行チャプターの見出し高 + セクション padding 16px」を含める。実測補正の発動は先行構築完了時の 1 回。
- Simulator QA: 横持ち 1P・1.5x で斜めドラッグしたとき、横成分がパンに寄与せずラバーバンドだけが動くことを確認。

## 9. 未解決（実装時に実測して決める）

- viewport meta `user-scalable=no` の要否（§5.1-5）。→ **結論（2026-09-21 Simulator iOS 27 実測）: 不要。** ガード 1〜4 だけで scale 1.0 のピンチインは Safari のタブ一覧に化けず、俯瞰が開いた。meta は変更せず、ブラウズ UI のアクセシビリティズームを温存。
- `#page-slider` の `touch-action:pan-x` が iOS でつまみドラッグを壊さないか。→ **結論: 壊れない。** Simulator でつまみをドラッグして 2/7 → 6/7 に移動した。
- 縮小帯のセル中心の算術が 2px 以内で実測と一致するか。→ **結論: 算術は採用せず、先行構築完了時に 1 回 `.cur` を実測してキャッシュ**（`Overview.cellCenter()`、`markCurrent` でページ送りごとに更新）。`touchmove` 内の実測はゼロ。
- REARM(0.7) は俯瞰が開いた後は `onMove` の `Overview.isOpen()` 早期 return により到達しない（`images.length===0` で開けなかった場合のみ有効）。§6 の「離さず 0.5→0.8→0.5 で 2 回発火」は「2 回目は no-op」が実挙動。
- 実装時に判明した事実: `window.snapTo`/`goNext`/`goPrev` は spread.js の休眠ツインで、app.js のローカルが唯一の生きた経路。`window.viewerSnapTo`/`viewerGoNext`/`viewerGoPrev` として公開して使う。CDP `touchEnd.touchPoints` は**離す点**（Chromium 実装）。gesture.js の ratio は指の接地距離基準。
