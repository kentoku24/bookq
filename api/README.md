# api/ — 公開 API 契約 (single source of truth)

サーバ (FastAPI → 将来 Rust) とクライアント (PWA / iOS) の間の正本仕様。
**実装ではなくこのディレクトリが契約の正** — 振る舞いを変える変更は必ずここを先に更新する。

| ファイル | 内容 |
|---|---|
| `openapi.v1.yaml` | REST API (OpenAPI 3.1)。`/api/v1/*` 正規形 + deprecated v0 併記。各レスポンスの `examples:` が唯一の fixture 源 (#237) |
| `v1-semantics.md` | v1 REST の意味論正本 (ETag 世代・positions LWW・owner HMAC・PKCE ライフサイクル・gzip)。openapi.v1.yaml とセットで読む |
| `asyncapi.yaml` | `/ws/reader-sync` のワイヤ形式 (AsyncAPI 3.0) |
| `reader-sync-protocol.md` | reader-sync の振る舞い契約 (フェーズ機械・逐次化・拒否理由・MANIFEST)。asyncapi.yaml とセットで読む |
| `dist/` | 生成物 (codegen のみが書く。手編集禁止): `openapi.v1.json` / `asyncapi.json` / `examples.json` + Web 型 `app/static/api/api-types.d.ts` |
| `dist/examples.json` | spec `examples:` の抽出バンドル (生成物・commit する)。PWA `tests/spec_examples.py` / iOS `SpecExamples.swift` / `scripts/api/mock_server.py` が読む |
| `vectors/` | 言語共通 conformance vectors (#280) — Python/JS/iOS/Android が同じ入力から同じ出力を出すことを pin する (path-lookup / solo-spreads / work-chapters) |
| (`tests/fixtures/reader_sync_cases.json`) | reader-sync ワイヤの共通ケース (digest golden / reject reasons) — api/ 外だが同じく契約の一部として各言語テストが参照 |

## 生成物の再生成

spec を編集したら必ず `./scripts/api/codegen.sh` を実行する
(ツール版固定のためローカルと CI は同一出力。`examples.json` も含む)。
CI (`api-spec-lint.yml` の codegen-drift job) が再生成後の
`git diff --exit-code` で drift を fail させる。

## 編集規約

- **後方互換**: 公開フィールドの削除・型変更・意味変更は不可。追加は optional フィールドのみ。
  破壊的変更が必要なら新バージョン (`openapi.v2.yaml` 等) を切る。
- **v0 → v1**: `/api/*` (v0) は deprecated。新規クライアントは `/api/v1/*` のみ使う。
  v1 で意味が変わる箇所 (envelope 統一、Bearer 認証、per-folder reading-positions) は
  各 operation の description を参照。
- **wire-stable 文字列**: `reader-sync-protocol.md` §5 の拒否理由・enum 値は
  iOS/web が文字列一致で処理する契約面。リネーム禁止、追加のみ可。
- **CI**: Spectral lint が PR で両 spec を検査し、Schemathesis が
  `openapi.v1.yaml` の実装適合を property-based で検証する
  (`.github/workflows/api-contract.yml`、意味的拒否は `schemathesis.toml` で許容)。
  `scripts/api/validate-examples.py` が全 `examples:` の schema 適合を検証し、
  bundle 鮮度 (`extract-examples.py` 再生成差分なし) は codegen-drift job が
  検査する (`.github/workflows/api-spec-lint.yml`)。
  さらに `api-breaking` ワークフローが PR の base リビジョンとの差分を
  `scripts/api_breaking_check.py` で検査し、後方互換を壊す変更
  (required 追加・プロパティ削除・enum 値削除・型変更・path/operation 削除等)
  を fail させる。ローカル確認:
  `npx @stoplight/spectral-cli lint api/openapi.v1.yaml api/asyncapi.yaml`
- **spec 先行の手順**: 新規 endpoint は spec + `examples:` から書き、
  `docs/client-standalone-dev.md` の「新規 endpoint を spec 先行で追加する」に従う。
  クライアント単独開発用モックは `./scripts/api/mock.sh` (+ `ws` サブコマンド)。

## 破壊的変更の検出 (CI)

`.github/workflows/api-breaking.yml` は PR で `openapi.v1.yaml` /
`asyncapi.yaml` を base リビジョンと比較し、旧クライアント
(App Store 配布で古いバイナリが残る iOS、キャッシュされた PWA shell) を
壊す変更を検出する。Spectral (形の lint) と Schemathesis (現実装⇔現 spec
の一致) は spec と実装の同時破壊を見逃すため、この差分検査が最後の砦。

検出対象 (抜粋): path/operation/channel/message の削除、response status の
削除、required パラメータの追加、`required` エントリの追加、プロパティ
削除、enum 値削除・const 変更、型変更、operationId 変更。通過する追加的
変更: optional プロパティ追加、enum 値追加、新規 path/operation。
詳細ルールは `scripts/api_breaking_check.py` の docstring を参照。
単体テストは `tests/test_api_breaking_check.py` (受入条件の 4 項目 +
例外運用を含む)。

## 破壊的変更の例外運用

原則、破壊的変更は新バージョン (`openapi.v2.yaml` 等) を切って行う
(v0 退役の手順は #212)。やむを得ず現行 v1 を壊す場合のみ、以下の
すべてを満たすこと:

1. PR に `api-breaking` ラベルを付与する (CI の breaking 検査は
   fail せず報告のみになる: `--allow-breaking`)。
2. 影響を受ける旧クライアント (iOS / PWA) の移行計画を PR 本文に記載する。
3. `info.version` の扱いに注意する: 現行 `1.0.0` は契約世代を示し、
   破壊的変更時は minor 以上の bump を検討する (semver 厳密運用は任意)。

ラベル無しで breaking 検査が赤の PR はマージしない。
