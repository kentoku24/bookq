# bookq 用語集

bookq の画面・UI要素・機能・API・状態に正式な和名と英名を付け、コード上の対応（DOM id・関数名・エンドポイント・定数名）と一緒にまとめた用語集。用語の権威は「UI に実際に表示されているラベル」に置き、無名の機能だけ提案名を付ける。これで用語集が実装と1対1で対応し、将来コードが動いてもシンボル名で追跡できる。

## 命名規約

1. **UI 実ラベル優先**: `aria-label` / `title` 属性 / ボタン表記に表示されている文言をそのまま正式名とする。
2. **既存呼称次点**: README.md・docs/core-features.md で使われている呼称を次の候補とする。
3. **新造名**: UI にも既存ドキュメントにも呼び名がない機能には新造名を付け、「（仮称）」を添える。

## 対象範囲と対象外

対象はブラウザから見える画面・UI要素・操作・サーバー API・オフライン/PWA・状態管理・QA専用面。**対象外: デプロイ・インフラ用語（dev.sh / prd.sh / docker compose / tailscale ノード等）**。行番号は参照しない（すぐずれるため、シンボル名のみで追跡する）。

各エントリの「根拠」は対応する DOM id・関数名・エンドポイント・定数名。「説明」は一言のみ。

## 1. 画面 (Screens)

### ログイン画面 (Login Screen)
- **根拠**: `index.html` の `{% if not user %}` 分岐 / `.login-box` / `.login-btn` / `/static/login.js` / `index()` (app/routes.py)
- **説明**: 未認証でアクセスしたときに表示される bookq の入口画面。

### Googleサインインボタン (Sign-in Button)
- **根拠**: `.login-btn` / `/auth/login` （ラベル「Sign in with Google」）
- **説明**: ログイン画面中央にある Google OAuth 認証を開始するボタン。

### ビューア画面 (Viewer Screen)
- **根拠**: `index.html` の `{% else %}` 分岐 / `#viewer` / `#sidebar` / `#toolbar`
- **説明**: 認証後に表示されるメイン画面で、サイドバーとリーダー領域からなる。

### ブラウズ表示 (Browse View)
- **根拠**: `#tile-grid` / `loadFolders()` / `renderTiles()`
- **説明**: サイドバーにフォルダタイルを並べる既定の一覧表示。

### 階層ブラウズ (Subfolder Browse)
- **根拠**: `restoreBrowse()` / `returnToBrowse()` / `updateBrowseHeader()` / URL `#/browse/<path>`
- **説明**: フォルダを辿ってそのサブツリーだけを表示する階層ナビゲーション。

### 検索結果表示 (Search Results View)
- **根拠**: `doSearch()` / `GET /api/search` （該当なし時は「No results found」）
- **説明**: 検索語に一致する作品をライブラリ全体から一覧表示する画面状態。

### 閲覧表示・リーダー (Reader View)
- **根拠**: `loadImages()` / `snapTo()` / `#img-container` / `#img-track`
- **説明**: 選んだ作品のページを右開きで読む画面状態。

### 未選択時の案内 (Top Prompt)
- **根拠**: `resetToTop()` / `#empty` （ラベル「📂 Select a folder from the sidebar」）
- **説明**: 作品が未選択のときリーダー領域に表示される案内。

### フィルタボトムシート (Filter Bottom Sheet)
- **根拠**: `#bottom-sheet` / `#bottom-sheet-overlay`
- **説明**: 画面下からスライドアップして絞り込み条件を選ばせるパネル。

### オフラインフォールバック画面 (Offline Fallback Page)
- **根拠**: `app/static/offline.html` / Service Worker のナビゲーションフォールバック (`sw.js`)
- **説明**: オフライン時にナビゲーションが失敗したとき代わりに表示される静的ページ。

### オフライン検証ハーネス (Offline Harness) 【QA専用】
- **根拠**: `app/static/offline-harness.html` / `#harness-link`
- **説明**: QA専用。Service Worker・IndexedDB・ストレージの状態を一括可視化する検証ページ。

### 空状態群 (Empty States)
- **根拠**: `emptyBrowseMessage()` / `loadFolders()` 失敗分支 / `loadImages()` 空分支 （ラベル「📭 No images in this folder」「No folders match the active filter」「No results found」「Failed to load folders」）
- **説明**: 空フォルダ・絞り込み該当なし・検索該当なし・一覧取得失敗の各場面で出る案内表示の総称。

### エラー表示 (Error States)
- **根拠**: `showErrorToast()` / `loadImages()` catch分 / `#toast` （「⚠️ Failed to load images」+ 404/401/403 は permanent、ネットワーク/サーバーエラーは transient のトースト分類）
- **説明**: 画像読み込み失敗時に出るエラー文と、状況別に分かれたトースト通知の総称。

## 2. UI要素 (UI Controls)

### ブラウズヘッダー (Browse Header)
- **根拠**: `#browse-header` （ラベル「📁 Folders」、サブフォルダ閲覧中は ↩ 付きパンくずに変化） / `updateBrowseHeader()`
- **説明**: サイドバー上部の見出しで、階層の現在位置を示す。

### オンライン表示 (Online Indicator)
- **根拠**: `#offline-status` （ラベル「●オンライン」/「○オフライン」） / `updateOnlineStatus()`
- **説明**: ヘルスチェックプローブの結果を ●オンライン / ○オフライン で示す表示。

### 容量バー (Storage Bar)
- **根拠**: `#storage-bar` / `#storage-text` / `#storage-fill` / `updateStorageBar()`
- **説明**: 「使用中 X / Y GB」のテキストと使用率バーでオフライン保存の容量を示す。

