# Mutation testing (StrykerJS / cargo-mutants)

テストが全て通っても「壊れた実装を検出できるか」は別問題。mutation testing は
実装を機械的に書き換え (mutant を生成し)、テストがそれを検出 (kill) できるかを
測る。検出されず残った mutant = survived がテストの穴、または等価変異
(意味を変えない変異) を示す。

- Rust (`rust/`) → **cargo-mutants** (`rust/.cargo/mutants.toml`)
- JS (`web/static/` のコア層) → **StrykerJS** + vitest (`stryker.config.mjs`)

対象外: iOS Swift / Android Kotlin (実用的な mutation ツールなし)、Python
(本番コードは #503 で削除済み)、DOM 直結のフロントモジュール
(`app.js`・gesture/zoom/pair transport 等 — Playwright 側の守備範囲)。

**ローカル実行専用**。全量実行は数時間オーダーのため CI には載せない。
日常の確認は差分スコープ (`--in-diff` / 変更ファイルのみ) を使う。

## 前提

```bash
# Rust 側 (cargo-mutants は rustup component ではなく cargo install 済み前提)
cargo mutants --version    # 27.x

# JS 側 (worktree ごとに 1 回)
npm install                # ルート package.json — node >= 20 推奨
```

## Rust: cargo-mutants

```bash
scripts/mutation/rust.sh                 # ワークスペース全体 (~3,972 mutants)
scripts/mutation/rust.sh --in-diff       # 未コミット/HEAD との差分行のみ
scripts/mutation/rust.sh -f archive.rs   # ファイル glob で絞る
scripts/mutation/rust.sh --list          # 実行せず mutant 一覧
scripts/mutation/rust.sh --shard 1/4     # 分割実行 (並列マシン向け)
```

### TMPDIR + シンボリックリンクが必須な理由

cargo-mutants は `rust/` **だけ** をスクラッチツリーにコピーする。しかし
`viewer-rs` は `include_str!("../../../web|api/...")` でコピー範囲外の
ファイルを参照しており、そのままでは baseline ビルドが必ず失敗する
(`couldn't read ../../../web/templates/index.html`)。

`scripts/mutation/rust.sh` はこれを解決する: worktree ごとの TMPDIR
(`/Volumes/Sabrent2T/mut-<tree>-tmp`、外付け未接続時は `~/.cache/bookq/`)
を作り、その直下に `web`/`api` を**この checkout への symlink** として置く。
cargo-mutants の一時コピーは `$TMPDIR/cargo-mutants-rust-*.tmp` に作られ、
`viewer-rs/src/../../../web` が `$TMPDIR/web` に解決される。

手動で回す場合も同じことをすればよい:

```bash
MUT_TMP=/Volumes/Sabrent2T/mut-<name>-tmp
mkdir -p "$MUT_TMP"
ln -sfn "$PWD/web" "$MUT_TMP/web"; ln -sfn "$PWD/api" "$MUT_TMP/api"
cd rust && TMPDIR="$MUT_TMP" cargo mutants -j 4
```

### 結果の読み方

`rust/mutants.out/mutants.out/` (gitignore 済み):

- `outcomes.json` — 集計 (caught/missed/timeout/unviable、所要時間)
- `missed.txt` / `caught.txt` / `timeout.txt` / `unviable.txt` — mutant 一覧
- `mutants.html` — ブラウザで見やすいレポート (diff 付き)
- `log/` — baseline・mutant ごとのテストログ

mutation score = `caught / (caught + missed + timeout)` (unviable は分母外)。
`timeout` はテストが timeout まで走った mutant — 多くは実質 kill だが
遅いテストが原因の場合もあるので個別に見る。

## JS: StrykerJS + vitest

```bash
scripts/mutation/js.sh test       # vitest 単体テスト (~秒)
scripts/mutation/js.sh mutate     # stryker run (~時間、初回は mutant 数千個)
npm run test:js:watch             # 開発中の watch
```

### 構成

- `package.json` (リポジトリルート・新規) — devDeps: `vitest`, `happy-dom`,
  `@stryker-mutator/core`, `@stryker-mutator/vitest-runner`
- `vitest.config.mjs` — `environment: 'happy-dom'`、setup file が
  `web/static/` の IIFE スクリプトを `index.html` の defer 順でロードする
  (本番コードをそのまま実行。コピーや再実装ではない)
- `web/tests/unit/` — `stubs.js` (Image/rAF の最小 stub) + `setup.js`
  (ロード順制御) + `*.test.js`
- `stryker.config.mjs` — `mutate` はコア 8 ファイルのみ:
  `offline-core.js`, `api/endpoints.js`, `api/v1.js`,
  `thumbs/resolveCover.js`, `viewer/pairModel.js`, `viewer/spread.js`,
  `search/facets.js`, `browse/hash.js`

### 結果の読み方

- `.reports/mutation/stryker.html` — mutant ごとの diff・status・実行テスト
- `.reports/mutation/stryker.json` — 機械可読 (score 集計・survived 一覧)
- コンソールの clear-text レポート — ファイル別スコアと survived mutant 抜粋

status の意味: `Killed` (テストが検出) / `Survived` (未検出=テストの穴 or
等価変異) / `NoCoverage` (どのテストもその行を実行しない) /
`Timeout` / `RuntimeError` / `CompileError`。

## survived mutant のトリアージ

スコアを上げるために闇雲にテストを足すのではなく、mutant ごとに 3 分類する:

1. **テスト不足** — 実装の意味ある分岐・境界を固定していない。
   振る舞いを固定するアサーションを追加する。
2. **等価変異** — 変異しても観測可能な振る舞いが変わらない
   (例: 到達不能な防御コード、同値な式変形)。`// Stryker disable` /
   `// cargo-mutants` の ignore 機構、またはコメントで理由を残して受理。
3. **対象外の責務** — DOM 描画・ブラウザ API 依存など unit の守備範囲外。
   `mutate` 対象から外すか、結合テスト側で担保する判断を記録する。

## 実行頻度の指針

- コアロジック (`offline-core`, `pairModel`, `spread`, `v1` 等) を変更したら
  PR 前に該当ファイルだけスコープ実行して survived が増えていないか確認。
- 全量実行は定期的な棚卸し (例: 四半期 or 大きなリファクタ前) に。
- ベースライン結果は `docs/reports/` に日付つきで記録する。
