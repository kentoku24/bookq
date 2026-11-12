# iOS ネイティブクライアント (`ios/`)

SwiftUI 製の iOS/iPadOS クライアント。iOS 17+、依存ゼロ（URLSession/Keychain/
CryptoKit/Vision のみ）。認証 + ブラウズ + 本リーダー（spread/chapter/進捗同期）。

## ビルド

```
open ios/Bookq.xcodeproj        # Xcode 27+
xcodebuild -project ios/Bookq.xcodeproj -scheme Bookq \
  -destination 'platform=iOS Simulator,name=iPhone 13' build
xcodebuild -project ios/Bookq.xcodeproj -scheme Bookq \
  -destination 'platform=iOS Simulator,name=iPhone 13' test
```

プロジェクトは PBXFileSystemSynchronizedRootGroup（フォルダ自動同期）なので
`Bookq/` 配下にファイルを置くだけでビルド対象になる。

## 開発用サーバーへの接続

シミュレータは Mac のネットワークを共有するため、ローカルの TEST_MODE
サーバーには `http://localhost:8080` で直接届く（ATS は localhost 対象外）:

```bash
BOOKQ_TEST_MODE=true DOWNLOADS_DIR=/path/to/images \
  uv run --with-requirements requirements-dev.txt \
  uvicorn app.main:app --port 8080
```

起動時に `-serverURL` 引数を渡すと URL をプリセットできる
（NSArgumentDomain → @AppStorage）:

```bash
xcrun simctl launch <udid> dev.bookq.reader -serverURL http://localhost:8080
```

実機からの接続には Tailscale アプリが必要（サーバーは tailnet 限定）。

## 認証

RFC 8252 PKCE（`app/auth_token.py` が契約）。`GET /auth/login?client=native`
`&code_challenge=<S256>&code_challenge_method=S256&state=<csrf>` →
ASWebAuthenticationSession（システムブラウザ）→ Google OAuth →
`bookq://auth?code=<one-time>&state=…` コールバック（state 照合必須）→
`POST /api/v1/auth/token` で `authorization_code` + `code_verifier` を交換し、
access JWT（1h）+ refresh（30d、rotation 付き）の pair を 1 Keychain item に
原子保存。全 API に `Authorization: Bearer` で付与し、401 → refresh → 1 回 retry
（single-flight coalescing）。API セッションは cookie storage を明示的に無効化
（Bearer 一本）。identity は JWT claims（`sub`/`name`/`picture`）由来の `AuthUser`
— `/api/me` のような検証 endpoint は存在しない。

native フローは TEST_MODE でも Google OAuth を通る（`?client=native` は
`_TEST_MODE` をバイパスしない）。dev 環境でのアプリ側フロー検証には
`AuthManager.testSeed(_:)` フックか実 OAuth が必要。

## 構成

| パス | 役割 |
|------|------|
| `Bookq/BookqApp.swift`, `AppModel.swift` | ルーティング（onboarding→login→library） |
| `Config/AppConfig.swift` | serverURL・原本/正規化切替（UserDefaults） |
| `Auth/` | ASWebAuthenticationSession + Keychain |
| `API/` | REST クライアント・encodeURIComponent 相当のパスエンコード・モデル |
| `Library/` | ツリー保持・ブラウズルール（flatten/self-tile/multiThumb）・進捗 |
| `Browse/` | タイル/リスト/カードグリッド・認証付き画像ローダ |
| `Reader/` | リーダー（`ReaderModel`=spread/chapter 組立、`ReaderView`=UI、`ZoomableScrollView`=ズーム、`ProgressSync`=進捗同期、`OverviewView`=複数ページグリッド） |
| `External/` | 外部ディスプレイ投影（USB-C→モニタ）|`ExternalDisplayStore`=投影状態+モード選択、`ExternalDisplayView`=accessory 宣言+フルスクリーン描画 |

## リーダー仕様（web parity）

- 縦=1ページ / 横=spread（spreadOffset 0=通常ペア、1=表紙単独、spread 離脱で 0 に
  リセット）を `spread.js` 通りに再現。RTL: 右端=最初のページ。タップゾーンは
  端30%=送り / 反対端30%=戻し / 中央=UI トグルで、`tapForward` 相当の
  方向切替ボタンあり（セッション限定・デフォルト左=次）。
