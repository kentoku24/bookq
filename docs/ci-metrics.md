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

- **collect**: `gh api repos/{repo}/actions/runs` を `created=<start>..<end>`
  の時間範囲で取得する。`created` フィルタは検索基盤経由で **1 クエリ
  1,000 件が上限**（total_count は正しく全件を数えるが page 11 以降は空）の
  ため、`total_count > 1000` のウィンドウは時間軸でバイセクションして
  葉が上限以下になるまで分割する（#651: 従来の `created>=` 単一クエリでは
  直近 ~3.7 日分しか取れず baseline が永久に空だった）。
  run 毎に jobs を `--workers`（既定 8）で並列 fetch — steps は jobs 一覧の
  レスポンスに埋め込み済みなので job 詳細の追加コールは不要。
  重い job（`ios-test` / `safari-gate` / `pytest` / `rust` 等）の steps のみ
  `--max-step-jobs`（既定 200）まで記録し step hotspot に使う。
  job の `created_at` / `started_at` / `completed_at` から
  duration（started→completed）と queue（created→started）を算出。
  null 時刻の job は集計から除外（0 扱いで統計を歪めない）。
  jobs 取得の API 失敗は `jobs_fetch_failures` として計上し、
  run 取得の未完了（API エラー・分割深さ上限）は `runs_truncated` として
  どちらも issue 本文に警告表示される。
- **report**: 直近 7 日 vs 前 7 日の workflow×job 別テーブル
  （n / duration p50・p90 / queue p50・p90 / fail率）+ step hotspot を issue 本文に
  render し、毎回上書き更新。履歴は analyze コメントが担う。
  state marker `<!-- ci-metrics:run:<run_id> -->` で最終更新を管理。
  fail率の分母は verdict のある conclusion（success/failure/timed_out）のみ —
  job `cancelled`・`skipped` と **cancelled run 内の全 job**（success 含む）は
  `n_cancelled` / `n_skipped` に別計上する。cancelled run は中断された計測
  であり verdict を一切数えない — キャンセル前に失敗した job も、supersede
  した新しい run が同一 or より新しいコードを再テストするため deterministic
  な失敗はそちらで再検出される（`always()` 集計ジョブが cancelled needs を
  failure と誤報告する artifact 対策を兼ねる、#651）。

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
