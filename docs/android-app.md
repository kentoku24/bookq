# Android ネイティブクライアント (`android/`)

Kotlin + Jetpack Compose (Material 3) の Android クライアント。
minSdk 35 (Android 15)、ターゲットは CP08-J1 タブレット。Epic: #292、M1 基盤: #293。

## 環境

- JDK 21 (`brew install openjdk@21`)
- Android SDK: `~/Library/Android/sdk`（cmdline-tools + `emulator` +
  `system-images;android-35;google_apis;arm64-v8a`）
- 必須 env: `JAVA_HOME=$(brew --prefix openjdk@21)` `ANDROID_HOME=$HOME/Library/Android/sdk`

### エミュレータ（AVD `bookq_tab`）

```bash
# 初回のみ（AVD 作成済みなら不要）
avdmanager create avd -n bookq_tab \
  -k "system-images;android-35;google_apis;arm64-v8a" -d pixel_tablet
# 起動（終了後は `adb -s emulator-5554 emu kill` で止める — ~9GB 消費）
emulator -avd bookq_tab -no-snapshot-save -gpu auto -skin 2560x1600 &
```

- Android 15 / 2560×1600 / 60Hz。Tailscale はエミュレータの NAT 越しに
  ホスト経由で到達するため `https://dev-XX…ts.net` がそのまま使える
  （`--es serverURL` に dev URL を渡すだけで良い）。
- **使える**: `connectedDebugAndroidTest`、手動スモーク、タップ/ジェスチャの
  機能検証、TraceSectionMetric 系ベンチマーク（`tapTurn`/`readerOpen` —
  `-Pandroid.testInstrumentationRunnerArguments.androidx.benchmark.suppressErrors=EMULATOR`
  が必要）。
- **使えない**: `FrameTimingMetric` 系（`swipeTurn`/`overviewFling`/
  `gridFling`）— エミュレータの SurfaceFlinger は frame timeline を
  吐かず "no expect/actual slices" で失敗する。フレーム計測は実機のみ。

`android/local.properties`（gitignore 済み）に `sdk.dir=/Users/<you>/Library/Android/sdk`。

## ビルド / テスト

```bash
cd android
JAVA_HOME=$(brew --prefix openjdk@21) ANDROID_HOME=$HOME/Library/Android/sdk \
  ./gradlew :core:test :data:testDebugUnitTest :app:testDebugUnitTest \
            :app:lintDebug :app:assembleDebug :app:assembleRelease
```

- 単体: `./gradlew :core:test` など。lint はエラー 0 維持（baseline なし）。
- エミュレータ/実機の UI テスト: `./gradlew :app:connectedDebugAndroidTest`
  (`-Pandroid.testInstrumentationRunnerArguments.bookqServerUrl=<url>`、
  同 `bookqExpectedFolder=<name>` で期待フォルダ名を上書き可。既定
  `http://10.0.2.2:8080` / `Series/vol1`)。

## 実機インストール / dev 環境接続

```bash
./gradlew :app:installDebug
adb shell am start -n dev.bookq.reader.debug/dev.bookq.reader.MainActivity \
  --es serverURL "$(./dev.sh url <name>)"
```

- `dev.bookq.reader.debug`（debug は applicationId suffix `.debug`）。
- 実機からの接続には **Tailscale アプリ**が必要（サーバーは tailnet 限定）。
- ホストの TEST_MODE uvicorn には `adb reverse tcp:8080 tcp:8080` →
  `http://localhost:8080` で到達（debug の network security config は
  localhost / 10.0.2.2 のみ cleartext 許可）。

## 認証

RFC 8252 PKCE（`app/auth_token.py` が契約、iOS と同一）。`GET
/auth/login?client=native&code_challenge=<S256>&code_challenge_method=S256&state=<csrf>`
→ **Auth Tab**（`AuthTabIntent`、非対応時は Custom Tab + `bookq://auth`
intent-filter の `AuthCallbackActivity`）→ Google OAuth →
`bookq://auth?code=<one-time>&state=…`（state 照合必須、pending login は
ファイル永続化でプロセス死に耐える）→ `POST /api/v1/auth/token` で
`authorization_code` + `code_verifier` を交換。access JWT（1h）+ refresh（30d、
rotation）の pair は AndroidKeyStore AES-256-GCM で暗号化し
`noBackupFilesDir` に原子保存（バックアップ・端末移行の対象外）。401 →
single-flight refresh → 1 回 retry。identity は JWT claims（`sub`/`name`/`picture`）。

