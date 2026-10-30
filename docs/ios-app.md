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

`GET /auth/login?native=1` → ASWebAuthenticationSession（システムブラウザ）→
Google OAuth → `bookq://auth/callback?token=...` コールバック → Bearer トークンを
Keychain 保存し全 API に `Authorization: Bearer` で付与。TEST_MODE サーバーでは
Google なしで即トークンを返すため、アプリ側フローは dev 環境でも検証可能。

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
  `?since=<gen>` 差分）。手動「再読み込み」は /api/refresh + 強制再取得。
- 隠しルート: GET で読み取り、トップレベルでフィルタ +
  「隠しルートを表示」トグル（web の 全表示 相当）。hide/unhide 操作 UI は Phase 5。

## 既知の制約（後続フェーズ）

- オフライン DL・検索/ファセット・隠しルート管理 UI・dual-reader 未実装
- ページ画像は既定 `/view/`（web 既定は `/image/`。原本モードは設定で切替可）
- idb の HID 注入では swipe/pinch が成立しないため実機 QA が必要
- UI 自動テストなし（単体テストのみ）
