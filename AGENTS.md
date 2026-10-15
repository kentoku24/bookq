# AGENTS.md — ボット運用規約 (bookq)

このリポジトリで動作する AI ボット (opencode) の共通規約。すべての自動化ワークフロー (implement / research) はこれに従うこと。

## 1. ブランチ命名

```
ai/<issue番号>-<slug>
```

例 (issue-track): `ai/42-add-login-endpoint`

- `main` への直接 push は禁止 (`opencode.json` の permission で deny 済み)
- feature branch (`ai/*`) からの push と PR 作成は許可
- issue-track（bot/auto）：`ai/<issue番号>-<slug>`（従来通り。`ai/implement`・`ai/research`・bridge 経由は必須）。
- plan-track（local /start-work）：`ai/<slug>`（番号なし。`.omo/plans/<slug>.md` が spec。番号不明時の捏造は禁止）。
- bot 経路（`ai/implement`・`ai/research`・bridge）では issue-track を使用すること。plan-track の使用は禁止。
- plan-track の slug は `^[0-9]+-` に一致させないこと（`ai/42-foo` との衝突防止）。plan slug は `.omo/plans/<stem>.md` を verbatim 使用し再導出しない。
- 形式: `ai/<N>-<slug> OR ai/<slug>`（issue-track OR plan-track）。

## 2. PR タイトル形式

```
<type>(ai): <要約> (#N)
```

- issue-track では `N` は必ず issue 番号を参照すること (必須)
- `type` は `feat` / `fix` / `docs` / `chore` / `refactor` / `test` など conventional commits 準拠
- 例: `feat(ai): ログインAPIエンドポイント追加 (#42)`
- issue-track（bot/auto）：`<type>(ai): <要約> (#N)`（従来通り。`N` は issue 番号必須、本文は `Closes #N`＋テスト考慮事項。詳細は §4 参照）。
- plan-track（local /start-work）：`<type>(ai): <要約>`（番号なし。本文に `Plan: .omo/plans/<slug>.md` の必須記載＋merge-base SHA 併記＋テスト考慮事項。詳細は §4 参照。`Related #N` は昇格時のみ任意追記。番号不明時の捏造は禁止）。
- 後付け昇格（任意・PR オープン前の未マージ作業のみ）：`gh issue create --title "<plan題>" --body "$(cat .omo/plans/<slug>.md)"` で `#N` を払出し、本文に `Closes #N` 追記可。PR オープン後の `git branch -m` は禁止し、必要なら skill 既存の replacement-PR 方式（fresh branch に cherry-pick、旧 PR は superseded で close）。
- plan-track の PR オープン中は plan ファイルを編集しない（可変ファイル参照の腐敗防止）。

## 3. スラッグ規則

issue タイトルからスラッグを生成する:

1. 小文字化
2. 空白 → ハイフン (`-`)
3. 英数字以外の文字を除去
4. **50 字以内** に切り詰め

例: issue タイトル `Add Login Endpoint!` → `add-login-endpoint`

## 4. implement タスク

- 小さなインクリメンタルコミットを積む (巨大な単発コミット禁止)
- コミットメッセージは conventional commits 形式
- PR 本文には**テストに関する考慮事項**を必ず記載する (実施したテスト、影響範囲、確認方法)
- live 実データ回帰 (`BOOKQ_LIVE_ROOT=... ./scripts/live-regress.sh`) の実施有無も PR 本文に記載する。live のフォルダ名はコード・ログ・レポートに出さない (tests/live/README.md の漏洩防止ルール遵守)

## 5. research タスク

- 調査結果の**要約コメントを issue に投稿**する
- 詳細レポートは `docs/reports/YYYY-MM-DD-<slug>.md` に保存する
  - 例: `docs/reports/2026-08-24-llm-provider-comparison.md`

## 6. 権限まわりの注意

- `.env` の読み取りは opencode の permission システムによりデフォルトで deny される
- `main` 保護・破壊的操作 (`rm -rf`, force push, POST リクエスト等) の deny ルールは `opencode.json` を参照

## 7. bookq 固有の注意

### フォルダツリー / scanner

- `app/scanner.py:92` `scan_tree` — 単一子フォルダの flatten: 画像がなく子が 1 つだけのフォルダは親にホイストされ `name` のみが `parent/child` に書き換わる。`thumb`/`thumbs` は書き換え不要 — フォルダ相対のためフロントが `/image/<_fullPath>/<thumb>` として解決する (`app/static/thumbs/resolveCover.js:11-16`, `_fullPath` は `app/static/app.js:1308` で構築)。prefix を付与しないこと。
- 変更時は `app/templates/index.html` のサムネ参照と整合させること。`thumbs` (2+ サブフォルダに thumb がある時のみ) による multi-thumb グリッド分岐がある。

