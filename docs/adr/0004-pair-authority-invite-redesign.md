# ADR 0004: ペア読書の権限を信頼軸に剥がし、招待 UX を統一 QR に一本化する

- **ステータス:** 提案中（レッドチームレビュー 2 ラウンド + Devin Review 反映済み v8）
- **日付:** 2026-11-26（v8 改訂: 2026-11-27）
- **決定者:** bookq-maintainers
- **Baseline:** `6af0250`（#652 guest access link・#661 iOS flake hardened 着陸済みの main）
- **関連:** #652 (guest access link) / 実装追跡: #662

## 背景

#652 でゲストアクセスリンク（ログイン不要の follower 参加）が入り、leader 側の招待経路は暗黙の 2 モードになった:

| 経路 | origin | grant | QR | guest が join できるか |
|---|---|---|---|---|
| 「近くの相手とつながる」 | match | mint しない | `#/join/<code>` plain | **できない**（login wall で dead end） |
| 「ゲストを招待」 | create | mint する | composite (`?gt=`) | できる |

問題点: (1) 2 種類の QR は見分けがつかないのに片方は guest に dead end、(2) member が composite QR から join しても WS リンクは cookie で member 分類されるため「member 専用招待」に capability 上の意味がない、(3) leader/follower role が信頼軸と順序軸を癒着させている。なお系譜の正直な記録として: match origin が mint しない根拠は「match は QR を使わない」だったが、**この前提は当初から偽だった** — match 待機画面でも QR は常に表示されており、non-mint 決定は既存 UI と不整合なまま生まれた。

### 前提事実の検証（v3 で修正・確定）

レビューで発覚し origin/main で検証済みの事実:

1. **guest の read はライブラリ全体である** — `main.rs:156` に意図的設計として明記（"grant resolves to the leader's identity for reads"）。`/image` `/thumb` `/view` `/api/v1/tree` 等の全 read 経路で leader 権限に解決され、current work への scope は**実装されていない**。v1/v2 が前提とした「pair スコープへの読取制限」は存在しない
2. **ブラウズミラーは既に双方向**である。leader-only なのは selecting 位相への入場だけ
3. **hidden roots は利便性機能であってアクセス制御ではない**（member についても）— tree/image 配信はフィルタせず、隠蔽はクライアント描画側。ただし guest の画面には既に出ない設計が検証済み: guest には show-all トリガーがバインドされず（`visibility.js:50-53`）、mirrored browse は hidden パスで follower を top にリダイレクトする（`pairSync.js:676-686`）。この invariant を regression test で固定する
4. **revoke は端到端で実際に効く** — leader の LEAVE → `end_session` → `revoke_session` で、HTTP read は毎リクエスト `GuestStore` を再検証するため即座に死に、WS リンクも tick で落ちる
5. 現状の follower の作品 tap は意図的な exit gesture（LEAVE + solo）であり、本 ADR はこの意味を反転させる

## 決定

### A. 招待 UX を chain-tap + 統一 QR に一本化する

プロダクトオーナーの明示要求する操作流れ:

- **member-member**: leader が chain icon を tap → 待機 leader になりモーダルに QR 表示。follower が chain icon を tap → 待機 leader を発見して接続。確立後 leader のモーダルは自動で閉じ、chain icon は接続中表示
- **leader-guest**: leader 側は同一。guest はカメラで QR を読みブラウザで開くだけ

変更:

1. **chain icon tap は `connect()`（MATCH）を直接発火する**（#160 の panel-first を戻す）。#160 の懸念への対処:
   - `DISMISS_GRACE_MS` は backdrop 誤閉じだけを防ぎセッション生成そのものは防がないことを認めた上で、待機画面に目立つキャンセル導線を置く
   - コード保持者は待機画面内の「コードで参加する」リンクを escape hatch とする。#160 系の既存 UI テストは**意図的な仕様変更として書き換え**、変更一覧を実装 PR に明記
   - wrong-match リスクは受容（match window 120 秒・複数待機時は picker・誤接続時の離脱は 1 操作）
   - 誤タップで bearer 入り QR が一瞬表示され得るが、露出窓は小さく（セキュリティ節）、受容する
   - **MATCH による seat 先取りを受容する**: 待機中に誰かが chain tap で MATCH 接続すると follower seat は消費され、QR を見ていた guest は join できない。また自分が招待したい時に別の待機者がいると、chain tap は待機 leader になるのではなくその相手に接続する — 「接続せず自分の招待 QR を出す」経路は設けない。2 台セッションの seat が 1 つである以上これは構造的であり、解決は社会的調整 + 再招待（C 節の再招待更新）に委ねる（Devin Review 指摘の受容判断）
