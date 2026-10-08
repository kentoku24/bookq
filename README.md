# bookq

ローカルの画像フォルダをブラウザで読むためのビューア。Google OAuth 認証付き。Docker Compose で完結。

> 本ツールは、自炊・正規購入など適法に入手した画像を、自分で管理するサーバーから読むためのものです。海賊版の取得・配布やDRM解除には使用しないでください。

## 構成

**本番** — tailscale サイドカーが唯一の入り口（ホストにポートを公開しない）:

```
ブラウザ ── HTTPS ──▶ tailscale ── HTTP ──▶ viewer (:8080)
 (Tailnet内)        (TLS終端 + Serve)         FastAPI + hypercorn
                     bookq.….ts.net
```

**開発環境** — 環境ごとに独立した tailnet ノード（本番と同じ構成パターン）:

```
                                        ┌─ dev-feat-a.….ts.net ─▶ viewer (ブランチ A)
ブラウザ ── HTTPS ──▶ tailnet ──────────┼─ dev-feat-b.….ts.net ─▶ viewer (ブランチ B)
 (Tailnet内)        （ノード dev-* が各 1 個）└─ dev-3ebd9e9.….ts.net ─▶ viewer (特定コミット)
```

| コンテナ | 役割 |
|---|---|
| `viewer` | 画像ビューア + API (FastAPI) |
| `tailscale` | 本番の Tailscale ノード、TLS 終端、tailnet 内公開（開発環境は環境ごとに `dev-<名前>` ノードを持つ） |

## 必要なもの

- Docker
- Tailscale アカウント
- Google Cloud Console で OAuth 2.0 クライアントを作成

## 構築手順

### 0. Google OAuth の設定

