# ADR 0004: ペア読書の権限を信頼軸に剥がし、招待 UX を統一 QR に一本化する

- **ステータス:** 提案中
- **日付:** 2026-11-26
- **決定者:** bookq-maintainers
- **関連:** #652 (guest access link) / 実装追跡: #662

## 背景

#652 でゲストアクセスリンク（ログイン不要の follower 参加）が入り、leader 側の招待経路は暗黙の 2 モードになった:

| 経路 | origin | grant | QR | guest が join できるか |
|---|---|---|---|---|
| 「近くの相手とつながる」 | match | mint しない | `#/join/<code>` plain | **できない**（login wall で dead end） |
| 「ゲストを招待」 | create | mint する | composite (`?gt=`) | できる |

この構造には 3 つの問題がある。

**1. QR の見分けがつかないのに挙動が違う。** 2 種類の QR は視覚的に同一だが、片方は未ログイン端末にとって dead end（login wall に落ち、理由も分からない）。leader は自分の QR が guest に効くかどうかを区別できない。

**2. 「member 専用招待」に capability 上の意味がない。** member が composite QR から join しても、WS リンクは handshake cookie で member 分類されるため full capability が得られる（grant 失効の coupling も guest 制限も載らない）。join 後の capability に member join / guest join の差は一切なく、member 専用招待が残しているのは「bearer を mint しない」というセキュリティ上の利益だけで、それは UI からは選択できない（MATCH の副産物としてしか現れない）。

**3. leader/follower role が 2 つの軸を癒着させている。** role は実際には 5 つの独立した関心事を束ねている:

| # | 関心事 | 現状 |
|---|---|---|
| 1 | side 割当（左右どちらを描画するか） | role から導出（leader は leaderSide、follower は逆） |
| 2 | 作品選択権（OPEN_WORK・ブラウズ駆動） | leader-only（`not_leader` で reject） |
| 3 | 参加承認（APPROVE/REJECT） | leader-only だが auto-approve のため実質 vestigial |
| 4 | ブラウズ同期の方向 | leader → follower の一方向 |
| 5 | ページめくり | 対称（意図的） |

このうち member-member ペアにおいて意味を持つのは 1 だけで、2 と 4 は純粋な摩擦である（2 台並べで follower 端末から作品変更できない）。一方 leader-guest ペアでは 2 は**セキュリティ境界**である: guest の画像アクセスはセッションの current work にスコープされているため、guest が OPEN_WORK できると pair チャネル経由でライブラリ全体の読取権を得ることになる。つまり権限剥奪が必要なのは「後から来た人」ではなく「guest」であり、role は信頼軸（member vs guest）と順序軸（先着/後着）を混ぜた誤った抽象になっている。

## 決定

### A. 招待 UX を chain-tap + 統一 QR に一本化する

目標とする操作流れ:

- **member-member**: leader が chain icon を tap → 待機 leader になりモーダルに QR 表示。follower が chain icon を tap → 待機 leader を発見して接続。確立後 leader のモーダルは自動で閉じ（既存挙動）、chain icon は接続中表示
- **leader-guest**: leader 側は同一。guest はカメラで QR を読みブラウザで開くだけ

実現のための変更:

1. **chain icon tap は `connect()`（MATCH）を直接発火する**（#160 で導入した panel-first を戻す）。#160 の懸念は以下で吸収する: 誤二重タップは既存の `DISMISS_GRACE_MS` が緩和し、コード保持者の escape hatch として待機画面内の「コードで参加する」リンクを残す。複数待機時は従来通り picker を出す
2. **match origin の待機セッションでも grant を mint する** — QR は常に composite (`#/join/<code>?gt=<token>`)。元の non-mint 根拠（「match は QR を使わない」）はこの UX が前提を覆すため消える
3. **「開始/ゲストを招待/参加」の 3 択パネルを解体する** — chain tap で待機か接続かが自動決定されるため選択の必要がなく、「ゲストを招待」ボタンは全待機が composite QR を持つことで存在意義が消える

### B. 権限を role ではなく信頼軸（member vs guest）から導く

