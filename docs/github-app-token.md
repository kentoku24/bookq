# GitHub App token — opencode-auto の workflow 変更 push を可能にする (#657)

## なぜ必要か

`opencode-auto` の git push は `GITHUB_TOKEN` (`github.token`) で認証される。GITHUB_TOKEN には `workflows` スコープが**存在しない**（GitHub App 専用権限）ため、`.github/workflows/` を変更するコミットはサーバ側で拒否される:

```
! [remote rejected] ai/N-x -> ai/N-x
(refusing to allow a GitHub App to create or update workflow
`.github/workflows/...` without `workflows` permission)
```

つまり CI 系 issue（improvement-scan が週次で起票する `[discovery] ci:` を含む）は、agent が正しく実装しても push 段階で構造的に失敗していた (#635)。#643 で失敗分類と warning は入ったが、実行可能にするには App token が必要。

## 仕組み

`implement-direct` ジョブのみ:

1. `Check GitHub App token secrets` — `GH_APP_CLIENT_ID` / `GH_APP_PRIVATE_KEY` secrets が両方あるか検査。無ければ warning を出して `configured=false`
2. `Mint GitHub App installation token` — `actions/create-github-app-token@v3` で installation token を発行。`owner`/`repositories` は省略（デフォルトで current repository のみに限定）、`permission-contents: write` + `permission-workflows: write` にダウンスコープ
3. `Configure git auth` — `insteadOf` のトークンを **App token 優先・GITHUB_TOKEN フォールバック**に。agent の途中 push・stale 掃除・pushpr ステップの全 git 操作に効く

`gh` CLI 系（issue/PR/label/comment）は従来通り `GITHUB_TOKEN` — 権限分離を維持。research-direct / official action 経路は対象外（research は docs 成果物のみ、official action は内部で `github.token` を使う）。

secrets 未設定でも job は壊れない — 現行動作（workflow 変更 push は失敗）にフォールバックする。

## セットアップ手順（人間側・要ブラウザ）

1. https://github.com/settings/apps → **New GitHub App**
   - 名前例: `bookq-automation`
   - Homepage URL: `https://github.com/kentoku24/bookq`
   - **Webhook: Active のチェックを外す**（イベント受信不要）
   - Repository permissions:
     - **Contents: Read and write**
     - **Workflows: Read and write**
     - 他はすべて No access（Metadata: Read は自動）
   - "Where can this GitHub App be installed?" → **Only on this account**
2. Create 後の App 設定ページで **Client ID** をコピー（`Iv23...` 形式。App ID ではなく Client ID — action v3 の推奨入力）
3. 同ページ下部 **Generate a private key** → PEM ファイルがダウンロードされる
4. 左メニュー **Install App** → `kentoku24` → **Only select repositories** → `bookq` を選択して Install
5. repo secrets を登録:
   ```bash
   gh secret set GH_APP_CLIENT_ID --repo kentoku24/bookq   # Client ID を貼る
   gh secret set GH_APP_PRIVATE_KEY --repo kentoku24/bookq # PEM 全文（BEGIN/END 行含む）を貼る
   ```

## 検証

- secrets 設定後の最初の implement-direct run で `Mint GitHub App installation token` が success になること
- `.github/workflows/` を変更する issue で agent が push → `gh pr create` まで完走すること（完了条件、#657）
- secrets 未設定の間は `Check GitHub App token secrets` が warning を出し fallback で動くこと

## ロールバック / 運用注意

- **ロールバック**: secrets を削除すれば即座に GITHUB_TOKEN フォールバックへ戻る（workflow 変更の再マージ不要）
- App token は repo スコープ・約1時間有効・権限は contents+workflows のみ。insteadOf で `.git/config` に埋め込まれるが、agent は既に `GH_TOKEN` を読める立場のため露出面は同等
- private key をローテーションする場合は App 設定ページで再生成 → `GH_APP_PRIVATE_KEY` を更新するだけ（workflow 変更不要）
- App に他の権限を後から追加しても、token は `permission-*` 入力で contents+workflows にダウンスコープされるため影響しない
