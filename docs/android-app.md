# Android ネイティブクライアント (`android/`)

Kotlin + Jetpack Compose (Material 3) の Android クライアント。
minSdk 35 (Android 15)、ターゲットは CP08-J1 タブレット。Epic: #292、M1 基盤: #293。

## 環境

- JDK 21 (`brew install openjdk@21`)
- Android SDK: `~/Library/Android/sdk`（cmdline-tools のみ。エミュレータ不要 — 実機検証）
- 必須 env: `JAVA_HOME=$(brew --prefix openjdk@21)` `ANDROID_HOME=$HOME/Library/Android/sdk`

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
| `app/` | Compose UI。`AppModel`（onboarding→booting→login→ready）、`AppGraph`（手書き DI）、Onboarding/Login/Home(M1 仮)/Settings、`BookqTrace`（`bookq.*` sections）、debug FPS HUD |

バージョンは `gradle/libs.versions.toml` に固定（公開 7 日以上の stable。
floating 指定禁止）。R8 full mode + shrinkResources。release 署名は
`~/.gradle/gradle.properties` の `BOOKQ_KEYSTORE_PATH/PASSWORD/BOOKQ_KEY_ALIAS/
BOOKQ_KEY_PASSWORD` を読み、未設定なら unsigned のままビルドは失敗しない。

## CI

`.github/workflows/android.yml` — `unit`（JVM テスト + lint + APK artifact）と
`ui`（reactivecircus/android-emulator-runner API 35 x86_64 + KVM。
TEST_MODE uvicorn を job 内で起動し `10.0.2.2:8080` 経由で接続）。

## 既知の制限（M1 時点）

- Home は仮画面（トップレベル名の一覧のみ）。Browse/Reader は M2 以降。
- offline snapshot fallback は seam のみ（`hasOfflineSnapshot()=false`）。
- 音量キー送り・ブラウズレイアウト切替は設定値の永続化のみ（機能は後続 M）。