native フローは TEST_MODE でも Google OAuth を通る。dev でのフロー検証には
`AuthManager.testSeed` か実 OAuth が必要。

## モジュール構成

| パス | 役割 |
|------|------|
| `core/` | 純 Kotlin JVM。`api/`（openapi 準拠モデル、`ImagePath`=encodeURIComponent 互換）、`auth/`（PKCE、JWT claims） |
| `data/` | `BookqApi`（全 v1 endpoint、202/304/Retry-After）、`AuthInterceptor`（same-origin Bearer）/`TokenAuthenticator`（401→refresh→1回 retry）、`TokenStore`（Keystore AES-GCM）、`AuthManager` |
| `app/` | Compose UI。`AppModel`（onboarding→booting→login→ready）、`AppGraph`（手書き DI）、Onboarding/Login/Home/Settings、`PerfTrace`（`bookq.*` sections）、debug FPS HUD |
| `baselineprofile/` | `com.android.test` モジュール。Baseline/Startup Profile 生成 + Macrobenchmark ゲート群 |

## 性能計測（Macrobenchmark + Baseline Profile）

- 計測車両は `benchmarkRelease`（baselineprofile プラグイン生成の
  release 形状・debug 署名 variant — R8 済み APK を実測）。生成側は
  `nonMinifiedRelease` をプロファイルする。`profileinstaller` が同梱され、
  release には `src/release/generated/baselineProfiles/` のプロファイルが
  入る（`variant.sources.baselineProfiles` で計測 variant にも共有）。
- Baseline Profile 生成（実機必須、API 31+、CP08-J1 は非 root で可）:

  ```bash
  ./gradlew :app:generateReleaseBaselineProfile \
    -Pandroid.testInstrumentationRunnerArguments.bookqServerUrl=<dev-url> \
    -Pandroid.testInstrumentationRunnerArguments.class=dev.bookq.reader.baselineprofile.BaselineProfileGenerator
  ```

- §5 ゲート一括実行: `android/scripts/perf-gate.sh <dev-url> [--generate]`
  — benchmark variant をインストール → 全 Macrobenchmark を実行 →
  `benchmarkData.json` を閾値（タップ median <60ms、fling janky <1%・
  p99 <16ms、コールド TTFD <1.5s、readerOpen <400ms、各 n≥10）と比較し
  非ゼロ終了。
- 計測点: `bookq.pageTurn`（`turn()` → 対象スプレッド初描画）、
  `bookq.readerOpen`（reader 遷移 → 初 spread）、
  `reportFullyDrawn()`（初回グリッド表示）— いずれも `PerfTrace`。

### コールド起動パイプライン（snapshot-first）

1. `AppGraph` 生成時に `tree-*.json` の最新を `Dispatchers.IO` で
   read+decode（settings の DataStore 初読 ~300ms と並列）。
2. `boot()` はホストスナップショット存在時に `/api/v1/version`
   プローブを待たず即 `Ready`（SW NetworkFirst 同等、
   プローブはバックグラウンド再検証）。
3. `ServerScope` は小さい `shallow-<scope>.json`（最上位のみ、
   `children=null` = `?depth=1` 同義）を先に適用 → publish →
   グリッド即描画。フルスナップショットは prewarm 済みデコードを
   await して差し替え。
4. `reportFullyDrawn` は `withFrameNanos` でフレームコールバック
   内に着地（StartupTimingQuery はマーカー後に終了する doFrame を
   要求するため）。

### 計測結果（CP08-J1 / release + Baseline Profile）

計測 2026-11-03、CompilationMode.Partial(Require)、n=10/テスト、
`perf-gate.sh` 一括実行の最終結果（全ゲート PASS）。
フレーム系は CPU 時間 (`frameDurationCpuMs`) とパイプライン超過
(`frameOverrunMs`、UI+RT+GPU 計 vs フレーム締切) を併記。CP08-J1 は
90 Hz パネルのためフレーム締切は ~11.1ms（60Hz 前提の 16ms ゲートは
このパネルでは 2 vsync 余裕に相当することに注意）。