2. **match origin の待機セッションでも grant を mint** — QR は常に composite
3. **3 択パネルの入口を解体** — パネルの他責務（waiting QR・コード入力・カメラ scan・picker・error 表示・focus/inert 管理）は残る

### B. 権限を role ではなく信頼軸から導く

#### B-1. trust をデータ構造として導入する

guest 性は現在 Link の属性であり `Member` に trust はない。relay（`payload.origin` 付き転送）を通した **confused deputy** を防ぐため:

1. **`Member` に admission 時確定の immutable な `trust: "member" | "guest"` を持たせる**
2. **認可は常に effective actor（`payload.origin` で解決した member）の stored trust で判定**。transport link や relay 元 leader の trust で判定しない
3. RESUME 時は member の stored trust と link の認証 class の一致を検証。bind の粒度（同一 grant/session まで詰めるか）は実装時に決め、新しい reject reason のクライアント側意味論（terminal/retry 判定、tombstone flush との相互作用）を protocol doc に明記する

**正当化の訂正（v3）**: この権限境界は read アクセスの境界では**ない**（guest はライブラリ全体を読める — 後述セキュリティ節で受容）。guest の OPEN_WORK/selecting 禁止は **「ゲストに共有読書の運転をさせない」UX・操作制御** として再正当化する。confused deputy を防ぐ意義は read 防御ではなく、この操作制御の bypass 防止である。

あわせて **relay の type confusion を塞ぐ**: leader client は DC 上の任意 frame を verbatim relay するため、control-plane（SET_ACTIVITY/TRANSPORT_STATE/SIGNAL/UI_SYNC）が leader の操作に偽装され得る。leader が relay してよい type を strict whitelist 化し（sequenced data-plane op のみ）、control frame に `payload.origin` があれば server は reject とする。

#### B-2. browse は単一の共有 logical 活動とする（per-member 化はしない）

v2 の per-member 導出モデルは**棄却** — browse mirror は論理的に 1 つの共有活動であり（片方の browse が他方にミラーされる）、refcounting はモデルと矛盾する（mirror auto-open が自分の sel を立てるか否かのどちらを選んでも stuck-selecting か browse 殺しが到達可能）。

正しいモデル: **session スコープの単一 browse**。mirror の実態と一致し、wire への影響も最小である:

- member なら leader/follower に関わらず browse を**開ける**（selecting 位相への入場）。guest は不可
- member なら**どちらでも閉じられる** — これにより stuck state は構造的に存在しない（片方が offline/死亡しても、もう片方が必ず閉じられる）
- OPEN_WORK の commit で browse は終了する（現行の observable behavior を維持）
- 同時に両者が開こうとした場合: server の直列化で先着が効き、後着は冪等（既に selecting）。「2 人が別々に browse する」は mirror 設計と両立しないため**サポートしない** — browse は常に 1 つで、両者にミラーされる

従って派生仕様として以下を protocol doc に明記する:

- **導出 cascade — 優先順位と初回状態を以下に固定する**（protocol doc への先送りではなく本 ADR の仕様とする — Devin Review 指摘）:
  1. `waiting` — pair 未成立（承認済み member が 2 人未満。pending は数えない）
  2. `paused` — pair のいずれかの member が active でない（suspended/offline）
  3. `loading`（pre-commit）— commit 前の load（pending_load）が in-flight
  4. `selecting` — 共有 browse が開いている、**または manifest が未 commit（初回状態）**
  5. `loading`（post-commit READY ゲート）— manifest は commit 済みだが全 member の READY が揃っていない
  6. `reading` — 上記以外

  **優先順位は現行 `derive_phase` と同一に固定する**（Devin Review 指摘 — 共有 browse と READY 待ちが重なった時は selecting が優先）。理由: browse mirror は selecting 位相でのみ動作するため、READY 待ち優先だと作品 commit 直後に browse を開いた場合 mirror が READY 完了まで死ぬ。pre-commit load だけは selecting に優先する（pick 確定中に browse 位相へ落とさない現行意味論の維持）

  初回状態の invariant: **manifest 未 commit の間は browse の有無に関わらず selecting** — leader bootstrap はこの位相で発火する現行仕様を維持する。初回の作品 commit 後は、selecting は共有 browse が開いている時だけになる。paused が selecting に優先するのは現行維持とし、suspend 中は mirror は disarm するが browse 状態自体は生きているため復帰時に re-arm する（閉じるのではなく一時停止 — browse が suspend で終了しない規則と整合）
- offline/resume を跨ぐ挙動: suspend は browse を終了**しない**（activity scalar と browse 状態は直交 — 片方が suspend しても他方は mirror を見続けられる）。resume 時に client は自己状態を reconcile する手順を定める
- close 操作の wire: **終了は visibility 由来の activity 報告と区別される明示シグナルでなければならない**。現行 client はタブ復帰（visibilitychange）でも `SET_ACTIVITY active` を送るため、「active = browse 終了」の意味論では相手が browse 中にタブへ戻っただけで共有 browse が閉じる（v3 の記述はこの意味で壊れていた — Devin Review 指摘）。設計: browse 終了は独立の wire シグナルとし（additive な payload フィールド例: `SET_ACTIVITY{state:"active", endBrowse:true}` または専用 op — 実装時に protocol doc で確定）、**生成はユーザの明示 close ジェスチャ（close ボタン・swipe での明示 close）に限定する**。sidebar の open→closed 遷移を raw に監視してはならない — mirror disarm は suspend 時に sidebar を**プログラム的に閉じ**（pairSync の disarm 経路）、MutationObserver はユーザ操作と区別できないため、遷移ベースの生成では片方の離席で共有 browse が殺される（Devin Review 指摘）。プログラム的 close（disarm・redirect）はシグナルを生成しない。visibility 復帰の active にも終了意味を持たせない。契約テストに入れる: (a) タブ復帰では共有 browse が閉じない、(b) 明示 close では閉じる、(c) **片方が suspend→復帰しても共有 browse は閉じず mirror が re-arm する**。旧 client（leader の active で session flag をクリアする現行サーバ意味論）との整合は B-5 の capability intersection で制御する — 混在ペアでは新意味論を使わない

#### B-3. OPEN_WORK の競合意味論

- latest-pick-wins（現行仕様）を維持。server の mutex が全順序を与えるため収束はする
- **不変式: 自動 bootstrap は故意の pick を決して上書きしない** — leader bootstrap は HTTP fetch 解決の遅延で故意の pick に後着し得るため、OPEN_WORK に `auto: true` を additive に載せ、受理時に「committed/in-flight の非 auto load があれば auto は reject/破棄」と server 側で実現する（client 側再検査では同時 in-flight を塞げない）
- bootstrap の発火は join-order 先着側に限る tie-break を維持 — role は権限を持たないが対称性を破る番人としては残る
- UI_SYNC（ephemeral mirror）は順序保証なしの現行仕様のまま

#### B-4. role の終状態を一文で固定する

> **role は join 順の位置（先着/後着）のみを示し、座席・relay hub・WebRTC initiator・dedup 名前空間・tie-break を決める。権限は決して role から導かない — 権限の唯一の源泉は trust class（member/guest）である。**

脚注: relay hub には availability 上の位置権力が残る（leader は follower 名義の op を捏造・検閲し得る）。対称 capability の下で逸脱利益はほぼないため受容するが、認識はしておく。

ubiquitous language: 位置 = leader/follower（wire frozen、`leaderSide` 等の綴りも位置語彙として固定）、信頼 = member/guest、セッションの開始者 = 「開始者」。**「owner」は禁止語**。

#### B-5. capability negotiation と混在互換

