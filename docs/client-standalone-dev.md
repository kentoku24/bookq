# クライアント単独開発 (spec-first, #237)

サーバ実装を待たずに PWA / iOS の UI 開発・テストを行う手順。
`api/openapi.v1.yaml` の named `examples` が唯一の fixture 源であり、
`api/dist/examples.json`（`scripts/api/extract-examples.py` 生成）が
各クライアントテストに配られる。

## 1. REST モックの起動

```bash
./scripts/api/mock.sh --port 4010        # stdlib 製 (依存ゼロ)
./scripts/api/mock.sh --port 4010 --prism  # Prism (node 必須、差分確認用)
```

- 認証なし・CORS `*`。バイナリ (`/image`・`/thumb`・`/view`・sprite) は 1px プレースホルダ。
- 可視性 owner/revision はインメモリで遷移する (トグル UI の動作確認可)。
- シナリオ切替 (Prism 互換サブセット):
  - `Prefer: example=<name>` ヘッダ、または `?__example=<name>`
  - `?__status=<code>` で宣言済みステータスを強制 (202 / 404 / 409 など)
  - 例: `curl 'http://127.0.0.1:4010/api/v1/tree?__status=202' -D -`
- `Retry-After: 2` (202)、`ETag: "mock-etag"` (304) 付き。

## 2. PWA をモックに向ける

```bash
./scripts/api/mock.sh --port 4010 &
# dev サーバとは別タブで PWA を開く (shell は dev/実サーバの静的配信でよい):
open 'http://localhost:8080/?api_base=http://127.0.0.1:4010'
```

`?api_base=` は `app/static/api/v1.js` (`BookqApi.base`) と
`app/static/offline-core.js` (image/thumb/view) の両方を re-base する
(既定は空文字 = same-origin、通常動作は不変)。

Playwright ではサーバ実装を完全に迂回できる
(`tests/test_spec_examples_mock.py` が実例):

```python
from tests.spec_examples import install_spec_routes
install_spec_routes(page, scenario={"getTree": [(202, "index_building"), (200, "default")]})
```

## 3. iOS をモックに向ける

`-serverURL` は既存の仕組み (docs/ios-app.md) をそのまま使う:

```bash
xcrun simctl launch <udid> dev.bookq.reader -serverURL http://localhost:4010
```

単体テストは `ios/BookqTests/SpecExamples.swift` 経由で bundle を読む:

```swift
StubURLProtocol.handler = { _ in StubURLProtocol.specExample("getTree") }
```

## 4. WS モック (dual-reader, #210 と調整)

```bash
./scripts/api/mock.sh ws list            # golden case 一覧 (#210)
./scripts/api/mock.sh ws show <case-id>  # フレーム列の確認
./scripts/api/mock.sh ws --port 4011     # HELLO/CREATE/JOIN/APPROVE/PING 応答
```

`ws_mock.py serve` は UI 配線用の最小実装 (決定的 ids: session
`sess-mock-01`、code `123456`)。プロトコル準拠の検証は
`scripts/reader-sync/verify*.py` + `api/reader-sync-cases.json` が正本
(モックは conformance を代替しない)。

## 5. 新規 endpoint を spec 先行で追加する

1. `api/openapi.v1.yaml` に operation + `examples:` (正常系 + 代表異常系) を書く
2. `python3 scripts/api/extract-examples.py` で `api/dist/examples.json` を再生成 (commit する)
3. `python3 scripts/api/validate-examples.py` で schema 適合を確認 (CI でも実行)
4. PWA/iOS テストは上記ヘルパ経由で bundle を消費する (手書き fixture を増やさない)
5. `npx @stoplight/spectral-cli lint --ruleset .spectral.yaml api/openapi.v1.yaml` も通す
