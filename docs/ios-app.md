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
| `External/` | 外部ディスプレイ投影（USB-C→モニタ）|`ExternalDisplayStore`=セッション管理+公開状態、`ExternalDisplaySceneDelegate`=外部 scene 構築、`ExternalDisplayView`=フルスクリーン描画 |

## リーダー仕様（web parity）

- 縦=1ページ / 横=spread（spreadOffset 0=通常ペア、1=表紙単独、spread 離脱で 0 に
  リセット）を `spread.js` 通りに再現。RTL: 右端=最初のページ。タップゾーンは
  端30%=送り / 反対端30%=戻し / 中央=UI トグルで、`tapForward` 相当の
  方向切替ボタンあり（セッション限定・デフォルト左=次）。
- 連打は 160ms 合流（最初のタップは即時、以後の連打は最終 spread へ一発遷移）
  で TabView アニメーションの直列詰まりを回避。
- 章結合: 兄弟 leaf を一続きで読み、各章境の前にスペーサーを挿入して
  spread ペア境界に揃える（app.js と同一式 `(startIdx-spreadOff+2)%2`）。
- 進捗は常に「leaf パス + 章ローカル page」で保存・同期（`sync.js`
  と同じワイヤ {folder,page,total,ts}、2s デバウンス+バックオフ）。
- ズームは UIScrollView ベース（pinch/pan のみ、double-tap 拡大なし）。
  1x 時は pan ジェスチャを無効化して外側ページングにスワイプを渡す。
- 複数ページ（`overview.js` 相当）: 右上グリッドボタン or ピンチインで
  `OverviewView` を開く。章見出し・章ローカルページ番号・現ページ
  ハイライト・現ページへ 1/3 位置スクロール・タップジャンプ。
- キーボード（iPad/外付け）: →=次 / ←=前 / Home・End=章頭・章末 / G=複数ページ。

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
  単ページ描画、タップゾーン TURN(cross)、スワイプ/キー TURN(clamp)、
  スライダー GOTO、offset/入替トグル。leader が閉じると
  SET_ACTIVITY selecting。
- `LibraryView` — leader+paired の作品タップは OPEN_WORK intent に
  ルーティング（solo リーダーは開かない）。phase→reading で
  PairedReaderView が全画面自動表示、手動 dismiss は workEpoch 毎に
  記憶され「同期中」バナーから再入室。

## 外部ディスプレイ投影（USB-C → モニタ）

- `windowExternalDisplayNonInteractive` scene role で第二 WindowScene を
  外部スクリーンに起動（`Info.plist` の `UIApplicationSceneManifest` +
  `ExternalDisplaySceneDelegate`）。非対話ロールのため操作は端末側のみ。
- 発動: 画面接続中にリーダーを開くと自動投影。リーダー内クロームの
  tv ボタンで ON/OFF（セッション限定）。リーダーを閉じるとセッション
  破棄 → ミラーリングに復帰。ケーブル抜き差しは
  `UIScreen.didConnect/didDisconnect`（deprecated だが物理接続の唯一の
  シグナル）+ 次のページ送りで追従。
- 解像度: 接続時に `screen.availableModes` から最大画素数のモードを選び
  `screen.currentMode` に設定（native 4K を強制）。オーバースキャン TV
  対策は `overscanCompensation = .insetBounds`（通常モニタでは no-op）。
- `ExternalDisplayRootView`/`ExternalReaderView` は端末リーダーと同じ
  規則で描画（外部 geometry から pageMode 決定、`work.withMode` で
  spread 再構成、`ReaderWork.spreadFit` 共有、RTL、chrome/gesture なし）。
  画像は `RemoteImageLoader.shared` のキャッシュをそのまま共用。
- `ReaderView` が `Presentation`（work/currentIdx/pageAR/spreadOffset/
  landscapeSpread/urlFor）を `ExternalDisplayStore.shared` に公開。
  paired（dual-reader）中は投影しない（solo リーダーのみ）。

## 既知の制約

- webrtc-dc データプレーン・UI_SYNC ブラウズ同期・QR スキャンは非対応
  （server-ws plane とコード/MATCH 入力で代替）
- ページ画像は既定 `/view/`（web 既定は `/image/`。原本モードは設定で切替可）
- idb の HID 注入では swipe/pinch が成立しないため実機 QA が必要
- 外部ディスプレイはシミュレータで生成できないため投影経路は実機 QA のみ
  （単体テストは state machine/レイアウト helper のみカバー）
- UI 自動テストなし（単体テストのみ）