Hyrum's law 観点の構造的修正: v2 の `features` は server→client 単方向で、**peer capability を知る手段がなかった**。これでは follower の browse 入場を旧 client ペアから gate できず、最悪セル（旧 native leader × 新 follower）では leader の reading が全画面 selecting overlay（誤コピー付き）に置換される。

1. **client→server の feature 広告**を追加（CREATE/JOIN/MATCH/RESUME payload に同梱）し、server は presence/STATE で **peer capability を反射**する（または intersection を広告）。member-drive 操作は pair の capability intersection で gate する
2. server 側も `features` を HELLO/ACK/STATE に載せ、capability 非対応時は新旧ともに旧挙動へ fallback。deploy flag で新機能を無効化可能に
3. `PROTOCOL_VERSION` は v1 維持 — 4 ランタイム全てが `v != 1` を即時終了するため bump は最悪手。unknown field/type は全ランタイムで吸収されることを検証済み
4. wire encoding: browse は session スコープのままなので phase encoding の変更は最小。member `status` enum（active/suspended/offline/pending）は**固定**し、意味変更をしない
5. 移行行列の正直な記述（実装 PR で各セルを検証）:

| server | client | 期待動作 |
|---|---|---|
| old | new web | capability 未広告 → member follower の駆動 UI は出さない |
| new | old web SW shell | capability intersection で駆動が gate される。QR 経路は shell 世代 × 開き方で分かれる: **in-app scan**（pairUI の unanchored 抽出）は全世代で composite から code を取れる。**外部カメラ→ブラウザ直開き**は、pre-#652 shell（bare 用 anchored regex `/^#\/join\/(\d{6})$/` のみ）では composite に一致せず **dead end — 退行として正直に記載**（post-#652 shell は `_handleGuestJoin` が先に処理するため機能）。手入力は全世代で生きる |
| new | old iOS/Android | 同上 + native leader QR は bare のまま（表示側更新は Phase 2）。scan は composite から code 抽出可能 |
| new | mixed pair | intersection gate により旧 client 側で reading が中断される事態を防ぐ |

### C. セキュリティ評価（v3 — プロダクト判断を反映）

**スコープの正直な前提**: guest の read はライブラリ全体であり、これを**受容する**（プロダクトオーナー判断）。hidden roots は利便性機能であり、guest の画面に出ないことは現行設計で達成済み（背景 3）— regression test で固定する。

**脅威モデル**: 本番は tailscale + cloudflared の公開経路を持ち得る。composite QR の写真・画面共有漏洩で、member アカウントを持たない第三者が follower seat を取り得る。

**露出の正直な分析**:

- 未ペアの待機セッションは code TTL（300 秒）で殺されるため、**bearer を redeem できる窓は ≤300 秒 × single-use**。ただしこれは redeem 窓であって、**侵入後のアクセス期間ではない** — pair 成立後はセッション寿命（grant TTL 最大 8h）までアクセスが続く
- 攻撃者の利得: auto-approve により pair 成立済みメンバーとして振る舞い、ライブラリ全体の read（Q1-b 受容済みのため「招待 guest と同等」のアクセス）+ LEAVE によるセッション破壊
- **限界利得の評価**: 招待 guest 本人がライブラリ全体を読める設計を受容した以上、漏洩 attacker の限界利得は「招待 guest と同等のアクセス × 検知までの時間」に過ぎない。この意味で QR 漏洩リスクはプロダクトオーナー判断により**受容**する

**検知と対応を実効化する**（v3 で scope に追加 — 現行は theater）:

- 現行の presence には peer の identity/trust 表示がなく、modal は成立時に自動で閉じる。**peer の trust class（member/guest）を presence に常時バッジ表示する**
- **member-scoped KICK を新設**: leader が不審な peer を切断できる操作。KICK は grant 失効 + WS close + RTCPeerConnection close まで行い、session 全体は殺さない
- **検知の実際の経路**: 正当 guest が `code_consumed` で失敗して leader に伝わる社会的検知が主経路。バッジは「まだ入ってないのにペア成立してる」への気付きを補強する
- revoke は端到端で効くことを検証済み（背景 4）— ただし**ダウンロード済みのコンテンツは取り返せない**ため、検知までの時間が実害を決める

