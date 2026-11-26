# ADR 0004: ペア読書の権限を信頼軸に剥がし、招待 UX を統一 QR に一本化する

- **ステータス:** 提案中（レッドチームレビュー反映済み v2）
- **日付:** 2026-11-26（v2 改訂: 同日）
- **決定者:** bookq-maintainers
- **Baseline:** `6af0250`（#652 guest access link・#661 iOS flake hardened 着陸済みの main）
- **関連:** #652 (guest access link) / 実装追跡: #662

## 背景

#652 でゲストアクセスリンク（ログイン不要の follower 参加）が入り、leader 側の招待経路は暗黙の 2 モードになった:

| 経路 | origin | grant | QR | guest が join できるか |
|---|---|---|---|---|
| 「近くの相手とつながる」 | match | mint しない | `#/join/<code>` plain | **できない**（login wall で dead end） |
| 「ゲストを招待」 | create | mint する | composite (`?gt=`) | できる |

この構造には 3 つの問題がある。

**1. QR の見分けがつかないのに挙動が違う。** 2 種類の QR は視覚的に同一だが、片方は未ログイン端末にとって dead end（login wall に落ち、理由も分からない）。leader は自分の QR が guest に効くかどうかを区別できない。なお系譜の正直な記録として: match origin が mint しない根拠は「match は QR を使わない」だったが、**この前提は当初から偽だった** — match 待機画面でも QR は常に表示されており、non-mint 決定は既存 UI と不整合なまま生まれた。それが QR trap を生んだ。

**2. 「member 専用招待」に capability 上の意味がない。** member が composite QR から join しても、WS リンクは handshake cookie で member 分類されるため full capability が得られる。join 後の capability に member join / guest join の差は一切ない。

**3. leader/follower role が 2 つの軸を癒着させている。** role は実際には複数の独立した関心事を束ねている:

| # | 関心事 | 現状 |
|---|---|---|
| 1 | side 割当（左右どちらを描画するか） | role から導出（leader は leaderSide、follower は逆） |
| 2 | 作品選択権（OPEN_WORK・selecting 位相への入場） | leader-only（`not_leader` で reject） |
| 3 | 参加承認（APPROVE/REJECT） | leader-only だが auto-approve のため実質 vestigial |
| 4 | relay hub・WebRTC initiator・dedup 名前空間・応答ルーティング | role ベースの恒久的な位置関数 |
| 5 | ページめくり | 対称（意図的） |

なお現状認識の訂正: **ブラウズミラーは既に双方向**である（`pairSync.js` — スクロール・フォルダ潜り・戻る・表示モード変更は双方向で伝搬し、UI_SYNC 中継もサーバで対称）。leader-only なのはミラーではなく **selecting 位相への入場**だけである。v1 の表はこの点で事実誤認していた。

member-member ペアにおいて 2 は純粋な摩擦である（2 台並べで follower 端末から作品変更できない）。一方 leader-guest ペアでは 2 は**セキュリティ境界**である: guest の画像アクセスはセッションの current work にスコープされているため、guest が OPEN_WORK できると pair チャネル経由でライブラリ全体の読取権を得ることになる。つまり権限剥奪が必要なのは「後から来た人」ではなく「guest」であり、role は信頼軸（member vs guest）と順序軸（先着/後着）を混ぜた誤った抽象になっている。

また、現状の follower の作品 tap は**意図的な exit gesture**である（`pairController.js` — "spec: follower work change = LEAVE + solo"、ペアを離れて solo で読む）。本 ADR の変更はこの意味を反転させる（後述「影響」に明記）。

## 決定

### A. 招待 UX を chain-tap + 統一 QR に一本化する

目標とする操作流れ（プロダクトオーナーの明示要求）:

- **member-member**: leader が chain icon を tap → 待機 leader になりモーダルに QR 表示。follower が chain icon を tap → 待機 leader を発見して接続。確立後 leader のモーダルは自動で閉じ（既存挙動）、chain icon は接続中表示
- **leader-guest**: leader 側は同一。guest はカメラで QR を読みブラウザで開くだけ

実現のための変更:

1. **chain icon tap は `connect()`（MATCH）を直接発火する**（#160 で導入した panel-first を戻す）。これはプロダクトオーナーの明示的な UX 要求であり、レビューでの慎重論（後述）を踏まえた上で**リスク受容の判断として採用**する。#160 の懸念への対処を個別に明記する:
   - 誤二重タップ → 既存の `DISMISS_GRACE_MS` は backdrop 誤閉じだけを防ぐものであり、**セッション生成そのものは防がない**。この事実を認めた上で、待機画面に目立つキャンセル導線を置く
   - コード保持者が waiting leader にされてしまう問題 → 待機画面内の「コードで参加する」リンクを escape hatch として残す。ただしこれは #160 以前のフローへの部分回帰であり、`#160` 系の既存 UI テスト（launcher tap で connect しない、idle から直接コード参加できる）は**意図的な仕様変更として書き換える**。変更するテストの一覧を実装 PR に明記すること
   - wrong-match（意図しない待機相手への接続）→ match window は 120 秒で待機 session は限られる。複数待機時は従来通り picker を出す。auto-approve との組合せで誤接続が即確定し得る点は受容し、誤接続時の離脱コストが小さい（LEAVE 1 操作）ことを根拠とする
   - セッション churn（コード参加目的で待機セッションを生成→即破棄）→ Phase 2 で常時 mint になるため、誤タップ 1 回で bearer 入り QR が一瞬表示され得る。露出窓は小さい（後述セキュリティ評価）が、この tradeoff は明示的に受容する
2. **match origin の待機セッションでも grant を mint する** — QR は常に composite (`#/join/<code>?gt=<token>`)。元の non-mint 根拠（「match は QR を使わない」）は最初から偽だった（待機画面に QR は常に表示されている）ため、不整合を解消する方向で統一する
3. **「開始/ゲストを招待/参加」の 3 択パネルを解体する** — chain tap で待機か接続かが自動決定されるため選択の必要がなく、「ゲストを招待」ボタンは全待機が composite QR を持つことで存在意義が消える。ただしパネルには選択肢以外の責務（waiting QR・コード入力・カメラ scan・picker・error 表示・focus/inert 管理）が多数残ることに注意 — 解体は入口の選択肢の除去であって、パネル全体の削除ではない

### B. 権限を role ではなく信頼軸（member vs guest）から導く

#### B-1. trust をデータ構造として導入する（チェック置換ではない）

v1 の「`not_leader` チェックを guest チェックに置き換える」は不正確だった。現状 guest 性は **Link の属性**（`link.guest_token`）であり `Member` には trust 情報がない。relay（`payload.origin` 付き転送）を考えると、これは単なる置換では済まない:

**confused deputy シナリオ**: guest follower が WebRTC DC に `OPEN_WORK` を送る → member leader が `payload.origin="follower"` 付きで server に relay する → server が transport link（member leader）の trust で判定すると**通ってしまう**。これは本 ADR が防ごうとしているライブラリ任意 read そのものである。

したがって:

1. **`Member` に admission 時に確定する immutable な `trust: "member" | "guest"` を持たせる**（join/resume 時の link 分類から確定し、以後変えない）
2. **認可は常に effective actor（`payload.origin` で解決した member）の stored trust に対して行う**。transport link の認証状態や relay 元 leader の trust で判定してはならない
3. RESUME 時は member の stored trust と link の認証が一致することを検証する（guest bearer で member trust の member に resume する、または逆は拒否）
4. trust を STATE/presence で公開するかは実装時に決める（UI での表示価値はあるが、最小実装では server 内部のみでもよい）

#### B-2. `selecting` を session 大域 boolean から per-member 状態に再設計する

現状 `selecting` は session 単位の boolean で、セットは leader のみ・クリアも leader の active/suspended のみ、という leader 所有フラグである。**このまま follower の selecting 入場を解放すると、follower は browse を開けるが誰も閉じられない、または片方の close が他方の browse を殺す** — 単一 boolean では 2 人の独立した browse 状態を表現できない（refcounting の問題）。

決定: **`paused` と同じ per-member 導出モデルを採用する**。activity は member ごとに記録し、session phase は導出値とする:

- `phase == selecting` iff **いずれかの active member が selecting**
- member が active/suspended に戻るのは自分の分だけ。他方の browse は影響を受けない
- browse 中の member が offline になった場合、その member の分は offline 扱いで phase 導出から外れる（残りの member の状態で再計算）

これは wire の presence/activity 語彙に影響するため「wire 無変更」ではない — additive な拡張として扱い、capability negotiation（後述 B-5）で新旧混在を制御する。

#### B-3. OPEN_WORK の競合意味論を定義する（「last-wins」の廃棄）

v1 の「同時ブラウズ駆動は last-wins」はテスト不可能な仕様だったので廃棄し、以下に置き換える:

- OPEN_WORK は既に epoch チェックをスキップし latest-pick-wins で受理される現行仕様を維持するが、**負けた側の UX を定義する**: 両者がほぼ同時に pick した場合、後着の OPEN_WORK が全体に適用され、先に pick した側は自分の選択が上書きされたことを presence/manifest の更新で観測する。明示的な superseded 通知は**今回は導入しない**（将来の改善として記録）
- leader bootstrap（ペア成立時に leader クライアントが自動で OPEN_WORK する既存挙動）と member の初回 pick の競合: **bootstrap の発火は join-order 先着側（leader）に限る tie-break を維持する**。つまり role は権限を持たないが、対称性を破る番人（tie-breaker）としては残る — これはモデルに組み込むべき位置関数である
- UI_SYNC（scroll 等の ephemeral mirror）は順序保証がなく収束も定義しない現行仕様のままとし、「last-wins」という語は使わない

#### B-4. role の終状態を一文で固定する

v1 の「意味を漸進的に弱める」は意味の拡散であり廃棄する。終状態を固定する:

> **role は join 順の位置（先着/後着）のみを示し、座席・relay hub・WebRTC initiator・dedup 名前空間・tie-break を決める。権限は決して role から導かない — 権限の唯一の源泉は trust class（member/guest）である。**

ubiquitous language:

| 概念 | 語彙 | 備考 |
|---|---|---|
| 位置（join 順） | leader / follower（wire frozen） | `leaderSide` 等の wire 綴りは位置語彙として frozen。意味を変えず、権限と無関係であることを明記 |
| 信頼 | member / guest | 権限の唯一の源泉 |
| セッションの開始者 | 「開始者」 | **「owner」は禁止語** — 所有と権限を含意し、剥がそうとしている癒着を再輸入するため |

#### B-5. capability negotiation と混在互換

semantic behavior の変更である以上、schema 互換だけでは不十分。additive な capability advertisement を導入する:

- HELLO/ACK/STATE に `features` を追加（例: `member-open-work`, `per-member-selecting`）。unknown features は無視（additive 原則）
- 新 UI は capability 非対応の server/peer では旧挙動に fallback する
- server は新 capability を無効化できる deploy flag を持ち、rollback は client/server 独立に可能にする
- `PROTOCOL_VERSION` は additive 範囲に留まる限り v1 のままとし、breaking になった時点で v2 に上げる判断を実装 PR で明示する

新旧混在の移行行列（各セルの期待動作を実装 PR で検証すること）:

| server | client | 期待動作 |
|---|---|---|
| old | new web | capability 未広告 → member follower の作品選択 UI は出さない（旧挙動 fallback） |
| new | old web SW shell | bare QR を描画し続けるが、server は create/match 両方で mint 済み — code join も composite も機能。member follow は leader-only のまま（退行なし） |
| new | old iOS/Android | native は ACK の `guestToken` を保持しないため **leader QR は bare のまま**（guest 不可）。scan は composite から code 抽出で member join 可能。統一 QR は native の表示側更新を要する（Phase 2 スコープに明記） |
| new | mixed web+native pair | member-open-work capability の有無で双方の UI が変わり得る。不整合は UX 上の劣化であって破壊ではない |

### C. セキュリティ評価（書き直し）

v1 の評価は結論ありきで露出モデルを誤っていた。正直に書き直す。

**コードと bearer は同じクレデンシャルではない。** bare `#/join/<code>` は認証能力ではなく、認証済み member のための短期 join slot selector である — WS handshake は認証必須なので、bare code の写真が漏れても member アカウントを持たない攻撃者は join できない。composite QR は inert な紙片を **live credential** に質的に変える。しかも本番は private tailnet だけでなく cloudflared の公開経路（`bookq.mats.dpdns.org`）を持ち得るため、QR 写真 1 枚で世界中の誰でも follower seat を取り得る。

それでも always-mint を採用する根拠は、**露出窓の小ささ**に置く:

- 未ペアの待機セッションは code TTL（300 秒）で `END_CODE_EXPIRED` によりセッションごと殺される。つまり bearer の露出窓は実効 **≤300 秒 × single-use**
- grant は session 紐付けで `end_session` により必ず失効し（どの teardown 経路でも）、tick による revoke/expiry の再検証で socket も落ちる。failed-JOIN throttle（`join_fails`）による brute-force 緩和も存在する
- ただし注意: session 紐付け・end_session 失効・TTL は全て「セッションの寿命」という **1 つの事実の系**であり、独立した多重防御として数えてはならない

