# 性能 KPI と計測

## KPI（決定 2026-09-21、iPhone プロファイル、example-host 実データ、3 run 中央値）

| 指標 | ベースライン (2026-09-21) | after PR #192 (2026-09-21) | 目標 |
|---|---|---|---|
| トップ初タイル描画（cold） | 826ms | 506ms | ≤300ms |
| トップ転送量 | 8.3MB | 576KB | ≤1MB |
| 本を開く→初画像 | 55ms | 54ms | ≤150ms 維持 |
| ページ送り中 long task | 0 | 0 | 0 維持 |
| 開く時の先読み転送量 | 2.9MB | 2.9MB（不変） | 観測のみ（画像は原寸配信を維持する決定） |

## 2 層のデグレ検知

1. **CI（決定的指標）** `tests/test_perf_budget.py` — `/api/folders` ボディ転送 2 回（shallow `?depth=1` 先行 + full 追従）・gzip 比、
   本を開いた直後の `/image/` リクエスト数 = `PREFETCH_AHEAD + 1`、ページ送り 10 回で long task 0。
   `tests/` 一括実行に含まれる。
2. **手動（ms 指標）** `scripts/perf/measure.py` — 実データのある example-host に対して PR ごとに before/after。

## 手動計測の手順

1. ブランチを push し、example-host 側で `git pull` → `./dev.sh up --lan-allow --local`（人間が実行）。
2. `uv run --with playwright --with-requirements requirements-dev.txt python scripts/perf/measure.py http://example-host.local:8080`
   （`ONLY=A` で iPhone のみ（B / A_cpu4x も可）、`--runs 1` で素早く）
3. 出力 `output/perf-<timestamp>/baseline.md` の表を PR 本文に貼る。

`/image` `/thumb` は gzip 対象外・リサイズ無し。`/api` `/static` `/sw.js` は gzip（`app/compression.py`）。
静的アセットは `Cache-Control: no-cache` + ETag（`app/static_files.py`）。

`/api/folders` の ETag はペイロードのダイジェストのみ（index キャッシュの timestamp を含まない）ため
5 分の index キャッシュ TTL をまたいでも回転しない。直列化済みボディも index キャッシュの世代ごとに
1 回だけ作り（`request.app.state.folders_body_cache`）、200/304 とも再シリアライズしない（#196）。

## root-folder-perf-under-5s after-state（2026-09-27、合成 localhost・A_iphone 390x844 DPR3）

plan-track `.omo/plans/root-folder-perf-under-5s.md` Todos 2–6 適用後の合成計測
（uiux_tree 24 tiles / 68 imgs、既存 KPI 行は不変・本ブロックのみ追加）。
実機 cold 中央値の合否は未測定 — runbook 実施後に本ブロックへ追記する。

| 指標 | before run1 (cold) | after run1 (cold) | before 中央値 | after 中央値 |
|---|---|---|---|---|
| 軽量 display-complete（合否点、単一カバー `?w=200` 全件 `img.complete`） | 1075.9ms | 359.4ms | 128.0ms | 224.4ms |
| first-tile | 56.3ms | 149.3ms | 43.4ms | 60.8ms |
| `/api/folders` TTFB（shallow `?depth=1` 先行 + full 追従） | 8.0ms | 1.4ms | 1.8ms | 2.5ms |
| `/thumb` 数 / 転送量 | 168 / 108470B | 176 / 117558B | 167 / 108470B | 176 / 117558B |

- 中央値が上がるのは極小合成ツリーのノイズ床（2段階fetchの往復が勝つのは実データcold）。
  cold run1 で約67%短縮（shallow先行 + scan N+1撤去 58.6→23.5ms + 単一カバー先行）。
  順序 shallow start < 初 `/thumb` start は全runで成立、最終DOM一致（24/68→68）。
- 証跡: `.omo/evidence/root-perf-final/baseline.md`（before/after表 + 実機runbook）
  + `constraints.md`（F3 11項目・各cite付き）+ `pytest.log`（FULL suite 536 passed）
  + `after-waterfall.json`、`.omo/evidence/root-perf-thumb/network-bytes.md`（軽量/追従バイト分離）。

### TODO（人間測定・example-host実データ・iPhone17実機Safari・tailnet HTTPS・cold 3runs中央値）

- [ ] 軽量 display-complete 中央値 ___ ms（<5000ms で合否、D3記録付き）
- [ ] first-tile 中央値 ___ ms（副目標 ≤1000ms）
- [ ] `/api/folders` TTFB + bytes 中央値 ___ ms / ___ B
- [ ] `/thumb` 数 + bytes（軽量 / 追従分離）中央値 ___ / ___ B
- [ ] `screenshot.png` を `.omo/evidence/root-perf-final/` に保存
- 手順は `.omo/evidence/root-perf-final/baseline.md` の Real-device runbook に固定。