1. [Google Cloud Console](https://console.cloud.google.com/apis/credentials) で OAuth 2.0 クライアント ID を作成
   - アプリケーションタイプ: **ウェブアプリケーション**
   - 承認済みのリダイレクト URI: `https://<your-tailscale-hostname>/auth/callback`
     - 具体例は [docs/deployment.md](docs/deployment.md) を参照
2. 発行されたクライアント ID とクライアントシークレットを `.env` に設定
3. セッション署名鍵を生成:
   ```bash
   openssl rand -hex 32  # → SECRET_KEY に設定
   ```
4. アクセスを許可する Google アカウントのメールアドレスを `ALLOWED_EMAILS` に設定（カンマ区切りで複数指定可能）

### 1. Auth Key の取得

https://login.tailscale.com/admin/settings/keys で **Reusable** な Auth Key を発行する。

### 2. 環境変数の設定

```bash
cp .env.example .env
# .env の各変数を実際の値に書き換え
```

### 3. 起動 / 停止

```bash
./prd.sh up      # 起動（`./prd.sh` でも同じ）
./prd.sh down    # 停止（認証情報などのボリュームは保持される）
```

`https://<your-tailscale-hostname>` でアクセス可能。

## API

| エンドポイント | 認証 | 説明 |
|---|---|---|
| `GET /` | 要 | 画像ビューア |
| `GET /api/health` | 不要 | ヘルスチェック |
| `GET /api/version` | 不要 | ビルド情報 {version, sha, build_date} |
| `GET /api/folders` | 要 | フォルダツリー一覧 |
| `GET /api/images?folder=...` | 要 | フォルダ内の画像一覧 |
| `GET /image/<path>` | 要 | 画像ファイルの配信 |
| `GET /api/search` | 要 | メタデータ検索（q / artist / tag / sort=relevance） |
| `GET /api/metadata/facets` | 要 | フィルタの選択肢（アーティスト・タグ）。?path= でサブツリー限定（省略時は全体） |
| `GET /auth/login` | 不要 | Google OAuth ログイン |
| `GET /auth/callback` | 不要 | OAuth コールバック |
| `GET /auth/logout` | 不要 | ログアウト |

## 拡大と俯瞰

- **ピンチアウト**でページを 1.0〜4.0 倍に拡大し、1 本指でパンできます。拡大中は左右のタップゾーンが無効になり、**中央タップで 1.0 に戻ります**。端まで寄せた状態からさらに同じ方向へ 80px ドラッグすると（振動で予告）次／前のページへ進み、倍率は 1.0 に戻ります。ページ送り（タップ／スワイプ／キー／スライダー）はどの経路でも 1.0 にリセットされます。
- **ピンチイン**（0.5 倍まで縮める）で、シリーズ全体を右→左のグリッドで俯瞰します。チャプターごとに見出しが付き、現在ページは枠線で示されます。タイルをタップするとそのページを開いて読書に戻ります。ツールバーの ⊞ ボタンまたは `G` キーでも開閉でき、`Esc`・下端の ✕・俯瞰内でのピンチアウトでも閉じられます。
- ネイティブのブラウザズームはビューア内で無効です（Safari の「ピンチインでタブ一覧」も出ません）。ブラウズ画面のアクセシビリティズームはそのまま使えます。

## オフライン閲覧

一度開いた作品は自動で保存され、圏外でも読めます。明示的に保存したい作品はリストからオフライン保存できます。

### 自動キャッシュ

ビューアで表示したページは Cache API に自動保存されます。`PREFETCH_AHEAD` で先読みしたページも含まれ、追加の操作は不要です。自動保存は容量不足でもダイアログを出さずに静かにスキップするので、閲覧を邪魔しません。

### リストペインでオフライン保存 / 削除

ブラウズのリストペインで作品ごとに保存を切り替えられます。tile / list / card の全表示モードに対応しています。

- **tile**: サムネ右上の `.offline-btn` ボタン。未保存なら**単タップで即オフライン保存開始**、完了すると `↓` から `✓` に変わり `state-idle` から `state-complete` になります。進行中はボタン周囲に穴あきの進捗リング（`conic-gradient` + マスク）が表示され、中央のグリフは常に見えます
- **list**: 行内の右上 `.offline-btn` ボタンで同じ単タップ開始 → リング → `✓` の遷移（tile と同一）
- **card**: 同じ右上 `.offline-btn` ボタンで同じ遷移（tile と同一）
- **DL中に単タップで即停止**します。取得済みのページは保持され部分保存（`state-partial`）になり、1件も保存できていなければ未保存に戻ります。停止確定までの一瞬は `state-stopping`（■表示＋ロック）になります
- 作品一覧の取得中（各 leaf の `/api/images` 応答待ち）は `is-listing` クラスで回転弧スピナを表示します（`prefers-reduced-motion` では静止弧）
- 検索結果（tile / list / card）でも同じ右上 `.offline-btn` ボタンが表示されます。部分保存のボタンをタップするとボタン直下にラベル付き popover（`.ux-pop`）が開き、`続きをダウンロード`（残りページ数）と `削除`（保存済みページ数）の2択を選べます。popover は1つだけ開き、再タップで閉じ、画面最下部では上側へ反転し、`Esc` / 外側タップでも閉じられます（`Esc` で閉げてもサイドバーは開いたまま）。ボタンと popover は実 `<button>` なのでキーボードの Enter / Space でも操作できます
- 削除は従来どおり2タップ確認です（完了 `✓` をタップ → 赤🗑 表示 → 再タップで確定、外側タップ / `Esc` で戻る）。削除実行中はロックされ、Undo はありません
- オフライン保存は `GET /api/images?folder=` で一覧を取得し、各画像を `fetch` して Cache Storage に保存します。削除は `caches.delete` と IndexedDB のメタ削除で解放されます

### 容量バーとオンライン表示

サイドバーのヘッダ直下に状態を表示します。

- **オンライン表示**: `●オンライン` / `○オフライン`（オフライン時は琥珀色 `#c9a24f`）。`navigator.onLine` ではなく `GET /api/health?online_check=1`（Service Worker がキャッシュを介さず素通しする専用経路）の到達確認で判定するので、tailnet だけが切れた状態でも正しくオフラインになります
- **容量バー**: `使用中 X / Y GB` と使用率バー。`navigator.storage.estimate()` で取得し、保存完了やオンライン状態の変化で更新されます。80%で黄、90%で赤に変わります
- 容量不足で明示的なオフライン保存が失敗したときはアプリ内モーダル（`保存容量が不足しています`）が出て、`削除して続行` / `キャンセル` を選べます（`window.confirm` は廃止）。続行すると最も古い作品を削除して1回だけ再試行し、キャンセル時は中断を通知します。自動キャッシュの容量不足はダイアログを出しません

### Safari 7日注意

iPad Safari では7日間起動しないとオフライン保存が自動削除されることがあります。

- 週1回はアプリを開いてください。起動時に保存済み作品のキャッシュを1件だけ触り直し（touch）、LRU の削除対象から外します
- 初回の操作時に `navigator.storage.persist()` を要求し、永続化が得られない Safari では `オフラインを保持するにはホーム画面に追加してください` と案内します
- 常設の注意バナーは出しません（表示が読書の邪魔になるため）。ホーム画面に追加すると削除されにくくなります

### 技術概要

- Service Worker は `/sw.js` で配信されます（`app/static/sw.js` を `Service-Worker-Allowed: /` ヘッダ付きで返却）。`/sw.js` と `/static/sw.js` の両方で同じ内容が取得でき、scope は `/` です。tailnet の HTTPS（Secure Context）でのみ登録され、`?offline=1` または `localStorage.bookq_enable_sw=1` でテスト時も有効化できます
- 画像は CacheFirst（作品ごとの `bookq-work-<encoded>-v1`、フォールバックは `bookq-images-fallback`）、API は NetworkFirst（`bookq-api-v1`）、ナビゲーションは NetworkFirst で失敗時にまず同一 URL のキャッシュ、次に `/static/offline.html`、最後に precache 済みの `/` にフォールバックします。`Range` リクエストはキャッシュをバイパスします
- メタデータは IndexedDB `bookq-offline` の `works` ストア（keyPath `folder`）で管理し、フォルダ、キャッシュ名、ページ数、キャッシュ済み数、容量、サムネ、追加日時、最終閲覧日時を保持します。キャッシュ名は `cacheNameFor(folder) => 'bookq-work-'+encodeURIComponent(folder)+'-v1'` で生成され、`/` は `%2F` に置換されます
- `manifest.json`（`display: standalone`）と `offline.html`、`icons`（192 any / 512 any / 512 maskable の3点）で PWA の最小構成を満たします。`theme_color` / `background_color` は `#0f172a`、`display` は `standalone`、`id` は `/`

### アイコン

アイコンは `app/static/icons/icon-192.png` / `icon-512.png` を正としつつ `favicon.svg` は `app/static/logo.svg` を参照して手描き（トレース生成はユーザー許可 2026-08-29 で可）、派生は Pillow で生成する。ファイル一覧: `app/static/favicon.svg` / `app/static/favicon-32x32.png` / `app/static/favicon.ico`（16/32/48） / `app/static/apple-touch-icon.png`（180x180 opaque RGB） / `app/static/icons/icon-512-maskable.png`（512x512）。`manifest.json` は `192 any` / `512 any` / `512 maskable` の3エントリに分離し `any maskable` の一括指定は使わない（maskable は `#0f172a` パディングで safe zone 80% を担保）。生成例: `python3 -c "from PIL import Image; im=Image.open('app/static/icons/icon-512.png'); im.resize((180,180), Image.LANCZOS).save('app/static/apple-touch-icon.png')"`、`python3 -c "from PIL import Image; i32=Image.open('app/static/favicon-32x32.png'); i16=i32.resize((16,16), Image.LANCZOS); i32.save('app/static/favicon.ico', sizes=[(16,16),(32,32),(48,48)])"`、maskable は `python3 -c "from PIL import Image; bg=Image.new('RGB',(512,512),'#0f172a'); fg=Image.open('app/static/icons/icon-512.png').resize((408,408), Image.LANCZOS); bg.paste(fg,(52,52)); bg.save('app/static/icons/icon-512-maskable.png')"`。

### オフライン検証ハーネス

開発時のオフラインQA用に、Service WorkerやIndexedDBの状態を一括で可視化する独立ハーネスを用意しています。

**開き方**

```
https://dev-*.ts.net/static/offline-harness.html
https://dev-*.ts.net/static/offline-harness.html?harness=1  # 後方互換で有効
```

`dev-*` は `dev.sh` が立てる開発環境のノード名に対応します。本番でも同じパスで取得できます。`?harness=1` なしでも直接開けます。

**配信とゲート**

`app/static/offline-harness.html` は `app/main.py:28` の `StaticFiles` マウントで常時配信されます。常時表示 (tailnet信頼境界で保護). サイドバーの🧪Harnessリンクは常に表示され、/static/offline-harness.html は ?harness=1 なしでも直接開けます (旧 ?harness=1 / localStorage.bookq_harness 方式は不要になりましたが後方互換で残存).

**何が見えるか**

- `caches.keys()` と各 `caches.open(k).keys()` の集計 (cache 数と entry 数)
- IndexedDB `bookq-offline` の `works` 全件 (folder, cacheName, pages, bytes, thumb, addedAt) と `raw sum` と `dedup sum` の比較
- `navigator.storage.estimate()` の raw JSON と `usage` と `dedup` の差分
- leaf 列挙 (`getLeafPathsFor`) の対象選択と件数表示
- 10回の `/api/health?online_check=1` 監視によるオンライン判定シグナル
- `health abortでオフライン` (AbortController で health fetch を中断) と `全断(setOffline)` (`window.dispatchEvent(new Event('offline'))`) のハイブリッド操作
- バージョン (⑤): /api/version から version/sha/build_date を表示
- 全消去 (⑥): 保存作品とAPI/FALLBACKキャッシュ + 読書進捗を全消去 (確認モーダル, SHELL保持)

**10の失敗モードと再現手順**

| # | 失敗モード | 再現手順 |
|---|---|---|
| 1 | navigate の ignoreSearch 未ヒット | `caches.match("/?offline=1", {ignoreSearch:true})` が miss する。SW の `ignoreSearch` 指定が無いと `?offline=1` 付きナビゲーションがオフラインで 404 になる |
| 2 | `/api/folders` の NetworkFirst フォールバック失敗 | オンラインで一覧を取得後 `page.context.set_offline(True)` にして再取得。キャッシュが無いとオフラインで空になる |
| 3 | 親フォルダのみのオフライン保存が 0 件 | `group/episodes` のような親フォルダに対して `getLeafPathsFor` が空配列を返すと保存対象が 0 件になる |
| 4 | `group/episodes` の flatten 越え leaf 潰れ | `scan_tree` の単一子 flatten で `group/episodes` が消え、leaf 列挙が 1 件に潰れる。ハーネスの `getLeafPathsFor('group/episodes').length` が 4 であることを確認 |
| 5 | 二重計上と削除リーク | 親と子を同時に保存すると `raw sum` が二重になる。`folderSet` と `startsWith(parent+"/")` の dedup で親をスキップし、削除後は `caches.delete` と IDB delete で 0 に戻ることを確認 |
| 6 | storage 表示の await 競合 | `navigator.storage.estimate` が遅延したときに badge が先に 0/0 で固まる。ハーネスでは 800ms 以内に `storage-text` が更新されることを確認 |
| 7 | iOS の estimate 0/0 と Content-Length 欠損 | `estimate()` が `{usage:0, quota:0}` を返しつつ `content-length` ヘッダが無いと bytes が 0 になる。`sizeClone.blob()` フォールバックで bytes が 0 にならないことを確認 |
| 8 | MB/GB の丸め不一致 | `fmt(50_000_000)` が `50.0 MB`、`fmt(1_500_000_000)` が `1.5 GB` にならないと容量バーとハーネスの表示がずれる。境界 `1e9` で `1.0 GB` になることも確認 |
| 9 | VPN 分断時の health abort 検出漏れ | `page.route('/api/health*', abort)` だけでは SW 経由で検出できない環境がある。`fetch` を reject するモックで `○オフライン` に遷移することを確認。ハーネスの `health abortでオフライン` ボタンでも同じ遷移を再現 |
| 10 | Response clone 消費順と quota 超過ハンドリング | `cacheClone` より先に `sizeClone.blob()` を消費すると put が失敗する。`cacheClone` `sizeClone` の順で clone し、`QuotaExceededError` 時に `confirm` で最古作品の削除か `容量不足` toast が出ることを確認 |

**curl での配信確認 (動的ポート注意)**

`server` fixture は毎回ランダムな空きポートで uvicorn を起動します。固定の 8080 や 18080 ではないので、テストや手元確認では起動ログや `server` fixture が返す URL のポートを使います。

```bash
# 例: fixture が http://127.0.0.1:54321 を返した場合
curl -s http://127.0.0.1:$PORT/static/offline-harness.html | grep -q "Offline Harness" && echo ok
# 手元で一時起動して試す場合
BOOKQ_TEST_MODE=true DOWNLOADS_DIR=/tmp/downloads uv run --with-requirements requirements-dev.txt uvicorn app.main:app --port $PORT &
curl -s http://127.0.0.1:$PORT/static/offline-harness.html | grep -q "Offline Harness" && echo ok
```

`grep -q "offline-harness" README.md` と `curl` の両方が通れば、StaticFiles 配信とゲート分離が保たれています。`.dockerignore` に `app/static` の除外が無いこと（`! grep -q "app/static" .dockerignore`）と、`Dockerfile:10` の `COPY app/ ./app/` で `app/static/offline-harness.html` がイメージに含まれることも併せて確認します。

## 停止

```bash
./prd.sh down
```

状態（認証情報）は Docker ボリュームに永続化されるため、再起動時に再認証は不要。
`./prd.sh down` はボリュームを残す。ボリュームごと完全に削除したい場合のみ `docker compose down -v` を使う。

## 開発

### 全体像

本番はひとつだけ、開発環境はいくつでも。両者は compose のプロジェクト名で完全に分離されています。

| | 本番 | 開発環境 |
|---|---|---|
| 起動/停止 | `./prd.sh up` / `./prd.sh down`（メインのチェックアウトのみ） | `./dev.sh up` / `./dev.sh down`（どこからでも、何個でも） |
| URL | `https://bookq.<your-tailnet>.ts.net` | `https://dev-<名前>.<your-tailnet>.ts.net` |
| 認証 | Google OAuth | なし（`BOOKQ_TEST_MODE=true`） |
| 画像ツリー | 読み書き可 | **読み取り専用** (`:ro`) |
| コンテナ | viewer + tailscale サイドカー | viewer + tailscale サイドカー（ノードは環境ごとに 1 つ） |

> **本番の compose を検証に流用しないこと。** worktree で素の `docker compose up` を打つと、ホスト名 `bookq` を要求する 2 つ目の tailscale ノードが立ち上がって本番と名前を争います。`prd.sh` は worktree からの実行を拒否し、`docker-compose.dev.yml` は `DEV_IMAGE` / `DEV_NAME` 等の必須変数（`:?`）が無いと起動自体が失敗するので、`dev.sh` を使う限りこの事故は起きません。

### 1. Docker なしで動かす

UI をさっと触るだけならこれが一番速い（OAuth もスキップされます）。

```bash
BOOKQ_TEST_MODE=true DOWNLOADS_DIR=/path/to/images \
  uv run --with-requirements requirements-dev.txt uvicorn app.main:app --port 8080
```

### 2. テスト

Playwright で実サーバを立てて叩く UI テストです。

```bash
uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/ -q
```

個別に実行:

```bash
uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_ui.py::test_search_filters_current_level -q
```

Chromium が無ければ初回だけ:

```bash
uv run --with playwright playwright install chromium
```

`--with-requirements requirements-dev.txt` は省略できません。`server` フィクスチャが `sys.executable -m uvicorn` を起動するので、pytest と同じ環境に fastapi/uvicorn が必要です。`uvicorn[standard]` は開発用の `requirements-dev.txt` 側にあり、ランタイムの `requirements.txt`（本番は hypercorn）には含まれません。

リンタ・フォーマッタは導入していません。

### 3. 開発環境（実機・実データでの動作確認）

任意のブランチ／コミットを、いくつでも立てられます。`dev.sh` はリポジトリ内の任意フォルダから実行できます（git 管理下に限る）。

```bash
./dev.sh up                                       # 現在の作業ツリーから
./dev.sh up review-42 --ref main                  # 名前と ref を明示
./dev.sh up old --ref 3ebd9e9                     # 任意のコミットでも可
./dev.sh up demo --fixture                        # 実データの代わりに合成テストツリーをマウント
```

`--fixture` を付けると、`tests/fixtures/uiux_tree.py`（依存パッケージ不要の色分け PNG 生成スクリプト）が作る合成ツリー — 9 フォルダ + metadata.toml 3 件（`simple_manga` / `series` / `mixed_ar`）— を `/tmp/bookq-fixture-<name>/downloads` に生成してマウントします。実データに手を付けたくない UI の動作確認やスクリーンショット用途に使います。単色ページは拡大しても違いが分からないので、ピンチズーム・ページ送り・エピソード境界の目視確認には `zoom_episodes/series/ep-1..3`（720x1080、グリッド・巨大な EP/ページ番号・START/END バッジ・拡大時のみ読める細字）を使います。ディープリンクは `#/view/zoom_episodes/series/ep-1?p=0`。兄弟チャプター連結は深さ2以上でしか発動しないため、`series/` を1段挟んでいます。マウント元は `DOWNLOADS_HOST_DIR` 環境変数で上書きも可能です（`--fixture` が指定された場合はそちらが優先。既存の環境変数上書きの挙動は従来どおり）。

表示された `https://dev-review-42.<your-tailnet>.ts.net` は tailnet 内なら **iPad からもそのまま開けます**。ホストからも同じ HTTPS URL でアクセスします（ポートは公開しません — ホストは tailnet に参加している前提）。

| コマンド | 動作 |
|---|---|
| `./dev.sh up [name] [--ref REF] [--fixture]` | 起動（既存なら同じ環境を作り直し）。`--fixture` で実データの代わりに合成テストツリーをマウント |
| `./dev.sh down [name]` / `--all` | 撤去（ボリュームと tailnet ノードの登録は保持）。`--full` でボリューム・イメージ・ノード登録まで完全撤去（TS_API_KEY 設定時。`--all --full` は `--yes` 必須） |
| `./dev.sh list` | 一覧（名前・状態・ref・commit・URL） |
| `./dev.sh url [name]` | URL だけ出力（スクリプト用） |
| `./dev.sh logs [name] [-f]` | ログ |
| `./dev.sh gc` | 未参照・dangling イメージの回収（up 成功時にも自動実行） |

環境名を省略すると現在のブランチから導出されます（`origin/`・`claude/`・`bookq-` の接頭辞は落とす）。**URL はノード名 `dev-<名前>` から決定的に決まるので、ポートの割り当ては存在しません。**

**`--ref` と作業ツリーの違い:**

| 指定 | ビルド元 | 使いどころ |
|---|---|---|
| `--ref <branch\|tag>` | `git archive` でそのコミットから直接 | 決定的。作業ツリーの状態に左右されず、worktree も不要。同じ ref は常に同じタグ（`bookq-dev:ref-<名前>`）に上書きするので、作り直してもイメージは 1 枚 |
| `--ref <hex SHA>` | 同上 | そのコミット専用タグ `bookq-dev:<短縮SHA>` で再利用（同じ SHA なら再ビルドしない） |
| 省略 | 現在の作業ツリー | 未コミットの変更を確認したいとき |

### 3b. ローカルモード（tailscale 無しで localhost 公開）

tailscale に参加せず `http://127.0.0.1:<port>` で確認する軽量モード。`TS_AUTHKEY` も `DEV_TS_DOMAIN` / `BASE_URL` も不要。

```bash
./dev.sh up [name] --local [--port N] [--lan-allow] [--fixture]
./dev.sh up demo-local --local --fixture               # 既定 http://127.0.0.1:8080
./dev.sh up demo-local2 --local --port 8081 --fixture  # 2つ目は --port が必須（衝突時はビルド前に失敗）
```

- 既定は `http://127.0.0.1:8080`。2つ目の同時環境は `--port 1024-65535` で別ポートを指定する（使用中ポートはビルド前に fail fast する）。
- 認証は `BOOKQ_TEST_MODE=true` でスキップされる（tailnet モードと同じ）。画像ツリーは読み取り専用。
- `--lan-allow` は bind を `0.0.0.0` に変える opt-in。`0.0.0.0` + `BOOKQ_TEST_MODE=true` + 開発用 `SECRET_KEY` の組み合わせは LAN 内に未認証の読み取り専用ツリーを晒すので、検証用 LAN でのみ使うこと。
- Secure Context 注意: `--lan-allow` で起動した場合も、普段の確認は `http://127.0.0.1:<port>` で開くこと。`http://127.0.0.1` は Secure Context だが、素の `http://<lan-ip>` は Secure Context ではない。そのため Service Worker とオフライン QA は `app/static/app.js:32` の条件で黙ってスキップされ、`?offline=1` か `localStorage.bookq_enable_sw=1` が無いと登録されない。LAN IP 経由の iPad ではオフライン QA はできない。
- iOS Simulator からもそのまま開ける（host network 共有）: `./scripts/open-simulator.sh http://127.0.0.1:<port>`。helper は `https://` と loopback `http://` のみ受理し、`http://<lan-ip>` は Secure Context でないため拒否する。手順は `.claude/skills/ios-simulator/references/overview.md: Local mode`。
- 10スロットプールを共有する。ローカル環境も `find_free_slot` の割当に数えられ、10/10 枯渇時は通常環境と同じく `pool exhausted (10/10)` で失敗する（仕様として許容）。
- `down` / `list` / `url` / `logs` はそのまま使える。`list` と `url` はローカル行に `http://<bind>:<port>` を出し、非ローカル行の `https://dev-XX` 形式は変わらない。`down` と `logs` の挙動は tailnet モードと同じ。

### 4. 複数エージェント／worktree での並行開発

各 worktree が自分専用の開発環境を持てます。URL は環境名から決定的に決まるので、レビュー依頼にそのまま貼れます。

```bash
# エージェント A の worktree で
./dev.sh up feat-a --ref claude/feat-a

# エージェント B の worktree で（同時に実行して問題ない）
./dev.sh up feat-b --ref claude/feat-b
```

同時に叩いても壊れないのは、ロックや状態管理の成果ではなく**構造**によるものです。

- 各環境は独立した compose プロジェクト（別ネットワーク・別コンテナ・別ボリューム・別 tailnet ノード）で、環境間で共有するリソースが存在しない
- URL は名前から決定的（`https://dev-<名前>.ts.net`）。ブックマークは環境名を変えない限り無効にならない
- 同じ名前の環境は 1 つだけ — 別の worktree が同じブランチで `up` すると作り直す（従来と同じ「最後勝ち」）
- ノード名が衝突した場合（同じ名前の環境を 2 つ同時に up した等）は、URL 検証が失敗して明示的に分かる

`tailscale serve` の設定はサイドカーの entrypoint が起動時に固定で行うため、`reconcile` のような収束操作は存在しません。使い終わったら `down` してください。`restart: unless-stopped` なのでスリープや Docker 再起動をまたいで生き残ります。

### 5. Git Hook（コミット時に開発環境を自動更新）

`app/` 配下・`requirements.txt`・`Dockerfile` を変更してコミットすると、pre-commit hook が**その worktree の開発環境**を更新し、URL を表示してコミットを一旦止めます。本番には触りません。

1. コードを変更して `git add <files>`
2. `git commit` → 開発環境が更新され、URL が表示されて停止
3. その URL で動作確認（iPad からも可）
4. もう一度 `git commit` → マーカーがあるのでリビルドをスキップし、コミットが通る

初回だけインストールが必要です。

```bash
./setup-hooks.sh
```

有効か確認（worktree でも動くパス解決を使うこと。`.git/hooks` を直に指定すると worktree で壊れます）:

```bash
ls -la "$(git rev-parse --git-path hooks)"
```

フックは全 worktree で共有されますが、一時停止用のマーカーは worktree ごとに独立しているので、複数の worktree で同時にコミットしても干渉しません。Docker が動いていない場合は警告してスキップするだけで、コミットはブロックしません。

### 6. 本番へのデプロイ

**メインのチェックアウトから**実行します（worktree からは拒否されます）。今いるブランチや作業ツリーの dirty 状態に左右されず、任意のブランチ・dirty 状態から実行できます。デプロイされるのは `--ref` のコミット済み内容のみで、作業ツリーの dirty は含みません（既定 `origin/main`。`origin/*` のみ指定可。解決できない ref は `git fetch origin` 後に再実行すること。自動 fetch はしません）。

```bash
./prd.sh up                          # 起動（origin/main のコミット済み内容をデプロイ）
./prd.sh up --ref origin/release-1.2 # 指定 ref のコミット済み内容をデプロイ
./prd.sh down                        # 停止
./rebuild.sh prd build <sha>         # 再ビルドのみ（rebuild.sh は prd / dev 共通のデプロイエンジン）
./rebuild.sh prd deploy              # prebuilt image で起動＋検証（素の `./rebuild.sh prd` は usage を出して終了）
```

ビルド → コンテナの healthcheck 待ち → **公開 URL への実 curl** まで（`rebuild.sh` が実行）行うので、「コンテナは起動しているのに配信されていない」型の障害を検出できます。worktree ガードは `prd.sh` にのみあり、`rebuild.sh` 単体にはありません（`dev.sh` からも開発環境デプロイに使うため）。

### 困ったときは

| 症状 | 対処 |
|---|---|
| 本番が 502 | viewer が compose ネットワークから外れています。`docker inspect $(docker compose ps -q viewer) --format '{{json .NetworkSettings.Networks}}'` で確認し、迷子のコンテナを消して `./prd.sh up --ref <ref>`（または `./rebuild.sh prd build <sha>` → `./rebuild.sh prd deploy`）で作り直す。素の compose での viewer 再ビルドは作業ツリーの dirty からビルドするため使わないこと |
| 開発環境の HTTPS に届かない | tailnet ACL で `dev-*` ノード宛の HTTPS (443) を許可する（旧構成の `8100-8199` 許可は不要）。それでも駄目なら `./dev.sh list` で環境の状態を確認 |
| ノード名が衝突した | 同じ名前の環境を 2 つ立てようとしています。`./dev.sh list` で確認して不要な方を `down` してから `up`。名前を完全に解放したい場合は `down <name> --full`（TS_API_KEY 設定時はノード登録も削除） |
| 開発環境が中途半端な状態 | `./dev.sh down <name>` してから `up` し直す |

より詳しい背景と設計理由は [docs/deployment.md](docs/deployment.md) にあります。

## AI 自動実装

issue を起点に AI エージェントが実装や調査を自律実行する仕組み。詳細なセットアップ手順は [docs/openresearch-auto-impl.md](docs/openresearch-auto-impl.md) を参照。

### 入口

| 入口 | トリガー | 動作 |
|---|---|---|
| `ai/implement` | issue に `ai/implement` ラベルを付与 | エージェントが `ai/<番号>-<slug>` ブランチで実装し `feat(ai): ... (#N)` 形式の PR を作成 |
| `ai/research` | issue に `ai/research` ラベルを付与 | issue へサマリーコメントを投稿し、詳細レポート `docs/reports/YYYY-MM-DD-<slug>.md` を含む PR を作成 |
| `/oc` | issue / PR コメントの先頭に `/oc` または `/opencode` | 追加指示をエージェントに渡す（メンションチャネル）。例: `/oc テストも追加して` |
| plan-track | .omo/plans/<slug>.md を起点に /start-work | issue なしで ai/<slug> ブランチ・番号なし題 PR（本文に Plan パス） |

bot 経路はいずれも `AGENTS.md` の規約（ブランチ命名 `ai/<issue番号>-<slug>`、PR タイトル `<type>(ai): <要約> (#N)`、deny ルール等）に従う。bot 排除と信頼著者ゲート（`OWNER` / `MEMBER` / `COLLABORATOR` のみ）を通過したイベントだけが実行される。

ラベル遷移は `🤖 processing`（実行中ロック）→ 成功時 `✅ done` / 失敗時 `❌ failed` + Actions ログ URL コメント。`🤖 processing` が残り続ける場合は失効ロック（stale lock）であり、次回 gate 通過時に自動削除される。

### openresearch ブリッジ（`target:bookq`）

`kentoku24/openresearch` 側で `target:bookq` ラベルが付いた issue を bookq へ自動ミラーするブリッジ。

- ワークフロー: `.github/workflows/openresearch-bridge.yml`
- スケジュール: 6 時間ごと（`0 */6 * * *`）に `target:bookq` ラベルの open issue をスキャンし、未ミラーなら bookq に `ai/implement` 付き issue を作成
- 手動実行: `workflow_dispatch` で `source_issue_number` を指定、または `repository_dispatch`（`openresearch-issue`）経由
- ミラー本文には `Related: kentoku24/openresearch#<番号>` を含めて相互トレーサビリティを確保し、openresearch 側へも `Mirrored to bookq#<番号>` コメントを返す
- ミラーで作成された issue は `github-actions[bot]` 起票のため gate の bot 判定ではブロックされるが、コンシューマワークフロー `.github/workflows/opencode-auto.yml` が `repository_dispatch` type `openresearch-issue`（`client_payload[issue_number]` / `client_payload[route]=implement`）をリッスンし、issue 本文中の `Related: kentoku24/openresearch#<N>` マーカーを検証した後に opencode 実行を行う（マーカーの無い偽装 dispatch は警告コメント付きで拒否される）
- ブリッジはプライベートリポジトリ `kentoku24/openresearch` を読み取るため、リポジトリシークレット `OPENRESEARCH_READ_TOKEN`（`kentoku24/openresearch` への READ 権限を持つ PAT または fine-grained token）が必須。未設定の場合、スケジュールスキャンは 401/404 を握りつぶして空振りし、何もミラーされない

### コスト

- **Zen 従量課金** — LLM 呼び出しは `OPENCODE_ZEN_API_KEY` のクレジットを消費する。モデルは `opencode-go/muse-spark-1.3-contributor`。
- **Actions 分課金** — private リポジトリのため GitHub Actions の実行分数も課金対象。
- **ラベルゲート = コストゲート** — `ai/*` ラベルを持つ信頼著者のイベントだけが LLM 呼び出しに到達する。
- **上限** — 信頼著者が N 件のラベル付き issue を連投すれば最大 N 本の run が並列起動する（concurrency は per-issue のみ）。フリート全体を止める唯一のハード制限値は **Zen クレジット上限**である。月次上限の設定は [docs/openresearch-auto-impl.md](docs/openresearch-auto-impl.md) §5 を参照。

### セキュリティ

- **認証** — ワークフローは `GITHUB_TOKEN` と `OPENCODE_ZEN_API_KEY` を使用する。`id-token: write` は公式 pinned Action を実行するジョブ（`opencode.yml` の `agent`、`opencode-auto.yml` の `implement` / `research`）にのみ付与されており、OIDC を GitHub App トークンに交換する処理はデフォルトでは配線されていない（App トークン認証が必要な場合は `actions/create-github-app-token` を追加する）。チェックアウトは `persist-credentials: false`。
- **最小権限 permissions** — ワークフローレベルは `contents: read` のみで、各ジョブが必要に応じて昇格する per-job モデル。公式 Action ジョブは `contents` / `issues` / `pull-requests: write` + `id-token: write`、direct-run ジョブは `contents` / `issues` / `pull-requests: write`（`id-token` なし）、`gate` / `cleanup` は `contents: read` + `issues: write`、ブリッジの `mirror` は `contents: read` + `issues: write`。
- **deny ルール**（`opencode.json`）— `rm -rf *`、`git push*main*`、force push、`curl * POST *`、`wget *`、外部ディレクトリアクセスを拒否。ただし事故防止ガードであり、exfiltration 対策の境界ではないことに注意。
- **ワークフロー権限** — リポジトリ既定の `default_workflow_permissions` は `read` のままでよい（PR 作成に必要な権限は各ワークフローが per-job `permissions` で宣言している）。詳細は [docs/openresearch-auto-impl.md](docs/openresearch-auto-impl.md) §3 を参照。

## ファイル構成

| ファイル | 説明 |
|---|---|
| `app/main.py` | FastAPI アプリケーション |
| `app/config.py` | 環境変数設定 |
| `app/auth.py` | Google OAuth 認証 + セッション管理 |
| `app/routes.py` | 全エンドポイント |
| `app/scanner.py` | フォルダ/画像スキャン |
| `app/templates/index.html` | ビューア HTML テンプレート |
| `requirements.txt` | Python 依存パッケージ |
| `docker-compose.yml` | **本番**: viewer + tailscale サイドカー |
| `docker-compose.dev.yml` | **開発環境**: viewer + tailscale サイドカー（1 環境 = 1 プロジェクト） |
| `Dockerfile` | viewer コンテナ |
| `prd.sh` | 本番の起動・停止スクリプト（`up` / `down`。メインのチェックアウト専用） |
| `rebuild.sh` | 汎用デプロイエンジン（`prd` / `dev build\|deploy\|verify`）。ビルド → healthcheck → URL 検証 |
| `dev.sh` | 開発環境マネージャ（up / down / list / url / logs） |
| `setup-hooks.sh` | git hook のインストール |
| `tailscale/` | Tailscale コンテナ |
| `.env.example` | 環境変数テンプレート |
| `docs/deployment.md` | デプロイと開発環境の詳細 |