### フィルターボタン (Filter Button)
- **根拠**: `#filter-btn` （グリフ 🔽、title="Filters"、aria-label="フィルタ"）
- **説明**: フィルタボトムシートを開くボタンで、適用中の絞り込みがあるときは色が変わる。

### 検索ボックス (Search Box)
- **根拠**: `#search-input` （placeholder「Search...」） / `doSearch()`
- **説明**: ライブラリ全体を対象に検索語を入力する入力欄。

### フィルタチップ (Filter Chips)
- **根拠**: `#filter-chips` （チップ 🎨アーティスト / 🏷️タグ、✕ で解除）
- **説明**: 適用中の絞り込み条件をチップ形式で一覧し、✕ で個別に解除できる。

### 更新インジケータ (Refresh Indicator) 【廃止 — Safari委譲】
- **根拠**: 廃止（旧 `#pull-indicator` / ラベル「Refreshing...」は Safari 標準に委譲し削除）
- **説明**: 旧カスタム表示は廃止。現在は Safari 標準のプルでページ自体が再読み込みされる。

### タイルグリッド (Tile Grid)
- **根拠**: `#tile-grid` / `renderTiles()`
- **説明**: フォルダと作品をサムネイル付きタイルで並べる一覧コンテナ。

### 表示切替ボタン (View Mode Button)
- **根拠**: `#footer-refresh-btn` （グリフ 🎨📋🃏、title="Switch view"、aria-label="表示切替"） / `updateFooterBtn()`
- **説明**: tile / list / card の表示モードを順に切り替えるサイドバーフッターのボタン。

### 件数表示 (Folder Count)
- **根拠**: `#folder-count` （初期ラベル「0 folders」）
- **説明**: 表示中のフォルダ件数を示すサイドバーフッターの表示。

### ハーネスリンク (Harness Link)
- **根拠**: `#harness-link` （ラベル「🧪 Harness」、gated 表示）
- **説明**: オフライン検証ハーネスへのリンクで、QA専用のため条件を満たしたときだけ表示される。

### ログアウトボタン (Logout Button)
- **根拠**: `#logout-btn` / `GET /auth/logout` （グリフ 🚪、title="Logout"、aria-label="ログアウト"）
- **説明**: セッションを破棄してログイン画面へ戻すボタン。

### シートオーバーレイ (Bottom Sheet Overlay)
- **根拠**: `#bottom-sheet-overlay`
- **説明**: フィルタボトムシート表示中に背後を覆う暗幕で、タップでシートを閉じられる。

### シート閉じるボタン (Sheet Close Button)
- **根拠**: `#bottom-sheet-close` （グリフ ✕、aria-label="閉じる"）
- **説明**: フィルタボトムシートのヘッダ右にある閉じるボタン。

### Artistセクション (Artist Section)
- **根拠**: `#facet-artists` （見出し「Artist」）
- **説明**: ボトムシート内でアーティストの選択肢をチップ表示するセクション。

### Tagsセクション (Tags Section)
- **根拠**: `#facet-tags` （見出し「Tags」）
- **説明**: ボトムシート内でタグの選択肢をチップ表示するセクション。

> **Removed注記**: 旧 third-facet セクション（パロディ）は削除。keeperファセットはartist/tagのみ。旧sortキー rating/newest、旧relevance重み character+5、旧metadata拡張 cover_url/favorites等も削除（詳細はgit履歴）。

### Sortセクション (Sort Section)
- **根拠**: `#facet-sort` （見出し「Sort」、radio: relevance のみ）
- **説明**: 関連度順のみを選ぶセクション（既定は関連度順）。

### Resetボタン (Reset Button)
- **根拠**: `#filter-reset-btn` （ラベル「Reset」）
- **説明**: ボトムシート内の選択状態をすべて解除するボタン。

### Applyボタン (Apply Button)
- **根拠**: `#filter-apply-btn` （ラベル「Apply」）
- **説明**: 選んだ絞り込み条件をブラウズ表示へ反映するボタン。

### メニューボタン (Menu Button)
- **根拠**: `#menu-btn` （グリフ ☰、title="Folders (F)"、aria-label="フォルダ一覧"） / `toggleSidebar()`
- **説明**: リーダーツールバーでサイドバーの開閉を切り替えるボタン（F キーでも操作可）。

### 作品名表示 (Folder Name Display)
- **根拠**: `#folder-name`
- **説明**: ツールバー中央に現在開いている作品名を表示する。

### ページスライダー (Page Slider)
- **根拠**: `#page-slider` （RTL、右端=1ページ目、title="Page slider — right=first, left=last"）
- **説明**: 右端が1ページ目の右開きスライダーで、ドラッグで任意ページへ移動できる。

### ページカウンタ (Page Counter)
- **根拠**: `#counter` （書式「N / M」、現在ページの画像読み込みに失敗すると ⚠ を付与） / `snapTo()`
- **説明**: 現在ページと総ページ数を「N / M」形式で示す表示。

### スピナー (Spinner)
- **根拠**: `#spinner`
- **説明**: 画像読み込み中にリーダー中央へ出るローディング表示。

### ページストリップ (Page Strip)
- **根拠**: `#img-track`
- **説明**: 全ページを横一列に並べたストリップ本体で、translateX で動く。

### タップゾーン群 (Tap Zones)
- **根拠**: `#zone-prev` （左45%=次ページ、title="Previous"） / `#zone-toggle` （中央10%、title="Toggle toolbar"） / `#zone-next` （右45%=前ページ、title="Next"）
- **説明**: 右開きに合わせて左タップで送り・右タップで戻す透明な操作レイヤーの総称。

### ヒント (Hint)
- **根拠**: `#hint` （ラベル「Swipe / ← → to flip pages | Center tap → hide toolbar」）
- **説明**: リーダー下部に出る操作ガイド表示。

