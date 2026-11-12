# api/ — 公開 API 契約 (single source of truth)

サーバ (FastAPI → 将来 Rust) とクライアント (PWA / iOS) の間の正本仕様。
**実装ではなくこのディレクトリが契約の正** — 振る舞いを変える変更は必ずここを先に更新する。

| ファイル | 内容 |
|---|---|
| `openapi.v1.yaml` | REST API (OpenAPI 3.1)。`/api/v1/*` 正規形 + deprecated v0 併記 |
| `v1-semantics.md` | v1 REST の意味論正本 (ETag 世代・positions LWW・owner HMAC・PKCE ライフサイクル・gzip)。openapi.v1.yaml とセットで読む |
| `asyncapi.yaml` | `/ws/reader-sync` のワイヤ形式 (AsyncAPI 3.0) |
| `reader-sync-protocol.md` | reader-sync の振る舞い契約 (フェーズ機械・逐次化・拒否理由・MANIFEST)。asyncapi.yaml とセットで読む |
| `dist/` | 生成物 (codegen のみが書く。手編集禁止): `openapi.v1.json` / `asyncapi.json` + Web 型 `app/static/api/api-types.d.ts` |

## 生成物の再生成

spec を編集したら必ず `./scripts/api/codegen.sh` を実行する
(ツール版固定のためローカルと CI は同一出力)。
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
  ローカル確認:
  `npx @stoplight/spectral-cli lint api/openapi.v1.yaml api/asyncapi.yaml`