- ページ送りは TabView ではなく自前の ZStack ページャ。視覚状態は連続値
  `pos`（spread 単位、小数=めくり途中）で、`floor(pos)` が上層として右へ
  スライドし `ceil(pos)` が下に静止する — pos 上昇=現在 spread が右へ
  出て次が現れ、pos 下降=前 spread が右からスライドインして覆う。
  1 つの式で両方向を表現するため、ドラッグ中の指の反転は向きの再判定
  なしに `pos` が原点を通って戻るだけで処理できる。`PageTurnMath`
  純粋関数がドラッグデルタ・端ラバーバンド・コミット方向判定を担当。
  ドラッグと ←/→ キーは**章をまたぐ**（本全体 `[0, spreads.count-1]`
  にクランプ、両端のみラバーバンド→バウンスバック）。タップゾーン・
  スワイプ・矢印キーの全ページ送り操作が章跨ぎ可 — エピソード間の
  移動は「最後のページでの送り」「最初のページでの戻し」でのみ起きる
  （**web/Android はスワイプ・キーを章内 clamp するが、iOS はユーザ
  要望で意図的に全操作を章跨ぎにしている差異**）。コミット閾値は
  web touchend と同じ `min(10%幅, 70pt)` + flick 予測 50%。
  `currentIdx` はコミット開始時点で着地し、カウンタ/スライダー/外部
  ディスプレイはアニメーションと同時に動く（web の strip 追従と同じ）。
- ページスライダーは**現在エピソード (章) のみにスコープ**
  （web `syncChapterSlider`/`idxToChapterSlider`/`chapterSliderToIdx`
  相当の `chapterSliderMax`/`idxToChapterSlider`/`chapterSliderToIdx`
  を `ReaderWork` に実装）。RTL: 右端=章の最初のページ、左端=最後の
  ページ。章をまたいだ時点で範囲が新しい章に追従する。スライダーから
  他エピソードへは飛べない（ジャンプはページ送りのみ）。
- 連打は合流しない: タップ毎に `posTarget` が 1 spread 進み、`pos` が
  定速チェイスで追従する。10 taps/s 程度でも各タップが 1 ページ送りとして
  計上され、ページは連続値を通って高速にめくれる（アニメーションの
  キューイングは発生しない）。
- 章結合: 兄弟 leaf を一続きで読み、各章境の前にスペーサーを挿入して
  spread ペア境界に揃える（app.js と同一式 `(startIdx-spreadOff+2)%2`）。
- 進捗は常に「leaf パス + 章ローカル page」で保存・同期（`sync.js`
  と同じワイヤ {folder,page,total,ts}、2s デバウンス+バックオフ）。
- ズームは UIScrollView ベース（pinch/pan のみ、double-tap 拡大なし）。
  1x 時は pan ジェスチャを無効化して外側ページングにスワイプを渡す。
  ピンチ中は `onPinchState` でページドラッグを停止する。
- 複数ページ（`overview.js` 相当）: 右上グリッドボタン or ピンチインで
  `OverviewView` を開く。章見出し・章ローカルページ番号・現ページ
  ハイライト・現ページへ 1/3 位置スクロール・タップジャンプ
  （ページャと同じアニメーションで遷移）。
- キーボード（iPad/外付け）: ←=次 / →=前（章跨ぎ可、両端で no-op）/
  Home=即時で章頭・End=章末 spread 先頭ページへアニメーション遷移
  （web `spreads[r.hi].pages[0]` と同じ着地）/ G=複数ページ
  （web・Android と同じ RTL 規約）。

## ブラウズ仕様（web parity）

- タイル/リスト/カード切替（永続化）、マルチサムネスプライト、
  単一子 flatten、self-tile（直下画像・混合フォルダの「このフォルダを読む」）、
  `N 件` カウンタ、`続きから: <name> p.<page>` ピル。
- ツリー自動更新: web の fill-poll 相当（15s 間隔・active 時のみ、
  `?since=<gen>` 差分）。手動「再読み込み」は /api/v1/index/refresh + 強制再取得。
