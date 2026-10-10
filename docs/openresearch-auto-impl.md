# セットアップ前提条件（LLM 自動化ワークフロー / bookq）

このリポジトリの AI 自動化（issue → opencode による implement / research）を動作させるために必要な手動セットアップ手順。openresearch の `docs/setup-prerequisites.md` を bookq（`kentoku24/bookq`）用に適応した版。

## 1. Zen API キーの発行とシークレット登録

1. https://opencode.ai/auth にアクセスし、API キーを発行する
2. リポジトリの Actions シークレットとして登録する:

   ```bash
   gh secret set OPENCODE_ZEN_API_KEY
   ```

   実行後、対話プロンプトにキー値を貼り付ける。
   - **キー値は絶対にファイル、ログ、issue、チャットに残さないこと**。ドキュメント上も常に `<your-key>` プレースホルダで表記し、具体的なキー文字列の例示は置かないこと
   - 登録確認: `gh secret list` に `OPENCODE_ZEN_API_KEY` が表示されれば OK

3. モデル ID は `opencode-go/muse-spark-1.3-contributor` を使用する。
   カタログ上の存在確認（任意、ローカルにキーがある場合のみ）:

   ```bash
    curl -s -H "Authorization: Bearer $OPENCODE_API_KEY" https://opencode.ai/zen/v1/models | grep muse-spark-1.3-contributor
   ```

   ローカル検証ができない場合は、初回ワークフロー実行での暗黙検証に委譲する。変更箇所は `opencode.json` の `"model"` 行が単一の変更点である（ワークフロー内の `model:` / `-m` 指定も同一値を参照）。

## 2. GitHub App のインストール（手動必須）

1. https://github.com/apps/opencode-agent を開く
2. 「Install」→ 対象アカウント `kentoku24` を選択 → リポジトリ `bookq` を限定して許可

> ⚠️ **このステップはブラウザ上で管理者がクリックする必要があり、自動化不可能です。**
> GitHub App のインストールは OAuth 同意画面を伴うため、API / CLI から実行できません。必ず人間が操作してください。
> 対象リポジトリは `kentoku24/bookq` のみに限定すること。全リポジトリへの許可は不要。

インストール済み確認: Settings → Integrations → Applications → Installed GitHub Apps に `opencode-agent` があること。`kentoku24/bookq` が許可リストに入っていることを確認する。

## 3. ラベルとリポジトリ変数・権限の設定

### ラベル 5 種の作成

次のコマンドで作成する（既存ならスキップされる）:

```bash
gh label create "ai/implement" --color "0075ca" --description "AI implementation task" --repo kentoku24/bookq || true
gh label create "ai/research" --color "0E8A16" --description "AI research task" --repo kentoku24/bookq || true
gh label create "🤖 processing" --color "FBCA04" --description "AI agent processing" --repo kentoku24/bookq || true
gh label create "✅ done" --color "0E8A16" --description "AI task done" --repo kentoku24/bookq || true
gh label create "❌ failed" --color "B60205" --description "AI task failed" --repo kentoku24/bookq || true
```

確認:

```bash
gh label list --repo kentoku24/bookq --json name,color,description
```

### リポジトリ変数 `USE_DIRECT_RUN=true`

ピン留め公式 Action の不具合を迂回するためのフラグ。`true` で direct-run バリアントが official action を迂回する:

```bash
gh api repos/kentoku24/bookq/actions/variables -f name=USE_DIRECT_RUN -f value=true
# 既存なら PATCH
gh api repos/kentoku24/bookq/actions/variables/USE_DIRECT_RUN -X PATCH -f value=true
```

確認:

```bash
gh api repos/kentoku24/bookq/actions/variables --jq '.variables[] | select(.name=="USE_DIRECT_RUN") | .value'
```

### ワークフロー権限 `write`

PR 作成に必要な権限は `.github/workflows/opencode.yml` / `opencode-auto.yml` / `openresearch-bridge.yml` が per-job `permissions` で宣言しているため、リポジトリ既定の `default_workflow_permissions` は `read` のままで動作する。ワークフローレベルは `contents: read` のみで、各ジョブが必要な権限に昇格する（`id-token: write` は公式 pinned Action を実行するジョブのみ）。

```bash
gh api repos/kentoku24/bookq/actions/permissions/workflow -X PUT -f default_workflow_permissions=write -F can_approve_pull_request_reviews=true
```

上記コマンドはレガシー／任意である（実行しても無害だが、もはや必須ではない）。

Settings → Actions → General → Workflow permissions で `Read` 選択のままであることを確認する。

### 必須シークレット / Prerequisites

| シークレット | 必須 | 用途 |
|---|---|---|
| `OPENCODE_ZEN_API_KEY` | 必須 | LLM 呼び出し（全ワークフロー共通） |
| `OPENRESEARCH_READ_TOKEN` | 必須（ブリッジ用） | プライベートリポジトリ `kentoku24/openresearch` の READ アクセスを持つ PAT または fine-grained token。ブリッジ（`.github/workflows/openresearch-bridge.yml`）が `target:bookq` issue を取得するために使用する。bookq 側の `GITHUB_TOKEN` ではプライベートな openresearch を読めないため必須。未設定の場合、スケジュールスキャンは 401/404 を握りつぶして空振りし（`issues_json="[]"` 扱い）、何もミラーされない |

