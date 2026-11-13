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
./dev.sh down --all          # 全部削除（全スロット解放。実行前に孤立していたボリュームも回収）
./dev.sh gc                  # 未参照・dangling イメージの回収（up 成功後にも自動実行）
./dev.sh gc-volumes [--dry-run]  # 孤立ボリューム（コンテナの無い bookq-dev-* の残骸）の回収
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
- **検出**: `./dev.sh up` は deploy 後に sidecar の実登録名（`tailscale status --json` の `Self.DNSName`）を期待名と比較する。`./prd.sh up` も deploy 時に BASE_URL と同じ比較を行う（共通実装は `rebuild.sh` の `heal_fqdn_collision`）。期待名のまま待つと offline ノードへ最大 ~6 分ハングするため、ずれの検出は verify_url の前に行う。手動確認はホスト側 `tailscale status | grep dev-` で offline の同名ノードを探す。
- **自動復旧**: ずれを検出すると以下を自動で試みる（資格情報は OAuth client 短命トークン → `TS_API_KEY` の順）。(1) 期待名を持つノードのうち **`tag:bookq-dev` 付きかつ offline** のものだけを admin API で削除 — 無タグ（手動登録）や他プロジェクト・オンラインの同名ノードは巻き込み防止のため触らず警告に留める。(2) 自分を `POST /api/v2/device/<id>/name` で期待名に rename（`up --hostname` は登録済み名を上書きしないため restart だけでは `-1` が残る)。(3) sidecar を `docker restart` して serve を正しい FQDN に張り直す。dev は `rebuild.sh dev heal-fqdn` 経由でも単独実行できる（`DEV_PROJECT`/`DEV_FQDN` 要 export）。
- **手動復旧**（自動復旧が拒否・失敗した場合）: [管理コンソール](https://login.tailscale.com/admin/machines)で旧ノードを削除 → 新ノードを期待名に rename → sidecar を `docker restart`。別プロジェクトに名を取られている場合は、そちらの hostname 命名を変えるか `down --full` で譲ってもらうしかない。
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

viewer は**ホストにポートを公開していない**。既定では `tailscale serve` が :443 で HTTPS を終端し、compose ネットワーク内の `http://viewer:8080` にプロキシする。独自ドメイン公開を有効化した場合はこれに `cloudflared` 経由の第 2 の経路が加わる（下記「独自ドメイン公開」）。つまり:

- ホストから `http://localhost:8080` は叩けない。ヘルスチェックはコンテナ内から行うしかない（`docker-compose.yml` の `healthcheck` がそれ）。
- **viewer が compose ネットワークにいないと 502 になる。** 実際に一度これが起きている（下記）。

### 独自ドメイン公開（Cloudflare Tunnel, #349）— 推奨

独自ドメインでの公開は opt-in の `cloudflared` コンテナ (compose profile `public`) で行う。cloudflared が外向きに Cloudflare edge へ接続を張る方式なので、**CGNAT・ポート開放不可の回線でも動く**（受信側にグローバル IP は不要）。TLS は Cloudflare edge が終端する。

```
ブラウザ → Cloudflare edge (TLS 終端) → トンネル → cloudflared → http://viewer:8080
```

`CLOUDFLARE_TUNNEL_TOKEN` が `.env`（またはシェル環境）にあると `rebuild.sh prd deploy` が自動で `--profile public` を付けて `docker compose up` する — つまり **token を置けば次の `prd.sh up` から cloudflared が起動する**。未設定なら従来どおり tailscale serve のみ。

#### セットアップ手順

1. Cloudflare にサイトを追加し、ドメインのネームサーバーを Cloudflare 指定の 2 台に変更（レジストラ側で操作）。**ドメインをメール等他用途で使っている場合は、既存の MX/TXT/A レコードを全て Cloudflare DNS に再現してから切り替えること** — NS 切替で既存レコードは全て無効になる。
2. Zero Trust ダッシュボード → Networks → Tunnels → Create a tunnel → cloudflared。発行された token を `.env` の `CLOUDFLARE_TUNNEL_TOKEN` に設定。
3. 同じダッシュボードで Public Hostname を登録: `<host>.<domain>` → service `HTTP` `viewer-rs:8080`（#409 以降 viewer-rs が本体）。
4. `.env` の `BASE_URL` を `https://<host>.<domain>` に変更。
5. Google Cloud Console で `https://<host>.<domain>/auth/callback` を承認済み redirect URI に追加。**OAuth consent screen が "Testing" の場合は `ALLOWED_EMAILS` の各アドレスを Test users に登録必須**（未登録だと本人もログインできない）。
6. `./prd.sh up` — cloudflared も起動し、`verify_url` が独自ドメイン経由の `/api/v1/health` を叩くのでトンネル疎通まで確認できる。`docker logs bookq-prd-cloudflared` で `Registered tunnel connection` ×4 が見えれば接続確立。

#### 運用上の注意

- `BASE_URL` が ts.net 以外になると、`prd deploy` の tailnet 登録名整合チェック (`heal_fqdn_collision`) は自動でスキップされる（無関係なため）。
- `*.ts.net` 側は引き続きコンテンツを返すが、OAuth ログインは `BASE_URL`（独自ドメイン）へ遷移する — cookie はドメイン別のため、実質独自ドメインが canonical になる。tailscale sidecar はそのまま残してよい。
- `ALLOWED_EMAILS` は据え置き前提。「公開到達・限定利用」であり、per-user 分離がない設計のため一般公開はスコープ外。
- **認証付きメディア endpoint (`/image/`, `/thumb/`, `/view/`, `/api/thumb-sprite`) は `Cache-Control: private` で応答する** — `public` だと Cloudflare edge が応答をキャッシュし、未認証リクエストにも画像バイトが返る認証バイパスになる。保険として Cloudflare 側で当該 hostname に Cache Rule「Bypass cache」を張ってもよい。
- Security → Bots の **Bot Fight Mode は OFF 推奨** — API クライアントや native アプリ (`Authorization: Bearer`) を bot 判定で弾きうる。
- Cloudflare 無料プランの TOS §2.8 は画像主体コンテンツの proxy 配信を制限する条項がある。個人利用なら実害はほぼない想定だが、転送量で問題化した場合の fallback は VPS リレー等の別方式。
- token を消したい場合は `.env` から `CLOUDFLARE_TUNNEL_TOKEN` を削除して `./prd.sh up` — profile が外れて cloudflared は作られない（既存コンテナは `up` で `--remove-orphans` していないと残ることがあるので `docker rm -f bookq-prd-cloudflared` で掃除）。

### 一般公開（Tailscale Funnel, #353）— 非推奨（帯域上限で画像配信不可）

`TS_FUNNEL=1` を `.env` に設定すると、sidecar が `tailscale serve` ではなく `tailscale funnel` を張る。これにより **`https://bookq.<tailnet>.ts.net` が tailnet 外の public internet からも到達できる**（funnel は public :443 をノードへ中継し、TLS は従来どおり sidecar 内で終端。tailnet 内アクセスも継続）。**ただし公式に変更不可の帯域上限があり、実測 ~数十KB/s/接続のため画像ビューア用途では実用上使い物にならない** — 公開には Cloudflare Tunnel を使うこと。本節は経路の記録として残す。

- 前提: tailnet policy file (admin console → ACL) の `nodeAttrs` に funnel 許可が必要:
  ```json
  "nodeAttrs": [{"target": ["*"], "attr": ["funnel"]}]
  ```
- 設定後は `./prd.sh up` で有効化。`verify_url` は deploy ホスト (tailnet メンバー) から `BASE_URL/api/v1/health` を叩くため **tailnet 経路**の疎通確認になる。公開経路そのものの確認は tailnet 外端末で行う
- `BASE_URL`・Google OAuth redirect URI は変更不要（ホスト名が変わらないため）
- 停止は `TS_FUNNEL` を外して `./prd.sh up` — entrypoint が `funnel --https=443 off` を明示して公開を確実に閉じる
- funnel 未許可のままだと `funnel --bg` が承認を待ち同一プロセス内でブロックする (exit しない)。その間 :443 proxy が無くサイト全体 (tailnet 経路も) 停止するが、sidecar コンテナは healthcheck 無しで Up のまま — restart loop にはならず自動検知もされない (実測で承認待ち ~106 秒)。復旧は承認リンク (`login.tailscale.com/f/funnel?node=...`) を踏むか `TS_FUNNEL` を外して `prd.sh up`。`funnel` コマンド自体が非ゼロ終了した場合のみ restart loop になる
- Tailscale Funnel は beta で調整不可の帯域上限あり (数値非公開、実測 ~数十KB/s/接続)。`*.ts.net` 証明書は CT log に記録されるため funnel 公開により tailnet 名は公開情報になる
- 認可は引き続き OAuth + `ALLOWED_EMAILS`（「公開到達・限定利用」）

### やってはいけないこと

**worktree で素の `docker compose up` を打たない。** compose はディレクトリ名からプロジェクト名を作るので、worktree では別プロジェクトが立ち上がる。そこには tailscale サイドカーも含まれ、ホスト名 `bookq` を要求して本番のノードと名前を争う（Tailscale が `bookq-1` にリネームするため、`BASE_URL` と証明書が食い違って本番が壊れる）。

検証は必ず `./dev.sh` を使う。素の `docker compose up` は本番用の `docker-compose.yml` を読み込むため、上記の危険はそのまま残る（`:?` ガードは `docker-compose.dev.yml` にのみあり、`docker compose -f docker-compose.dev.yml up` を直接打った場合に `DEV_IMAGE` / `DEV_NAME` 等の必須変数が無くて起動前に失敗させるだけ）。`dev.sh` は `-p` でプロジェクト名を明示する。

### 過去の障害: 502

`BOOKQ_TEST_MODE=true` で手動起動されたコンテナが、本番の compose ラベル（`project=bookq, service=viewer`）を持ちながら `bridge` ネットワークに繋がっていた。tailscale サイドカーは `viewer` を名前解決できず 502 を返し続けた。

切り分け:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://bookq.your-tailnet.ts.net/api/v1/health
docker inspect $(docker compose ps -q viewer-rs) --format '{{json .NetworkSettings.Networks}}'
```

viewer が `bookq_default` にいて `viewer` エイリアスを持っていれば正常。復旧は迷子のコンテナを消し、`./rebuild.sh prd build <sha>` でビルドしたイメージで `./rebuild.sh prd deploy`（または `./prd.sh up --ref <ref>`）から作り直す。素の `docker compose up -d --build viewer` は作業ツリーの dirty からビルドするため使わないこと:

```bash
docker rm -f <迷子のコンテナ名>
./prd.sh up --ref origin/main   # デプロイしたい ref を指定（既定 origin/main）
```

`./rebuild.sh prd build` / `./rebuild.sh prd deploy` は最後に公開 URL を叩くので、この障害を自動で検出する。

### 本番監視（prod-monitor）

`.github/workflows/prod-monitor.yml` が **10 分毎**（cron `*/10 * * * *`、手動は `gh workflow run prod-monitor.yml`）に公開ドメイン `https://bookq.mats.dpdns.org` の無認証エンドポイントをプローブする。デプロイ時の `verify_url` は「デプロイの瞬間」しか見ないのに対し、こちらは継続監視。コストは public repo の Actions 無料枠のみで、解析用 opencode は障害時だけ走る。

プローブ本体は `scripts/prod_monitor.py`（stdlib + `gh` CLI のみ）。7 つの check（#407 で v1 のみに整理 — v0 は viewer 停止と同時廃止のため監視対象外）:

| check | path | 意味 |
|---|---|---|
| `v1_health` | `/api/v1/health` | edge→tunnel→app の生存 |
| `v1_version_gate` | `/api/v1/version` | 認証ゲートが 401 を返すこと（200 = バージョン情報漏洩で最重要。#359 の回帰監視を兼ねる。sha 記録は v1 が認証必須のため廃止） |
| `root` | `/` | ログインページ shell の配信 |
| `auth_login` | `/auth/login` | OAuth 開始リダイレクト（302 → accounts.google.com） |
| `tree_gate` | `/api/v1/tree` | 認証ゲートが 401 を返すこと（200 = 認証バイパスで最重要） |
| `static_sw` / `static_app` | `/static/sw.js`, `/static/app.js` | SW・メインバンドルの配信健全性 |

失敗は層別に分類される: `unreachable`（transport 不能 = ホスト/回線/DNS）、`cf_tunnel`（CF 520-530 系 or CF ブランドの 502/504 = tunnel/origin 到達不能）、`app_error`（app の JSON 5xx）、`contract_break`（200 だが内容不一致）、`auth_bypass`（認証ゲート喪失）、`probe_error`（監視スクリプト自体の故障 — 盲点化を防ぐため fail として issue 化）。

**issue ライフサイクル**: FAIL 検知で `monitor` ラベル付き issue を作成（本文に全 check の HTTP/latency/cf-ray・分類・run URL を収録）。同じ signature の継続障害は 60 分毎のハートビートコメントのみ、signature 変更時は即コメント＋再分析。全 check 回復で復旧コメントを残して自動 close。解析ジョブは新規検知・signature 変更時のみ `opencode run` を実行し、層の切り分けと最有力原因を issue にコメントする（`GITHUB_TOKEN` 製 issue は sender が `github-actions[bot]` で opencode-auto の bot ゲートを通らないため、この workflow 内で完結させている）。

**カバレッジ外（既知の隙間）**: tailnet 経路 `*.ts.net`（serve/viewer-rs 側だけの障害は拾えない）、認証付き API のコンテンツ正しさ（folders 一覧・画像・検索は無認証では 401 確認まで）、ホスト指標（disk/memory/docker events）。認証面まで監視するには monitor token 付き `/api/monitor/*` endpoint の新設が必要。検知遅延は cron 間隔 + GH スケジューラ遅延で実質 ~10-20 分。

無効化したい場合は workflow の `schedule` を消して merge、または Actions UI で workflow を disable する。

### viewer-rs が本体（#409 cutover 完了形）

`docker compose up` の既定で起動するバックエンドは **viewer-rs** であり、Python `viewer` は profile `py-rollback` 時のみ起動する rollback 用退避サービス。到達経路は `tailscale serve → viewer-rs:8080`（`TS_SERVE_TARGET` 既定）と opt-in cloudflared（ingress はダッシュボード管理、`http://viewer-rs:8080`）の 2 系統。

構成:

- `viewer-rs` は `BOOKQ_UPSTREAM` 未設定 = **upstream 不在モード**（#408）で動く（明示的に有効化する `BOOKQ_RS_NO_UPSTREAM=1` も同等）。従来の暗黙デフォルト `http://viewer:8080` は #408 で廃止 — 指定し忘れは fail-closed になる。native 化されていない経路・router fallback への着弾は **501 + path WARN ログ**、認証不能なリクエスト（`caller_or_proxy` の Delegate）は **401** をローカルで返す。v0 `/api/*` は全て 501。lenient（STRICT 無し）起動時は未対応 gate 一覧を WARN に出す。
- `BOOKQ_RS_NATIVE` 既定は全 gate 列挙（`auth` 含む）。`BOOKQ_RS_NATIVE_STRICT=1` 既定で、gate 抜け・auth OAuth 設定欠落・部分グループは起動拒否。
- `viewer` (Python) は `profiles: ["py-rollback"]` — `docker compose up` では起動しない。user-state/thumb-cache volume は viewer-rs と共有される。
- `tailscale.depends_on` / `cloudflared.depends_on` は viewer-rs を指す（profile service への depends_on は profile を auto-enable してしまうため、viewer への依存は残さない）。
- `./prd.sh up` は `bookq-prd-viewer`/`bookq-prd-viewer-rs`/`tailscale` 3 イメージをビルドし、deploy は `ps -q viewer-rs` の healthcheck + `viewer-rs` image タグの sha 照合で検証する（`/api/v1/version` は認証必須のため HTTP 照合は不可）。

#### auth gate の cutover 注意

`auth` が native 化された時点で、Python 側発行済みの **refresh token は全て 401** になる（code/refresh はプロセス内メモリ）。native クライアントは一度再ログインが必要（session cookie と access JWT は `SECRET_KEY` 共用のため影響しない）。切替は再ログイン許容タイミングで。

#### rollback

障害時の復旧は 2 段階。いずれも `docker-compose.prd-image.yml` が `PRD_VIEWER_IMAGE`/`PRD_RS_IMAGE` を pin 済みであることが前提（`./prd.sh up` 経路なら常に成立）。

```bash
# 1) Python を rollback サービスとして起動（py-rollback profile）
docker compose -f docker-compose.yml -f docker-compose.prd-image.yml \
    --profile py-rollback up -d --no-build viewer
# 2) tailscale serve の向きを py に戻す（数秒の断）
#    .env に TS_SERVE_TARGET=http://viewer:8080 を設定してから
docker compose -f docker-compose.yml -f docker-compose.prd-image.yml up -d tailscale
# 3) cloudflared 利用時は Cloudflare ダッシュボードの ingress を
#    http://viewer:8080 に戻す
# 4) viewer-rs は upstream 不在のまま残してよい（traffic は来ない）。
#    完全停止する場合: docker compose stop viewer-rs
```

戻った後の恒久化は `./prd.sh up --ref <cutover 前の ref>` — その ref の compose には viewer が既定 service として残る。

#### 新規 native route の有効化

`BOOKQ_RS_NATIVE` の列挙に新 gate 名を追加する（`all` は将来 route の自動有効化を防ぐため使わない）。追加の前提は従来どおり:

| 条件 | 確認方法 |
|---|---|
| 契約テストが正本として存在 | `api/*cases.json` / schemathesis / 共有 vectors |
| `cargo test --workspace` が通る | `rust/`（CI `rust.yml` の `test`） |
| gateway-contract が該当 endpoint を native で通る | CI `rust.yml` の `gateway-contract` |

部分グループ（`tree,search,facets,images` / `tree,reader-sync`）は STRICT 既定により起動時に拒否される。

#### 検証済みの運用ポイント

- `verify_url` は `/api/v1/health`（公開・native）を叩く。v0 `/api/health` は 501。
- コンテナ内 healthcheck は viewer-rs 自身の `/api/v1/health` — upstream 不在でも standalone で healthy になる。
- proxy-hit の観測: no-upstream では fallback 着弾が WARN ログに path 付きで出る（`docker compose logs viewer-rs`）。cutover 後に残るはずのない 501 が継続していたら contract 漏れの兆候。

切り戻し後も `user-state` volume の共有で hidden_roots 等の永続状態はそのまま引き継がれる。**reading positions も #375 以降 `user-state` の `folder-visibility.sqlite3`（`position_users`/`position_folders` テーブル、スキーマ v2）に永続化**され、py/rs どちらが書いても同じ DB を読むため、切替/切戻しで「どこまで読んだか」は失われない。スキーマバージョンが新しい側が古い側を拒否する仕組みは hidden_roots と同じ（docs/user-state-db.md 参照）。

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