**availability 修正を scope に入れる**（attacker set が拡大する以上、既知 issue でも本 ADR の受容条件とする）:

- REJECT/KICK は member-scoped にし session を殺さない。rejected/dropped candidate は code を解放する（`drop_pending` の `code_consumed` リセットで部分実装済み）
- follower/guest の LEAVE が session 全体を殺す現行仕様は、少なくとも leader が再招待可能な状態にする（完全な member-scoped teardown は別 issue でもよいが、方向を明記）
- **再招待の更新意味論を定義する**（Devin Review 指摘 — v3 は未定義だった）: 現行の code TTL はセッション作成時に固定で、未ペアに戻った瞬間に期限切れなら tick が session ごと殺す。つまり成立から数分後に KICK/LEAVE でコードを解放しても再招待は成立しない。設計: paired member の脱落（KICK/LEAVE/REJECT）でセッションが 1 人の待機に戻った時、server は **新しい code + 新しい TTL + 新しい grant を rotate/mint** し、leader の client は新しい composite QR を表示する。期限後の再招待テストを Phase 0 の行列に含める
  - **待機タイマは全て refresh する**（Devin Review 指摘）: `matchable` の候補判定は code 期限とは別に `created_at` からの経過（match window 120s）を見るため、code だけ rotate しても member 自動接続は復活しない。rotation は waiting 状態への再突入として扱い、match window の anchor も更新する — これで KICK された member の再ペア（chain-tap MATCH）と guest 再招待の両方が機能する。**ただし origin 意味論は不変**（Devin Review 指摘）: `matchable` は match-origin のみを候補にする現行を維持し、create-origin（旧 client の手動開始）は rotation 後も QR/code 参加のみで自動接続の対象にしない — 旧 client の意味論を変えない保守的な選択。新 UX では chain-tap が全て match-origin で発行するため、end state ではこの区別は実質消滅する
  - **新しい参加者への作品引き継ぎ**（Devin Review 指摘）: 現行の承認は STATE を配信するが commit 済み manifest 本体は配信しない — late-join が存在しなかった（LEAVE でセッションが死んだ）ため再配布経路がない。再招待設計ではこれが初めて発生する。仕様: **作品は引き継ぐ**（選択やり直しではない）。server は commit 済み manifest を保持または再解決し、新規承認 member に MANIFEST chunk 群を再配布する。同期は READY ゲートに自然に乗る — 新 member は未 READY なので phase は loading に落ち、manifest 受信・検証・適用で READY して reading に復帰する。**READY の意味論を明記する**（Devin Review 指摘）: 現行 web は manifest のメタデータ検証直後（deferred STATE 適用・描画より前）に READY を送る — READY は「検証済み manifest のメタデータを保持した」時点で送信され、**deferred STATE の適用は送信条件に含まない**（実装は `_finishManifest` で manifest 保持直後に `_sendReady()` を呼び、保留 STATE の適用はその後 — Devin Review 指摘を受け送信条件をこの意味に確定する）。もちろん**描画完了も保証しない**。画像は progressive に load されるストリーミング配信なので描画完了は定義不能であり、この意味論を再利用する。KICK で mid-reading 中断した leader の意図（同じ本の続きを正しい相手と読む）と整合する

**credential invariant**（実装済みのものも設計として固定）:

1. invite secret は CSPRNG 生成の少なくとも 128 bit（現行 192 bit）
2. **single-use が効くのは redeem ではなく seat（bind）である** — v3 の「初回 redeem で server-side consume」は現行実装ともフローとも矛盾する誤り（Devin Review 指摘）。現行の guest 参加フローは login.js と hash.js で **二度 redeem する**（login shell で cookie を得て reload → fragment が生き残り viewer 側で再度 redeem してから join）ため、redeem で consume すると通常フロー自体が壊れる。正しい invariant: `gt` は失効まで何度でも redeem できる bearer であり、消費されるのは follower seat（bind 時の `code_consumed`、drop_pending で解放済み）。二度目の redeem を不要にする画面遷移の整理（cookie 済みなら skip）は Phase 2 の polish として扱う
3. invite/credential は session id に bind し、session end / owner disable / expiry / KICK で atomic に失効
4. token 値は log/analytics/pair-report/persist record に出さない
5. QR host と public host の関係: token が host-independent なら tailnet QR の写真でも cloudflared host に付け替えて使えることを脅威モデルに明記
6. redeem 後の authenticator の分離（現行は cookie = grant 本体）は将来の hardening として記録 — per-request 再検証があるため revoke 上の実害はない

