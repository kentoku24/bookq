# bookq reader-sync プロトコル仕様（振る舞い契約）

`api/asyncapi.yaml` の companion doc。asyncapi は**ワイヤ形式**（フレームの形）を定義し、
本書は**振る舞い**（ライフサイクル・逐次化・並行性制御・拒否理由）を定義する。
iOS ネイティブが web クライアントと相互運用するために必要な契約はこの2つで完結する。

実装根拠: `app/reader_sync/{protocol,sessions,state,transport}.py`（拒否理由文字列・フェーズ・
列挙値はワイヤ安定 = 変更すると新旧クライアント混在で壊れる）。

---

## 1. トランスポートと面の分離

| 面 | 経路 | 役割 |
|---|---|---|
| シグナリング/制御 | `GET /ws/reader-sync`（server-ws） | 常設。全リクエスト・STATE・SIGNAL 中継 |
| データプレーン | `server-ws` or `webrtc-dc` | 昇格時のみ dc。同一フレーム形を運ぶ P2P |

- `data_plane` はセッション状態の一部。`webrtc-dc` は dc 昇格可能な場合にのみ遷移
- **画像/バイナリはデータプレーンを通らない** — 必ず `/image/*` HTTP 経由
- 逐次化権限（sequencer）は論理ロール: server-ws では**サーバが受信順**、
  webrtc-dc では **leader が逐次化**（同tick到着は role-deterministic タイブレーク）。
  どちらでも `revision` は厳密単調増加

---

## 2. セッションライフサイクル

```
CREATE ──► ACK{sessionId, code, expiresInSec}        code は6桁・TTL付き・使い捨て
              │ (origin='create': code 保持者専用)
              ▼
JOIN{code} ──► pending member                        有効性: code 一致・未消費・未失効
              │
       leader が APPROVE / REJECT                    pending のみ承認対象
              │
              ▼                                        paired = 2 members && no pending
            paired
              │ RESUME (resume_token)                  link 切れ → 同一 Member を再接続
              │                                        (take-over 経路なし: 他人の再占有不可)
              ▼
            LEAVE / END                                メンバー離脱 / セッション終了

MATCH (別経路): origin='match' のオープン待ち受け。payload.pick の token で
マッチング。コード共有ペアとは別系（drive-by join を防ぐため 'create' には join 不可）
```

### メンバー状態
`active | suspended | offline | pending`。`presence` は link id → member info の map。
メンバーが missing/suspended なら phase は `paused` に落ちる。

---

## 3. フェーズ機械（derive_phase — 常に導出値、独立した真実ではない）

```
waiting   pair 未成立（leader 単独 or join 承認待ち）
selecting paired だが work 未コミット、または leader が作品リスト閲覧中
loading   manifest コミット済み/in-flight だが全員 READY 前
reading   全メンバー READY（ページ操作が合法な唯一の状態）
paused    メンバー欠落（link 死）or 非 active
ended     終了
```

TURN/GOTO は `reading` でのみ受理:
`paused`→`peer_offline`、`loading`→`not_ready`、work 無し→`no_work`、その他→`bad_phase`

---

## 4. リクエストの逐次化と並行性制御

クライアント→サーバは統一リクエストフレーム
`{v:1, requestId, sessionId, workEpoch, baseRevision, type, payload}`。

| op 分類 | 該当 type | 楽観的検査 |
|---|---|---|
| 相対 op | TURN | **baseRevision 不検査**。sequencer 受信順に適用。chapter 限定時は `fromChapter` が現行と一致必須（不一致 `stale_chapter`） |
| 絶対 op | GOTO / SET_OFFSET / SWAP_SIDES | `baseRevision == session.revision` 必須（不一致 `stale_revision` + 最新 snapshot 返却） |
| work pick | OPEN_WORK | **楽観検査スキップ**。pick は自己完結（folder+page）。"latest pick wins"。pending_load generation guard が遅延ロードを棄却 |
| その他 | READY / APPLIED / SET_ACTIVITY / MATCH / JOIN / CREATE / RESUME / LEAVE | 各種型・範囲・フェーズ検査 |

共通ガード:
- `workEpoch` 不一致 → `stale_epoch`（work 切替時に epoch が進み、旧 work に向けた op を無効化）
- `payload.manifestVersion` が指定され現行と不一致 → `stale_manifest`
- **拒否されたリクエストはコミット済み snapshot を一切変更しない**（validate → mutate の順）
- **冪等**: `dedup` が `role:requestId` → 応答を記録。再送の重複適用を防ぐ（dedup_seen tombstone 付き）

### READY ゲート
manifest コミット後、全メンバーが READY するまで `loading`。
全員 READY で `reading` へ。

### MANIFEST
`OPEN_WORK` 受理 → manifest 解決（非同期・load_seq generation guard）
→ `MANIFEST` chunk フレーム群 `{kind:"manifest-chunk", digest, index, count, data}` + `type:MANIFEST, sessionId, workEpoch, manifestVersion` 配布 → コミットは続く `STATE` フレームが担う（`MANIFEST_COMMIT` は wire メッセージではなくサーバ内部の apply-log 種別）。
解決失敗は manifest error → 拒否理由にマップ（下表）。

