# ADR 0001: ブラウザ操作の動画デバッグ — Muse Spark 1.2 をフレーム抽出で利用する

- **Status:** Accepted
- **Date:** 2026-08-23
- **Deciders:** bookq-maintainers, Sisyphus
- **Context:** Phase 0 検証 (`ses_...` 2026-08-23)

## Context

bookq のビューアは単一 HTML IIFE でスプレッド配置・ページめくり・アニメーションを JS で制御しており、静止画の差分では捉えられない不具合（黒背景の滞留、スプレッドのリフロー、遷移中のチラつき）が存在する。`muse-spark-1.2` は動画入力を謳うため、「ブラウザ操作を録画→モデルに時系列で何が起きたかを語らせる」デバッグが可能か検証した。

検証環境: `opencode-go/muse-spark-1.2-contributor` / `opencode/muse-spark-1.2-contributor-free`, `ffmpeg 8.1.2`, `playwright 1.62.1`, `opencode 1.18.20`, `chrome-devtools` (screenshot/snapshot/performance trace)。

## Decision

**`opencode/muse-spark-1.2-contributor-free` (opencode zen 無料枠) を `opencode run -f` 経由で利用し、動画は `ffmpeg -vf fps=2~3` でフレーム抽出して複数画像として渡す。** ネイティブ `.mp4` 直投げおよび `command-code` Provider プランへの課金は行わない。

パイプライン:

```
Playwright recordVideo / ffmpeg screencast → /tmp/*.webm
  → ffmpeg -i video.webm -vf fps=2 /tmp/frame_%03d.png
  → opencode run -m opencode/muse-spark-1.2-contributor-free -f frame_*.png
  → または look_at(file_paths=[...])
```

## Alternatives Considered

| # | 案 | 検証結果 | 棄却理由 |
|---|---|---|---|
| A | `https://api.commandcode.ai/provider/v1` に `meta/muse-spark-1.2-contributor` を直接 `curl` で動画投げ | `{"error":"Your Go plan doesn't include API access. Upgrade to Provider..."}` で全モデル拒否 (`gemini-3.7-flash`, `Qwen` も同様) | ビリングでブロック。OpenCode の正規経路ではない |
| B | `opencode run -f video.mp4` でネイティブ動画を渡す | `Cannot read binary file: /tmp/ui_transition.mp4` / `look_at` は `No response from multimodal-looker` | `opencode` の file reader が画像のみ許可。無料/有料どちらも不可 |
| C | **採用: フレーム抽出 + 複数画像** | 3枚(`赤→青→緑`)、6枚(`ui_frame×3 + testsrc×3`)ともに時系列・色変化・スクロール方向・カウンタ `0→1→2` を完全正答。`cost: 0` | 既存ツールで完結、課金不要、精度は十分 |

## Validation (Phase 0 Evidence)

- 合成動画生成: `ffmpeg -f lavfi testsrc=640x360:r=10:d=3` → `/tmp/test_3s.mp4` (20KB), `color=red/blue/green concat` → `/tmp/ui_transition.mp4` (2.7KB)
- フレーム抽出: `ffmpeg -vf fps=1` → 3枚、それぞれ `~1.5KB`, base64 `~2KB`
- `opencode run -m opencode/muse-spark-1.2-contributor-free -f ui_frame_01.png` → `赤色` 正答
- `opencode run -m ...-free -f ui_frame_01.png -f ui_frame_02.png -f ui_frame_03.png` → `赤→青→緑` 正答
- `look_at(file_paths=[frame_01,02,03])` → グラデーション左スクロール + カウンタ増加を正確に記述
- 6枚同時投入でも前半/後半を分離して記述成功
- `opencode-go/muse-spark-1.2-contributor` (有料) でも同結果、差分なし。無料枠で十分

詳細ログは本セッションの Phase 0 会話を参照。

## Consequences

### Positive

- 追加課金なしで動画デバッグが可能。`ffmpeg` は既導入、Playwright `recordVideo` は `tests/conftest.py` の1行追加で有効化できる
- bookq の viewer 固有の不具合（`spreadMetrics`/`anchorSpread`/`rebaseAnchor` 周りの動画でしか見えないバグ）に直接適用可能
- `look_at` / `opencode run -f` の両経路で動作するため、TUI デバッグと CI デバッグの両方で再利用可能

### Negative / Limitations

- ネイティブ動画の音声・フレーム間補間は失われる。2fps では 500ms 未満のチラつきは見落とす可能性がある → 必要に応じ `fps=3~5` に上げる
- `opencode` の file reader が将来 `video/*` を許可した場合は本 ADR を見直す（その際も中身はサーバ側でフレーム分解されるため効果は同等と想定）

### Not Doing

- Provider プランへのアップグレード
- `opencode` 本体への `video/*` 対応パッチ（必要になるまで保留）

## References

- `~/.config/opencode/opencode.json` — `model: opencode-go/muse-spark-1.2-contributor`
- `~/.local/share/opencode/auth.json` — `opencode-go`, `meta` credentials
- `opencode models` — `opencode/muse-spark-1.2-contributor-free` / `opencode-go/muse-spark-1.2-contributor` 存在確認
- Phase 0 一時ファイル: `/tmp/test_3s.mp4`, `/tmp/ui_transition.mp4`, `/tmp/ui_frame_*.png`, `/tmp/frame_*.png`
- `CLAUDE.md` — viewer conventions (spread, anchor, rebase) — 動画デバッグの主対象
