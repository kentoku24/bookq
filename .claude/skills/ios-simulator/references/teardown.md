# Teardown — sim 検証終了時の片付けと CPU 監査

sim 検証は **`./scripts/sim-teardown.sh` の実行 + 合格で完了** とする。
「sim を shutdown した」だけでは終了扱いにしない — shutdown しても残る
残留プロセスが実在し、ホストの CPU を食い潰す (2026-11-01 実例:
`xcodebuild test` の `simctl diagnose` 収集が停滞し、孤児 simruntime プロセスと
孤児 `spindump` が CPU 100% に張り付いた)。

## 手順

```bash
./scripts/sim-teardown.sh                  # sim 系の片付け + CPU 監査
./scripts/sim-teardown.sh --dev-env <name> # 検証用 dev 環境の down も一括
./scripts/sim-teardown.sh --udid <u>       # shutdown 対象を1台に限定
./scripts/sim-teardown.sh --keep-clones    # Clone デバイスを残す
./scripts/sim-teardown.sh --keep-idb       # idb_companion を残す
```

スクリプトがやること (冪等、何度実行してもよい):

1. `--dev-env` 指定時: `./dev.sh down <name>` (user-state volume は保持。`--full` は使わない)
2. booted sim を shutdown (`--udid` 指定時はその1台のみ)
3. xcodebuild テスト Clone デバイス (名前が `Clone *`) を delete
4. `~/Library/Developer/XCTestDevices/` の残留データを削除 —
   `xcodebuild test` が作るテストデバイス data (sim/実機共通) で、
   実行ごとに残り十数GBにまで膨らむ。実行中テストがあれば skip
5. `idb_companion` 停止 — 次回 `idb` 使用時に自動再接続するので止めてよい
6. 残留 `simctl diagnose` と孤児化 `spindump` (ppid==1) を kill
7. CPU 監査: simruntime / CoreSimulatorService / testmanagerd / liveactivitiesd /
   spindump で %CPU>20 の残留があれば非0終了

## スクリプトが殺さないもの (報告のみ)

- **実行中 `xcodebuild`** — 別セッションが回している可能性がある。
  自分が起動したものなら終了まで poll すること (後述)。他セッション物と
  分かっている停滞は `ps -o ppid` で親を確認してから個別に kill。
- **別セッション所有の dev 環境** — `./dev.sh list` で名前を確認し、
  自分の環境名だけを `--dev-env` に渡すこと。
- Devin/iTerm/WindowServer/Docker VM などホスト常駐プロセス。
- root 所有の `spindump` — kill には sudo が必要。通常は数分で自己終了する。

## xcodebuild test を使った検証の注意

`xcodebuild test` は全テスト完了後に `simctl diagnose` でシミュレータの
ログ収集を行う (`--timeout=600`)。ここでバックグラウンド放置すると
診断対象の simruntime プロセス (liveactivitiesd, mobileassetd 等) が
CPU を空転し続けることがある。

- `xcodebuild test` は**必ず終了まで poll** して結果を受け取る
- 終了確認後に `pgrep -fl "xcodebuild|simctl diagnose"` が空であることを確認
- Clone デバイスは xcodebuild が自動削除するが、kill した場合は残る →
  `sim-teardown.sh` が `Clone *` を拾って削除する

## 残った高CPUの見方

`ps -Ao pid,pcpu,command -r | head` で上位を見る。sim 検証由来はだいたい
パスに `iOS XX.X.simruntime` を含むか `CoreSimulator`/`simctl`/`testmanagerd` の名前。
`Devin Helper`/`WindowServer`/Docker VM (`Virtualization.VirtualMachine`) は
ホスト常駐なので触らない。loadavg が高いまま収束しない場合は
`pgrep -fl simruntime` が空かどうかをまず確認する。