> ブリッジは `OPENRESEARCH_READ_TOKEN` が無いと何も見つからない — これはエラーではなく静かに空振りする仕様（401/404 を `echo "[]"` で吸収）。`gh secret list --repo kentoku24/bookq` で `OPENRESEARCH_READ_TOKEN` が表示されることを確認すること。

## 4. 初回ワークフロー失敗時の切り分け表

| 症状（エラー例） | 分類 | 対応 |
|---|---|---|
| issue に `❌ failed` が付いた / `🤖 processing` が残り続ける | エージェント実行失敗または失効ロック（stale lock） | issue に投稿された **Actions ログ URL 付きコメント**を確認。`🤖 processing` は次回 gate 通過時に自動削除されて通常実行される（手動削除も可） |
| `secret OPENCODE_ZEN_API_KEY not found` / 認証ヘッダ欠落による 401 | シークレット未設定 = BLOCKED | 手順 1 を実施: `gh secret set OPENCODE_ZEN_API_KEY`（値は対話入力） |
| `unable to exchange token` / OIDC token exchange の 4xx (404, 403, 401) | **App 未インストール（App トークン交換を配線している場合のみ）** | デフォルトでは OIDC→App 交換は配線されていないため通常は発生しない。`actions/create-github-app-token` を追加している場合のみ、手順 2 を実施して再実行 |
| `404 Not Found` on token endpoint / `403 Forbidden`（App 権限系） | **App 未インストール（App トークン交換を配線している場合のみ）** | デフォルトでは発生しない。App トークン交換を配線している場合のみ、手順 2 を再確認 |
| 上記以外（YAML 構文エラー、スクリプト内バグ、タイムアウト等） | **バグ** | ワークフローファイル / スクリプトを修正 |
| `Failed to parse JSON` / `p.rest` / `Unexpected error` (official action v1.18.21) | **Action 不具合 — direct-run 迂回で解消** | リポジトリ変数 `USE_DIRECT_RUN=true` が設定済みか確認。未設定なら `gh api repos/kentoku24/bookq/actions/variables -f name=USE_DIRECT_RUN -f value=true` で有効化 |
| `pull request create failed: GitHub Actions is not permitted...` | **Workflow 権限不足** | 該当ワークフローファイルの per-job `permissions` を確認する（そのジョブに `contents: write` / `pull-requests: write` が付与されているか）。リポジトリ既定の `default_workflow_permissions` は `read` のままでよい |

判定のコツ: エラーメッセージに **token exchange / OIDC / installation 関連の 4xx** が出ている場合はまず App インストール状況を疑う。`❌ failed` 単体では原因が分からないため、必ず紐づく Actions ログ URL コメントからログを確認する。

> **補足:** `USE_DIRECT_RUN=true` は上記手順で設定する。リポジトリ既定の `default_workflow_permissions` は `read` のままでよい（各ワークフローが per-job `permissions` を宣言している）。`opencode-auto.yml` / `opencode.yml` / `openresearch-bridge.yml` は `GITHUB_TOKEN` と `OPENCODE_ZEN_API_KEY` を使用して動作し、`id-token: write` は公式 pinned Action を実行するジョブにのみ付与されている（`gate` / direct-run / `cleanup` / ブリッジには付与なし）。OIDC を GitHub App トークンに交換する処理はデフォルトでは配線されていない（App トークン認証が必要な場合は `actions/create-github-app-token` を追加する）。

## 5. Zen クレジット上限 / 自動チャージ設定の確認

コスト暴走の最終防波堤として、月次上限の設定を推奨:

1. https://opencode.ai/auth （または billing 設定ページ）にログイン
2. Billing / Usage 設定を開く
3. **Monthly limit（月次クレジット上限）** を設定する（推奨: 小さめの値から開始）
4. Auto-recharge（自動チャージ）が有効な場合は、意図した上限額になっているか確認する
5. 設定値をチーム内で共有し、超過アラートの挙動を確認しておく

worst case として、信頼著者が N 件のラベル付き issue を連投すれば最大 N 本の run が並列起動する（concurrency は per-issue のみで、リポジトリ全体の同時実行上限はない）。フリート全体を止める唯一のハード制限値は **Zen クレジット上限**である。

---

## チェックリスト（全項目完了で自動化稼働可能）

- [ ] `gh secret list --repo kentoku24/bookq` に `OPENCODE_ZEN_API_KEY` と `OPENRESEARCH_READ_TOKEN`（`kentoku24/openresearch` READ 用）がある
- [ ] GitHub App `opencode-agent` を `kentoku24/bookq` リポジトリにインストール済み（手動、bookq に限定）
- [ ] ラベル 5 種（`ai/implement`, `ai/research`, `🤖 processing`, `✅ done`, `❌ failed`）が作成済み
- [ ] リポジトリ変数 `USE_DIRECT_RUN=true` が設定済み
- [ ] 各ワークフローに per-job `permissions` が宣言されている（リポジトリ既定 Workflow permissions は `read` のままでよい）
- [ ] 初回ワークフロー実行が成功（または失敗原因を上表で分類済み）
- [ ] Zen billing の月次上限を設定済み