攻撃者の利得を正直に書く:

- auto-approve により、先に座った攻撃者は **pair 成立済みメンバーとして MANIFEST/current work を受け取る**（DoS ではなく pair スコープへの侵入）。pair スコープでも private library の内容の読取であり侵害である
- 現行仕様では **どちらの member の LEAVE でもセッション全体が終了する**ため、侵入者は両者のセッションを破壊できる（既知の未解決 issue、#652 レビュー時から open）
- 検知可能性: presence 表示で leader は join 者を視認でき、code/grant の消費は可観測。不審な join への対処は leader の LEAVE（ただし現行は session 全体を殺す）
- hidden roots との相互作用: pair 共有の hidden veto はクライアント側実装であり、guest は信頼できないクライアントを使い得る。guest の画像アクセスがサーバ側で session の current work に強制されていることの確認、および hidden root の work を current work にした場合の扱いを、実装時のセキュリティテストで明示すること

UX 要求との関係: プロダクトオーナーの要求する UX（全待機で QR 表示）では、mint-on-display（遅延 mint）を採用しても QR は常に表示されるため **実効的に always-mint と等価**になる。つまり always-mint は UX 要求の必然的な帰結であり、残余リスクは上記の通り受容する。リスクを下げたい場合の将来の hardening 選択肢として、guest join 時に leader 承認を要求するモード（vestigial な APPROVE/REJECT の repurposing — 後述）を記録する。

### D. APPROVE/REJECT の扱い

「vestigial と診断しながら保持」は最悪の組合せなので、明確に決める: **wire 上は保持し、member-member では auto-approve を既定のままとする。guest join への leader 承認モード（in-band 認可チェックポイントとしての repurposing）は将来の hardening 選択肢として記録するが、本 ADR のスコープ外とする**。ただし実装フェーズで APPROVE/REJECT が権限の根拠（leader-only チェック）として使われ続けることは B-4 の原則と整合しないため、承認権限の根拠は「session 開始者であること」に明示的に移し替える（位置ではなくライフサイクル責任）。

## 検討した代替案

| # | 案 | 棄却理由 |
|---|---|---|
| A | 現状維持 + コピーで明示（member 専用/誰でも参加可の出し分け） | QR trap の構造自体は残る。leader の操作数も減らない |
| B | 役割のネゴシエーション（pairing 時に leader/follower を協議で決める） | ネゴシエートすべき選好が存在しない。side は SWAP で事後修正可能で、割当方式は問題ではない。問題は follower role が何を剥奪するかであり、それは信頼軸の話である |
| C | role 概念の完全廃止 | guest の権限境界は残す必要があり、また relay hub・dedup・initiator・tie-break 等の位置関数は join 順を必要とするため不可能。必要なのは role の削除ではなく権限からの分離 |
| D | 遅延 mint / mint-on-display | UX 要求（全待機で QR 表示）の下では実効的に always-mint と等価。クライアントに mint トリガの状態機械を増やすコストだけが残る |
| E | member-only 招待を first-class UI として残す | capability 上の意味がない（member は composite 経由でも full capability）。「bearer を mint しない」性質は UX 要求（全待機 QR 表示）と両立しない。受容する代わりに露出窓の小ささと検知可能性でリスクを抑える（C 節） |
| F | guest join 時の leader 承認を既定にする | 操作数最小化の UX 要求と衝突。将来の hardening 選択肢として記録 |

## 影響

### 良い影響

- member 2 台のユースケースでどちらの端末からでも作品選択・ページ操作ができ、「誰が leader か」をユーザが意識する必要が消える
- 操作数の削減: chain tap 1 回で待機/接続が決まり、ゲスト招待も同じ QR で完結する
- QR の 2 モード trap が構造的に消える（常に composite のため区別の必要がない）
- 権限の説明が「guest だから制限される」という自明な形になり、UI コピーが単純になる

### 悪い影響・コスト（正直な記述）

- bearer token が常に mint される。露出窓（≤300s × single-use）で抑制されるが、公開経路があるデプロイでは QR 写真漏洩で部外者が pair スコープを読み、セッションを破壊し得る（C 節の受容リスク）
- **follower の作品 tap の意味が反転する**: 今日は「ペアを離れて solo で読む」（軽い退出 gesture）だが、変更後は「2 人の共有作品を変える」になる。退出は明示の解除操作のみになる。この行動変更は意図的なものとして受容する
- `selecting` の per-member 化は wire の additive 拡張を伴い、rust/web/swift/kotlin の 4 ランタイムに跨る。Phase 1 は「チェック置換」ではなく位相制御の再設計である
- 新旧混在期間は capability 差による UX の非対称が生じる（破壊ではないが劣化はある）

