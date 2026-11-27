---
name: expert-review
description: Use when red-teaming a design document, ADR, protocol redesign, migration plan, or security-relevant architecture decision before approval or implementation. Trigger on "expert-review", "レッドチームレビュー", "設計レビュー", "ADR review", or when multiple expert personas should attack a design against the actual code.
---

# expert-review — 7人格レッドチームレビュー

設計ドキュメントの主張を、7つの専門家人格を持つ並列 read-only subagent が実コードに照らして攻撃し、orchestrator が重要主張を再検証して synthesis する運用 playbook。ADR 0004 のレビュー（2ラウンド・6人格・収斂 findings）で得られた運用知見を、canonical 7 personas へ一般化したもの。

目的は実装品質のレビューではなく、**設計の前提・不変条件・移行・脅威モデルが実コードと事実に対して成立するか**を潰すこと。

## When to Use

- ADR / design doc / architecture decision / migration plan の承認前レビュー
- プロトコル再設計、状態同期、認証・公開範囲、キャッシュ、offline/pair sync など security-relevant または stateful な設計
- 「後で直す」ことが高コストな、不可逆または移行を伴う判断
- 単一レビュアーの bias を避け、独立した attack lens からの収斂を見たいとき

使わない: 通常のコードレビュー、文体レビュー、小さなバグ修正、設計判断を含まない実装タスク。

## Hard Rules（実事故由来。省略禁止）

1. **レビュー対象を動く repo から取らせない。** 対象本文は各 subagent prompt に全文 inline 埋め込みするか、review/round ごとに一意な固定 temp path へ書いて exact path を渡す。branch 上だけに存在するファイルを「探して読め」と指示してはいけない。実際に subagent が対象 branch を発見できず全滅した。
2. **verification ref を明示に固定する。** working tree は無関係な branch にいる前提で、`git show <ref>:<path>` / `git grep <pattern> <ref>` を使わせる。レビュー開始時に `<ref>` は `git rev-parse origin/main` などで SHA 解決する。実際に全 reviewer が stale branch を分析し、false findings を後で除去する羽目になった。
3. **load-bearing claim は orchestrator が独立検証する。** 「feature X が未実装」「この前提は code に存在しない」など verdict を左右する主張は、1〜2本の targeted command で自分が確認してから採用する。wrong-branch artifact は捨て、confirmed claim だけを残す。
4. **収斂は signal。** 2+ personas が独立に同じ欠陥へ到達したら confirmed-priority として扱い、synthesis で必ず追跡する。ただし critical claim の検証責任は残る。
5. **persona には domain focus を与えるが、cross-domain finding を禁止しない。** 実例では最有力 finding（security premise が事実として false）を state-machine reviewer が発見した。
6. **出力形式を固定する。** verdict first（`ship` / `fix-first` / `no-go`）、severity 順の concrete findings、短い what's-right。「何が壊れるか・なぜ・代わりに何をすべきか」のない批評は禁止。
7. **personas は implementation quality ではなく DESIGN を攻撃させる。** 修辞や文章の粗探しではなく、document の claim を actual code / invariant / operational reality と突き合わせる。
8. **round structure を使う。** Round 1 は broad personas → 設計修正 → Round 2 は revision に対し complementary/deeper personas または同じ sequence を使う。synthesis 後に product owner が決めるべき論点を隠して勝手に解決しない。

## Canonical Personas

| # | Persona | Attack lens | 主に突くもの |
|---|---|---|---|
| 1 | **Leslie Lamport** | distributed state machines: invariants, reachable states, message ordering, convergence | 未定義 cascade、stuck state、race interleaving、再送・切断・順序逆転後の収束 |
| 2 | **Bruce Schneier** | attacker-centric security | attacker が実際に得るもの、detection vs theater、credential hygiene、attack tree、trust boundary |
| 3 | **Hyrum Wright** | backward compatibility / Hyrum's Law | 観測可能な挙動への依存、mixed-version cells、wire encoding、migration matrix、stale client |
| 4 | **Martin Fowler** | architecture / domain | conceptual compression、ubiquitous language、evolutionary design、honest trade-off、incremental migration correctness |
| 5 | **t-wada（和田卓人）** | testability / TDD | すべての claim が testable spec か、phase independence、failing test を先に書けるか、小さく検証可能な移行か |
| 6 | **Linus Torvalds** | brutal pragmatism | data structures over code、“we do not break userspace”、over-engineering、本当に存在する問題か、security hand-waving |
| 7 | **Rich Hickey** | simplicity / complect | 結び付けられるべきでない概念の complect、vocabulary honesty、incidental complexity |