1. **OPEN_WORK・SET_ACTIVITY(selecting) の `not_leader` チェックを guest チェックに置き換える** — full member であれば leader/follower に関わらず作品選択・ブラウズ駆動ができる。guest は引き続き禁止（ライブラリ全体 read の境界として必須）
2. **member 同士の同時ブラウズ駆動は last-wins とする** — サーバの直列化で競合は自然に解決される。現実には稀で、ミラー表示が整合を保つ
3. **side 割当は join 順のまま**（後着が反対側を描画）とし、SWAP で事後修正 — 権限とは独立の単なる座席である
4. **wire protocol 上の role フィールドは残す** — leader/follower の語はプロトコル互換のため残し、意味を漸進的に弱める。UI コピーからは「leader」を減らし「招待者/owner」に置き換えていく
5. **APPROVE/REJECT は保持** — auto-approve が既定のまま。権限の根拠としては使わない

### セキュリティ評価（always-mint の tradeoff）

match origin を含む全待機で grant を mint すると、bearer token が「常に存在する」状態になる。これを許容する根拠:

- QR を画面に表示する時点で 6 桁コードというクレデンシャルは既に露出している。bearer が増える差分は「非 member でも使える」ことだけで、それは意図した機能そのもの
- grant は session 紐付け・single-use（code_consumed）・最長 8h・`end_session` で必ず失効と多重に縛られている
- member-member で bearer が不要でも、漏洩時の実害は「部外者が 1 枠を先取りする DoS」までで、ライブラリへの read アクセスは pair スコープに留まる
- 遅延 mint（QR 表示後 N 秒で未 match なら mint）も検討したが、複雑さに対して利益が小さいと判断した

## 検討した代替案

| # | 案 | 棄却理由 |
|---|---|---|
| A | 現状維持 + コピーで明示（member 専用/誰でも参加可の出し分け） | QR trap の構造自体は残る。leader の操作数も減らない |
| B | 役割のネゴシエーション（pairing 時に leader/follower を協議で決める） | ネゴシエートすべき選好が存在しない。side は SWAP で事後修正可能で、割当方式は問題ではない。問題は follower role が何を剥奪するかであり、それは信頼軸の話である |
| C | role 概念の完全廃止 | guest の権限境界（OPEN_WORK 禁止）は残す必要があるため不可能。必要なのは role の削除ではなく信頼軸への分解 |
| D | 遅延 mint | QR 表示時点でコードは既に露出しており、時間差で bearer を減らす利益は小さい。クライアントに mint トリガの状態機械を増やすコストに見合わない |
| E | member-only 招待を first-class UI として残す | capability 上の意味がない（member は composite 経由でも full capability）。残すべきは「bearer を mint しない」性質であり、それは UX 統一後は専用の選択肢にするほどの価値がない |

## 影響

### 良い影響

- member 2 台のユースケースでどちらの端末からでも作品選択・ページ操作ができ、「誰が leader か」をユーザが意識する必要が消える
- 操作数の削減: chain tap 1 回で待機/接続が決まり、ゲスト招待も同じ QR で完結する
- QR の 2 モード trap が構造的に消える（常に composite のため区別の必要がない）
- 権限の説明が「guest だから制限される」という自明な形になり、UI コピーが単純になる

### 悪い影響・コスト

- bearer token が常に mint される（上記セキュリティ評価の通り許容と判断）
- pairUI/pairClient の panel 状態機械の解体は中規模の書き換えで、#160 系の UX 回帰テストが必要
- native app は composite QR から code を抽出する既存実装で無変更で動くが、web の join 経路整理に合わせた確認が要る

### 影響なし・範囲外

- side 割当・SWAP・ページ同期のプロトコルは無変更
- guest の HTTP read-only 制限（library write・reading-positions 拒否等）は無変更
- solo 読書は範囲外

## 実装フェーズ

- **Phase 1**: member-follower の権限解放。OPEN_WORK/SET_ACTIVITY の role チェックを guest チェックに置き換え + UI 制御（作品変更ボタンの hidden 条件等）を trust ベースに。wire 互換のまま単独マージ可能
- **Phase 2**: UX 統一。match origin での mint、chain-tap connect、3 択パネル解体、QR 常時 composite 化
- **Phase 3**: 残件。member が composite QR を開いた時の code-join フォールバック（redeem 不要化・grant 切れ時の robustness）、UI コピーの leader 語の整理
