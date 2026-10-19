# デプロイと開発環境

本番はひとつだけ、開発環境は固定 10 スロットプール。両者は compose のプロジェクト名で完全に分離されている。

| | 本番 | 開発環境（プール） |
|---|---|---|
| compose ファイル | `docker-compose.yml` | `docker-compose.dev.yml` |
| プロジェクト名 | `bookq` | `bookq-dev-pool-01`〜`10`（ENV名はスロットにマッピング） |
| tailnet ノード | `bookq` | `dev-01`〜`dev-10`（固定 10 個を使い回し） |
| URL | `https://bookq.your-tailnet.ts.net` | `https://dev-XX.your-tailnet.ts.net`（XX=01..10） |
| 認証 | Google OAuth + メール許可リスト | なし（`BOOKQ_TEST_MODE=true`） |
| 画像ツリー | 読み書き可でマウント | **読み取り専用** (`:ro`) |
| 起動/停止方法 | `./prd.sh up` / `./prd.sh down`（メインのチェックアウトのみ） | `./dev.sh up` / `./dev.sh down`（どこからでも、Freeプランでノード再利用） |

---

## 開発環境

### 構成

各開発環境は本番と同じ構成パターン（viewer + tailscale サイドカー）の独立した compose プロジェクトだが、**tailnet ノードは固定 10 個（`dev-01`〜`dev-10`）をプールして使い回す**:

```
dev-01.your-tailnet.ts.net ──▶ viewer (例: ENV feat-a → slot 01)
dev-02.your-tailnet.ts.net ──▶ viewer (例: ENV feat-b → slot 02)
dev-03.your-tailnet.ts.net ──▶ viewer (例: ENV review-42 → slot 03)
```

`dev.sh up [name]` は空きスロット 01..10 から最初に見つかったものを割当、環境名との対応は `com.bookq.name` / `com.bookq.pool_slot` ラベル（`docker ps -a`）だけから導出するため外部の状態ファイルは不要。同じ名前で再 `up` すると同じスロットを冪等に再利用する。プールが 10/10 で枯渇すると `pool exhausted (10/10)` で失敗し、`down` で空ける必要がある。

ノード名 `dev-XX` がそのまま HTTPS URL（:443）になるため、**ポートの割り当ては存在しない**。serve の設定はサイドカーの entrypoint（`tailscale/entrypoint.sh` の既定 `http://viewer:8080`）が起動時に固定で行う。

スロットごとに `tailscale-state` ボリュームを持つので、`down`（ボリューム保持）後は同じ `dev-XX` の identity で再利用する。Free プランでは API でノード登録を削除できないため、この再利用で tailnet 上のノードを増やさずに運用できる。

環境間に共有するリソース（ネットワーク・serve 設定・ポート・ボリューム）が無いので、**排他ロックも serve の収束も不要**。並行実行の安全性は「構造的に干渉しない」ことで保証される。同じ名前の環境は 1 つだけ（後から `up` した方が作り直す）。

### 使い方

```bash
./dev.sh up                                   # 現在の作業ツリーから（空きスロット dev-01..10 に自動割当）
./dev.sh up review-42 --ref main              # 名前と ref を明示（例: slot 03 → https://dev-03...）
./dev.sh up old --ref 3ebd9e9                 # 任意のコミットでも可
./dev.sh up review-42                         # 同じ名前は同じスロットを冪等に再利用

./dev.sh list                # 一覧（ENV → SLOT → URL を表示）
./dev.sh url [name]          # URL だけ出力（プールスロット dev-XX ベース）
./dev.sh logs [name] [-f]    # ログ
./dev.sh down [name]         # 削除（ボリューム保持でスロット再利用。dev-XX の identity は残る）
./dev.sh down [name] --full  # 完全撤去（ボリューム・イメージまで削除。プールスロット dev-01..10 は API 削除せず再利用）
./dev.sh down --all          # 全部削除（全スロット解放）
./dev.sh gc                  # 未参照・dangling イメージの回収（up 成功後にも自動実行）
```

環境名を省略すると現在のブランチから導出される（`origin/`・`claude/`・`bookq-` の接頭辞は落とす）。URL の tailnet ドメインは `DEV_TS_DOMAIN` か、リポジトリ／worktree の `.env`、無ければメインのチェックアウトの `.env` の `BASE_URL` から導出する。

### ローカルモード（tailscale 無しで localhost 公開）

