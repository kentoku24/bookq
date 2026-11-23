# improvement-scan — 定期ボトルネック調査 → 改善 Issue 起票

`.github/workflows/improvement-scan.yml`。リポジトリと CI 状態を定期的に調査し、
ボトルネックを発見して改善 Issue を起票するメタワークフロー。
起票された Issue は人間がトリアージし、`ai/implement` / `ai/research` を
付けると既存の opencode-auto パイプラインに乗る。

issue #630 で新設。

## 構成

```
scan ジョブ (ubuntu-latest, 30min timeout)
  1. checkout (fetch-depth: 0)
  2. .github/scripts/discovery-collect.sh → signals.md
     (決定的シグナル収集。gh api / gh issue|pr list / git grep のみ、read-only)
  3. `discovery` ラベルを ensure (無ければ作成)
  4. prompt.txt を組み立て → opencode CLI (direct-run) を実行
     - signals.md + repo + gh を調査
     - ボトルネック候補を最大3件評価、証拠必須
     - dedup (open/closed issue 検索 + `discovery` ラベル照合)
     - 新規 Issue は `[discovery] <area>:` 形式で起票
       (ai/* ラベルは付けない)
     - 既存 open issue があれば証拠コメントを1件追加 (新規起票しない)
     - discovery-report.md を出力 (commit しない)
  5. discovery-report.md を artifact 化

report ジョブ (if: always())
  - meta Issue `[meta] improvement-scan 実行ログ` に実行サマリをコメント
  - scan 失敗時も結果と run URL を残す
```

## スケジュール / 手動実行

- cron: `37 21 * * 0` — 日曜 21:37 UTC = **月曜 06:37 JST** に週1回
- 手動: Actions → improvement-scan → Run workflow。`focus` 入力で
  今回の調査ヒントを指定できる (例: `iOS UITest flakes`)

## 収集するシグナル (discovery-collect.sh)

| セクション | 内容 |
|---|---|
| §1 | 既存自動化インベントリ (全 workflow の name + cron) — 再提案防止 |
| §2 | workflow 毎の直近14日 run 統計 (回数/成功/失敗/中央値 wall 分) |
| §3 | 直近失敗 run 15件 + 失敗 job 名5件 (flake 検知) |
| §4 | open issue 棚卸し (76件規模: label histogram + 最新60件) |
| §5 | open PR 棚卸し (draft/dependabot 含む) |
| §6 | 直近14日の merge 数 |
| §7 | TODO/FIXME/HACK の領域別ヒートマップ |
| §8 | docs/reports 最新一覧 + docs/performance.md の未消化 TODO |

run 統計は workflow 毎に `actions/workflows/{id}/runs` を叩く
(リポジトリ全体で ~1800 runs/週あり、全体一括 per_page=100 では
ios に偏るため)。各 workflow 100件上限・truncated 注記あり。

## 安全弁

- **新規 Issue は最大3件/回**。証拠閾値未満なら0件で正常終了
- **dedup 必須**: open/closed issue 検索 + `discovery` ラベルの既存
  finding と照合。wontfix/duplicate は再起票しない
- **`ai/implement` / `ai/research` を付けない**: bot が自動実行しない
  ための人間ゲート。GITHUB_TOKEN 起点の `issues:opened` はそもそも
  ワークフローを再発火しないので二重に安全
- エージェントはコード変更・commit・push・PR 作成を行わない
  (issue 操作のみ)。テストゲートも回さないため research 系より軽い
- 既存自動化 (opencode-auto / prod-monitor / dependabot-auto-merge /
  api-* / 各 CI) の再提案を禁止 — §1 インベントリを参照

## `[discovery]` Issue のトリアージ

1. Issue を読み、証拠 (`## 証拠`) が妥当か確認
2. 対応するなら `ai/implement` (実装) または `ai/research` (追加調査)
   ラベルを付与 → opencode-auto が拾う
3. 対応しないなら close (理由をコメント)。wontfix/duplicate で閉じた
   ものは次回 run の dedup 対象になる
4. open のまま長期滞留する discovery Issue は aging シグナルとして
   次回 run が検知する

実行ログは meta Issue `[meta] improvement-scan 実行ログ` に各 run の
サマリが積まれる。異常時 (scan 失敗・Issue 暴発) はここを確認。

## 前提・コスト

- `vars.USE_DIRECT_RUN=true` が前提 (opencode-auto.yml と同じ
  direct-run 経路。false に戻す場合は公式 action 版の追加が必要)
- 追加シークレット不要 — OIDC → OpenCode API キー交換は既存と同一
- 権限: `contents: read`, `issues: write`, `pull-requests: read`,
  `actions: read`, `id-token: write`
- LLM 課金は opencode Zen クレジット。週1回・最大3 Issue 起票の
  上限で抑制。月次上限は docs/openresearch-auto-impl.md §5 参照

## トラブルシュート

| 症状 | 確認 |
|---|---|
| run が失敗 | meta Issue の最新コメント or Actions ログ。collect 失敗時は signals.md 未生成で scan ごと skip |
| Issue が起票されない | `discovery-report.md` (artifact) に「候補なし/全て dedup」の記述があるか。エージェント判断で0件は正常 |
| 重複 Issue が起票された | dedup 検索が効かなかった可能性。issue を close してプロンプトの dedup 節を強化検討 |
| 既存自動化を再提案された | signals.md §1 のインベントリが古い/不足。collect script の収集範囲を確認 |
