# bookq reader-sync プロトコル仕様（振る舞い契約）

`api/asyncapi.yaml` の companion doc。asyncapi は**ワイヤ形式**（フレームの形）を定義し、
本書は**振る舞い**（ライフサイクル・逐次化・並行性制御・拒否理由）を定義する。
iOS ネイティブが web クライアントと相互運用するために必要な契約はこの2つで完結する。

実装根拠: `rust/viewer-rs/src/reader_sync/{protocol,mod,state,catalog,model}.rs` と
WS bind/drain の `rust/viewer-rs/src/main.rs`（拒否理由文字列・フェーズ・
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

- **frame trust は到着 transport で決まり、data_plane 値は認証境界ではない**。
  server-originated frame（STATE/END/UI_SYNC/TRANSPORT_STATE 等）は常に自分の
  server-ws からのみ正当に到着する。webrtc-dc 上の inbound は peer input であり、
  follower は全て drop する。leader は sequenced-op whitelist（TURN/GOTO/SET_OFFSET/
  SWAP_SIDES/OPEN_WORK/READY/APPLIED）だけを plane に関係なく relay-or-drop し、
  server-originated/control frame を `_dispatch` や server-ws へ注入してはならない

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

### ゲストアクセス（guestToken JOIN）

アカウントを持たない端末向けの QR 経路。`CREATE` ACK の `data.guestToken`
（owner email が解決できるときのみ発行、urlsafe base64 16..=128 文字）が
bearer になる。

```
CREATE ACK{…, guestToken}  →  クライアントが QR
  `/#/join/<code>?gt=<token>` を表示（native・メンバーは code をそのまま解釈）
未ログイン端末: GET / → login page → POST /api/v1/guest/redeem{token}
  → 200 + Set-Cookie: bookq_guest=<token> → viewer shell
  → hash `?gt=<token>` → redeem 再検証 → JOIN{guestToken}