### 見開きずらしボタン (Spread Shift Button)
- **根拠**: `#spread-shift` （グリフ ◀、title="Shift spread pairing"、aria-label="見開きペアリング切替"）
- **説明**: 表紙ソロなど見開きのペアリングを1ページずらすボタン。

### ページモード切替ボタン (Spread Toggle Button)
- **根拠**: `#spread-toggle` （グリフ 2P⇄1P、title="Toggle 1-page / 2-page"、aria-label="ページモード切替"）
- **説明**: 2ページ見開きと1ページ表示を切り替えるボタン。

### トースト (Toast)
- **根拠**: `#toast` （role="alert"） / `showErrorToast()`
- **説明**: エラーや完了などを一瞬表示する通知領域。

### 再開ピル (Resume Pill)
- **根拠**: `#resume-btn` / `#resume-btn-text` / `showResumeBtn()` / `hideResumeBtn()` / `updateResumeButton()`
- **説明**: 前回の続きから再開できることを促すピル型ボタン。

## 3. 操作・機能 (Interactions & Features)

### 自然順ソート (Natural Sort)
- **根拠**: `natural_key()` (app/scanner.py) / `_naturalSortKey()` (app.js) / `scan_tree()`
- **説明**: 埋め込み数字を数値として比較し「1 < 2 < 10」の順にファイル名・フォルダ名を並べる。

### 漢数字正規化 (Kanji Numeral Normalization)
- **根拠**: `_normalizeKanji()` (app.js) / `natural_key()` (app/scanner.py)
- **説明**: 第X部・第X話の漢数字を文脈保護付きで数値化してソート順を揃える（固有名詞は保護）。

### 単一子フラット化 (Single-child Flattening)
- **根拠**: `scan_tree()` (app/scanner.py)
- **説明**: 画像を持たず子フォルダが1つだけのフォルダを潰して、深いチェーンを1行にまとめる。

### ドリルダウン (Drill-down)
- **根拠**: `navigateToFolder()` / URL `#/browse/<path>`
- **説明**: フォルダタイルをタップしてサブフォルダ階層へ降りていく操作。

### パンくず戻り (Breadcrumb-back)
- **根拠**: `returnToBrowse()` / `updateBrowseHeader()` （↩ ボタン）
- **説明**: ブラウズヘッダーの ↩ で祖先フォルダへ一気に戻る操作。

### プルツーリフレッシュ (Pull-to-refresh) 【Safari委譲】
- **根拠**: Safari 標準に委譲（app.js の独自閾値・再取得は廃止）
- **説明**: 一覧最上部のプルはブラウザ標準に委譲しページ再読み込みになる（独自の 40px 閾値・一覧再取得は廃止）。下端 18px ラバーバンド、左右スワイプ 60px/48px は保持。

### スクロール位置復元 (Scroll Position Restore)
- **根拠**: `scrollPositions` / `restoreBrowse()`
- **説明**: 階層を行き来しても一覧のスクロール位置を記憶して戻す。

### 表示モード切替 (View Mode Cycle)
- **根拠**: `viewMode` / `updateFooterBtn()` / `#footer-refresh-btn` （tile 🎨 → list 📋 → card 🃏）
- **説明**: tile / list / card の3表示モードをボタン1つで順に巡る。

### マルチサムネイルグリッド (Multi-thumbnail Grid)
- **根拠**: `renderTiles()` / `thumbs` （`scan_tree()` が直下2個以上のとき最大9件添付）
- **説明**: サブフォルダのサムネイルを最大9枚のグリッドで見せるタイル表示。

### ライブラリ検索 (Library Search)
- **根拠**: `doSearch()` / `GET /api/search` （debounce 300ms）
- **説明**: 検索ボックスの入力を300msデバウンスしてライブラリ全体を検索する。

### ファセット絞り込み (Facet Filtering)
- **根拠**: `loadFacets()` / `nodeMatchesFilters()` / `GET /api/metadata/facets?path=`
- **説明**: アーティスト・タグで、ブラウズ中のサブツリーに限定して絞り込む。

### ソート指定 (Sort Options)
- **根拠**: `#facet-sort` / `GET /api/search` の `sort` パラメータ（sort=relevance のみ）
- **説明**: 検索結果の並び順は関連度のみ。同点時は自然順で安定化する。

### 関連度スコアリング (Relevance Scoring)
- **根拠**: `_calculate_relevance()` (app/metadata.py) （title+10 / artist+8 / tag+3、複数フィールドボーナス +2/field）
- **説明**: 検索語のヒットフィールドごとに重み付けし、複数フィールド一致にボーナスを与える。

### 右開ナビゲーション (RTL Manga Navigation)
- **根拠**: `goNext()` / `goPrev()` / `#zone-prev` / `#zone-next` / `PREFETCH_AHEAD = 8` / `PREFETCH_BEHIND = 3` / `EVICT_AHEAD = 14` / `EVICT_BEHIND = 6` / `POOL_SIZE = 6`
- **説明**: 左タップ・←キーで次ページ、右タップ・→キーで前ページという右開きのページ送り（前後ページを先読みし、範囲外のDOMセルを再利用する）。

### 指追従スワイプ (Finger-follow Swipe)
- **根拠**: タッチ処理 (app.js) （軸ロック8px・ラバーバンド0.35倍・閾値 min(10vw, 70px)）
- **説明**: 指にストリップが追従し、端では0.35倍の抵抗を感じるスワイプ操作。

### モメンタム送り (Momentum Flick)
- **根拠**: `MOMENTUM_VELOCITY_THRESHOLD = 0.05` px/ms (app.js)
- **説明**: 速いフリックの残速で複数ページを一気に送る。

