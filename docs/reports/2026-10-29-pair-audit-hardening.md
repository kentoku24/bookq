# 2台で見開き (reader-sync) 監査・硬化レポート

- 日付: 2026-10-29
- 対象: `/ws/reader-sync` ペアリング機能全般 (issue #75 関連)
- ブランチ: `ai/75-pair-audit-hardening` (worktree: `.worktrees/75-pair-audit-hardening`)
- 範囲: `app/reader_sync/` 全 backend、`app/static/viewer/pair*.js`、
  `app/static/browse/pairSync.js`、viewer/gesture/navigation/app.js の
  paired 接合部、および関連テスト

## 検証結果サマリ

- ペア関連テスト一式: **375 passed / 0 failed / 0 errors** (約107秒)
  - test_reader_pairing.py (FakeLink 単体) + test_reader_sync_model.py: 63
  - test_reader_sync_client.py (実 pairClient.js + Playwright 注入fake): 25
  - test_reader_state / manifest / transport / verify / browse_sync /
    browse_smoothing / sync_integration / ws / ws_state / paired_view /
    pairing_ui / folder_visibility_pair を含む
- 実サーバ (uvicorn) 起動・実 WebSocket 往復: 正常 (health ~7-10s の
  コールドスタートを考慮しテスト deadline を 10s→30s に引上げ)

## 重大度順の発見と対応

### Critical (修正済)

| # | 不具合 | 影響 | 修正 |
|---|--------|------|------|
| C1 | pending 候補の `LEAVE` がセッション全体を `_end_session` していた | 6桁コードを知る第三者が join→leave するだけで待機中ペアを破壊できた (実質 DoS)。正常利用でも「QRを読んだが承認前にやめた」で leader 側が巻き添え | `sessions.py:_drop_pending` — 候補のみを外し `JOIN_CANCEL` + `pending_left` でリンクのみ閉じる |
| C2 | リレー要求の ACK/REJECT/dedup再送がリレーリンク (leader) に返った | webrtc-dc 経由で leader が運ぶ follower 要求の応答が follower 自身に届かず、leader は他人の requestId 応答を受ける。stale 要求の修正 STATE も follower に届かず発散の恐れ | `sessions.py` sequenced dispatch に `resp_link` — 応答は起点メンバのリンクへ (起点リンク喪失時は relay link にフォールバック) |
| C3 | ブラックホール化ソケットを検出する手段がなかった | モバイルの NAT タイムアウト等では close も error も届かず、サーバ PING だけではクライアントは死亡を認識できず再接続が永久に始まらない | `pairClient.js` 受信 watchdog: 20s silence → onclose 相当の経路で再接続。リンク死亡/teardown/新規 open で必ず disarm |
| C4 | `derive_phase` が `pending_load` 受理中に `selecting` を返した | pick 受理後〜manifest commit 間の STATE で leader の shared browse グリッドが「selecting」として再武装し、読者画面がちらつく/操作競合 | `state.py` — pending_load は manifest 到着前でも `loading` |

### High (修正済)

| # | 不具合 | 影響 | 修正 |
|---|--------|------|------|
| H1 | pending リンク切断時 `code_consumed` が残存 | コードは TTL 中使えず、leader は同じコードで再招待不能 (QR再表示でも同じコード) | `_drop_pending` で `code_consumed=False` |
| H2 | 遅延 `APPROVE`/`REJECT` が現在の pending 候補に誤適用 | f1 離脱→f2 参加の間に leader が f1 宛てに押した承認が f2 を勝手に承認/拒否 | `memberId` 照合 — 不一致は `NO_PENDING` で拒否 |
| H3 | `_attemptResume` が保持中 RESUME entry を上書き | リンク死亡を跨いだ RESUME の呼び出し元 promise が永遠に未解決 | 既存 RESUME entry は HELLO 後に再送 (overwrite しない) |
| H4 | `_startEntry` が実行中 entry を黙殺 | create→join 連打等で先の entry promise が settle 不能 | `superseded` で明示 reject |
| H5 | バインド済み `create`/`join`/`match` がソケットを黙殺 | 相手側が resume window (5分) の paused に残り「切断したのに相手が残っている」 | `_leaveBound` — 先に LEAVE を送出して polite 終了 |
| H6 | `_sendReady` が送信前に `_readyFor` をマーク | drop した READY でメンバが未 READY のまま phase 'loading' に停滞 | 実送信成功時のみマーク |
| H7 | RESUME 後サーバが member を 'active' に戻す | ローカル suspended 中に再接続すると「見えないメンバが active 扱い」でセッションが un-pause | `_onAck` で `_suspended` なら `SET_ACTIVITY suspended` を再 assert |

### Medium (記録済み・今回の変更セットには含めず)

ユーザー指示により Critical+High+テスト基盤にスコープを絞り、
以下は発見・修正案のみ記録 (未適用)。

| # | 不具合 | 影響 | 修正案 |
|---|--------|------|--------|
| M1 | `apply_log` 無制限増大 | 長時間セッションでメモリ増 | `APPLY_LOG_MAX=4096` で上限化 (forensic 用途のみ) |
| M2 | 匿名リンク (未バインド) が PONG 応答で無限生存 | `_end_session` でメンバを失ったソケット含め、匿名 TCP が集積 | `unbound_since` + `ANON_LINK_TTL_SECONDS=60` の reaper、ticker の due 計算にも反映 |
| M3 | 他セッション MANIFEST の混入 | `_manifest` poison → commit STATE 適用を影で停止 | `_onManifest` に `sessionId` 一致ガード |
| M4 | `_dataSend` が死んだ DC を `_dataT` に残したまま送信 | dc up→down と `_syncDataT` の隙間で drop | 死んだ DC 検出時に `_syncDataT` 再選択 → WS へフォールバック |
| M5 | pairUI の未処理 promise rejection | create/join/connect の wire 失敗が uncaught rejection | `Promise.resolve(...).catch(()=>{})` (UI 通知は既存経路) |

### テスト基盤の修正 (本番コードではないが実害あり)

- `tests/conftest.py` `server` fixture・`tests/folder_visibility_helpers.py`
  `spawn_vis_server`: health deadline 10s→30s。コールドスタートで
  metadata warmup が ~7-10s 掛かり、負荷時は10s deadline がコイン投げで
  34件の setup error を出していた。
- `test_pending_commit_latest_stashed_and_retried`: route で GET を保留
  してもクライアント側 `GET_TIMEOUT_MS=3000` の abort が 'error'/'readonly'
  に遷移させ、最大3秒しか 'loading' が持たない設計上の race。init script
  で boot-embedded snapshot と localStorage cache を殺し `hasSnapshot=false`
  で「判定不能」を決定的に維持 (error+snapshot無しでも stash する本番契約と
  一致)。
### 新規回帰テスト (tests/test_reader_pairing.py)

- `test_pending_leave_keeps_waiting_session_and_frees_code` (C1/H1)
- `test_pending_detach_frees_code_for_retry` (H1)
- `test_approve_with_stale_member_id_does_not_swap_candidate` (H2)
- `test_relayed_request_reject_goes_to_origin_link` (C2)
- `test_pending_load_phase_is_loading_not_selecting` (C4)
- 既存 `test_pending_leave_ends_attempt` を新仕様に書き換え

## 残存リスク / 未検証領域

1. **実機ブラウザでのライフサイクル** — iOS Safari の BFCache/サスペンド、
   Android Chrome のバックグラウンドタブ強制切断は iOS Simulator /
   実機 tier での確認が望ましい (watchdog 追加で緩和したが実測は未了)。
2. **実 WebRTC data channel** — DC 経路は pcFactory 注入の fake でのみ
   検証。実 SDP/ICE の switch 中メッセージ抜けはシミュレーション不可。
   dev.sh 環境での両端実機確認を推奨。
3. **`test_boot_resume_waits_for_visibility_ready`** — 同じ held-route
   パターンで 3s の abort window に構造的に依存 (ただし assert 窓は
   ~1.5s と狭い)。ready() を stub しない限り完全決定化は不可。
4. **relay 経路の e2e** — FakeLink レベルでは応答 routing を検証済み。
   実 WS+DC で leader が follower 要求を運ぶ経路の end-to-end 確認は
   実機/dev 環境待ち。
5. **大規模 manifest + 低速リンクの実負荷** — bulk キューで溢れ対策済み
   (PR #141) だが、実回線での再送・順序維持の計測は未実施。

## 既知の設計上の注意 (不具合ではない)

- `OPEN_WORK` は latest-pick-wins で通常の optimistic check をスキップ
  — 仕様通り。競合 pick は後着が勝つ。
- dedup key は `role:requestId` — 直接要求とリレー要求で同一。
- `SET_ACTIVITY selecting` は leader のみ受理 (REASON_NOT_LEADER)。
- blank 側は spinner/error ではなく専用 blank state — 既存実装済み。
- 遅延 swipe finalizer は generation guard で remote commit に敗北
  — 既存実装済み。

## 変更ファイル

- `app/reader_sync/sessions.py` — pending ライフサイクル、resp_link
  routing、stale-APPROVE 照合
- `app/reader_sync/state.py` — derive_phase の pending_load 扱い
- `app/static/viewer/pairClient.js` — liveness watchdog、RESUME/entry
  再送・supersede、_leaveBound、READY マーク、suspended 再 assert
- `tests/` — 回帰テスト 5件追加 + 既存1件更新、fixture deadline、flake 修正

(Medium 項目 M1-M5 はユーザー指示により今回の変更セットから除外)

## コミット

- `8cf43cd` fix(ai): ペアセッションのライフサイクルとリレー応答を硬化
- `75ccef1` fix(ai): pairClient の死活監視・再送・世代ガードを硬化
- `a6a9cc9` fix(ai): pairUI の未処理 promise rejection を解消 (後に scope 外として revert)
- `54fa8d2` test(ai): ペア監査の回帰テスト + テスト基盤 flake 修正
- scope 絞り込みコミット (Medium M1-M5 を変更セットから除外)
