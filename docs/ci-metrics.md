# CI ボトルネック定期計測 (ci-metrics)

`.github/workflows/ci-metrics.yml` が **毎日 07:00 JST**（cron `0 22 * * *`、手動は
`gh workflow run ci-metrics.yml`）に直近 14 日分の Actions 実行を取得し、単一の
`ci-metrics` ラベル付き tracking issue を upsert する。改善の before/after 評価と
ボトルネック再発検知のための計測基盤（#627）。

## 仕組み

| job | 内容 |
|---|---|
| `collect` (ubuntu-latest) | `python3 scripts/ci_metrics.py collect` → `ci-metrics.json` を artifact upload |
| `report` | `report` 実行 → issue upsert → outputs `needs_analysis` / `issue_number` / `summary` |
| `analyze` | `needs_analysis == 'true'` の時のみ実行。prod-monitor と同じ OIDC→OpenCode API key 交換で診断コメントを **1件** 投稿（コード変更・PR・close/relabel 禁止） |

本体は `scripts/ci_metrics.py`（stdlib + `gh` CLI のみ、prod_monitor.py と同型の
collect/report 2サブコマンド構成）。

- **collect**: `gh api repos/{repo}/actions/runs` をページネーションで取得し、
  run 毎に jobs、重い job（`ios-test` / `safari-gate` / `pytest` / `rust` 等）のみ
  steps を取得。job の `created_at` / `started_at` / `completed_at` から
  duration（started→completed）と queue（created→started）を算出。
  null 時刻の job は集計から除外（0 扱いで統計を歪めない）。
  `--max-pages`（既定 10）と `--max-step-jobs`（既定 200）で API コールに上限。
- **report**: 直近 7 日 vs 前 7 日の workflow×job 別テーブル
  （n / duration p50・p90 / queue p50・p90 / fail率）+ step hotspot を issue 本文に
  render し、毎回上書き更新。履歴は analyze コメントが担う。
  state marker `<!-- ci-metrics:run:<run_id> -->` で最終更新を管理。

## シグナル (`decide_analysis`)

current 側 n>=5 のみ評価。baseline 無しの初回は回帰/new-job 判定を skip し絶対値のみ表示。

- job duration p90 が baseline の **1.5倍超**
- macOS job（label に `macos` / `xcode`）の queue p90 > **15分**
- job failure rate > **10%**
- 新規 workflow/job の出現

## 運用

- 手動実行: `gh workflow run ci-metrics.yml`（collect→report が完走し tracking issue が作成/更新される）
- ローカル確認: `gh auth login` 済みなら
  `GH_REPO=<owner/repo> python3 scripts/ci_metrics.py collect --days 14` →
  `python3 scripts/ci_metrics.py report --dry-run`
- 無効化したい場合は workflow の `schedule` を消して merge、または Actions UI で disable する。