### キーボード操作 (Keyboard Shortcuts)
- **根拠**: keydown ハンドラ (app.js) （←=次 / →=前 / F=サイドバー / Home / End / Esc / Enter・Space）
- **説明**: 矢印や F、Home / End、Esc などでリーダーとサイドバーを操作する。

### ツールバー切替 (Toolbar Toggle)
- **根拠**: `#zone-toggle` / `toggleSidebar()`
- **説明**: リーダー中央タップでツールバー類の表示を切り替える。

### 見開きモード (Spread Mode)
- **根拠**: `landscapeSpread` / `pageMode` / `rebuildSpreads()`
- **説明**: 横画面で既定ONになる2ページ見開き表示。

### 表紙ソロ (Cover-solo Shift)
- **根拠**: `spreadOffset` / `#spread-shift`
- **説明**: 表紙を単独表示させ、以降の見開きペアリングを1ページずらす。

### コンテインフィット (Contain-fit Sizing)
- **根拠**: `spreadMetrics()` / `pageAR` / `GAP_INNER = 5` / `GAP_OUTER = 10`
- **説明**: ページの実アスペクト比からトリミングなしでビューポートに収まる大きさをJSで計算する。

### アンカー配置 (Anchored Positioning)
- **根拠**: `anchorSpread` / `spreadLeft()` / `rebaseAnchor()`
- **説明**: ストリップ位置を読書中の見開き基準で測り、遅延ロードでも現在ページが動かないようにする。

### 章連結 (Cross-chapter Concatenation)
- **根拠**: `getSiblingLeafFolders()` / `__spacer__` / `CHAPTER_GAP_PX = 20` / `SIBLING_AUTOLOAD_MIN_DEPTH = 2`
- **説明**: 兄弟チャプターを1つのストリップに連結し、章境界にギャップとラベルを挿入する。

### しおり再開 (Resume)
- **根拠**: `ReadingProgress` / `updateResumeButton()` / `loadImages()` 復元分支
- **説明**: localStorage の続きから再開を促し、URLページ指定時はそちらを優先する。

### ディープリンク (Deep Linking)
- **根拠**: URL `#/view/<folder>?p=<n>` / `#/browse/<path>` / `loadImages()` 復元分支
- **説明**: ハッシュURLから任意の作品・ページ・階層へ直接移動できる。

## 4. サーバー・API概念 (Server & API Concepts)

### ビューアホーム (Viewer Home)
- **根拠**: `GET /` / `index()` (app/routes.py)
- **説明**: 認証状態に応じてログイン画面かビューア画面を返すアプリの入口エンドポイント。

### ヘルスチェック (Health Check)
- **根拠**: `GET /api/health` / `health()` （認証不要。`?online_check=1` プローブ変種は SW をバイパスする専用経路）
- **説明**: サーバーの生存確認に使う認証不要エンドポイント。

### フォルダツリーAPI (Folder Tree API)
- **根拠**: `GET /api/folders` / `api_folders()` / `_ensure_index()` / `INDEX_CACHE_TTL = 300`
- **説明**: フォルダツリーにメタデータと subtree_facets を添付して返す（索引は300秒キャッシュ）。

### 検索API (Search API)
- **根拠**: `GET /api/search` / `search()` （パラメータ q / artist / tag / sort=relevance）
- **説明**: 自由語とファセット条件で検索し、関連度順に並び替える。

### ファセットAPI (Facets API)
- **根拠**: `GET /api/metadata/facets?path=` / `get_facets()` / `facets_for()` (app/metadata.py)
- **説明**: フィルタ選択肢（アーティスト・タグ）を path 指定でサブツリー限定取得できる。

### 画像一覧API (Image List API)
- **根拠**: `GET /api/images?folder=` / `api_images()` / `list_images()` (app/scanner.py)
- **説明**: 指定フォルダ直下の画像一覧を返す（再帰しない）。

### 画像配信 (Image Serving)
- **根拠**: `GET /image/{path}` / `serve_image()` （ETag / Last-Modified / 304 / Cache-Control max-age=3600）
- **説明**: 画像ファイルを条件付きリクエスト対応・1時間キャッシュ付きで配信する。

### SW配信エンドポイント (SW Serving Route)
- **根拠**: `GET /sw.js` / `serve_sw()` （`Service-Worker-Allowed: /` ヘッダ付き FileResponse、`/static/sw.js` も同一内容）
- **説明**: Service Worker を scope `/` で登録できるようルートから配信する。

### Google OAuth認証 (Google OAuth Authentication)
- **根拠**: `/auth/login` / `/auth/callback` / `/auth/logout` / `login()` / `auth_callback()` / `logout()` (app/auth.py) / `ALLOWED_EMAILS` → `allowed_emails_set`
- **説明**: Google OAuth でサインインさせ、メール許可リスト外は403で拒否する。

### テストモードバイパス (Test Mode Bypass)
- **根拠**: `BOOKQ_TEST_MODE` / `_TEST_MODE` (app/auth.py) / `get_current_user()` / `require_auth()`
- **説明**: import時に読み込んだフラグが立っていると認証をバイパスしてモックユーザーを返す。

### コミックメタデータ (Comic Metadata)
- **根拠**: `metadata.toml` → `ComicMetadata` / `from_folder()` (app/metadata.py) （keeper fields: folder_path / folder_name / title_jp / title_en / artist / tags / source-generic / has_metadata / total_pages のみ）
- **説明**: 各フォルダの metadata.toml を構造化したコミック情報モデル（keeper項目のみ保持）。