- 隠しルート: GET で読み取り、トップレベルでフィルタ +
  「隠しルートを表示」トグル（web の 全表示 相当）。hide/unhide 操作 UI は Phase 5。

## オフラインDL（web parity: `offline-core.js` + `storeAdapter.js`）

- 作品タイル/行の右上円ボタン（`OfflineButton` = web `offlineControlsHtml` 相当）。
  状態機械: idle ↓ / listing 回転弧 / downloading 進捗リング+■ / stopping /
  partial ↓◯(黄) / complete ✓(緑) / confirm-delete 🗑(赤) / deleting。
- タップ: idle→即DL / downloading→中断(部分保持→partial) / partial→
  「続きをDL or 削除」シート / complete→確認armed(3s)→再タップで削除。
- 列挙: leaf フォルダをツリーから展開（親が直接画像を持つ場合は自身も leaf）。
  各 leaf を `/api/v1/images?folder=` で listing → `/view/`（原本設定時 `/image/`）で
  `Documents/BookqOffline/<enc(folder)>/<name>` へ逐次保存。
  records は leaf 毎に works.json へ永続化 {pageCount,cachedCount,bytes,names,…}。
- 読み出し: `RemoteImageLoader` が remote URL (`/view/`,`/image/`,`/thumb/`) を
  folder+name に戻してローカル優先（SW cache-first 相当）。Reader の leaf
  listing も `/api/v1/images` 失敗時は record の names ∩ 実在ファイルで代替。
- オフライン起動: ツリーは最終成功取得を Application Support に snapshot
  (`tree-<host>.json`)。boot probe（`/api/v1/version`）失敗でも snapshot があれば `.ready` で
  ライブラリを開き、ダウンロード済み作品を読める（web の SW cache parity）。
  バックグラウンド refresh 失敗はツリーが空の時だけエラー画面にする
  （stale ツリーがある限り閲覧継続 = web の SW NetworkFirst 相当）。
- ストレージ: 設定画面に「オフライン保存 N 作品・X MB」。上限超過
  （ディスク溢れ）は中断して toast 表示（web の quota modal 相当の扱いは flag）。

## dual-reader 同期（Phase 6, server-ws plane）

- `Sync/PairSocket.swift` — `URLSessionWebSocketTask` で `/ws/reader-sync`、
  `Authorization: Bearer` 認証、テキスト JSON フレーム（v:1, 64KiB）。
- `Sync/PairStore.swift` — pairClient.js + pairController.js の状態機械移植。
  CREATE/JOIN/MATCH/RESUME エントリ、ACK/REJECT 相関、STATE dedup
  (epoch,rev strictly-greater)、MANIFEST chunk 再構成＋sha256 検証、
  READY/APPLIED、PING→PONG + 20s liveness、reconnect RESUME
  (300s window)。TURN/GOTO/SET_OFFSET/SWAP_SIDES/OPEN_WORK は
  phase==reading ゲート + manifestVersion 付き（web と同一ルール）。
  JOIN_REQUEST は auto-approve（web #114 相当）。
- `Sync/CanonicalJSON.swift` — `json.dumps(sort_keys,separators,
  ensure_ascii)` バイト互換。manifest digest 検証に使用。
- `Sync/PairSheet.swift` — ペアリング UI（作成=コード+QR / 6桁参加 /
  MATCH 自動接続 / セッション情報 / 切断）。
- `Sync/PairedReaderView.swift` — pairedView.js 相当。自分の side の
  単ページ描画、タップゾーン/スワイプ/キー TURN(cross — ソロリーダーと
  同じく全ページ送り操作が章跨ぎ可)、章ローカルスライダー GOTO
  （`PairModel.chapterRange` で現在 chapterFolder の spread 範囲に
  スコープ）、offset/入替トグル。leader が閉じると
  SET_ACTIVITY selecting。
- `LibraryView` — leader+paired の作品タップは OPEN_WORK intent に
  ルーティング（solo リーダーは開かない）。phase→reading で
  PairedReaderView が全画面自動表示、手動 dismiss は workEpoch 毎に
  記憶され「同期中」バナーから再入室。

## 外部ディスプレイ投影（USB-C → モニタ）