### Domain-specific alternatives

必要に応じて 1〜2 人格を入れ替える。人格数は 7 を維持しつつ、domain fit を優先する。

| Persona | Lens | Swap in when |
|---|---|---|
| **Pat Helland** | idempotency / retry / resume | queue、workflow、cloud storage、at-least-once delivery |
| **Kyle Kingsbury / Aphyr** | Jepsen-style fault injection | consensus、lock、network partition、clock skew |
| **Rob Pike** | protocol complexity | wire protocol、API surface、option/flag の爆発 |
| **Martin Kleppmann** | concurrent data semantics | CRDT、causality、cache coherence、offline replication |
| **Ross Anderson** | security economics | abuse incentive、insider threat、scale 後の economics |
| **Werner Vogels** | eventual consistency / operations | availability、degraded mode、運用負荷、failure-as-normal |

## Procedure

### 1. Target と verification base を freeze する

```bash
# repo root で実行。対象が大きい場合は review/round ごとに一意な固定 path へコピーする。
REVIEW_DIR=$(mktemp -d /tmp/expert-review.XXXXXX)
TARGET_FIXED_PATH="$REVIEW_DIR/review-target.md"
cp <target-doc> "$TARGET_FIXED_PATH"
VERIFY_REF=$(git rev-parse origin/main)
printf 'VERIFY_REF=%s\n' "$VERIFY_REF"
printf 'TARGET_FIXED_PATH=%s\n' "$TARGET_FIXED_PATH"
shasum -a 256 "$TARGET_FIXED_PATH"
```

- inline にする場合は**全文**を各 prompt に入れる。抜粋だけを渡さない。
- 固定 path にする場合は review/round ごとに `mktemp -d` で一意な directory を作り、生成済みの exact absolute path を全 subagent に渡す。`/tmp/review-target.md` のような固定名は並行レビューで上書きされるため使わない。レビュー中にファイルを更新しない。
- `VERIFY_REF` は moving ref 名だけでなく SHA を記録する。prompt には `origin/main = <sha>` のように両方書く。
- 必要な context path（関連 module、API spec、既存 migration doc）を列挙してよいが、target の取得先を repo にしてはいけない。

### 2. 7 personas を並列 read-only subagent で起動する

- 7人を**同時に**起動し、互いの出力を見せない。独立した収斂を測るため。
- すべて read-only。file edit、branch checkout/switch、commit、network mutation、server 起動を禁止する。
- prompt には同じ target と verification instructions を渡し、persona lens だけ変える。
- Round 2 では revision を再 freeze し、Round 1 findings の status と設計差分を添える。未修正のまま引き継がない。

### 3. Reusable subagent prompt template

`{{...}}` を置換して使う。target が小さい場合は `{{TARGET_INLINE}}` に全文を入れ、fixed path section を削る。大きい場合は inverse にする。

```text
You are {{PERSONA_NAME}} performing an adversarial DESIGN review.
Use this persona lens: {{PERSONA_LENS}}

Round: {{ROUND_NUMBER}}
Repository root: {{REPO_ROOT}}
Verification base: {{VERIFY_REF_NAME}} = {{VERIFY_REF_SHA}}

CRITICAL OPERATING CONSTRAINTS
- You are read-only. Do not edit files, create branches, commit, checkout, switch branches, or run destructive/mutating commands.
- The working tree may be on an UNRELATED branch. Never trust files read without an explicit revision.
- Verify repository claims only against the pinned ref using commands like:
  git -C {{REPO_ROOT}} show {{VERIFY_REF_SHA}}:<path>
  git -C {{REPO_ROOT}} grep -n <pattern> {{VERIFY_REF_SHA}} -- <path>
- Do NOT fetch the review target from the repo or from a branch. The target is exactly what is provided below.
- Attack the DESIGN, not prose style or implementation quality. Compare the document's claims with actual code, invariants, operational behavior, and migration reality.
- Your primary lens is {{PERSONA_LENS}}, but cross-domain findings are welcome if concrete.
- Do not criticize without specifics. Every finding must state: what breaks, why it breaks, evidence, and what should be done instead.
- If a claim is uncertain, label it as a hypothesis and say what command/artifact would confirm it.

REVIEW TARGET
{{TARGET_INLINE}}
-- or --
The complete review target is at this exact fixed path: {{TARGET_FIXED_PATH}}
Read only that path for the target. Do not search branches for a newer copy.

Useful context paths (optional, verify at pinned ref):
{{CONTEXT_PATHS}}

OUTPUT FORMAT — obey exactly:
1. Verdict: one of ship / fix-first / no-go, with at most 3 sentences.
2. Findings, ordered by severity (Critical, High, Medium, Low). For each:
   - Title
   - Severity
   - What breaks
   - Why / attack path or invariant violation
   - Evidence: pinned-ref file:line or exact command result when possible
   - What instead
   - Confidence: confirmed / hypothesis
3. Cross-domain observations (optional; only concrete ones).
4. What's right: brief bullets, no praise filler.
```

