# オフライン仕様 (offline-spec)

bookq のオンライン/オフライン動作の合意仕様。対象は **Web PWA と iOS**
(Android は現時点では未対応・後続タスク)。

## 1. 目的・UX 原則

- オンラインでもオフラインでも、可能な範囲でベストな UX・レスポンスを目指す。
- **オフラインファースト起動**: 起動時はまずオフライン状態とみなし、
  ローカルに保持された情報 (ツリースナップショット + ダウンロード済み
  レコード) から即座にトップ画面を描画して、ユーザの操作を素早く受け
  付けられる状態にする。サーバ接続はその後バックグラウンドで確立する。
- 「読めるもの」を先に見せ、「読めないもの」は見せるが誤って開かせない
  (グレーアウト + 明示的な案内)。

## 2. 状態モデル

接続状態は 3 値。初期状態は `offline`。

| 状態 | 表示 | 意味 |
|---|---|---|
| `offline` | `○オフライン` | サーバ非到達。自動リトライしない |
| `connecting` | `◌接続中…` | 有限回の接続試行を行っている最中 (オフラインの亜種 — ブラウズ表示規則は offline と同一) |
| `online` | `●オンライン` | サーバ到達済み |

```
offline ──connect()──▶ connecting ──試行成功──▶ online
   ▲                      │                      │
   └── リトライバジェット消費 ┘                      └── リクエスト失敗 ──▶ connecting (即座)
```

`reportSuccess` (実ネットワーク応答の観測) は connecting の残り試行を
キャンセルして即 `online` へ遷移する。

## 3. connect() トリガ

`connect()` = 1 回の接続サイクル (下記バジェット内での試行列) を開始する。

| トリガ | 備考 |
|---|---|
| アプリ起動 | 初回描画の後 (描画をブロックしない) |
| フォアグラウンド復帰 | Web: `visibilitychange`(visible)/`pageshow`(persisted)。iOS: `scenePhase == .active`。「復帰時」はネットワーク復帰ではなくフォアグラウンド復帰を指す |
| OS オンラインイベント | Web: window `online`。iOS: 経路変化 (将来 NWPathMonitor 等、現状は復帰で代替可) |
| 明示的リトライ | ステータスバッジのタップ (offline 時のみ有効) |
| `reportFail` | `online` 中にサーバリクエスト失敗を観測 → 即 `connecting` へ |

- connecting 中の `connect()` 再入は無視する (同一サイクル内で coalesce)。
- `offline` 状態では自動リトライしない。従来の定期ポーリングは廃止
  (iOS `OnlineStatusStore` の 30s ループ、Web の health 定期確認)。

## 4. 接続試行 (リトライバジェット)

- 試行回数: **3 回**
- 各試行: `GET /api/v1/health?online_check=1&_t=<ts>`、ヘッダ
  `X-Online-Check: 1`、タイムアウト **4s**
- 試行間隔: 指数バックオフ **1s → 2s**
- 合計最大: `4s + 1s + 4s + 2s + 4s` ≈ **15s**
- バジェット消費後 `offline` へ。次のサイクルは §3 のトリガでのみ再開。

Web の health プローブは Service Worker 側で `online_check` /
`X-Online-Check` を検出してネットワークオンリーで通す (既存挙動)。
iOS も同じクエリ・ヘッダを送りワイヤ形状を揃える。

## 5. 成否判定

### Web (`Connectivity.fetch` 経由のすべてのリクエスト)

| 結果 | 判定 |
|---|---|
| fetch 解決かつ `res.ok` かつ `X-Bookq-SWCache` ヘッダ無し | 成功 → `reportSuccess` |
| fetch 解決かつ `res.ok` かつ `X-Bookq-SWCache` ヘッダ有り | 中立 (キャッシュ応答はオンラインの証拠にしない) |
| HTTP 502 / 503 / 504 | 失敗 → `reportFail` (プロキシ到達・上流不可) |
| その他の HTTP ステータス (4xx, 500 等) | 中立 (サーバは応答している) |
| fetch 拒否 `TypeError` (SW `Response.error()` 含む) | 失敗 → `reportFail` |
| fetch 拒否 `AbortError` | 無視 (意図的な abort — ダウンロード中断・旧リクエスト破棄など) |