### D. APPROVE/REJECT の扱い

wire 上は保持し、member-member では auto-approve を既定のままとする（プロダクトオーナー判断 — 操作数最小化の UX 要求）。guest join も auto-approve のままとし、検知は trust バッジ + KICK + 社会的検知で担保する（C 節）。承認権限の根拠は role ではなく「session 開始者であること」に明示的に移し替える。leader 承認モードは将来の hardening 選択肢として記録。

## 検討した代替案

| # | 案 | 棄却理由 |
|---|---|---|
| A | 現状維持 + コピー明示 | QR trap の構造が残り操作数も減らない |
| B | 役割のネゴシエーション | ネゴシエートすべき選好が存在しない |
| C | role 完全廃止 | 位置関数（座席・relay・initiator・tie-break）は join 順を必要とする |
| D | 遅延 mint / mint-on-display | UX 要求（全待機で QR 表示）の下では always-mint と実効的に等価 |
| E | member-only 招待を first-class UI で残す | capability 上の意味がない |
| F | guest join の leader 承認を既定に | UX 要求と衝突。バッジ + KICK + 社会的検知で代替（将来の hardening として記録） |
| G | guest read の current work スコープ（deny-by-default whitelist + manifest scoped media） | プロダクトオーナー判断で受容 — 招待 guest はライブラリを読んでよい。hidden roots は別途保護済み |
| H | per-member selecting（v2 モデル） | mirror が論理的に単一の共有活動であるためモデルと矛盾。stuck-selecting が到達可能（Lamport 指摘） |
| I | guest への hidden roots server-side enforcement | 利便性機能のままでよいという判断。guest 画面に出ない invariant は既存実装で達成済み |

## 影響

### 良い影響

- member 2 台でどちらの端末からでも作品選択・ページ操作ができ、「誰が leader か」を意識する必要が消える
- 操作数の削減と QR 2 モード trap の構造的解消
- browse は単一の共有活動として直感に合う（mirror されているものをどちらでも閉じられる）

### 悪い影響・コスト（正直な記述）

- bearer token が常に mint される。攻撃者の限界利得は guest 同等アクセス × 検知時間（C 節で受容）
- **follower の作品 tap の意味が反転する**: 今日は LEAVE+solo の退出 gesture、今後は共有作品の変更。退出は明示の解除操作のみ
- REJECT/KICK/LEAVE の member-scoped 化は session ライフサイクルの意味論変更を伴い、既存テストの逆仕様固定を書き換える必要がある
- 新旧混在期間は capability 差による機能の非対称がある（intersection gate で破壊は防ぐ）

### 影響なし・範囲外

- side 割当・SWAP・ページ同期・UI_SYNC の順序仕様は無変更
- hidden roots の member 向け挙動は無変更（利便性機能のまま）
- any-member-offline sweep 等の既知 issue は方向のみ明記、完全修正は別 issue 可

## 実装フェーズ

**どの中間状態でも member-member の QR/MATCH join が今日と同等以上に動く**ことを保証する。変更する既存テストの一覧は各実装 PR に明記（壊れたから消す、は禁止）。

- **Phase 0（契約テスト先行）**: golden cases は `not_leader` negative case を持たないため green は何も証明しない点に注意。少なくとも:
  - member follower OPEN_WORK accept（direct / relay 両方）、guest reject（**direct / member leader 経由 relay 両方** — confused deputy 回帰）
  - relay type whitelist（control frame の origin 付きは reject）
  - 共有 browse の状態機械行列: member どちらが開いても閉じられる / opener が offline でも peer が閉じられる / commit で終了 / suspend では終了しない / 同時 open は冪等 / bootstrap auto は故意 pick を上書きない
  - resume の trust 一致、hidden roots が guest 画面に出ない regression