manifest 本体の shape:
```json
{"v":1, "folder":"<work root>", "offset":0,
 "chapters":[{"name":"ch1","folder":"<chapter dir>","pages":[{"folder":"...","name":"p001.jpg"}]}],
 "pageRefs":[{"folder":"...","name":"..."}],
 "spreadTable":[{...serialized spread...}],
 "digest":"sha256:<canonical hash>"}
```
- `digest` = コンテンツキー (`v,folder,offset,chapters,pageRefs,spreadTable`) の
  canonical JSON (sorted keys, ASCII) の sha256 — 両端の byte 一致検証
- 転送は chunk 分割: `{v,kind:"manifest-chunk",digest,index,count,data}` を
  index 順に結合 → 再構築後に digest 検証（改竄・欠落は typed error）

---

## 5. 拒否理由一覧（ワイヤ安定文字列）

REJECT フレームの `reason` は以下の enum。実装間で文字列一致が契約:

| reason | 意味 |
|---|---|
| `already_bound` | link が別セッションに既バインド |
| `bad_code` | JOIN の code 不一致 |
| `bad_origin` | 'create' セッションへの不正経路 join |
| `bad_phase` | フェーズ不適合の op |
| `bad_resume` | resume_token 不正 |
| `broken_archive` | 書庫破損 |
| `code_consumed` | code 使用済み |
| `code_expired` | code TTL 切れ |
| `empty_work` | 作品にページ無し |
| `forbidden` | 権限なし |
| `malformed` | フレーム/ペイロード検査失敗 |
| `no_initial_page` | 初期ページ解決不可 |
| `no_pending` | pending member 不在で APPROVE/REJECT |
| `no_session` | sessionId 不整合 |
| `not_found` | manifest 対象が無い |
| `not_leader` | leader 限定 op を非 leader が送信 |
| `not_paired` | 未ペアで paired 必須 op |
| `not_ready` | READY ゲート未通過 |
| `no_work` | work 未コミットでページ op |
| `page_not_found` | manifest ページ参照失敗 |
| `peer_offline` | ピア欠落中（paused） |
| `range` | 値域外 |
| `resume_expired` | resume 有効期限切れ |
| `resume_in_use` | resume_token 多重使用 |
| `size` | サイズ上限 |
| `stale_chapter` | fromChapter 不一致の chapter 限定 TURN |
| `stale_epoch` | workEpoch 不一致 |
| `stale_manifest` | manifestVersion 不一致 |
| `stale_request` | 古いリクエスト再送 |
| `stale_revision` | baseRevision 不一致（絶対 op） |
| `timeout` | manifest 解決タイムアウト |
| `type` | 未知の request type |
| `unavailable` | 一時的不可 |
| `wrong_session` | リクエスト先セッション不一致 |

フレームレベルの致命エラー（切断）は別系: `ERR_VERSION`（v 不一致）、
`ERR_MALFORMED`、`ERR_TYPE`、`ERR_RANGE`、`ERR_SIZE`（64KiB/32MiB 上限）。

---

## 6. 非逐次フレーム

| type | 用途 | 規則 |
|---|---|---|
| SIGNAL | WebRTC offer/answer/ice 中継 | シーケンスしない。dc ネゴシエーション専用 |
| UI_SYNC | browse 画面ミラー (scroll progress 等) | ephemeral。reader state 混入禁止。1s レート窓でリレー |
| PING/PONG | keepalive | シーケンスしない |
| TRANSPORT_STATE | dc up/down 報告 | メンバー dc_state の更新 |

---

## 7. iOS 実装の最小要件

web クライアントと相互運用するために iOS 側が実装すべき範囲:

1. WS 接続（cookieAuth または Bearer）→ HELLO 受領 → Request 送信 → ACK/REJECT/APPLIED/STATE 処理
2. CREATE + JOIN + APPROVE/REJECT/RESUME/LEAVE のライフサイクル
3. TURN/GOTO/OPEN_WORK/READY/SET_ACTIVITY/PING の常用 op
4. derive_phase と同じルールでの UI ゲーティング（paused/loading/selecting で入力抑止）
5. workEpoch/baseRevision のトラッキング（STATE snapshot が唯一の真実）
6. SIGNAL/UI_SYNC/webrtc-dc は**後回し可** — server-ws のみで機能完結
7. manifest パース + digest 検証 + chunk 再構築（§4 MANIFEST の shape 参照）

---

## 8. 変更管理

- `v` フィールドでフレームバージョン管理（現行 1）。新 type/payload 追加は
  後方互換（未知 type は `type` 拒否で安全側）か `v` bump で扱う
- 拒否理由・フェーズ・role/side/phase/signal kind の enum 値は**追加のみ許容**、
  既存文字列の変更・削除は新旧混在を壊すため禁止