### サブツリーファセット集計 (Subtree Facets)
- **根拠**: `subtree_facets()` (app/metadata.py) / `_attach_subtree_facets()` (app/routes.py)
- **説明**: 各ノードにそのサブツリー内のファセット候補を事前集計して添付する。

### フォルダツリースキャン (Folder Tree Scan)
- **根拠**: `scan_tree()` / `list_images()` (app/scanner.py) （拡張子8種・ドットファイル除外・thumb/thumbs 最大9件）
- **説明**: downloads 配下を走査して {name, type, children} のツリーを組み立てる。

### パス閉じ込め (Path Containment)
- **根拠**: `serve_image()` / `api_images()` （resolve後の前方一致チェック）
- **説明**: リクエストパスが downloads_dir 内に収まることを確認し、逸脱は403にする。

## 5. オフライン・PWA (Offline & PWA)

### Service Worker登録 (SW Registration)
- **根拠**: `registerSW()` (app.js) / `?offline=1` / `localStorage.bookq_enable_sw` / `updatefound` / `controllerchange` （hadController のときのみ再読み込み）
- **説明**: Secure Context でのみ SW を登録し、更新検知(updatefound)でトーストを出し、controllerchange 再読み込みは既存コントローラがあるときだけ行う。

### シェルプレキャッシュ (Shell Precache)
- **根拠**: `SHELL_CACHE = "bookq-shell-v1"` / `SHELL_ASSETS = ["/", "/static/offline.html", "/static/manifest.json"]` (sw.js install)
- **説明**: インストール時にアプリシェル3ファイルを先にキャッシュする。

### 画像CacheFirst戦略 (Image CacheFirst)
- **根拠**: `perWorkCacheName()` → `cacheNameFor()` → `bookq-work-<encoded>-v1` / フォールバック `bookq-images-fallback` (sw.js) （`/image/` と `/thumb/` の両方が作品キャッシュに入る。206 Range応答は非保存）
- **説明**: 作品ごとのキャッシュを優先して画像を返し、ミス時はネットワーク取得（`cache: "no-store"` でHTTPキャッシュを迂回）を保存する。ヒット時は次項の再検証を通す。サムネイルは `prefetch.js` が書くのと同じ作品キャッシュに保存し、`caches.match()` が返すコピーと書き込み先が食い違わないようにする。

### 画像キャッシュ再検証 (Image Cache Revalidation)
- **根拠**: `revalidateRequest()` / `revalidationRequest()` / `REVALIDATE_TIMEOUT_MS = 1200` (sw.js) （ヒット時に `If-None-Match`/`If-Modified-Since` で条件付きGET。304 はキャッシュ継続、200 は差し替えて新バイトを返す。オフライン・タイムアウト・非OKはキャッシュにフォールバック）
- **説明**: 作品の画像URLは差し替え後も不変なため、キャッシュを無条件に返すとライブラリ側で直した画像が端末へ永久に届かない（2026-10-13 の実機報告）。ヒット時は必ず再検証し、304 では本文を転送し直さない。**同一オリジンの再検証リクエストは URL から `mode: "same-origin"` で組み直す**: `<img>` は `no-cors` で届き、no-cors の Request を再構成するとヘッダ guard が CORS セーフリスト外のヘッダ（`If-None-Match` 等）を例外なく捨てるため、そのままでは条件付きGETにならず毎回本文全体を再取得してしまう（＝遅い回線では 1200ms のタイムアウトで差し替えが反映されない）。

### 自動キャッシュ (Auto-cache)
- **根拠**: sw.js fetch ハンドラの CacheFirst 保存処理 / `PREFETCH_AHEAD = 8` (app.js)
- **説明**: 閲覧したページを操作なしで保存し、容量不足でもダイアログを出さず静かにスキップする。

### API NetworkFirst戦略 (API NetworkFirst)
- **根拠**: `API_CACHE = "bookq-api-v1"` (sw.js) （`/api/folders` は失敗時キャッシュへ特別フォールバック）
- **説明**: API はまずネットワークからの取得を試み、失敗時はキャッシュへフォールバックする。

### ナビゲーションフォールバック連鎖 (Navigate Fallback Chain)
- **根拠**: sw.js navigate ハンドラ （同一URL → ignoreSearch → `/static/offline.html` → precache済み `/`）
- **説明**: オフライン時のページ遷移をキャッシュ→オフライン画面→トップの順にフォールバックさせる。

### レンジバイパス (Range Bypass)
- **根拠**: sw.js fetch ハンドラ冒頭 （Range リクエストと non-GET をキャッシュ外へ直通）
- **説明**: Range 付きリクエストをキャッシュを介さずネットワークへ直接渡す。

### オンラインチェックプローブ (Online-check Probe)
- **根拠**: `GET /api/health?online_check=1` / `X-Online-Check` ヘッダ （sw.js が network-only でバイパス） / `updateOnlineStatus()`
- **説明**: SW キャッシュを経由しない専用経路でオンライン判定を行う。

### 明示ダウンロード (Explicit Download)
- **根拠**: `downloadWork()` (app.js) / `GET /api/images?folder=`
- **説明**: 作品単位で全画像を取得して Cache Storage へ保存する。

### リーフ列挙 (Leaf Enumeration)
- **根拠**: `getLeafPathsFor()` / `leafPathsCache`
- **説明**: ダウンロード対象となる末端フォルダ一覧を導出してメモ化する。

### 進捗リング (Progress Ring)
- **根拠**: conic-gradient リング (app.js `offlineControlsHtml()` 周辺)
- **説明**: ダウンロード進捗をサムネ中央の円形リングで示す。

