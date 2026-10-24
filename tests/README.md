# UI テスト

FastAPI アプリを合成フィクスチャ（`conftest.py` が生成する downloads ツリー）に対して起動し、Playwright（headless Chromium）でブラウザ操作して UI の挙動を検証する。

## 実行

```bash
# 初回のみ: Chromium が未取得なら
uv run --with playwright playwright install chromium

# テスト実行（server フィクスチャが uvicorn を起動するため requirements-dev.txt が必須）
uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/ -q
```

## カバー範囲

| 領域 | 内容 |
| --- | --- |
| サイドバー | タイル一覧、検索フィルタ、ドリルダウンと戻る、マルチサムネイル、リスト表示切替、空フォルダ |
| ビューア起動 | 画像のみフォルダで開く、サイドバー自動クローズ、自然順ソート、Unicode フォルダ名の画像配信 |
| ページ操作 | タップゾーン（左=進む/右=戻る/中央=ツールバー）、キーボード、スライダー、先読み/退避ウィンドウ |
| 見開き | 横向き時の 2P 既定、1P/2P 切替、spread-shift（表紙ソロ）でのページ送り幅 |
| ズーム / 俯瞰 | ピンチ帯域（発火 0.5 / 再アーム 0.7 / 1.15 スナップ / バウンス）、中点アンカー、パン clamp と端越えページ送り、パーム除外、俯瞰グリッド（RTL・セクション・±200 窓）と入退場（FLIP / クロスフェード / 俯瞰内ピンチアウト）。合成タッチは `tests/viewer_touch.py`（CDP `Input.dispatchTouchEvent`、`touch_end(points)` は**離す点**）。`tests/test_viewer_{gesture,zoom,overview}.py` |
| URL 状態（URL state） | `#/view/<folder>?p=<page>` ハッシュの更新、リロード復元、ディープリンク直開き、戻る、範囲外ページのクランプ、章またぎ、ログイン往復の bridge（`tests/test_url_state.py`）。`#/browse/<path>` の更新・復元・戻る（`tests/test_browse_url_state.py`） |
| iOS viewport / PWA | iPhone 390x844 / iPad 820x1180 / 1180x820 での viewer, hash, レスポンシブ, PWA offline 検証（`tests/test_ios.py` + `tests/test_ios_pwa.py`, fixtures `tests/conftest.py:ios_page/ipad_page/ipad_landscape_page`） |

タッチスワイプ（指追従ドラッグ・モメンタム）は合成タッチイベントでの速度計算が不安定なため対象外にしている（`test_ios_swipe_synthetic` は削除済み）。実機の慣性スクロールは Simulator 手動QA（Tier2）のみ。

## 実行例

```bash
# iOS viewport のみ（chromium 既定）
uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_ios.py -q

# WebKit で実行（webkit が未取得なら先に install）
uv run --with playwright playwright install webkit
BROWSER=webkit uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_ios.py -q
BROWSER=webkit uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_ios_pwa.py -q

# 失敗時の video/trace（external_directory deny のため .omo/evidence 固定）
RECORD_VIDEO=1 RECORD_VIDEO_DIR=.omo/evidence BROWSER=webkit uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_ios.py -q
# WebKit narrow check: Cache と storage estimate を pass（persist toast assert は削除済み）
```

## Simulator 手動QA（iOS 実機 Safari）

> **Source of truth: `.claude/skills/ios-simulator/SKILL.md`**
> 自動テスト（Chromium / `BROWSER=webkit`）では拾えない Safari 固有の挙動は、このスキルが正本。詳細はスキルとその `references/` を参照。

- 対象: `Service Worker` / `Cache API` / `storage.estimate()` 0/0 / 回転・spread / PWA offline（7日 touch は存在確認のみ）
- 手順: `references/quickstart.md` の 3ステップ（`./dev.sh up demo --fixture` → `./scripts/open-simulator.sh $(./dev.sh url demo)` → `screenshot`）
- 棲み分け: `references/tier-strategy.md` の Tier1 WebKit(CI) / Tier2 Simulator(手動) / Tier3 Appium(将来)
- 運用詳細（到達性, `SIMULATOR_DEVICE`, `check`/`list`/`verify`/`tap`）は `references/` を参照

### Tier 対応表（使い分け）

