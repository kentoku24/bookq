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
| `Reader/` | リーダー（`ReaderModel`=spread/chapter 組立、`ReaderView`=UI、`ZoomableScrollView`=ズーム、`ProgressSync`=進捗同期） |

## リーダー仕様（web parity）

- 縦=1ページ / 横=spread（spreadOffset 0=通常ペア、1=表紙単独）を
  `spread.js` 通りに再現。RTL: 右端=最初のページ、タップゾーンは
  左30%=次 / 右30%=前 / 中央=UI トグル。
- 章結合: 兄弟 leaf を一続きで読み、各章境の前にスペーサーを挿入して
  spread ペア境界に揃える（app.js と同一式 `(startIdx-spreadOff+2)%2`）。
- 進捗は常に「leaf パス + 章ローカル page」で保存・同期（`sync.js`
  と同じワイヤ {folder,page,total,ts}、2s デバウンス+バックオフ）。
- ズームは UIScrollView ベース（pinch/double-tap/pan）。1x 時は pan
  ジェスチャを無効化して外側ページングにスワイプを渡す。

## 既知の制約（後続フェーズ）

- オフライン DL・検索/ファセット・dual-reader 未実装
- UI 自動テストなし（単体テストのみ）
