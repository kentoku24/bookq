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

# 実機 Safari (safaridriver)。macOS のみ・必ず直列（-n 不可 — Safari.app は
# WebDriver セッションをグローバルに 1 つだけ許可する）。selenium は
# requirements-dev.txt 同梱。初回のみ `sudo safaridriver --enable`
BROWSER=safari uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_ui.py -q
# ブラウザを使うテストだけ選ぶなら -m browser（fixture 要求で自動付与）
BROWSER=safari uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/ -q -m browser
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

## BROWSER=safari（実機 Safari / safaridriver）

`tests/safari/` の WebDriver アダプタが Playwright API サブセットを safaridriver に橋渡しし、既存のブラウザテストを**実機デスクトップ Safari** でそのまま実行する。WebKit エミュレーション（`BROWSER=webkit`）ではなく本物の Safari。

- 実行は必ず直列: Safari.app は WebDriver セッションを 1 つしか持てない。`pytest -n` は使わないこと（fixture が `PYTEST_XDIST_WORKER` 検出で即 raise する）。CI では `--num-shards N --shard-id i` で macOS ランナー間に分割する（`workflow-smoke.yml` の `safari-gate`）。
- 環境差のエミュレーション: safaridriver の自動化ウィンドウは occluded 扱いで `document.hidden === true` になり、rAF・CSS transition/animation が永久に tick しない。`_VISIBILITY_JS` が前景タブ相当を再現する（rAF→setTimeout、frozen animation の finish、`document.hidden`→false、style mutation 監視で transitionend を合成）。
- 未対応（`SafariUnsupported` → skip として扱われる）: `page.route`/`context.set_offline`（ネットワーク傍受は WebDriver 非対応）、`context.new_cdp_session`（CDP は Chromium 専用 — `viewer_touch.py` の `touch_session` が JS TouchEvent shim に自動切替）、複数 context/newContext 並行、`ios_page`/`ipad_page`/`ipad_landscape_page`（デスクトップ Safari はモバイル UA/touch をエミュレートできない — iOS 実機相当は Tier2 Simulator が正本）。
- `expect` は `pytest_configure` で `playwright.sync_api.expect` を `tests.safari.expect` に差し替えるのでテスト本体の import は無変更。

### 到達性（host network share）

- Simulator はホストのネットワークを共有（host network share）するため追加ポート開放は不要。`dev.sh up demo --fixture` で立つ `https://dev-demo.<tailnet>.ts.net` に `xcrun simctl openurl booted <url>` でそのまま到達できる。
- 前提: ホストが tailnet に参加済みであること。配信は `tailscale serve`（HTTPS :443 → viewer:8080）のみで、ホストポート公開はしない。
- URL は `dev.sh url demo` で取得する（`./dev.sh up demo --fixture` → `bash scripts/open-simulator.sh $(./dev.sh url demo)` → `xcrun simctl io booted screenshot .omo/evidence/sim.png`）。
- GHA は `ubuntu-latest` 自動（`pytest tests/ -q`）+ Simulator を使う `macos` は手動 `workflow_dispatch` のみ。ローカル Mac では `bash scripts/open-simulator.sh verify` で一連検証（`check` → `boot` → `open https://example.com` → `screenshot`）。

人間向けの1枚要約は `docs/ios-simulator.md`（スキルへの thin stub）。