### 影響なし・範囲外

- side 割当・SWAP・ページ同期のプロトコルは無変更
- guest の HTTP read-only 制限（library write・reading-positions 拒否等）は無変更
- UI_SYNC（ephemeral mirror）の順序・収束仕様は無変更
- solo 読書は範囲外
- guest LEAVE によるセッション破壊・any-member-offline sweep 等、#652 レビュー時から open の既知 security 課題は本 ADR のスコープ外（ただし C 節の受容リスク評価には組み込み済み）

## 実装フェーズ

各フェーズに exit criterion を設け、**どの中間状態でも member-member の QR/MATCH join が今日と同等以上に動く**ことを保証する。変更する既存テストの一覧は各実装 PR に明記すること（壊れたから消す、は禁止）。

- **Phase 0（契約テスト先行）**: 新権限モデルの失敗するテストを先に書く。golden cases（`api/reader-sync-cases.json`）は `not_leader` の negative case を持たず happy path のみなので、green であることは新旧どちらの権限の証明にもならない点に注意。少なくとも以下を追加:
  - member follower による OPEN_WORK → accept（direct / leader 経由 relay 両方）
  - guest follower による OPEN_WORK → reject（direct / **member leader 経由 relay** 両方 — confused deputy の回帰テスト）
  - guest RESUME 後も trust が維持される / member cookie + gt 併用時の分類が一意に決まる
  - per-member selecting の状態機械行列: follower が開いて follower が閉じる / follower が開いて leader が閉じる / 両者が開き逆順に閉じる / browse 中の offline / browse 中の OPEN_WORK commit / leader bootstrap と member pick の同時
- **Phase 1（trust モデル + 権限解放 + selecting 再設計）**: `Member.trust` 導入、effective-actor 認可、per-member selecting、capability advertisement、UI 制御の trust ベース化。exit criterion: 上記テストが green で、capability 非対応環境では旧挙動に fallback する。**対称化後も bootstrap tie-break は leader（先着）に残る**
- **Phase 2（UX 統一）**: match origin での mint、chain-tap connect、3 択パネル入口の解体、QR 常時 composite 化。**member の composite QR code-join フォールバック（member なら gt を無視して code join — grant 切れ時の robustness）をこのフェーズに含める**（v1 では Phase 3 だったが、これがないと Phase 2 の中間状態で member の QR join が退行する）。web の deep-link parser は composite 対応済み（#652 着陸済み）だが、native の **表示側**（ACK の guestToken を保持し composite QR を描画する）の更新をこのフェーズに明記する — これがないと native leader の QR は bare のままで統一が壊れる。exit criterion: web 新旧・native 新旧の移行行列の各セルが検証済み
- **Phase 3（コピー掃除のみ）**: UI コピーの leader 語の整理（「開始者」への置換、owner 語の不使用）


## レッドチームレビューの記録（v2 改訂の由来）

v1 は 3 人格（t-wada / Fowler / Linus 相当）のレッドチームレビューで No-Go / fix-first と判定された。収束した指摘と対応:

1. **trust がデータ構造に存在しない + relay confused deputy**（t-wada・Linus 独立に指摘）→ B-1 で対応
2. **`selecting` boolean では同時 browse を表現できない**（t-wada・Fowler・Linus 全員）→ B-2 で per-member 導出に再設計
3. **「意味を漸進的に弱める」「last-wins」はテスト不可能**（t-wada・Fowler）→ B-3・B-4 で終状態定義に置換
4. **移行行列不在・native は composite QR を表示できない**（Linus・Fowler）→ B-5 と Phase 2 で対応
5. **セキュリティ評価が結論ありき**（Linus・Fowler）→ C 節を全面書き直し。ただし評価の結論（受容）は、露出窓 ≤300s × single-use の分析に基づき維持
6. **follower pick の意味反転が未記載**（Fowler）→ 影響節に明記
7. **ブラウズミラーは既に双方向という事実誤認**（Fowler・Linus）→ 背景を訂正
8. **#160 revert の根拠不足**（t-wada・Linus）→ chain-tap 直発火はプロダクトオーナーの明示要求として維持するが、慎重論の個別懸念への対処と受容リスクを A-1 に明記
