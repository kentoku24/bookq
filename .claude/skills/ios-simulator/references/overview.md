# iOS Simulator — Overview

bookq は PWA。`simctl install` は不要。Safari で dev URL を `xcrun simctl openurl booted <dev-url>` で開く。

Simulator は host network share。host が Tailnet に参加していれば `https://dev-*.ts.net` にそのまま到達できる。ホストポートは公開しない。唯一の到達経路は `tailscale serve` (HTTPS :443 → viewer:8080)。

- `<your-tailnet>` は tailnet 名の例。各自の `DEV_TS_DOMAIN` / `.env` `BASE_URL` の tailnet ドメインに読み替えること（正準は `docs/deployment.md`）。
- `TS_AUTHKEY` と `BASE_URL` から `dev.sh` の `tailnet_domain()` が `https://dev-<name>.<tailnet>.ts.net` を組み立てる。`docker-compose.dev.yml:62` の `tailscale` サービス (`hostname: dev-${DEV_NAME}`) が viewer:8080 を :443 で配信する。

詳細な host 共有の仕組みは `docs/deployment.md` を参照。

## Local mode（tailscale 無しの host）

host に Tailscale が無い（`tailscale status` が失敗する、`.env` が無い）と、`./dev.sh up demo --fixture` は `tailnet_domain()` で
「tailnet ドメインを導出できません。DEV_TS_DOMAIN か .env の BASE_URL を確認してください。」で止まる。この場合は tailnet を諦めて
**local mode** を使う（2026-09-20 に実際にこの経路で確認済み）。

```bash
./dev.sh up demo --local --fixture          # sidecar 無し、viewer だけ。http://127.0.0.1:8080 に bind
./scripts/open-simulator.sh http://127.0.0.1:8080
./dev.sh down demo
```

- `TS_AUTHKEY` / `DEV_TS_DOMAIN` / `BASE_URL` は不要。`docker-compose.local.yml` を `docker-compose.dev.yml` に重ねて viewer だけ起動する。
- Simulator は host network を共有するので `127.0.0.1` にそのまま届く。`http://127.0.0.1` は Secure Context なので Service Worker / オフライン QA も動く。
  `--lan-allow` の `http://<lan-ip>` は Secure Context ではないため SW が登録されない（`README.md: Secure Context 注意`）。helper もこの URL は拒否する。
- 既定 port は 8080。2つ目の同時 local 環境は `--port N` が必須（衝突はビルド前に失敗する）。
- 初回表示が「0 folders」でも異常ではない。初回の `/api/folders` スキャンが遅いだけなので、まず reload する。
  それでも 0 なら `docker inspect bookq-dev-viewer-dev-XX --format '{{json .Mounts}}'` で `/tmp/bookq-fixture-<name>/downloads → /app/downloads (ro)` を確認する。
- Simulator に「Booted」の iPhone 17 が既にあれば helper の boot は skip される。複数 Booted 時は `booted` エイリアスの解決先が不定なので `xcrun simctl list devices booted` で UDID を確認する。

正準は `README.md: 3b. ローカルモード` と `docs/deployment.md: ローカルモード`。