### 容量不足時の最古作品削除確認 (Quota Eviction Confirm)
- **根拠**: quota 処理 (app.js `downloadWork()` 内) （確認文「容量不足: 最も古い作品「...」を削除して続行しますか？」・1回だけ再試行）
- **説明**: 明示ダウンロードの容量不足時に最古作品の削除を確認し、承諾で1回だけ再試行する。

### オフライン削除 (Offline Delete)
- **根拠**: `deleteWork()` (app.js) （`caches.delete` + IndexedDB メタ削除）
- **説明**: 作品キャッシュとメタをまとめて解放する。

### 全データ削除 ／ 全消去 (Wipe All)
- **根拠**: `OfflineCore.wipeTargets()` / `OfflineCore.wipeClientData()` (offline-core.js) と ⑥ 全消去の `doWipeAll()` (offline-harness.html) （`bookq-work-*` / `bookq-api-*` / `bookq-images-*` を削除、`bookq-shell-*` は保持。IndexedDB は作品レコード+ツリー スナップショットを `OfflineStore.clear()` で消し、`bookq_progress` 等の読書データも削除）
- **説明**: 端末のデータをまとめて消す出口。**IndexedDB レコードを持たない「閲覧だけで作られた `bookq-work-*`」も対象**に含む（保存作品が0件でもボタンが有効）。QA フラグ（`bookq_harness` / `bookq_enable_sw` / `bookq_touch` / `bookq_perf`）とアプリ本体の SHELL は残す。ブラウザのメモリキャッシュ／HTTPキャッシュは消せないため、**既に画面に出ている画像はアプリを開き直すまで古いままになり得る**（2026-10-15 の実機確認: 消去直後の同一セッションでは旧バイト、Safari を終了して開き直すと新バイト）。

### オフラインバッジと操作ボタン (Offline Badge & Controls)
- **根拠**: `offlineControlsHtml()` / `#tile-grid` への委譲リスナ （✓オフライン / ⬇︎ / DL / 削除）
- **説明**: 各作品タイルに保存状態バッジと DL・削除ボタンを表示する。

### 容量バー更新 (Storage Bar Update)
- **根拠**: `updateStorageBar()` / `navigator.storage.estimate()` （iOS 0/0 フォールバック・80%黄90%赤）
- **説明**: 使用容量と使用率バーを更新し、80%で黄、90%で赤に変える。

### 永続化要求 (Persistence Request)
- **根拠**: `ensurePersist()` / `navigator.storage.persist()` / `bookq_persist_toast` （Safari「オフラインを保持するにはホーム画面に追加してください」案内）
- **説明**: 初回操作時に永続化を要求し、得られない Safari ではホーム画面追加を案内する。

### Safari LRUタッチ (Safari Cache Touch)
- **根拠**: `scheduleOfflineTouch()` (app.js)
- **説明**: 起動時に保存済み作品のキャッシュへ1件アクセスして最終使用時刻を更新し（touch）、Safari の7日LRU削除対象から外す。

### オフライン閲覧復旧 (Offline Browse Recovery)
- **根拠**: `loadFolders()` 復旧分支 / `bookq-api-v1`
- **説明**: オフライン起動時に API キャッシュから一覧を復元して閲覧を続けられる。

### PWAマニフェスト (PWA Manifest)
- **根拠**: `/static/manifest.json` （display: standalone / theme #0f172a / icons 192・512 maskable）
- **説明**: ホーム画面追加可能な最小 PWA 構成を宣言する。

## 6. 状態管理・ストレージ (State & Storage)

### ビューアURL状態 (Viewer URL State)
- **根拠**: URL `#/view/<folder>?p=<n>` / `parseViewerHash()` / `replaceState`
- **説明**: 表示中の章パスとページをハッシュに保持し、ページ送りでは履歴を増やさず現在エントリを置き換える。

### ブラウズURL状態 (Browse URL State)
- **根拠**: URL `#/browse/<path>` / `browseHash()` （ルート = hashなし）
- **説明**: ブラウズ中の階層パスをハッシュに保持する（最上位はハッシュなし）。

### 履歴スタック (History Stack)
- **根拠**: `urlStack` / `reconcileUrlStack()`
- **説明**: 戻る操作を 本→階層→最上位 の順で辿れるよう自前管理する。

### 復元トークン (Restore Token)
- **根拠**: `urlRestoreGen` / `applyUrlState()`
- **説明**: 世代トークンで競合した復元処理を破棄し、古いロードが後から書き込まないようにする。

### OAuth復帰ブリッジ (Return-hash Bridge)
- **根拠**: sessionStorage `bookq_return_hash` （10分TTL）
- **説明**: ログイン前のディープリンクを OAuth 往復の間だけ退避して復帰させる。

### しおりストア (Reading Progress Store)
- **根拠**: localStorage `bookq_progress` / `ReadingProgress` （最大50件）
- **説明**: 作品ごとの続きページを保存し、上限50件で古いものから落とす。

### 表示設定ランタイム (Layout Prefs)
- **根拠**: `pageMode` / `spreadOffset` / `viewMode` （URL非保存・ランタイムのみ）
- **説明**: ページモードや表示モードはURLに載せず、実行中だけ有効にする。

### SW有効化フラグ (Test SW Enable Flag)
- **根拠**: `?offline=1` / `localStorage.bookq_enable_sw`
- **説明**: テストモードでも Service Worker 登録を強制できるゲート。

### 永続化案内フラグ (Persist Toast Flag)
- **根拠**: localStorage `bookq_persist_toast`
- **説明**: Safari への永続化案内トーストを一度だけ出すための記憶。

### オフラインメタDB (Works DB)
- **根拠**: IndexedDB `bookq-offline` / `works` ストア keyPath=`folder` / `OfflineStore` (offline-core.js) （fields: cacheName / pageCount / cachedCount / bytes / thumb / addedAt / lastReadAt）
- **説明**: 作品ごとのオフライン状態メタをブラウザ内に永続化する。