### 4. Orchestrator が critical claims を検証する

synthesis 前に、verdict を左右する claim と「未実装」「不存在」「必ず破壊される」類の claim を自分で確認する。

```bash
# 例: シンボル探索
git -C <repo> grep -n '<feature-or-symbol>' <verify-sha> -- rust web ios android api

# 例: 呼び出し元・schema・test の pinpoint 確認
git -C <repo> show <verify-sha>:<path> | sed -n '<start>,<end>p'
```

判定を findings に付ける:

- `confirmed`: orchestrator が pinned ref で再現確認
- `rejected-artifact`: wrong branch / stale target / 読み違い由来と判明
- `uncertain`: 検証不能。load-bearing なら verdict に使わない

特に「feature X is not implemented」は、定義・呼び出し・生成物・test の少なくとも2面を見る。検証せず採用しない。

### 5. Synthesis する

1. 重複 finding を正規化し、persona 名をすべて保持する。
2. confirmed findings は **severity を第一キー**に並べる。同一 severity 内では、2+ personas が独立に同じ defect に到達した **convergent / confirmed-priority** を先に置く。convergence は severity を上書きしない。
3. confirmed / rejected-artifact / uncertain を分離する。
4. 設計者が直すべき内容と、product owner が決めるべき内容を分ける。trade-off を勝手に1つへ潰さない。
5. Round 2 では、Round 1 finding ごとに `fixed / partially fixed / unfixed / rejected` を追跡する。

Suggested severity ladder:

| Severity | Meaning |
|---|---|
| Critical | security premise false、data loss、破壊的互換性、system invariant が成立しない |
| High | 主経路が壊れる、migration が成立しない、運用で確実に詰む |
| Medium | edge case・運用負荷・test 不能が限定的に残る |
| Low | 明確化や局所簡素化で済む |

## Synthesis Output Format

```markdown
## Verdict

<ship / fix-first / no-go> — <2〜4文の根拠>

## Confirmed findings（severity順。同一severity内ではconvergent優先）

| Severity | Finding | Personas | Evidence | Required action |
|---|---|---|---|---|
| Critical/High/... | <何が壊れるか> | <names> | <pinned ref または orchestrator command> | <what instead> |

## Convergence table

| Defect | Independent personas | Interpretation | Status |
|---|---|---|---|
| <finding> | <2+ names> | <なぜ独立収斂と言えるか> | confirmed-priority / uncertain |

## Product-owner decision points

1. <本当に owner が決めるべき論点> — options, cost, risk。設計レビューで隠蔽しない。

## What's right

- <短く具体的に>

## Discarded or uncertain claims

- <rejected-artifact / uncertain と理由>
```

## Practical Notes

- reviewer 出力は生のまま保存し、synthesis で暗黙に書き換えない。artifact 除外は理由付きで記録する。
- findings を人数投票で決めない。収斂は強い signal だが、証拠と orchestrator verification が優先。
- 7人全員が `ship` でも、critical premise が unverified なら `ship` にしない。
- Round 1 の修正後は、差分だけでなく revision 全文を再 freeze する。古い target を見たままの reviewer を混ぜない。
- 最終 output は長いエッセイではなく、次の行動（修正・採用・拒否・owner decision）に直結させる。