- iOS 27+ の `ExternalNonInteractiveAccessory`（SwiftUI `.sceneAccessory`）
  を `ReaderView` に宣言。システムが接続ディスプレイに accessory シーンを
  作成して投影（ミラーリング解除）。非対話のため操作は端末側のみ。
- 発動: リーダー表示中にディスプレイ接続すると自動投影（accessory の
  `isEnabled` = `userEnabled` 既定 ON）。リーダー内クロームの tv ボタンで
  ON/OFF（OFF は `isEnabled=false` → ミラーリングに復帰）。リーダーを
  閉じると accessory 宣言ごと除去 → ミラーリングに復帰。抜き差しは
  `onAvailabilityChange` が追跡し tv ボタンの表示を駆動。
- 解像度: accessory シーンには delegate が無いため、view 内の
  `ScreenProbe`（0 サイズ UIViewRepresentable）が
  `window.windowScene.screen` から UIScreen を取得し、
  `screen.availableModes` 中の最大画素モードを `screen.currentMode` に
  設定（native 4K を強制）。`overscanCompensation = .none` でフルパネル
  使用 — `.insetBounds` はオーバースキャン報告ディスプレイで外周に黒枠が
  残るため（TV では端クリップの可能性あり）。
- `ExternalDisplayRootView`/`ExternalReaderView` は端末リーダーと同じ
  規則で描画（外部 geometry から pageMode 決定、`work.withMode` で
  spread 再構成、`ReaderWork.spreadFit` 共有、RTL、chrome/gesture なし）。
  画像は `RemoteImageLoader.shared` のキャッシュをそのまま共用。
  accessory シーンには environment object が届かないため状態は
  `ExternalDisplayStore.shared`（シングルトン）経由。
- `ReaderView` が `Presentation`（work/currentIdx/pageAR/spreadOffset/
  landscapeSpread/urlFor）を `ExternalDisplayStore.shared` に公開。
  paired（dual-reader）中は投影しない（solo リーダーのみ）。
- iOS <27 では accessory API が無いため外部投影は無効（tv ボタン非表示）。

## 既知の制約

- webrtc-dc データプレーン・UI_SYNC ブラウズ同期・QR スキャンは非対応
  （server-ws plane とコード/MATCH 入力で代替）
- ページ画像は既定 `/view/`（web 既定は `/image/`。原本モードは設定で切替可）
- idb の HID 注入では swipe/pinch が成立しないため実機 QA が必要
- 外部ディスプレイはシミュレータで生成できないため投影経路は実機 QA のみ
  （単体テストは state machine/レイアウト helper のみカバー）

## テスト

- `BookqTests`（単体 + `SpecConformanceTests` 契約スイート）と
  `BookqUITests`（XCUITest UX スイート）の 2 ターゲット。`Bookq.xcscheme`
  の Testables に両方登録済みで `xcodebuild test` が両方走る。
- UI テストは `BookqUITests/UITestSupport.swift` が `-serverURL` /
  `-testToken` 起動引数で TEST_MODE サーバーへ直接接続する（fixture は
  android.yml と同じ合成木: Series/vol1+vol2, Other）。タップ対象は
  `tile:<path>` / `selfTile:<path>` / `readerClose` / `readerCounter` /
  `layoutMenuBtn` / `offlineBtn:<path>` 等の accessibilityIdentifier
  （Android の testTag 相当。Browse/Search/Reader/Offline の主要操作をカバー）。
- CI: `.github/workflows/ios.yml`（`macos-latest`、pull_request +
  workflow_dispatch）。paths-filter で `ios/**` `api/**` `app/**`
  `requirements*.txt` `rust/viewer-rs/**` に限定し、非対象 PR では
  skipped になる（required check 互換）。`.github/scripts/ios-ui-test.sh`
  がシミュレータ選択（iPhone 13 優先・最新 runtime へフォールバック）→
  DerivedData キャッシュ → `build-for-testing` → `BookqTests` →
  `BookqUITests` を一括実行し、xcresult + サーバーログを artifact 化する。
  実行時間はジョブサマリに記録される（macOS 分は ubuntu の約10倍のため、
  required 化の判断材料とする）。