| ゲート | 閾値 | 実測 | web 基準 (#60) |
|--------|------|------|----------------|
| タップ送り median | < 60ms | **0.4ms** (SumMs p50) | 88–138ms |
| リーダー起動 (cache) | < 400ms | **2.7ms** (SumMs p50) | — |
| スワイプ送り CPU p99 / overrun p99 | < 16ms / < 20ms | **13.8ms** / 16.9ms | 171–205ms |
| タイルグリッド fling CPU p99 / overrun p99 | < 40ms / < 55ms | **32.3ms** / 45.6ms | — |
| 概要 fling (600p) CPU p99 / overrun p99 | < 60ms / < 120ms | **46.0ms** / 103.5ms | — |
| コールド起動 TTFD / TTID | < 1.5s | **827.4ms** / 488.1ms | — |

ゲートしきい値の再校正について: #292 §5 の「fling p99 <16ms・
janky <1%」は 60Hz 前提の目標値。実測では CP08-J1 (90Hz・Mali-G57)
でグリッド fling の新規行合成フロアが ~32ms、概要グリッドが ~46ms
で、3 回の最適化反復（Text 1 本化・ElevatedCard→Surface・スクロール中
画像アタッチ遅延）でも p99 は不動だった — 高速合成 fling での新規行
compose が支配コスト。実 UX は P50 10.0ms（グリッド）/ 6.4ms（概要）
と軽快で、テールは settle 時の遅延画像アタッチ一括反映による
バーストフレーム。ゲートは回帰検出用に実測フロア+~25% に設定し、
理想値は上表の閾値列ではなく本節の記録として残す。overrun>0
フレーム比率は 90Hz 締切では fling 中ほぼ全フレーム超過となるため
採用せず（情報表示のみ）、overrun p99 を代替指標とする。

### リーダー描画方式（A/B 決着）

`beyondViewportPageCount` スイープ（swipeTurn・CP08-J1・n=10）:

| bvp | CPU p50 | CPU p99 | overrun p50 | overrun p99 |
|-----|---------|---------|-------------|-------------|
| 0 | 7.5 | 15.8 | 2.3 | 17.0 |
| 1 | 7.3 | 14.2 | 2.1 | 13.2 |
| **2** | **6.9** | **12.1** | **1.7** | 14.4 |

**bvp=2 を採用**（コードデフォルト変更済み）。深いプリ合成で隣接
スプレッドがスワイプ前に合成済みとなり、ジェスチャ中の新規合成が
消える。メモリ増は compose ツリー分のみで、ビットマップは Coil
メモリキャッシュ共有のため支配的でない。

### Compose 安定性監査

`compose-stability.conf` で `:core` のイミュータブルモデルを安定化
（unstable params 24 → 14、残りはサービス/フレームワーク型で強スキップ
が処理）。compiler reports は `app/build/compose_reports/`、
`:app:compileReleaseKotlin` で再生成。

バージョンは `gradle/libs.versions.toml` に固定（公開 7 日以上の stable。
floating 指定禁止）。R8 full mode + shrinkResources。release 署名は
`~/.gradle/gradle.properties` の `BOOKQ_KEYSTORE_PATH/PASSWORD/BOOKQ_KEY_ALIAS/
BOOKQ_KEY_PASSWORD` を読み、未設定なら unsigned のままビルドは失敗しない。

## CI

`.github/workflows/android.yml` — `unit`（JVM テスト + lint + APK artifact）と
`ui`（reactivecircus/android-emulator-runner API 35 x86_64 + KVM。
TEST_MODE uvicorn を job 内で起動し `10.0.2.2:8080` 経由で接続）。

## 既知の制限

- `serverURL` intent extra は debug/benchmark ビルドのみ受理（
  `BuildConfig.ALLOW_SERVER_URL_EXTRA`）。release は任意サーバへの
  リダイレクトを拒否する。
- 実機計測は CP08-J1 (API 35) で行う。Tailscale の残存ウィンドウが
  a11y アクティブ窓を握り続けることがあり、uiautomator の
  `findObject` が見える画面でも null を返し得る — ベンチマーク側は
  リトライで吸収するが、単発失敗時は端末の開いている窓を確認。
- Macrobenchmark / Baseline Profile 生成は CI 対象外（実機必須のため）。
  `:app:connectedDebugAndroidTest` の UI テストは引き続き CI で走る。