Service Worker は `networkFirst` が API キャッシュへフォールバックした
応答に `X-Bookq-SWCache: 1` を付与する (`Response` 再構築)。これが
キャッシュ由来の 200 と実応答を区別する唯一の手段。

### iOS (`BookqAPI.send` の結果分類)

| 結果 | 判定 |
|---|---|
| `URLError`: `.notConnectedToInternet` `.timedOut` `.cannotConnectToHost` `.networkConnectionLost` `.cannotFindHost` `.dnsLookupFailed` | 失敗 |
| HTTP 502 / 503 / 504 | 失敗 |
| HTTP 2xx | 成功 |
| その他 (4xx, 500, decode 失敗, キャンセル) | 中立・無視 |

### 判定されないもの

`<img>` の成功/失敗、WebSocket、Range リクエストは状態判定に使わない
(img の失敗は既存のリトライ UI、pair は WS 自身の再接続に委ねる)。

## 6. ブラウズ絞り込み規則 (offline / connecting 共通)

レンダリングされたレベル P における子ノードの表示規則 (ルートは常時
レンダリングされる):

- **非リーフ子フォルダ c**: `subtreeHasDownload(c)` (c 配下に
  `cachedCount > 0` の works レコードが 1 つ以上。c 自身の mixed
  フォルダ直置き画像レコードも数える) → 表示。なければ**非表示**
  (グレーではなく出さない)。
- **リーフ子 (エピソードフォルダ / mixed フォルダの self タイル)**:
  `subtreeHasDownload(P)` — つまりそのレベル自体がダウンロードを含む
  場合に全表示。リーフが未ダウンロードなら**グレーアウト**
  (`filter: grayscale` + 透過 + `aria-disabled` + 「オフライン」
  オーバーレイ) し、タップはビューア遷移せず toast
  「オフラインのため読めません」を表示する。
- 非ルートのレベル P は `subtreeHasDownload(P)` が真のときのみ辿れるので、
  到達したレベルのリーフは常に全表示となる (DL 済みは通常、未 DL はグレー)。
- ルートでレコードが 0 件のときは空グリッド +
  「ダウンロード済みの作品がありません」。
- 部分ダウンロード (PARTIAL、`cachedCount > 0`) は読み取り可とみなす
  (キャッシュ済みページは読める)。

「ダウンロード済み」の定義: Web は IDB `works` レコード
(`cachedCount > 0`)、iOS は `OfflineStore.records` の `LeafRecord`
(ダウンロード中断で残った partial を含む)。

### 状態ごとの機能ゲート

| 機能 | online | connecting | offline |
|---|---|---|---|
| ブラウズ (絞り込み後) | ✓ | ✓ | ✓ |
| DL 済み読書 | ✓ | ✓ | ✓ |
| 未 DL 作品のタップ | ✓ | toast | toast |
| ダウンロード開始/再開 | ✓ | toast「オフラインのため開始できません」 | 同左 |
| ダウンロード削除 | ✓ | ✓ (ローカル操作) | ✓ |
| 検索 (サーバ側) | ✓ | 入力 disabled | 同左 |
| ファセット絞り込み | ✓ | ✓ (スナップショット内蔵で動く) | ✓ |
| pair 開始 | ✓ | disabled | 同左 |
| 非表示ルート編集 | ✓ | FV 既存 readOnly 経路 | 同左 |
| 読書位置のローカル記録 | ✓ | ✓ | ✓ |
| 読書位置のサーバ送信 | ✓ | 保留 (online 復帰で flush) | 同左 |
| ツリー再検証・サムネ prefetch・fill ポーリング | ✓ | 保留 | 保留 |