tailscale に参加せず `http://127.0.0.1:<port>` で確認する軽量モード。`TS_AUTHKEY` も `DEV_TS_DOMAIN` / `BASE_URL` も不要。`docker-compose.local.yml` の fragment を `docker-compose.dev.yml` に重ね、viewer のみ起動する（tailscale サイドカー無し）。

```bash
./dev.sh up [name] --local [--port N] [--lan-allow] [--fixture]
./dev.sh up demo-local --local --fixture               # 既定 http://127.0.0.1:8080
./dev.sh up demo-local2 --local --port 8081 --fixture  # 2つ目は --port が必須（衝突時はビルド前に失敗）
```

- 既定は `http://127.0.0.1:8080`。2つ目の同時環境は `--port 1024-65535` で別ポートを指定する（使用中ポートはビルド前に fail fast する）。
- 認証は `BOOKQ_TEST_MODE=true` でスキップされる（プール通常環境と同じ）。画像ツリーは読み取り専用。
- `--lan-allow` は bind を `0.0.0.0` に変える opt-in。`0.0.0.0` + `BOOKQ_TEST_MODE=true` + 開発用 `SECRET_KEY` の組み合わせは LAN 内に未認証の読み取り専用ツリーを晒すので、検証用 LAN でのみ使うこと。
- Secure Context 注意: `--lan-allow` で起動した場合も、普段の確認は `http://127.0.0.1:<port>` で開くこと。`http://127.0.0.1` は Secure Context だが、素の `http://<lan-ip>` は Secure Context ではない。そのため Service Worker とオフライン QA は `app/static/app.js:32` の条件で黙ってスキップされ、`?offline=1` か `localStorage.bookq_enable_sw=1` が無いと登録されない。LAN IP 経由の iPad ではオフライン QA はできない。
- iOS Simulator からもそのまま開ける（host network 共有）: `./scripts/open-simulator.sh http://127.0.0.1:<port>`。helper は `https://` と loopback `http://` のみ受理し、`http://<lan-ip>` は Secure Context でないため拒否する。手順は `.claude/skills/ios-simulator/references/overview.md: Local mode`。
- 10スロットプールを共有する。ローカル環境も `find_free_slot` の割当に数えられ、10/10 枯渇時は通常環境と同じく `pool exhausted (10/10)` で失敗する（仕様として許容）。
- `down` / `list` / `url` / `logs` はそのまま使える。`list` と `url` はローカル行に `http://<bind>:<port>` を出し、非ローカル行の `https://dev-XX` 形式は変わらない。`down` と `logs` の挙動は通常環境と同じ。

### `--ref` と作業ツリー

| 指定 | ビルド元 | 用途 |
|---|---|---|
| `--ref <branch\|tag\|SHA>` | `git archive` でそのコミットから直接 | **決定的**。作業ツリーの状態に一切影響されず、worktree を用意する必要もない。タグ方式は ref の種別で 2 分岐（下記） |
| 省略 | 現在の作業ツリー | 未コミットの変更を確認したいとき |

イメージタグは ref の種別で 2 分岐する:

- **シンボリック ref（ブランチ / タグ。ブランチ上の HEAD を含む）** → 可変エイリアスタグ `bookq-dev:ref-<slug>`。既存タグがあっても必ず再ビルドして上書きするので、同じ ref から何度作り直してもイメージはたかだか 1 枚（入れ替わった旧イメージは dangling 化し、[gc](#イメージの回収gc) で回収される）。
- **純 hex SHA / detached など不変参照** → コミット専用タグ `bookq-dev:<短縮SHA>`。存在すれば再ビルドせず再利用する。

エイリアスタグの slug は環境名と同じ規則で導出されるため、slug 衝突する別 ref は同一環境扱い（後勝ち）になる — 環境名だけでなくイメージタグも同じ衝突挙動で、後からビルドした側の内容にタグが上書きされる。

### イメージの回収（gc）

`./dev.sh gc` で未参照・dangling になった開発イメージを回収する（`up` 成功後にも自動実行され、gc 自体の失敗は up を失敗させない）。対象は次の 3 種:

1. 自作ラベル（`com.bookq.dev-image=1`）付きの dangling イメージ
2. 開発環境（compose プロジェクト名が `bookq-dev-*`）のサイドカーイメージの dangling
3. `bookq-dev` リポジトリのうち、どの dev コンテナからも参照されていないイメージ

3 には **30 分の猶予期間**がある（環境変数 `BOOKQ_GC_GRACE_MIN` で変更可）。並行エージェントが別環境を up 中（build 完了・コンテナ作成前）の新イメージを誤回収すると相手の up が失敗するため。削除には force を使わず、使用中で消せなかったものは寛容にスキップする。**本番プロジェクト `bookq` のイメージには一切触れない**（フィルタは自作ラベル / `bookq-dev` リポジトリ / `bookq-dev-*` プロジェクト接頭辞に厳密限定）。

### down とノードのライフサイクル（プール方式）

- `down` は**ボリュームを保持する**ので、同じスロット `dev-XX` の identity が残る。次回 `up` で別名を同じスロットに割当ても同じ `dev-XX` ノードを再利用する（Free プランでノード登録が残っても問題ない）。
- 環境の掃除は `./dev.sh list` で一覧（ENV → SLOT → URL）を確認し、不要なものを `./dev.sh down <名前>`（または `./dev.sh down --all`）でスロットを解放する（state ボリュームは保持）。
- 現行のプールでは `down` がボリュームを保持するのでこの問題は起きず、`down --full` でのみ state をリセットする。手動で `docker compose -p bookq-dev-pool-XX down -v` を直接叩くのは非推奨（`--full` 経由でラベルからのイメージ削除まで行う）。
- **tailnet ノードは docker では消さない（Free プラン）**。プール固定 `dev-01`〜`dev-10` を使い回すので、ノード登録は 10 個で頭打ちになる。手動で tailnet から消したい場合は[管理コンソール](https://login.tailscale.com/admin/machines)で `dev-01` 等を削除し、`down --full` で state ボリュームをリセットしてから再 `up` する。

#### `down --full` と Tailscale API（legacy のみ）

- プールスロット `dev-01`〜`dev-10` では `down --full` でも **API 削除をスキップ**する（再利用が前提）。`TS_API_KEY` があってもプールでは呼ばない。
- legacy プロジェクト（`bookq-dev-<名前>` / `dev-<名前>`）のみ従来通り API 削除を試みる: `TS_API_KEY` は Tailscale API トークン（`tskey-api-...` 形式）。[管理コンソールの Keys ページ](https://login.tailscale.com/admin/settings/keys) で発行する。所有者（Owner）または管理者（Admin）ロールが必要。`TS_AUTHKEY`（`tskey-auth-...`）とは別物。
- `TS_TAILNET` は任意。未設定なら API のデフォルト tailnet（`-`）が使われる。
- legacy のノード削除は `hostname` が `dev-<名前>` と**完全一致**するものだけが対象。
- `TS_API_KEY` 未設定・API 失敗時は警告して続行する（ローカル資源の撤去は完了）。

#### ノード名の衝突（`dev-XX-1` が出来てしまう仕組みと直し方）

`tailscale up --hostname=dev-XX` は、tailnet に同名ノードが既に残っていると**自動で `dev-XX-1` / `-2` の尾番を付ける**。`dev-XX-1` が見えたら「`dev-XX` 名を持つ別ノードが tailnet に残っている」サイン。

- **よくある原因 1 — ゴーストノード**: state ボリューム（= ノードの machine key）をリセット・再作成したのに、tailnet 側の旧ノード登録が残ったまま再 `up` した場合。旧ノードは offline のまま残り続け、新ノードが `-1` を取る。
- **よくある原因 2 — 他プロジェクトとの名前空間衝突**: `dev.sh` 派生の別アプリ（同じ `dev-XX` 命名を使う compose プロジェクト）が同じ tailnet にあると、先に登録した側が `dev-XX` 名を取る。実例: comicrypt 側のゲートウェイが `dev-02` を取得し、`https://dev-02...` が bookq ではなく comicrypt を返した（2026-10 に発生・同プロジェクト退役のため解消済み）。
- **検出**: `./dev.sh up` は deploy 後に sidecar の実登録名（`tailscale status --json` の `Self.DNSName`）を期待名と比較し、ずれていれば警告して実名で verify する（期待名で待つと offline ノードへ最大 ~6 分ハングする）。手動確認はホスト側 `tailscale status | grep dev-` で offline の同名ノードを探す。
- **復旧**: [管理コンソール](https://login.tailscale.com/admin/machines)で旧 `dev-XX` ノードを削除 → sidecar を `docker restart bookq-dev-tailscaleGateway-dev-XX`（entrypoint が起動毎に `tailscale up --hostname=dev-XX` を再実行するため、state を保ったまま `dev-XX` 名を再取得できる）。別プロジェクトに名を取られている場合は、そちらの hostname 命名を変えるか `down --full` で譲ってもらうしかない。
- `dev.sh down`（pool slot）が API 削除をスキップするのは identity 再利用のため — **state ボリュームがある限りノードは同一登録を名乗る**。問題なのは state 無しでの再登録で、その場合は上記のように `-1` が付く。
- **逆方向の罠**: ノードを管理コンソールで削除した後も state ボリュームが残っていると、sidecar は「認証済み」判定だが tailnet 側は `Online:False`（ログアウト）で URL が 000 になる。entrypoint は `BackendState` が `NeedsLogin` の場合 `tailscale login` の対話 URL をログに出して待機する — `docker logs bookq-dev-tailscaleGateway-dev-XX` の URL をブラウザで承認すれば即復旧する。`TS_AUTHKEY` が有効な再利用キーならこの対話は不要。

### iPad から見る

`./dev.sh up` が表示する `https://dev-<名前>.your-tailnet.ts.net` をそのまま Safari で開く。

**届かない場合:** tailnet の ACL が HTTPS (443) を絞っている可能性がある。ACL 設定で `dev-*` ノード宛の到達を許可する（旧構成の `bookq-dev` 宛 `8100-8199` の許可は不要になる）。

### 初回認証

サイドカーの認証キーは環境ごとの state ボリュームに残る。`dev.sh` は次の順で探す:

1. 環境変数 `TS_AUTHKEY`
2. 実行しているリポジトリ／worktree の `.env`
3. メインのチェックアウトの `.env`（worktree には `.env` が無いのでここに落ちてくる）

一度認証すれば以後の `up` でキーは不要（本番と同じ）。

#### キーの期限切れと自動更新

`TS_AUTHKEY`（`tskey-auth-`）も `TS_API_KEY`（`tskey-api-`）も最長 90 日で期限切れになる。検知と更新は `scripts/ensure-ts-authkey.sh` が担う（`./dev.sh up`・`./dev.sh list`・`./prd.sh` から自動で呼ばれる。警告のみで、デプロイはブロックしない）。

- **静的な期限記録**: `.env` の `TS_AUTHKEY_EXPIRES` / `TS_API_KEY_EXPIRES`（YYYY-MM-DD）を残 14 日または期限切れで警告する。キーを手動再発行したときは日付も更新すること（記録が無い・不正な場合も警告が出る）。
- **OAuth client による自動更新（推奨）**: `.env` に `TS_OAUTH_CLIENT_ID` / `TS_OAUTH_CLIENT_SECRET` を設定すると、期限が近い・不明な authkey を `up` 時に自動で mint し `.env` の `TS_AUTHKEY` / `TS_AUTHKEY_EXPIRES` / `TS_AUTHKEY_ID` を書き換える（記録済みの旧キーは DELETE で失効）。発行は[管理コンソールの Trust credentials](https://login.tailscale.com/admin/settings/trust-credentials)で scope `auth_keys`（`down --full` のノード削除にも使うなら `devices` も）。client secret 自体に期限はないため、設定後の更新作業は不要になる。
- **OAuth mint のキーはタグ付き**: Tailscale API の仕様で OAuth 発行の authkey は tailnet 所有＝タグ必須。mint されるキーには既定で `tag:bookq-dev` が付く（`TS_AUTHKEY_TAGS` で変更可）。そのため tailnet ACL の `tagOwners` に `"tag:bookq-dev": ["autogroup:admin"]` を追加済み（2026-10）。このキーで新規登録したノードはタグ所有デバイスになり、ユーザー所有ではなくなる — 既存の state ボリューム持ちノード（dev-01/02 等）は保存済み login で動くので変わらない。現行 ACL は全許可 (`*:*`) のため到達性への影響はない。
- OAuth 未設定・失効時は手動再発行の案内だけが出る: [Keys ページ](https://login.tailscale.com/admin/settings/keys)で **reusable / 非 ephemeral** なキーを発行し `TS_AUTHKEY` と `TS_AUTHKEY_EXPIRES` を更新。
- `down --full` のノード API 削除は OAuth client 設定時、`TS_API_KEY` の代わりに短命トークンを使う（`TS_API_KEY` の期限切れ後も機能する）。
- `TS_AUTHKEY` をシェル環境に export していると .env より優先される（自動更新後に警告が出る）。恒久的には .env 側だけに置くこと。

---

## 本番

### デプロイ

```bash
./prd.sh up                          # 起動（`./prd.sh` でも同じ。origin/main のコミット済み内容をデプロイ）
./prd.sh up --ref origin/release-1.2 # 指定 ref のコミット済み内容をデプロイ
./prd.sh down    # 停止（認証情報などのボリュームは保持される）
```

- **メインのチェックアウトからのみ実行できる。** worktree から実行すると拒否される（`--git-dir` と `--git-common-dir` の食い違いで判定）。未検証コードが本番に乗る事故を防ぐため。`down` にも同じガードがかかる。
- デプロイされるのは `--ref`（既定 `origin/main`。`origin/*` のみ指定可）のコミット済み内容だけで、作業ツリーの dirty は含まない。任意のブランチ・dirty 状態から実行できる。ref が解決できない場合は先に `git fetch origin` を実行すること（自動 fetch はしない）。
- `.env` が必要（`.env.example` を参照）。`TS_AUTHKEY` はシェル環境か `.env` のどちらかにあればよい。
- ビルド → 起動 → viewer コンテナの healthcheck 待ち → **公開 URL への実 curl** までを行う。最後の確認が重要で、「コンテナは動いているのに tailscale serve が配信できていない」障害を検出できる。

再ビルドだけしたい場合は `./rebuild.sh prd build <sha>` でイメージをビルドし、`./rebuild.sh prd deploy` で起動＋検証する。`rebuild.sh` は prd / dev 共通のデプロイエンジンで、worktree ガードを持たない（本番のガードは `prd.sh` の責務。開発環境デプロイは `dev.sh` から `./rebuild.sh dev build|deploy|verify` として呼ばれる）。

切替後のバージョン照合で mismatch した場合はそのまま停止する（自動ロールバックはしない）。復旧は `./prd.sh up --ref <previous-ref>` で直前の ref を指定して戻す（前回・今回の tag/sha は `prd.sh up` のバナーに表示される）。

停止は `./prd.sh down`（= `docker compose down`）。認証情報などのボリューム `bookq_*` は保持されるので、再起動時に再認証は不要。`warmup-cache`・`thumb-cache` ボリュームも `down`/作り直しをまたいで保持され、`down -v`/`--full` でのみリセットされる（性能のみの影響で、miss は自動で作り直され #265 の eviction で間引かれる）。ボリュームごと完全に削除したい場合のみ `docker compose down -v`。

### 唯一の到達経路

viewer は**ホストにポートを公開していない**。`tailscale serve` が :443 で HTTPS を終端し、compose ネットワーク内の `http://viewer:8080` にプロキシする。つまり:

- ホストから `http://localhost:8080` は叩けない。ヘルスチェックはコンテナ内から行うしかない（`docker-compose.yml` の `healthcheck` がそれ）。
- **viewer が compose ネットワークにいないと 502 になる。** 実際に一度これが起きている（下記）。

### やってはいけないこと

**worktree で素の `docker compose up` を打たない。** compose はディレクトリ名からプロジェクト名を作るので、worktree では別プロジェクトが立ち上がる。そこには tailscale サイドカーも含まれ、ホスト名 `bookq` を要求して本番のノードと名前を争う（Tailscale が `bookq-1` にリネームするため、`BASE_URL` と証明書が食い違って本番が壊れる）。

検証は必ず `./dev.sh` を使う。素の `docker compose up` は本番用の `docker-compose.yml` を読み込むため、上記の危険はそのまま残る（`:?` ガードは `docker-compose.dev.yml` にのみあり、`docker compose -f docker-compose.dev.yml up` を直接打った場合に `DEV_IMAGE` / `DEV_NAME` 等の必須変数が無くて起動前に失敗させるだけ）。`dev.sh` は `-p` でプロジェクト名を明示する。

### 過去の障害: 502

`BOOKQ_TEST_MODE=true` で手動起動されたコンテナが、本番の compose ラベル（`project=bookq, service=viewer`）を持ちながら `bridge` ネットワークに繋がっていた。tailscale サイドカーは `viewer` を名前解決できず 502 を返し続けた。

切り分け:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://bookq.your-tailnet.ts.net/api/health
docker inspect $(docker compose ps -q viewer) --format '{{json .NetworkSettings.Networks}}'
```

viewer が `bookq_default` にいて `viewer` エイリアスを持っていれば正常。復旧は迷子のコンテナを消し、`./rebuild.sh prd build <sha>` でビルドしたイメージで `./rebuild.sh prd deploy`（または `./prd.sh up --ref <ref>`）から作り直す。素の `docker compose up -d --build viewer` は作業ツリーの dirty からビルドするため使わないこと:

```bash
docker rm -f <迷子のコンテナ名>
./prd.sh up --ref origin/main   # デプロイしたい ref を指定（既定 origin/main）
```

`./rebuild.sh prd build` / `./rebuild.sh prd deploy` は最後に公開 URL を叩くので、この障害を自動で検出する。

---

## オフライン / PWA 配信

Service Worker は `app/static/sw.js` に配置され、FastAPI の `GET /sw.js` ルートで `FileResponse` として配信されます（`Content-Type: application/javascript`、`Service-Worker-Allowed: /` ヘッダ、`Cache-Control: no-cache` 付き）。scope を `/` にする必要があるため、`/static/sw.js` の静的配信だけでは scope が `/static/` に狭まって拒否されるので、ルート直下の `/sw.js` が必須です。`/static/sw.js` でも同じ内容が取得できますが、登録は必ず `/sw.js` に対して行われます。

`.dockerignore` は `*.md` と `docs/`、`tests/` を除外しますが、`app/static` は除外しません。`Dockerfile` の `COPY app/ ./app/` で `app/static/sw.js`、`offline-core.js`、`manifest.json`、`offline.html`、`icons/` がそのままイメージに含まれるので、追加の例外行は不要です。`! grep -q "app/static" .dockerignore` と `grep -q "COPY app" Dockerfile` の両方が通ることで配信を担保します（`grep -v` は「一致しない行が1つでもあれば成功」なので担保にならない）。

Service Worker は Secure Context（HTTPS）でのみ登録されます。本番と開発環境はともに `tailscale serve` で HTTPS 終端しているので条件を満たします。HTTP や `localhost` 以外の素の HTTP では登録されず、テスト時は `BOOKQ_TEST_MODE=true` の分岐で通常はスキップし、`?offline=1` または `localStorage.bookq_enable_sw=1` のときだけ登録します。

### オフライン検証ハーネス `/static/offline-harness.html`

`app/static/offline-harness.html` は Service Worker と同じ `app/static` 配下に置かれ、`app/main.py:28` の `StaticFiles` マウントで配信されます。dev では常時リンク表示の対象ですが、本番ではリンクのみ gated です。`?harness=1` クエリか `localStorage.bookq_harness` か `BOOKQ_TEST_MODE` が真のときだけ `#harness-link` が表示され、それ以外ではリンクが隠れます。ファイル自体は tailnet の信頼境界で保護された `StaticFiles` の素の GET で常に 200 を返します。リンクを隠すだけで 404 にしないのが仕様です。

`.dockerignore` は `*.md` と `docs/` と `tests/` を除外しますが、`app/static` は除外しません。`! grep -q "app/static" .dockerignore` が通ることで除外漏れが無いことを担保します。`Dockerfile:10` の `COPY app/ ./app/` で `app/static/offline-harness.html` は `sw.js` や `manifest.json` と同じくイメージに必ず含まれます。`grep "COPY app" Dockerfile` で確認できます。ホストポートの追加や Workbox の導入は不要です。

```bash
# 配信確認 (ポートは毎回ランダムなので fixture の URL を使う)
grep -q "offline-harness" README.md && grep -q "harness" docs/deployment.md
! grep -q "app/static" .dockerignore
grep -q "COPY app" Dockerfile
curl -s http://127.0.0.1:$PORT/static/offline-harness.html | grep -q "Offline Harness" && echo ok
```

---

## git フック

`./setup-hooks.sh` でインストールする（クローン後に一度）。

コミット時にアプリコード（`app/*.py`, `app/templates/*`, `app/static/*`, `requirements.txt`, `Dockerfile`）が含まれていると:

1. `pre-commit` が**その worktree の開発環境**を更新し、URL を表示してコミットを止める
2. その URL で動作確認する
3. もう一度 `git commit` するとマーカーがあるので通る

フックは**本番に一切触らない**。マーカーは `git rev-parse --git-path rebuild-marker` で解決するので worktree ごとに独立していて、複数の worktree で同時にコミットしても干渉しない。

注意点:

- フックの実体は共有の `.git/hooks` に置かれる（全 worktree で同じ）。一方フックが読む `.githooks/lib.sh` は worktree 側なので、挙動はチェックアウトしているブランチに従う。`lib.sh` が無いブランチでは何もせず素通しする。
- Docker が動いていない、`dev.sh` が無い場合は警告してスキップする。コミットはブロックしない。