### 作品キャッシュ名 (Work Cache Name)
- **根拠**: `cacheNameFor(folder)` → `"bookq-work-" + encodeURIComponent(folder) + "-v1"` (offline-core.js)
- **説明**: フォルダパスから作品専用キャッシュ名を一意に生成する。

## 7. QA専用面 (QA-only Surfaces)

### ハーネス状態パネル (Status Panel) 【QA専用】
- **根拠**: `#harness-status` / `#harness-cache-count` / `#harness-idb-table` / `#harness-idb-dedup` / `#harness-storage-raw` / `#harness-storage-diff` / `#harness-leaf-count`
- **説明**: QA専用。キャッシュ数・IndexedDB 全件と dedup 合計・storage 生値と差分・leaf 件数を一画面に可視化する。

### ハーネス操作パネル (Ops Panel) 【QA専用】
- **根拠**: `#btn-health-abort` 「health abortでオフライン」（AbortController で health fetch を中断） / `#btn-set-offline` 「全断(setOffline)」（window offline イベント発火）
- **説明**: QA専用。ネットワークを切らずにオフライン状態を擬制する2ボタン。

### ハーネス対象選択 (Target Select) 【QA専用】
- **根拠**: `#harness-target-select`
- **説明**: QA専用。leaf enumeration の対象フォルダを選ぶセレクト。

### ヘルスシグナル監視 (Health Signal Probes) 【QA専用】
- **根拠**: harness inline script （`GET /api/health?online_check=1` を10並列・1200msタイムアウト）
- **説明**: QA専用。複数プローブの並列結果からオンライン判定シグナルを作る。

### ハーネスゲート (Harness Gate) 【QA専用】
- **根拠**: `?harness=1` / `localStorage.bookq_harness` / `__BOOKQ_TEST_MODE__` （`#harness-link` の表示条件）
- **説明**: QA専用。条件を満たしたときだけビューア内リンクを表示する配信ゲート。

### テストモードフラグ (Test Mode Flag) 【QA専用】
- **根拠**: `window.__BOOKQ_TEST_MODE__` （index.html が routes.py 経由の `auth._TEST_MODE` から注入）
- **説明**: QA専用。テストモードかどうかをフロントエンド全体で共有するフラグ。

## 索引

五十音順（英数字は先頭）。