ログイン済み端末: redeem は {"ok":true} のみ（cookie 無し・identity 保持）
→ 同じく JOIN{guestToken} で follower 参加
```

- `JOIN{guestToken}` は `JOIN{code}` と同じ pending→APPROVE 経路に合流する
  （`code_consumed` も共通 — ペアに入れるのは常に1台）。grant は発行時の
  sessionId に束縛されるため、code の `by_code` ルックアップを経由しない
- guestToken は **origin='create' のセッションにのみ発行** — `match` の
  ワンタップ自動ペアは QR を要求しないため、bearer grant を生やさない
- guest 認証済み link は `JOIN{code}` を `forbidden` で拒否 — code 経路は
  member 専用。link 単位の失敗 JOIN スロットルあり（60 秒で 8 回超 →
  transport close 1008）
- grant のライフサイクル: `end_session`（LEAVE/REJECT/期限切れ/end_all/
  shutdown/owner 無効化の全経路）で `revoke_session` — **リーダーがペアを
  解消した時点で guest の閲覧も即失効**。END 送信後に guest link の
  transport も close される。発行から 8 時間の TTL がバックストップ
- guest link の WS は tick 毎に grant を再検証 — ハンドシェイク時点の
  スナップショットではなく、失効すれば socket close（`guest_revoked`）
- owner の member 無効化（`/admin` UI）でその owner が発行した grant を
  `revoke_owner` + リード中のセッションを `owner_disabled` で終了 —
  disable→re-enable で grant が蘇らない
- `bookq_guest` cookie での認証解決は **read-only**: リーダー identity の
  GET 系は全て有効だが、非 GET/HEAD/OPTIONS、`/admin*`、`/api/v1/library/*`
  は HTTP 403（`guest_write_guard` — library GET は副作用を持つため）。
  ペア同期は WebSocket のため影響なし — follower として TURN/GOTO/
  SET_OFFSET 等は通常 follower と同一に受理される。ただし作品選択の drive
  （OPEN_WORK）と shared browse open/close は trust=member のみで、guest は
  direct/relay のどちらでも `forbidden`。UI_SYNC は ephemeral だが closed
  browse で送ると受信側の一覧を開き得るため、**member/guest を問わず**
  shared browse が開いていない間の UI_SYNC は `forbidden` で拒否する
  （member が既に開いた mirror への参加は trust に関係なく拒否しない）
- guest 応答には `X-Bookq-Guest: 1` + `Cache-Control: private, no-store`
  — SW・ページ側キャッシュとブラウザ HTTP キャッシュの双方が失効後に
  再提供しない
- follower の表示名は `ゲスト`（WS の `resolve_ws_user` が供給）。
  member 名は grant ではなく接続 identity から来る
- `POST /api/v1/guest/redeem` 自体は per-IP レート制限（60 秒 30 回）+
  same-site initiation 検査付き。トークンは 192bit で推測不能
- entropy 源（/dev/urandom）不在時は token/code の mint が失敗し
  `internal_error` で reject — 弱化した乱数で bearer を発行しない
- hidden roots はアクセス制御ではなく UI 利便性機能である。server は tree/image を
  hidden roots でフィルタしないが、guest クライアント面に show-all trigger を
  bind しない invariant は regression test で固定する

### メンバー状態と trust

`active | suspended | offline | pending`。`presence` は member id → member info の map。
メンバーが missing/suspended なら phase は `paused` に落ちる。

各 member は admission 時に immutable な `trust` を持つ:

| trust | 固定条件 |
|---|---|
| `member` | member session/Bearer として認証された link で CREATE/JOIN/MATCH した端末 |
| `guest` | 有効な `bookq_guest` bearer として認証された link で JOIN した端末 |

`trust` は presence の各 member entry と CREATE/JOIN/RESUME ACK の `data.trust` に現れる。
role（leader/follower）とは独立で、admission 後に変更されない。ログイン済み member が
`JOIN{guestToken}` を使っても link 認証 class は member のままなので trust は `member` になる。
逆に guest bearer 上の表示名や grant owner email は trust を member に引き上げない。

RESUME は stored member trust と現在の link 認証 class の完全一致を要求する。不一致は
`trust_mismatch` で拒否する。これは同じ link 上で retry しても成功し得ない terminal error であり、
クライアントは resume_in_use のような transient retry をせず、当該 link では session_lost 相当に
扱う（LEAVE tombstone flush でも無限再試行しない）。

---

## 3. capability 広告と effective-actor 認可

### capability 広告

クライアントは CREATE/JOIN/MATCH/RESUME payload に任意で `features`（string array,
最大32件・各64文字）を広告する。未知 token は保持されてよい。server は自身の feature set を
HELLO・admission/resume ACK `data.features`・STATE `features` に載せ、各 member の最新広告を
STATE `presence.<memberId>.features` に反射する。広告は trust ではなく runtime capability なので、
RESUME 毎に現在値へ更新される。

現行の意味を持つ feature:

| feature | 意味 |
|---|---|
| `member-drive-v1` | member trust を持つ follower も OPEN_WORK と shared browse open/close を drive できる。明示 endBrowse close 意味論を理解する |

`member-drive-v1` の intersection が成立する条件は、server deployment flag
`BOOKQ_PAIR_MEMBER_DRIVE`（既定 enabled。`0|false|off|no` で無効）が有効で、paired な
2 member 双方の最新広告が同 feature を含むことである。intersection 自体に trust は含めない
（trust は immutable authority であり capability のように runtime 更新されない）。guest の操作は
intersection 成立後も個別の認可ゲートで拒否する。intersection 非成立の混在ペアでは
新旧混在を壊さないため、legacy 意味論（leader-only OPEN_WORK/shared browse、leader の
非 selecting activity で browse close）を厳密に維持する。offline の間は最後に広告した
capability を保持し、一時切断だけでは downgrade しない。

RESUME は capability を現在 runtime の値で上書きする。intersection が成立していた pair が
RESUME によって intersection を失い、その時点で shared browse が開いていた場合、server は
STATE 配布前に browse を明示的に close する。legacy 意味論では follower close が効かず、
opener によっては leader close 経路も UI 上存在しないため、selecting wedge を許容しない。

### effective actor と relay whitelist

`payload.origin` は sequenced data-plane op（TURN/GOTO/SET_OFFSET/SWAP_SIDES/OPEN_WORK/
READY/APPLIED）にのみ存在できる。webrtc-dc 昇格中、follower の data-plane request は leader が
verbatim に中継し、leader が `payload.origin:"follower"` を付ける。dedup namespace・APPLIED.by・
レスポンス宛先は origin role に紐付く。

認可は常に **origin で解決した member の stored trust** に対して行う。relay した transport
（leader link）の trust で判定してはならない。したがって guest follower の OPEN_WORK は direct でも
member leader 経由 relay でも `forbidden` で拒否される（confused deputy 防止）。

control frame（SET_ACTIVITY/SIGNAL/UI_SYNC/TRANSPORT_STATE/LEAVE/APPROVE/REJECT 等）に非-null
`payload.origin` があれば server は `bad_origin` で拒否する。leader client も data channel から
control frame を relay してはならない。

### OPEN_WORK・shared browse・bootstrap

capability intersection 成立時:

- `member` trust なら leader/follower どちらも OPEN_WORK を受理できる。`guest` は `forbidden`
- shared browse open は `SET_ACTIVITY{state:"selecting"}`、close は non-selecting
  activity（`active` または `suspended`）に載せた直交シグナル `endBrowse:true`
  （例: `SET_ACTIVITY{state:"active", endBrowse:true}`）。`endBrowse` は visibility/liveness
  activity ではなく close gesture の検査対象として独立して解釈され、どの non-selecting
  state でも受理される。open/close とも member なら role に関係なく可。guest は両方 `forbidden`
- 同時/重複 open は server の直列化順で deterministic に解決する。closed→open の先着だけが
  opener/generation を確定し、後着 open は冪等で opener を上書きしない（エラーにもしない）
- opener が offline でも peer member は close できる
- OPEN_WORK manifest commit は shared browse を必ず終了する（明示 close 不要）
- `active`/`suspended` 単体は liveness 更新だけであり shared browse を終了しない。tab visibility
  復帰や mirror sidebar の programmatic close は endBrowse を生成してはならない。endBrowse は
  ユーザー明示 close gesture だけが生成する
- STATE は member-drive intersection 成立中の open に限り `browse:true` を載せる（open の
  true-only encoding。legacy/mixed-pair の open frame をビット一致で変えない）。`paused` は
  `selecting` を mask するため、client の close/reconcile は派生 phase ではなくこの直交状態を使う
- `browse:true` と同じ frame に限り、additive metadata として `browseOpener`（member id）、
  `browseOpenerRole`、`browseGeneration` を送る。`browseGeneration` は session-scoped で、
  初期値は 0、closed→open transition だけが increment する。close・work commit・capability
  downgrade では reset されず、RESUME/reconnect を跨いでも session 終了まで reset しない。
  duplicate open でも増えない。client mirror seed は自分の DOM 状態ではなく
  opener member id で決める。opener だけが初期 seed を送り、非 opener は最初の remote frame を
  適用してから送信 unlock する。generation は open 中だけでなく close 後の STATE でも継続消費し、
  変化した時は opener/unlock/seed/pending mirror state を新 generation 用に再初期化する
- 一度 generation が存在した後の closed STATE では、capability intersection の有無に関係なく
  additive な `browse:false` と `browseGeneration` を送る。これは close を commit した STATE
  だけの one-shot ではなく、offline 中の close を resume 後にも観測できるよう closed の間保持する。
  特に RESUME capability downgrade は close と intersection 喪失を同じ STATE に原子化するため、
  この explicit false がなければ close を観測できない。`browseOpener*` は close 時に省略する。
  未知 field を無視する legacy client には影響せず、mixed pair でも server の browse 真実は同じなので
  additive false を送ってよい。初回 open 前（generation 0）の frame には browse metadata を載せない

intersection 非成立時は旧挙動: OPEN_WORK/selecting open は leader のみ、leader の非 selecting
activity が browse flag をクリアする。

`OPEN_WORK payload.auto:true` は pairing bootstrap の自動初期 load を示す。committed または
in-flight の非 auto load が既にあれば auto load は `stale_request` で拒否され、故意 pick を
上書きしない。逆に auto load の後に届いた非 auto pick は通常どおり latest pick wins で勝つ。

---

## 4. フェーズ機械（derive_phase — 常に導出値、独立した真実ではない）

優先順位は固定（ADR 0004 の導出 cascade）:

1. `waiting` — pair 未成立（承認済み member が 2 人未満。pending は数えない）
2. `paused` — pair のいずれかの member が active でない（suspended/offline/link dead）
3. `loading`（pre-commit）— commit 前の load（pending_load）が in-flight
4. `selecting` — shared browse が開いている、**または manifest が未 commit（初回状態）**
5. `loading`（post-commit READY ゲート）— manifest は commit 済みだが全 member READY 前
6. `reading` — 上記以外（ページ操作が合法な唯一の状態）
7. `ended` — 終了（session 自体は終了理由とともに破棄される）

したがって READY 待ちと shared browse が重なった時は `selecting` が優先される。pre-commit load
だけは `selecting` に優先する。manifest 未 commit の間は browse 開閉に関わらず初回状態として
`selecting` になるが、これは shared browse open を意味しない — open/close の真実は直交状態
`browse` のみが持つ。`paused` は `selecting` を mask するが browse を終了せず、復帰時に mirror が
re-arm する。

TURN/GOTO は `reading` でのみ受理:
`paused`→`peer_offline`、`loading`→`not_ready`、work 無し→`no_work`、その他→`bad_phase`

---

## 5. リクエストの逐次化と並行性制御

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
- **冪等**: `dedup` が `role:requestId` → 応答を記録。再送の重複適用を防ぐ（dedup_seen tombstone 付き）。
  work manifest commit で epoch が進む時、epoch-changing load を起こした request 本人の ACK は新しい
  dedup window へ carry forward され、post-commit resend は再実行ではなく同一 ACK replay になる。
  それ以外の evicted key は tombstone 化し、OPEN_WORK が epoch gate を skip する仕様と組み合わさっても
  古い再送が最新 pick として再実行されないようにする

### REJECT snapshot

sequenced op の rejection は最新 committed snapshot を `snapshot` に載せる。snapshot は STATE から
`v/type` を除いた complete な観測状態で、`presence`（各 member の role/status/trust/features/name/ready）
と server `features` を必ず含む。client は rejection 処理で capability intersection を再評価できる必要があり、
presence/features を欠いた snapshot で前回 STATE を合成してはならない（shared browse close 権限が一時的に
flap する）。`browse` additive metadata も open/closed の現行 encoding で入る。

この fail-closed 要件は `member-drive-v1` を実装・広告する runtime に適用する。member-drive を
実装しない legacy runtime（現行 iOS/Android の PairStore を含む）には pre-existing の presence
合成動作が残っているが、その align は別 issue で行う。legacy runtime はこの合成動作のまま
`member-drive-v1` を広告してはならない。

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

## 6. 拒否理由一覧（ワイヤ安定文字列）

REJECT フレームの `reason` は以下の enum。実装間で文字列一致が契約:

| reason | 意味 |
|---|---|
| `already_bound` | link が別セッションに既バインド |
| `bad_code` | JOIN の code 不一致 |
| `bad_origin` | relay origin が role として不正、または sequenced data-plane 以外の control frame に付与された |
| `bad_phase` | フェーズ不適合の op |
| `bad_resume` | resume_token 不正 |
| `broken_archive` | 書庫破損 |
| `code_consumed` | code 使用済み |
| `code_expired` | code TTL 切れ |
| `empty_work` | 作品にページ無し |
| `forbidden` | 権限なし（guest link の `JOIN{code}` 等、この link 種別では不可の op） |
| `internal_error` | サーバ側失敗（entropy 源不在等）— 弱化した状態で op を実行しない |
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
| `stale_request` | 古いリクエスト再送、または deliberate load 存在下の bootstrap auto OPEN_WORK |
| `stale_revision` | baseRevision 不一致（絶対 op） |
| `timeout` | manifest 解決タイムアウト |
| `trust_mismatch` | RESUME 時の link 認証 class が member admission 時の stored trust と不一致（同 link で retry 不可能な terminal） |
| `type` | 未知の request type |
| `unavailable` | 一時的不可 |
| `wrong_session` | リクエスト先セッション不一致 |

フレームレベルの致命エラー（切断）は別系: `ERR_VERSION`（v 不一致）、
`ERR_MALFORMED`、`ERR_TYPE`、`ERR_RANGE`、`ERR_SIZE`（64KiB/32MiB 上限）。

---

## 7. 非逐次フレーム

| type | 用途 | 規則 |
|---|---|---|
| SIGNAL | WebRTC offer/answer/ice 中継 | シーケンスしない。dc ネゴシエーション専用 |
| UI_SYNC | browse 画面ミラー (scroll progress 等) | ephemeral。reader state 混入禁止。shared browse open 中のみ relay。1s レート窓 |
| PING/PONG | keepalive | シーケンスしない |
| TRANSPORT_STATE | dc up/down 報告 | メンバー dc_state の更新 |

---

## 8. iOS 実装の最小要件

web クライアントと相互運用するために iOS 側が実装すべき範囲:

1. WS 接続（cookieAuth または Bearer）→ HELLO 受領 → Request 送信 → ACK/REJECT/APPLIED/STATE 処理
2. CREATE + JOIN + APPROVE/REJECT/RESUME/LEAVE のライフサイクル
3. TURN/GOTO/OPEN_WORK/READY/SET_ACTIVITY/PING の常用 op
4. derive_phase と同じルールでの UI ゲーティング（paused/loading/selecting で入力抑止）
5. workEpoch/baseRevision のトラッキング（STATE snapshot が唯一の真実）
6. SIGNAL/UI_SYNC/webrtc-dc は**後回し可** — server-ws のみで機能完結
7. manifest パース + digest 検証 + chunk 再構築（§4 MANIFEST の shape 参照）

---

## 9. 変更管理

- `v` フィールドでフレームバージョン管理（現行 1）。新 type/payload 追加は
  後方互換（未知 type は `type` 拒否で安全側）か `v` bump で扱う
- 拒否理由・フェーズ・role/side/phase/signal kind の enum 値は**追加のみ許容**、
  既存文字列の変更・削除は新旧混在を壊すため禁止