### URL ハッシュ状態 / Viewer

- `app/static/browse/hash.js` `applyUrlState` (490L, 単一 `hashchange` リスナ) — ビューア状態は URL ハッシュ `#/view/<folder>?p=<page>`、ブラウズ階層は `#/browse/<path>` で管理。`app/static/app.js` (3252L composition root IIFE) は `window.parseViewerHash / window.applyUrlState` へ委譲し `window.urlRestoring` / `window.urlRestoreGen` でミラー共有。`hashchange` のみをリッスンし `popstate` と二重登録しないこと。`urlRestoring` / `urlRestoreGen` トークンで競合する非同期 load をガードする。
- `app/templates/index.html` — 161行 shell (`{% if not user %}` でログイン/ビューア分岐)。JS は外部分割: `app/static/app.js` 3252L composition root + 12 modules (`offline-core.js` 333L, `thumbs/resolveCover.js` 58L, `browse/hash.js` 490L, `browse/render.js` 510L, `browse/scroll.js` 157L, `search/facets.js` 356L, `offline/storeAdapter.js` 646L, `thumbs/prefetch.js` 140L, `viewer/spread.js` 442L, `viewer/gesture.js` 147L, `viewer/zoom.js` 301L, `viewer/overview.js` 639L; `index.html` の defer 順; CSS は `style.css` 内包)。ビルドステップなし。編集後は再起動不要 (`app/routes.py:26` の global `_jinja_env = Environment(auto_reload=True, cache_size=0)` が `get_template` をキャッシュしつつ即時反映)。

### テスト

```bash
uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/ -q
```

- `BOOKQ_TEST_MODE=true` が必須 — `app/auth.py` の `_TEST_MODE` は import 時に一度だけ読まれ、OAuth をバイパスして mock user を返す。
- `DOWNLOADS_DIR` を一時ディレクトリに向ける (`tests/conftest.py` が合成ツリーを生成)。
- `uvicorn[standard]` は `requirements-dev.txt` 側のみに存在 (runtime `requirements.txt` は hypercorn)。`--with-requirements requirements-dev.txt` の付与を省略しないこと。

### デプロイ / ネットワーク

- viewer はホストポートを公開しない。唯一の到達経路は tailscale サイドカーの `tailscale serve` (HTTPS :443 → viewer:8080)。`viewer` が `bookq_default` ネットワーク外だと 502 になる。
- 開発環境は `./dev.sh up` で管理 (10スロットプール、project `bookq-dev-pool-XX` 形式。真実は docker label `com.bookq.name` / `com.bookq.pool_slot` のみ)。本番 `docker compose up` を worktree で直接叩かないこと。

### iOS Simulator / エージェントによる実機確認

- **Source of truth: `.claude/skills/ios-simulator/SKILL.md`**。エージェントはスキル経由で実行。詳細は `references/` を参照。薄い人間向け要約は `docs/ios-simulator.md`。
- 既定 `iPhone 17`（`SIMULATOR_DEVICE` で変更可）、次は Android 15 1920x1200 Unisoc T620 2.2GHz / Mali-G57。`./scripts/open-simulator.sh <dev-url>` で開く。

## 8. 並行作業の隔離

- 書き込みを伴う作業は単発でも必ず別 worktree (`ai/<N>-<slug> OR ai/<slug>` ごとに1つ、`.worktrees/` 内)。1 issue/plan = 1 worktree を維持する (2026-10-05 以降、単発作業の除外なし)。
- メインのチェックアウト (リポジトリ直下) は常時 `main` に固定し、直接編集・commit しない (`prd.sh` による本番操作専用)。
- 作業開始時チェックリスト: `git worktree list` で既存確認 → なければ `git worktree add .worktrees/<name> -b ai/<N>-<slug> origin/main` (plan-track は `-b ai/<slug>`) → その worktree に移動して作業。`origin/main` から切り、ローカル HEAD から切らないこと。
- 1 worktree = 1 dev 環境名。worktree 作成時に `./dev.sh up <name>` でスロット占有を宣言し、マージ時に `./dev.sh down <name>` + `git worktree remove` で解放する (10スロットプールのため同時10以下を仮定)。
- 読み取り専用 (調査・レビュー) のみ同居可。`app/static/app.js` の同時編集は直列化すること。
- 残置掃除は `./scripts/cleanup-worktrees.sh` (既定 dry-run、`--apply` で削除)。マージ済み・clean のみ対象、detached/dirty/dev 環境は手動。