| 和名 | 英名 | カテゴリ |
| --- | --- | --- |
| API NetworkFirst戦略 | API NetworkFirst | 5. オフライン・PWA |
| Artistセクション | Artist Section | 2. UI要素 |
| Applyボタン | Apply Button | 2. UI要素 |
| Google OAuth認証 | Google OAuth Authentication | 4. サーバー・API概念 |
| Googleサインインボタン | Sign-in Button | 1. 画面 |
| OAuth復帰ブリッジ | Return-hash Bridge | 6. 状態管理・ストレージ |
| PWAマニフェスト | PWA Manifest | 5. オフライン・PWA |
| Resetボタン | Reset Button | 2. UI要素 |
| Safari LRUタッチ | Safari Cache Touch | 5. オフライン・PWA |
| Service Worker登録 | SW Registration | 5. オフライン・PWA |
| SW配信エンドポイント | SW Serving Route | 5. オフライン・PWA |
| SW有効化フラグ | Test SW Enable Flag | 6. 状態管理・ストレージ |
| Sortセクション | Sort Section | 2. UI要素 |
| Tagsセクション | Tags Section | 2. UI要素 |
| アンカー配置 | Anchored Positioning | 3. 操作・機能 |
| 永続化案内フラグ | Persist Toast Flag | 6. 状態管理・ストレージ |
| 永続化要求 | Persistence Request | 5. オフライン・PWA |
| オフライン閲覧復旧 | Offline Browse Recovery | 5. オフライン・PWA |
| エラー表示 | Error States | 1. 画面 |
| 閲覧表示・リーダー | Reader View | 1. 画面 |
| オフライン検証ハーネス | Offline Harness | 1. 画面 |
| オフライン削除 | Offline Delete | 5. オフライン・PWA |
| オフラインバッジと操作ボタン | Offline Badge & Controls | 5. オフライン・PWA |
| オフラインフォールバック画面 | Offline Fallback Page | 1. 画面 |
| オフラインメタDB | Works DB | 6. 状態管理・ストレージ |
| オンラインチェックプローブ | Online-check Probe | 5. オフライン・PWA |
| オンライン表示 | Online Indicator | 2. UI要素 |
| 階層ブラウズ | Subfolder Browse | 1. 画面 |
| 画像一覧API | Image List API | 4. サーバー・API概念 |
| 画像CacheFirst戦略 | Image CacheFirst | 5. オフライン・PWA |
| 画像配信 | Image Serving | 4. サーバー・API概念 |
| 漢数字正規化 | Kanji Numeral Normalization | 3. 操作・機能 |
| 関連度スコアリング | Relevance Scoring | 3. 操作・機能 |
| キーボード操作 | Keyboard Shortcuts | 3. 操作・機能 |
| 空状態群 | Empty States | 1. 画面 |
| 検索API | Search API | 4. サーバー・API概念 |
| 検索結果表示 | Search Results View | 1. 画面 |
| 検索ボックス | Search Box | 2. UI要素 |
| 件数表示 | Folder Count | 2. UI要素 |
| 更新インジケータ | Refresh Indicator | 2. UI要素 |
| コミックメタデータ | Comic Metadata | 4. サーバー・API概念 |
| コンテインフィット | Contain-fit Sizing | 3. 操作・機能 |
| 再開ピル | Resume Pill | 2. UI要素 |
| 未選択時の案内 | Top Prompt | 1. 画面 |
| 作品キャッシュ名 | Work Cache Name | 6. 状態管理・ストレージ |
| 作品名表示 | Folder Name Display | 2. UI要素 |
| サブツリーファセット集計 | Subtree Facets | 4. サーバー・API概念 |
| シェルプレキャッシュ | Shell Precache | 5. オフライン・PWA |
| しおり再開 | Resume | 3. 操作・機能 |
| しおりストア | Reading Progress Store | 6. 状態管理・ストレージ |
| 自然順ソート | Natural Sort | 3. 操作・機能 |
| 指追従スワイプ | Finger-follow Swipe | 3. 操作・機能 |
| 自動キャッシュ | Auto-cache | 5. オフライン・PWA |
| シートオーバーレイ | Bottom Sheet Overlay | 2. UI要素 |
| シート閉じるボタン | Sheet Close Button | 2. UI要素 |
| 章連結 | Cross-chapter Concatenation | 3. 操作・機能 |
| 進捗リング | Progress Ring | 5. オフライン・PWA |
| スクロール位置復元 | Scroll Position Restore | 3. 操作・機能 |
| スピナー | Spinner | 2. UI要素 |
| ソート指定 | Sort Options | 3. 操作・機能 |
| タイルグリッド | Tile Grid | 2. UI要素 |
| タップゾーン群 | Tap Zones | 2. UI要素 |
| 単一子フラット化 | Single-child Flattening | 3. 操作・機能 |
| ツールバー切替 | Toolbar Toggle | 3. 操作・機能 |
| ディープリンク | Deep Linking | 3. 操作・機能 |
| テストモードバイパス | Test Mode Bypass | 4. サーバー・API概念 |
| テストモードフラグ | Test Mode Flag | 7. QA専用面 |
| トースト | Toast | 2. UI要素 |
| ドリルダウン | Drill-down | 3. 操作・機能 |
| ナビゲーションフォールバック連鎖 | Navigate Fallback Chain | 5. オフライン・PWA |
| パス閉じ込め | Path Containment | 4. サーバー・API概念 |
| パンくず戻り | Breadcrumb-back | 3. 操作・機能 |
| ハーネスゲート | Harness Gate | 7. QA専用面 |
| ハーネス操作パネル | Ops Panel | 7. QA専用面 |
| ハーネス状態パネル | Status Panel | 7. QA専用面 |
| ハーネス対象選択 | Target Select | 7. QA専用面 |
| ハーネスリンク | Harness Link | 2. UI要素 |
| ヒント | Hint | 2. UI要素 |
| ビューアURL状態 | Viewer URL State | 6. 状態管理・ストレージ |
| ビューアホーム | Viewer Home | 4. サーバー・API概念 |
| ビューア画面 | Viewer Screen | 1. 画面 |
| 表示切替ボタン | View Mode Button | 2. UI要素 |
| 表示設定ランタイム | Layout Prefs | 6. 状態管理・ストレージ |
| 表紙ソロ | Cover-solo Shift | 3. 操作・機能 |
| 表示モード切替 | View Mode Cycle | 3. 操作・機能 |
| ファセットAPI | Facets API | 4. サーバー・API概念 |
| ファセット絞り込み | Facet Filtering | 3. 操作・機能 |
| フィルタチップ | Filter Chips | 2. UI要素 |
| フィルターボタン | Filter Button | 2. UI要素 |
| フィルタボトムシート | Filter Bottom Sheet | 1. 画面 |
| フォルダツリーAPI | Folder Tree API | 4. サーバー・API概念 |
| フォルダツリースキャン | Folder Tree Scan | 4. サーバー・API概念 |
| 復元トークン | Restore Token | 6. 状態管理・ストレージ |
| プルツーリフレッシュ | Pull-to-refresh | 3. 操作・機能 |
| ページカウンタ | Page Counter | 2. UI要素 |
| ページストリップ | Page Strip | 2. UI要素 |
| ページスライダー | Page Slider | 2. UI要素 |
| ページモード切替ボタン | Spread Toggle Button | 2. UI要素 |
| ヘルスシグナル監視 | Health Signal Probes | 7. QA専用面 |
| ヘルスチェック | Health Check | 4. サーバー・API概念 |
| ブラウズURL状態 | Browse URL State | 6. 状態管理・ストレージ |
| ブラウズ表示 | Browse View | 1. 画面 |
| ブラウズヘッダー | Browse Header | 2. UI要素 |
| マルチサムネイルグリッド | Multi-thumbnail Grid | 3. 操作・機能 |
| 右開ナビゲーション | RTL Manga Navigation | 3. 操作・機能 |
| 見開きずらしボタン | Spread Shift Button | 2. UI要素 |
| 見開きモード | Spread Mode | 3. 操作・機能 |
| 明示ダウンロード | Explicit Download | 5. オフライン・PWA |
| メニューボタン | Menu Button | 2. UI要素 |
| モメンタム送り | Momentum Flick | 3. 操作・機能 |
| 容量バー | Storage Bar | 2. UI要素 |
| 容量バー更新 | Storage Bar Update | 5. オフライン・PWA |
| 容量不足時の最古作品削除確認 | Quota Eviction Confirm | 5. オフライン・PWA |
| ライブラリ検索 | Library Search | 3. 操作・機能 |
| リーフ列挙 | Leaf Enumeration | 5. オフライン・PWA |
| レンジバイパス | Range Bypass | 5. オフライン・PWA |
| 履歴スタック | History Stack | 6. 状態管理・ストレージ |
| ログアウトボタン | Logout Button | 2. UI要素 |
| ログイン画面 | Login Screen | 1. 画面 |