- **Phase 1（trust モデル + 権限解放 + 共有 browse）**: `Member.trust`、effective-actor 認可、relay whitelist、共有 browse（開放・close 意味論）、bootstrap `auto` flag、capability 広告（双方向 + peer 反射）+ intersection gate。exit: 上記 green + capability 非対応で旧挙動 fallback
- **Phase 2（UX 統一）**: match origin mint、chain-tap connect、パネル入口解体、常時 composite QR。**member の composite QR code-join フォールバック（grant 切れでも code で join）を含める**（これがないと中間状態で member の QR join が退行）。native の表示側（ACK の guestToken を保持し composite QR を描画）をこのフェーズに明記。presence trust バッジ + member-scoped KICK/REJECT + LEAVE 再招待可能化 + 再招待 rotate（code/TTL/grant）もここ。**RESUME 応答に live grant token を含める** — 別タブへの引き継ぎでは token が復元されず（#652 で cross-tab persist に入れない判断済み）、引き継いだタブの待機画面は code-only QR になって 2 モード trap が再発するため、server が session の live grant を RESUME ACK で返し composite QR を再描画できるようにする（完了条件に含める — Devin Review 指摘）
- **Phase 3（コピー掃除のみ）**: UI コピーの leader 語整理（「開始者」への置換）

## レッドチームレビューの記録

2 ラウンドのレビュー経緯:

**Round 1（t-wada/Fowler/Linus 人格）**: v1 は 3 者とも No-Go/fix-first。収束指摘: trust のデータ構造不在 + relay confused deputy、selecting boolean の表現力不足、「漸進的に弱める」「last-wins」の非仕様性、移行行列不在、セキュリティ評価の不正直、follower pick 意味反転の未記載、browse mirror 双方向の事実誤認、#160 根拠不足。

**Round 2（Lamport/Schneier/Hyrum Wright 人格）**: v2 は 3 者とも fix-first。主要な発見と対応:

1. **guest read は current work にスコープされていない**（Lamport・Schneier 独立に発見、origin/main で検証確定）→ v3 で前提を修正し Q1-b 受容をプロダクト判断として明記
2. **per-member selecting は mirror 設計と矛盾**（Lamport）→ 単一の共有 logical browse に再設計（Q2-a）
3. **≤300s は redeem 窓であってアクセス期間ではない**（Schneier）→ C 節で区別を明記
4. **presence の検知は theater**（Schneier）→ trust バッジ + member-scoped KICK をスコープに追加（Q3-b）
5. **features は server→client 単方向で peer fallback が実装不可能**（Hyrum）→ client→server 広告 + peer 反射 + intersection gate を B-5 に追加
6. **pre-#652 stale shell で composite deep link が dead end**（Hyrum）→ 移行行列に正直に記載
7. bootstrap が故意の pick を上書きする経路（Lamport）→ `auto` flag の不変式
8. relay type confusion（Schneier）→ relay whitelist

**Round 3（Devin Review bot — PR #663 inline）**: v3 に対する指摘と対応:

1. **タブ復帰で共有 browse が閉じる** — v3 の「SET_ACTIVITY active = close」は visibility 復帰の active と区別がつかず壊れていた → B-2 の close wire を明示シグナルに再設計
2. **redeem の二重実行** — v3 の invariant「初回 redeem で consume」は現行フロー（login.js → reload → hash.js の二度 redeem）と矛盾 → invariant を「single-use は seat の bind」に修正
3. **招待期限後の再招待が未定義** — code TTL が作成時固定で、KICK 後に解放しても session ごと死ぬ → 再招待時の code/TTL/grant rotate を定義
4. **RESUME で composite QR を復元できない** — token が cross-tab persist に入らない設計のため、RESUME ACK に live grant を含めることを Phase 2 完了条件に追加
5. **MATCH の seat 先取り・待機者への接続で招待 QR が出ない** — 2 台セッションの構造的制約として受容、社会的調整 + 再招待に委ねる旨を明記