**棲み分け: Safari/iOS 固有の事象はローカル Mac の Simulator で解消、一般的なブラウザ互換は CI で実行。** 詳細は `references/tier-strategy.md` が正本。

| Tier | 対象 | 実行環境 | コマンド | 用途 |
| --- | --- | --- | --- | --- |
| Tier1 | Playwright WebKit エミュレーション | ubuntu CI 自動 | `BROWSER=webkit uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_ios.py -q` | **一般的なブラウザ互換**は CI で自動（PR 毎） |
| Tier2 | Simulator 本物 Safari（`xcrun simctl openurl booted <url>`） | macOS ローカル手動 | `bash scripts/open-simulator.sh $(./dev.sh url demo)` + `xcrun simctl io booted screenshot .omo/evidence/sim.png` | **Safari/iOS 固有の事象**はローカル Mac で解消 — SW / 容量バー 0/0 / 回転・spread / PWA offline |
| Tier3 | Appium + WebDriverAgent | 将来 | `appium` + `xcuitest` driver + 署名（手順のみ `references/tier-strategy.md` の `<details>`、実装しない） | 完全自動化が必要になった時のみ |

### 到達性（host network share）

- Simulator はホストのネットワークを共有（host network share）するため追加ポート開放は不要。`dev.sh up demo --fixture` で立つ `https://dev-demo.<tailnet>.ts.net` に `xcrun simctl openurl booted <url>` でそのまま到達できる。
- 前提: ホストが tailnet に参加済みであること。配信は `tailscale serve`（HTTPS :443 → viewer:8080）のみで、ホストポート公開はしない。
- URL は `dev.sh url demo` で取得する（`./dev.sh up demo --fixture` → `bash scripts/open-simulator.sh $(./dev.sh url demo)` → `xcrun simctl io booted screenshot .omo/evidence/sim.png`）。
- GHA は `ubuntu-latest` 自動（`pytest tests/ -q`）+ Simulator を使う `macos` は手動 `workflow_dispatch` のみ。ローカル Mac では `bash scripts/open-simulator.sh verify` で一連検証（`check` → `boot` → `open https://example.com` → `screenshot`）。

人間向けの1枚要約は `docs/ios-simulator.md`（スキルへの thin stub）。

## Android 実機タッチ回帰（Tier 2 / ローカル）

Android Chrome の既存タブに ADB + CDP で接続し、端末レベルの tap/swipe を実行します。ページ状態とグリッドの scrollTop をアサートし、JSON と各ステップのスクリーンショットを保存します。CDP 合成 touch event ではなく adb shell input を使うため、合成イベントだけのテストと区別できます。

前提: Android 端末が unlock 済みで adb devices に表示されること（ワイヤレス ADB の IP 直結と mDNS エイリアスによる2表示はハードウェアシリアルで自動 dedupe。物理的に別の端末が複数ある場合のみ --serial が必須）、端末 Chrome にリモートデバッグが有効なこと、PC から localhost:9222 の ADB forward が可能なこと。--url-match は対象タブの URL に一意に一致する文字列を指定します。一致タブが0件のときは --reader-url を am start で Chrome に開かせてからポーリングするため、事前にタブを開いておく必要はありません。画面がスリープ中は KEYCODE_WAKEUP + keyguard 解除を試み、復帰しなければ environment_unavailable として失敗します（PIN/パターン lock がある端末は事前に人間が解除してください）。

```bash
uv run --with websockets python3 scripts/android-touch-regress.py \
  --url-match dev-01.example.ts.net \
  --reader-url 'https://dev-01.example.ts.net/#/view/long/book?p=0' \
  --browse-url 'https://dev-01.example.ts.net/#/browse/long'
```

成功時は exit code 0、テスト失敗または環境未準備は exit code 2 です。実行結果は既定で .omo/evidence/android-touch-regress/<timestamp>/result.json に記録され、成功/失敗時のスクリーンショットも同じディレクトリに保存します。端末不在・ADB/CDP 接続不良は environment_unavailable として記録され、成功扱いしません。シナリオ URL は、複数ページの本と縦スクロール可能な十分な数のタイルを持つ browse フォルダを指定してください。

この初版は reader の左ゾーン tap / 左向き swipe と browse の縦 flick を検証します。実指との相関、EventLatency、性能ベースライン比較はまだ対象外です。性能測定は既存の .claude/skills/tile-flick-fps を併用してください。
