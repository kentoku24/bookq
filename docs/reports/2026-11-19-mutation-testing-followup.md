# Mutation testing フォローアップ: state.rs position + archive.rs (2026-11-19)

`2026-11-19-mutation-testing-risk-supplement.md` の残課題のうち、
データ整合性・UX 直結度が高い 2 領域にテストを追加し、スコープ付き
mutation 再測定で効果を計測した記録。
ブランチ: `ai/mutation-testing-followup` (PR #583 の stacked branch)。

## 追加したテスト (計 32 件、全パス)

### `viewer-rs/src/state.rs` — +16 件

Slot 規則は `api/vectors/positions-cases.json` で固定済みのため、
**永続化境界と HTTP 契約**を対象に:

| 領域 | テスト |
|---|---|
| PositionStore | delete_all tombstone が再起動後も stale write を拒否 (データ整合性の核心)、per-user delete_all 分離、delete_user hard reset、MAX_USERS=100 での stalest user 退避、folder フィルタ |
| バリデーション | `as_int` (bool 拒否・整数 float 受理・範囲外拒否)、`valid_folder_param` (2048 chars 境界・マルチバイト)、`validate_root` (../・ドット・バックスラッシュ・NUL) |
| VisibilityStore | revision は実変更時のみ bump、root は ORDER BY |
| ハンドラ | `put_position` 検証行列と stored/stale/REJECTED→400 写像、`list_positions` JSON 形状と no-store、`delete_positions`/`delete_user_positions`、`put/get_folder_visibility` の owner HMAC・content-type・{root,hidden} 完全一致・ツリー membership 404・private headers |

### `bookq-core/src/archive.rs` — +16 件

ベースライン最大の穴 (missed 740) のうち、**fixture 不要で確実に効く
純粋ヘルパと破損耐性**を優先 (RAR3/RAR5 パーサコアは別タスク級):

| 領域 | テスト |
|---|---|
| パス/URL | `unquote` (%XX・UTF-8 lossy・'+' リテラル)、`posix_normpath` (// 保持・.. 解決・root 脱出不可)、`posix_dirname`、`urlsplit_path`、`epub_resolve`、`splitext` — Python 移植の忠実性が spine 解決とメンバ検索を支える |
| ボリューム命名 | `next_newvol`/`next_oldvol` の桁上がり・Fatal |
| ZIP extra | `decode_extra` zip64 sentinel のみ pull・途中切れ Err・0x7075 unicode path の crc 一致時のみ置換 |
| RAR3 名 | `UnicodeFilename::decode` type 0-3・フラグビット共有・std_name 枯渇時 '?' fill |
| 破損耐性 | ZIP EOCD 欠落 → None、RAR 非対象/切断で panic しない (NotRarFile→[] 契約を明文化) |
| EPUB | container.xml→OPF→spine→xlink:href 解決の最小 fixture、container 不在時 `Some((raw, []))` フォールバック契約 |

## スコープ付き mutation 再測定

`cargo mutants -f 'viewer-rs/src/state.rs' -f 'archive.rs'`、1,475 mutants、
35 分 (bookq-core は mutant あたり ~1-2s、viewer-rs は ~5-9s)。
`--cargo-test-arg '-- --skip view_caps_and_flattens'` で低速サムネイル
テストを除外 (baseline 40s → mutant 毎の test を短縮)。

| file | relevant mutants | caught | missed | timeout | score | 前回 |
|---|---|---|---|---|---|---|
| archive.rs | 1,218 | 644 | 550 | 24 | **52.9%** | 38.0% (463/1218) |
| state.rs | 157 | 148 | 9 | 0 | **94.3%** | 43.9% |
| **計** | **1,375** | **792** | **559** | **24** | **57.6%** | — |

unviable 100 件はビルド不能 mutant でスコア対象外。
state.rs は初回測定 (92.4%) 後に下記 +3 ケースを追加し `-f
'viewer-rs/src/state.rs'` で再測定 (6 分): 対象 4 mutant を全て kill、
94.3% に到達。残り 9 missed は `now_secs` (時刻依存)・`store_error`/
`unprocessable` (エラー応答)・`Slot` seq 演算・`load_slot` フィールド・
equivalent mutant 1 件で、これ以上の unit テスト費用対効果は低い。

- `-f 'viewer-rs/src/state.rs'` はパス glob で `reader_sync/state.rs` を
  除外 (前回の `-f state.rs` basename 一致の巻き込みを回避)。
- archive.rs は +181 caught / +14.9pt。state.rs は +81pt — 永続化境界・
  バリデーション行列・HTTP ハンドラ契約のテストが直接効いた。

## 初回測定後の追加テスト (state.rs +3 ケース)

初回 scoped run で残った state.rs missed 11 件をトリアージし、
プライバシー直結の 4 mutant を追加テストで kill:

- `put_folder_visibility` owner 定数時間比較 (`&&`→`||`, 畳み込み `|`→`&`/`^`)
  — 同一長・文字入替 owner と切り詰め owner が 409 になることを固定。
  owner token は可視性書込みの CSRF/stale-tab ガードなので実害あり。
- `hidden_roots.any(|r| r == root)` の `==`→`!=` — hidden set が非空の
  ときツリー非所属 root の hide が membership check を skip しないこと
  (fail-closed) を固定。
- 残りは `now_secs` (時刻依存)・`store_error`/`unprocessable`/
  `load_slot`/`delete_folder` のエラー経路で unit では取りにくい。
- `put_folder_visibility` 973:52 の `||`→`&&` は **equivalent mutant**
  と判定 (下流の `hidden` bool / `root` 検証が同じ 400 を返す)。

## 残課題

- archive.rs missed 550 の内訳はほぼパーサコア:
  `rar3_read_store` 66, `r5_process_extra` 47, `r3_parse_block_header` 45,
  `read_endrec64` 35, `r3_parse_file_header` 28, `read_cd` 23,
  `rar3_names`/`rar5_names` 各 23, `r5_parse_file_block` 22,
  `read_endrec`/`r5_parse_block_header` 各 21, `r3_parse_subblocks` 19,
  `r3_parse_ext_time` 16, `rar5_read_store` 16, `load_byte` 12。
  圧縮展開・マルチボリューム結合・暗号化・ZIP central directory の
  実 fixture が必要で別タスク級。破損入力の fail-safe は今回の
  テストで固定済み。
- state.rs は 94.3% まで改善。残りは時刻依存・エラー応答 shape・
  seq 演算など unit では取りにくい領域と、下流検証で同じ 400 を返す
  equivalent mutant (`put_folder_visibility` 973:52 `||`→`&&`)。