## 7. 起動・復帰シーケンス

### Web

1. DOMContentLoaded: `Connectivity` を `offline` で初期化 (バッジ `○`)。
2. `restoreOfflineFirst()`: IDB `OfflineTree` スナップショット +
   works レコードを読み、ツリーを復元して即 `renderView` (オフライン
   絞り込み適用) → `applyUrlState` で深リンク復元 (復元先が unreadable
   なら toast でブロック、接続前に利用不可コンテンツを開かない)。
3. `Connectivity.connect()` → `connecting` (バッジ `◌接続中…`)。
4. 成功 → `online` → 回復処理: ツリー再読込 (5 分 freshness を無視)、
   facets、読書位置 pull、FV スナップショット refresh、auto-sync、
   サムネ prefetch、fill ポーリング開始。
5. 失敗 (3 回) → `offline`。グリッドは絞り込み状態のまま操作可能。

### iOS

1. `AppModel.boot()`: サーバ URL 設定済みなら `.booting` スピナーを経ず
   即 `.ready` (LibraryStore は初期化時にスナップショット復元済み)。
   boot probe はバックグラウンド: `401 → .login`、失敗 → ready を維持
   (offline のまま)。
2. `LibraryView.task`: `online.connect()`。online 遷移で `library.load()`
   とポーリング開始、!online でポーリング休止。
3. `RootView` scenePhase `.active` → `online.foregrounded()` → connect()。

## 8. ツリー復元優先順位 (汎用復元・初回特別扱いなし)

1. サーバツリー (online 時のみ)
2. 永続スナップショット (Web: IDB `OfflineTree` / iOS: UserDefaults JSON)
3. **works レコードからのツリー合成**: レコードの `folder` パスから
   疑似ノード鎖を生成 (フラグ `_synthetic`、`children: []`、
   `thumb: rec.thumb`)。スナップショットが存在する場合は、スナップ
   ショットに含まれないレコードの分だけ合成ノードを union 追加する。
   合成リーフはタップ可能で、開くと画像一覧を `imagesUrl` の API
   キャッシュから取得する (downloadWork が事前に取得しており、通常
   hit する)。API キャッシュ miss 時は読書失敗トーストで degrade。

## 9. 既知の制限・判断記録

- **FV (非表示ルート) スナップショット無し × オフライン**: DL 済みのみ
  の絞り込みを fail-open で適用する (採用案)。理由: DL はユーザ自身が
  デバイスへ置いたデータであり、オフライン絞り込み自体が既に強い限定
  フィルタ。保守的代替 (従来通り blockedGridHtml で読込中→取得失敗を
  表示) は、初回オフライン起動で DL 済み作品が一切見えなくなるため不採用。
  スナップショットがある通常経路では従来通り fail-closed で併用する。
- **`X-Bookq-SWCache` 移行期**: 旧 SW (v2 系) が残る間は API キャッシュ
  応答にマーカーが付かず、キャッシュ 200 を online と誤認し得る。次の
  実リクエスト失敗で connecting へ自己修正され、health プローブは常に
  network-only なので最終状態は収束する。
- **`img` 読込の失敗は状態判定しない**: グレータイルのカバー画像が
  キャッシュ未保有でもタイル自体は正しく表示される (プレースホルダ)。
- **接続中に online 判定できない窓**: 起動〜プローブ確立の数百 ms は
  connecting。深リンク復元は先に走るため、非 DL への深リンクは toast
  ブロックとなる (仕様上の合意)。
- **未認証ユーザの初回**: iOS で `.booting` 廃止後、サーバ URL はあるが
  未ログインの起動は一瞬ライブラリ (空グリッド+接続中) が見えてから
  login へ遷移する。オフラインファーストの仕様上一貫した挙動。
- **Android**: 未対応。同等の状態機械・表示規則を後続タスクで移植する。
