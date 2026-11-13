//! The `auth` gate — OAuth + native-token surface (issue #400).
//!
//! Ports `app/auth.py`'s three auth routes and `app/auth_token.py`'s
//! `/api/v1/auth/token` under a single native id so the whole surface
//! crosses backends together (the callback mints the one-time codes the
//! token endpoint consumes — splitting them across processes 401s every
//! login).
//!
//! Google round-trip differences vs authlib, and why they are safe:
//!
//! * `authorize_access_token` verifies the `id_token` signature via
//!   Google's JWKS. This port fetches `/v1/userinfo` with the access
//!   token instead — the token endpoint exchange is client-secret
//!   authenticated and TLS-pinned, and only Google answers userinfo for
//!   a valid token, so origin assurance is equivalent without a JWKS
//!   round-trip.
//! * authlib binds the OAuth `state` to the browser through the signed
//!   `session` cookie (`_state_google_*` + `bookq_native_pkce` ride
//!   inside it). This port carries the same data in a dedicated
//!   `bookq_oauth` cookie signed with the same itsdangerous scheme —
//!   same browser binding, same tamper evidence, and it survives
//!   restarts exactly like the session cookie. A bare server-side state
//!   map would drop the browser binding (login CSRF).
//! * Code/refresh stores are in-process `Mutex<HashMap>`s — verbatim
//!   port of `app.state.auth_codes`/`refresh_tokens` (single worker,
//!   restart invalidates everything). Cutover consequence: enabling
//!   the `auth` gate invalidates every Python-issued refresh token —
//!   native clients must log in once more (session cookies and access
//!   JWTs survive via the shared `SECRET_KEY`).
//! * The tx cookie is deliberately tighter than authlib's: one
//!   `bookq_oauth` slot per browser (a second login supersedes the
//!   first) and a 600 s TTL (authlib's `_state_*` rides the 7-day
//!   session). Both shrink the attack surface; a stalled or
//!   superseded flow just retries.
//! * `nonce` is not sent on the authorize URL — authlib includes it to
//!   bind the `id_token` it verifies; this port never reads `id_token`
//!   (userinfo path above), so a nonce would bind nothing.
//!
//! Cookie attributes mirror `SessionMiddleware`: login emits
//! `bookq_oauth=<signed>; path=/; Max-Age=600; httponly; samesite=lax;
//! secure`, the callback mints `session=<signed>; path=/; Max-Age=604800;
//! httponly; samesite=lax; secure`, and clears emit `name=null; path=/;
//! expires=Thu, 01 Jan 1970 00:00:00 GMT; <flags>`. `Vary: Cookie` is
//! appended on every endpoint that touches the session upstream — all
//! three `/auth/*` routes do; the token endpoint does not.
//!
//! Config: `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET`/`BASE_URL`/
//! `SECRET_KEY` are all required when the `auth` gate is on — the gate
//! is atomic, so missing config falls the whole surface back to proxy
//! (warned at startup, fatal under `BOOKQ_RS_NATIVE_STRICT`). This keeps
//! `BOOKQ_RS_NATIVE=all` dev rigs working without Google creds.
//!
//! Any signed `session` cookie whose payload won't parse (starlette's
//! `json.loads` raising → HTTP 500 before the handler runs) proxies
//! upstream instead of being answered natively — the callers check
//! `SessionLookup::Corrupt`.

use std::collections::{HashMap, HashSet};
use std::sync::Mutex;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use axum::{
    body::Body,
    http::{header, HeaderMap, HeaderValue, Method, StatusCode},
    response::Response,
};
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
use hmac::{Hmac, Mac};
use rand::RngCore;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::auth::{self, SessionKey, SessionLookup};
use crate::state;

const AUTH_ENDPOINT: &str = "https://accounts.google.com/o/oauth2/v2/auth";
const TOKEN_ENDPOINT: &str = "https://oauth2.googleapis.com/token";
const USERINFO_ENDPOINT: &str = "https://openidconnect.googleapis.com/v1/userinfo";

/// Signed-cookie transaction store replacing the `_state_*` +
/// `bookq_native_pkce` session entries authlib maintains.
const OAUTH_COOKIE: &str = "bookq_oauth";
const OAUTH_TX_MAX_AGE_S: i64 = 600;

const ACCESS_TTL_S: i64 = 3600;
const CODE_TTL_S: f64 = 120.0;
const REFRESH_TTL_S: f64 = 30.0 * 86400.0;
/// `auth_token.py:_MAX_ENTRIES` — per-store bound.
const MAX_ENTRIES: usize = 10_000;

/// `exchange_token` principal — `name`/`picture` stay `Value` so a JSON
/// null survives into the session/JWT exactly like Python's `None`.
struct Principal {
    email: String,
    name: Value,
    picture: Value,
}

struct CodeEntry {
    principal: Principal,
    challenge: String,
    exp: f64,
}

struct RefreshEntry {
    principal: Principal,
    exp: f64,
}

trait HasExp {
    fn exp(&self) -> f64;
}
impl HasExp for CodeEntry {
    fn exp(&self) -> f64 {
        self.exp
    }
}
impl HasExp for RefreshEntry {
    fn exp(&self) -> f64 {
        self.exp
    }
}

/// `_evict_expired`: drop `exp <= now` first, then bound the store by
/// evicting the earliest-expiry entry.
fn evict_expired<V: HasExp>(store: &mut HashMap<String, V>) {
    let now = now_secs_f64();
    store.retain(|_, v| v.exp() > now);
    if store.len() > MAX_ENTRIES {
        if let Some(oldest) = store
            .iter()
            .min_by(|a, b| {
                a.1.exp()
                    .partial_cmp(&b.1.exp())
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
            .map(|(k, _)| k.clone())
        {
            store.remove(&oldest);
        }
    }
}

/// What `from_env` resolved for the `auth` gate.
pub enum Configured {
    /// `auth` is not in `BOOKQ_RS_NATIVE` — surface proxies.
    Disabled,
    /// Gate on + config complete — `/auth/*` and `/api/v1/auth/token`
    /// are served natively.
    Ready(AuthFlow),
    /// Gate on but required env is missing — whole surface stays
    /// proxied (atomic fallback; startup warns, STRICT fails).
    Missing(Vec<&'static str>),
}

/// Everything the native auth surface needs. Constructed once at
/// startup; `AppState.auth_flow` holds `Option<Arc<AuthFlow>>` where
/// `Some` already implies "gate on AND configured".
pub struct AuthFlow {
    http: reqwest::Client,
    client_id: String,
    client_secret: String,
    /// `BASE_URL.rstrip("/") + "/auth/callback"` — rs cannot derive the
    /// public URL from behind the sidecar, so BASE_URL is required.
    redirect_uri: String,
    session_key: SessionKey,
    secret_key: String,
    allowed_emails: std::sync::Arc<HashSet<String>>,
    /// `app.state.auth_codes` — one-time PKCE codes (`token_urlsafe(32)`).
    codes: Mutex<HashMap<String, CodeEntry>>,
    /// `app.state.refresh_tokens` — rotating refresh tokens
    /// (`token_urlsafe(48)`).
    refresh: Mutex<HashMap<String, RefreshEntry>>,
}

impl AuthFlow {
    pub fn from_env(
        native: &crate::Native,
        allowed_emails: std::sync::Arc<HashSet<String>>,
    ) -> Configured {
        if !native.enabled("auth") {
            return Configured::Disabled;
        }
        let mut missing = Vec::new();
        let mut get = |name: &'static str| match std::env::var(name) {
            Ok(v) if !v.is_empty() => Some(v),
            _ => {
                missing.push(name);
                None
            }
        };
        let client_id = get("GOOGLE_CLIENT_ID");
        let client_secret = get("GOOGLE_CLIENT_SECRET");
        let base_url = get("BASE_URL");
        let secret_key = get("SECRET_KEY");
        drop(get);
        let (Some(client_id), Some(client_secret), Some(base_url), Some(secret_key)) =
            (client_id, client_secret, base_url, secret_key)
        else {
            return Configured::Missing(missing);
        };
        // `session_key` returns None only on an empty secret, already
        // excluded by the non-empty check above.
        let Some(session_key) = auth::session_key(&secret_key) else {
            return Configured::Missing(vec!["SECRET_KEY"]);
        };
        Configured::Ready(Self {
            http: reqwest::Client::builder()
                .timeout(Duration::from_secs(15))
                .build()
                .expect("reqwest client"),
            client_id,
            client_secret,
            redirect_uri: format!("{}/auth/callback", base_url.trim_end_matches('/')),
            session_key,
            secret_key,
            allowed_emails,
            codes: Mutex::new(HashMap::new()),
            refresh: Mutex::new(HashMap::new()),
        })
    }

    /// `issue_auth_code` — mint a one-time code bound to the PKCE
    /// challenge the callback carried in its tx cookie.
    fn issue_code(&self, principal: Principal, challenge: &str) -> String {
        let mut codes = self.codes.lock().unwrap();
        evict_expired(&mut codes);
        let code = token_urlsafe(32);
        codes.insert(
            code.clone(),
            CodeEntry {
                principal,
                challenge: challenge.to_string(),
                exp: now_secs_f64() + CODE_TTL_S,
            },
        );
        code
    }

    /// `_issue_pair` — refresh token + HS256 access JWT, py's literal
    /// response dict order.
    fn issue_pair(&self, principal: Principal) -> Value {
        let mut tokens = self.refresh.lock().unwrap();
        evict_expired(&mut tokens);
        let refresh = token_urlsafe(48);
        tokens.insert(
            refresh.clone(),
            RefreshEntry {
                principal: Principal {
                    email: principal.email.clone(),
                    name: principal.name.clone(),
                    picture: principal.picture.clone(),
                },
                exp: now_secs_f64() + REFRESH_TTL_S,
            },
        );
        json!({
            "access_token": mint_access(&self.secret_key, &principal),
            "token_type": "Bearer",
            "expires_in": ACCESS_TTL_S,
            "refresh_token": refresh,
        })
    }

    /// `exchange_token` — verbatim port: consume code (PKCE-verified) or
    /// rotate a refresh token; return (status, payload).
    fn exchange(&self, body: &Value) -> (StatusCode, Value) {
        if !body.is_object() {
            return (
                StatusCode::BAD_REQUEST,
                json!({"detail": "expected JSON object"}),
            );
        }
        let principal = match body.get("grant_type").and_then(Value::as_str) {
            Some("authorization_code") => {
                let code = body.get("code").and_then(Value::as_str).unwrap_or("");
                let verifier = body
                    .get("code_verifier")
                    .and_then(Value::as_str)
                    .unwrap_or("");
                if code.is_empty() || verifier.is_empty() {
                    return (
                        StatusCode::BAD_REQUEST,
                        json!({"detail": "code and code_verifier required"}),
                    );
                }
                // Pop first: one-time even when the verifier is wrong —
                // guessing attempts get no second shot at the same code.
                let entry = self.codes.lock().unwrap().remove(code);
                let Some(entry) = entry.filter(|e| e.exp > now_secs_f64()) else {
                    return (
                        StatusCode::UNAUTHORIZED,
                        json!({"detail": "invalid or expired code"}),
                    );
                };
                if !ct_eq(&s256(verifier), &entry.challenge) {
                    return (
                        StatusCode::UNAUTHORIZED,
                        json!({"detail": "PKCE verification failed"}),
                    );
                }
                entry.principal
            }
            Some("refresh_token") => {
                let token = body
                    .get("refresh_token")
                    .and_then(Value::as_str)
                    .unwrap_or("");
                if token.is_empty() {
                    return (
                        StatusCode::BAD_REQUEST,
                        json!({"detail": "refresh_token required"}),
                    );
                }
                // Rotation: the presented token is consumed whether or
                // not it was still valid — a replayed old token dies.
                let entry = self.refresh.lock().unwrap().remove(token);
                let Some(entry) = entry.filter(|e| e.exp > now_secs_f64()) else {
                    return (
                        StatusCode::UNAUTHORIZED,
                        json!({"detail": "invalid or expired refresh_token"}),
                    );
                };
                entry.principal
            }
            _ => {
                return (
                    StatusCode::BAD_REQUEST,
                    json!({"detail": "unsupported grant_type"}),
                )
            }
        };
        (StatusCode::OK, self.issue_pair(principal))
    }
}

fn now_secs_f64() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

fn now_secs_i64() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

/// `secrets.token_urlsafe(n)` — n random bytes, urlsafe-b64 without
/// padding (n=32 → 43 chars).
fn token_urlsafe(nbytes: usize) -> String {
    let mut b = vec![0u8; nbytes];
    rand::rng().fill_bytes(&mut b);
    URL_SAFE_NO_PAD.encode(b)
}

/// `urllib.parse.quote_plus(s, safe="")` — alnum + `_.-~` pass through,
/// space → `+`, everything else `%XX` uppercase on the UTF-8 bytes.
fn quote_plus(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for &b in s.as_bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'_' | b'.' | b'-' | b'~' => {
                out.push(b as char)
            }
            b' ' => out.push('+'),
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

/// `_s256` — base64url(SHA256(verifier)) without padding. Non-ASCII
/// verifiers make Python's `.encode("ascii")` raise → `ok=False`; here
/// UTF-8 bytes hash the same way and simply mismatch, so both 401.
fn s256(verifier: &str) -> String {
    URL_SAFE_NO_PAD.encode(Sha256::digest(verifier.as_bytes()))
}

/// `hmac.compare_digest` — constant-time over equal lengths.
fn ct_eq(a: &str, b: &str) -> bool {
    let (a, b) = (a.as_bytes(), b.as_bytes());
    if a.len() != b.len() {
        return false;
    }
    a.iter().zip(b.iter()).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

/// `_mint_access` — HS256 compact JWT, claims `{sub,name,picture,iat,exp}`.
fn mint_access(secret: &str, principal: &Principal) -> String {
    let now = now_secs_i64();
    let header = URL_SAFE_NO_PAD.encode(br#"{"alg":"HS256","typ":"JWT"}"#);
    let claims = json!({
        "sub": principal.email,
        "name": principal.name,
        "picture": principal.picture,
        "iat": now,
        "exp": now + ACCESS_TTL_S,
    });
    let payload = URL_SAFE_NO_PAD.encode(serde_json::to_string(&claims).unwrap());
    let input = format!("{header}.{payload}");
    let mut mac = <Hmac<Sha256> as Mac>::new_from_slice(secret.as_bytes()).expect("hmac key");
    mac.update(input.as_bytes());
    let sig = URL_SAFE_NO_PAD.encode(mac.finalize().into_bytes());
    format!("{input}.{sig}")
}

// ── responses ───────────────────────────────────────────────────────────────

fn redirect(location: &str, status: StatusCode) -> Response {
    Response::builder()
        .status(status)
        .header(header::LOCATION, location)
        .body(Body::empty())
        .unwrap()
}

fn append_set_cookie(res: &mut Response, value: String) {
    res.headers_mut()
        .append(header::SET_COOKIE, HeaderValue::from_str(&value).unwrap());
}

fn append_vary_cookie(res: &mut Response) {
    res.headers_mut()
        .append(header::VARY, HeaderValue::from_static("Cookie"));
}

/// `SessionMiddleware` set-cookie for a non-empty modified session:
/// `session=<signed>; path=/; Max-Age=604800; httponly; samesite=lax;
/// secure` (https_only=True in main.py → `secure` always).
fn session_set_value(key: &SessionKey, payload: &[u8]) -> String {
    format!(
        "session={}; path=/; Max-Age={}; httponly; samesite=lax; secure",
        auth::sign_session(key, payload, now_secs_i64() as u64),
        auth::SESSION_MAX_AGE,
    )
}

/// Starlette's clear-cookie emission: `name=null; path=/;
/// expires=Thu, 01 Jan 1970 00:00:00 GMT; <security_flags>`.
fn cookie_clear_value(name: &str) -> String {
    format!(
        "{name}=null; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT; httponly; samesite=lax; secure"
    )
}

/// The `bookq_oauth` tx cookie — same signer/flags as `session`, shorter
/// lifetime (the consent round-trip should not linger for a week).
fn oauth_tx_set_value(key: &SessionKey, payload: &[u8]) -> String {
    format!(
        "{OAUTH_COOKIE}={}; path=/; Max-Age={OAUTH_TX_MAX_AGE_S}; httponly; samesite=lax; secure",
        auth::sign_session(key, payload, now_secs_i64() as u64),
    )
}

/// `RedirectResponse(url="/?error=access_denied", status_code=303)` —
/// authlib's OAuthError path (bad/absent state, missing code, `error`
/// param, token-exchange failure all land here upstream). The tx cookie
/// is cleared so a rejected attempt can't be replayed.
fn access_denied() -> Response {
    access_denied_with(true)
}

/// `clear_tx=false` mirrors upstream's non-GET path: authlib rejects
/// before touching the session, so the pending tx survives and a GET
/// retry still completes.
fn access_denied_with(clear_tx: bool) -> Response {
    let mut res = redirect("/?error=access_denied", StatusCode::SEE_OTHER);
    if clear_tx {
        append_set_cookie(&mut res, cookie_clear_value(OAUTH_COOKIE));
    }
    append_vary_cookie(&mut res);
    res
}

// ── handlers ────────────────────────────────────────────────────────────────

/// `_native_login_params` → `Some` for `?client=native`, `Err` on the
/// three 400s, `Ok(None)` for a browser login.
#[derive(Debug)]
struct NativeParams {
    code_challenge: String,
    state: String,
}

fn native_params(query: &HashMap<String, String>) -> Result<Option<NativeParams>, Response> {
    if query.get("client").map(String::as_str) != Some("native") {
        return Ok(None);
    }
    let challenge = query.get("code_challenge").cloned().unwrap_or_default();
    let method = query
        .get("code_challenge_method")
        .cloned()
        .unwrap_or_else(|| "S256".into());
    let client_state = query.get("state").cloned().unwrap_or_default();
    if !(43..=128).contains(&challenge.chars().count()) {
        return Err(state::detail(
            StatusCode::BAD_REQUEST,
            "invalid code_challenge",
        ));
    }
    if method != "S256" {
        return Err(state::detail(
            StatusCode::BAD_REQUEST,
            "code_challenge_method must be S256",
        ));
    }
    if client_state.chars().count() > 256 {
        return Err(state::detail(StatusCode::BAD_REQUEST, "invalid state"));
    }
    Ok(Some(NativeParams {
        code_challenge: challenge,
        state: client_state,
    }))
}

/// `GET /auth/login` — validate native params (400s first), then 302 to
/// Google's consent screen with the tx state in `bookq_oauth`.
pub fn login(flow: &AuthFlow, query: &HashMap<String, String>) -> Response {
    let native = match native_params(query) {
        Ok(n) => n,
        Err(resp) => return resp,
    };
    let oauth_state = token_urlsafe(32);
    let tx = match &native {
        Some(n) => json!({
            "st": oauth_state,
            "n": {"cc": n.code_challenge, "s": n.state},
        }),
        None => json!({"st": oauth_state}),
    };
    let url = format!(
        "{AUTH_ENDPOINT}?response_type=code&client_id={}&redirect_uri={}&scope=openid+email+profile&state={}",
        quote_plus(&flow.client_id),
        quote_plus(&flow.redirect_uri),
        quote_plus(&oauth_state),
    );
    let mut res = redirect(&url, StatusCode::FOUND);
    append_set_cookie(
        &mut res,
        oauth_tx_set_value(
            &flow.session_key,
            serde_json::to_string(&tx).unwrap().as_bytes(),
        ),
    );
    append_vary_cookie(&mut res);
    res
}

/// `GET /auth/callback` — verify the tx cookie binds `?state=`, exchange
/// the code, fetch userinfo, then either mint `session` (browser) or
/// hand the native app a one-time `bookq://auth?code=` (PKCE).
///
/// `session_lookup` is the caller's already-computed resolution —
/// `User`/`NoUser` means a session was present so `session.clear()`
/// semantics emit the null cookie (403 path).
pub async fn callback(
    flow: &AuthFlow,
    method: &Method,
    headers: &HeaderMap,
    query: &HashMap<String, String>,
    session_lookup: &SessionLookup,
) -> Response {
    // authlib only processes GET — every other method falls into its
    // form branch and answers access_denied *without* consuming the tx
    // cookie or the code. HEAD reaching this route must not mint a
    // session.
    if *method != Method::GET {
        return access_denied_with(false);
    }
    // `error` param, missing `code`/`state`, or a tx mismatch all raise
    // OAuthError upstream → the same denial redirect.
    if query.contains_key("error") {
        return access_denied();
    }
    let (Some(code), Some(oauth_state)) = (query.get("code"), query.get("state")) else {
        return access_denied();
    };
    let tx = auth::cookie_value(headers, OAUTH_COOKIE)
        .and_then(|raw| {
            auth::unsign_session(&flow.session_key, &raw, OAUTH_TX_MAX_AGE_S, now_secs_i64())
        })
        .and_then(|payload| serde_json::from_slice::<Value>(&payload).ok());
    let tx_valid = tx
        .as_ref()
        .and_then(|t| t.get("st"))
        .and_then(Value::as_str)
        == Some(oauth_state.as_str());
    if !tx_valid {
        return access_denied();
    }
    let native = tx
        .as_ref()
        .and_then(|t| t.get("n"))
        .filter(|n| n.is_object())
        .map(|n| NativeParams {
            code_challenge: n
                .get("cc")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string(),
            state: n
                .get("s")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string(),
        });

    let token_resp = flow
        .http
        .post(TOKEN_ENDPOINT)
        .form(&[
            ("client_id", flow.client_id.as_str()),
            ("client_secret", flow.client_secret.as_str()),
            ("code", code.as_str()),
            ("grant_type", "authorization_code"),
            ("redirect_uri", flow.redirect_uri.as_str()),
        ])
        .send()
        .await;
    let access_token = match token_resp {
        Ok(r) if r.status().is_success() => r
            .json::<Value>()
            .await
            .ok()
            .and_then(|t| t.get("access_token").and_then(Value::as_str).map(str::to_string)),
        _ => None,
    };
    let Some(access_token) = access_token else {
        return access_denied();
    };
    let userinfo_resp = flow
        .http
        .get(USERINFO_ENDPOINT)
        .bearer_auth(&access_token)
        .send()
        .await;
    let userinfo = match userinfo_resp {
        Ok(r) if r.status().is_success() => r.json::<Value>().await.ok(),
        _ => None,
    };
    let Some(userinfo) = userinfo.filter(|u| u.is_object()) else {
        // authlib fills token["userinfo"] from verified claims; a
        // provider answer with no usable userinfo is upstream's 502.
        let mut res = state::detail(
            StatusCode::BAD_GATEWAY,
            "Auth provider error: no userinfo",
        );
        append_set_cookie(&mut res, cookie_clear_value(OAUTH_COOKIE));
        append_vary_cookie(&mut res);
        return res;
    };

    let email = userinfo
        .get("email")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_lowercase();
    if !flow.allowed_emails.contains(&email) {
        // `session.clear()` → the null cookie only when a session was
        // actually present (initial_session_was_empty == False).
        let mut res = state::detail(StatusCode::FORBIDDEN, "Email not allowed");
        if matches!(session_lookup, SessionLookup::User(_) | SessionLookup::NoUser) {
            append_set_cookie(&mut res, cookie_clear_value("session"));
        }
        append_set_cookie(&mut res, cookie_clear_value(OAUTH_COOKIE));
        append_vary_cookie(&mut res);
        return res;
    }

    let principal = Principal {
        email: email.clone(),
        // `.get(key, default)` parity: missing → default, null → null.
        name: userinfo
            .get("name")
            .cloned()
            .unwrap_or(Value::String(email.clone())),
        picture: userinfo
            .get("picture")
            .cloned()
            .unwrap_or(Value::String(String::new())),
    };

    if let Some(n) = native {
        let code = flow.issue_code(principal, &n.code_challenge);
        // `urlencode({"code": c, "state": s})` — state omitted on "".
        let mut loc = format!("bookq://auth?code={code}");
        if !n.state.is_empty() {
            loc.push_str(&format!("&state={}", quote_plus(&n.state)));
        }
        let mut res = redirect(&loc, StatusCode::SEE_OTHER);
        append_set_cookie(&mut res, cookie_clear_value(OAUTH_COOKIE));
        append_vary_cookie(&mut res);
        return res;
    }

    let payload = json!({"user": {
        "email": principal.email,
        "name": principal.name,
        "picture": principal.picture,
    }});
    let mut res = redirect("/", StatusCode::SEE_OTHER);
    append_set_cookie(
        &mut res,
        session_set_value(&flow.session_key, serde_json::to_string(&payload).unwrap().as_bytes()),
    );
    append_set_cookie(&mut res, cookie_clear_value(OAUTH_COOKIE));
    append_vary_cookie(&mut res);
    res
}

/// `GET /auth/logout` — `session.clear()` + 303 `/`. The null cookie is
/// emitted only when a session existed (starlette's
/// `initial_session_was_empty` rule); `bookq_oauth` clears too since a
/// session clear also wipes pending authorize data upstream.
pub fn logout(_flow: &AuthFlow, session_lookup: &SessionLookup) -> Response {
    let mut res = redirect("/", StatusCode::SEE_OTHER);
    if matches!(session_lookup, SessionLookup::User(_) | SessionLookup::NoUser) {
        append_set_cookie(&mut res, cookie_clear_value("session"));
    }
    append_set_cookie(&mut res, cookie_clear_value(OAUTH_COOKIE));
    append_vary_cookie(&mut res);
    res
}

/// `POST /api/v1/auth/token` — public bootstrap; `exchange_token` +
/// `Cache-Control: no-store` on the JSONResponse.
pub fn token(flow: &AuthFlow, body_bytes: &[u8]) -> Response {
    let body: Value = match serde_json::from_slice(body_bytes) {
        Ok(v) => v,
        Err(_) => {
            return no_store(state::detail(StatusCode::BAD_REQUEST, "invalid JSON body"));
        }
    };
    let (status, payload) = flow.exchange(&body);
    let res = Response::builder()
        .status(status)
        .header(header::CONTENT_TYPE, "application/json")
        .body(Body::from(serde_json::to_string(&payload).unwrap()))
        .unwrap();
    no_store(res)
}

fn no_store(mut res: Response) -> Response {
    res.headers_mut().insert(
        header::CACHE_CONTROL,
        HeaderValue::from_static("no-store"),
    );
    res
}

#[cfg(test)]
mod tests {
    use super::*;

    fn flow() -> AuthFlow {
        let key = auth::session_key("test-secret").unwrap();
        AuthFlow {
            http: reqwest::Client::new(),
            client_id: "cid".into(),
            client_secret: "csec".into(),
            redirect_uri: "https://bookq.example/auth/callback".into(),
            session_key: key,
            secret_key: "test-secret".into(),
            allowed_emails: std::sync::Arc::new(["a@example.com".to_string()].into_iter().collect()),
            codes: Mutex::new(HashMap::new()),
            refresh: Mutex::new(HashMap::new()),
        }
    }

    fn v64() -> String {
        "v".repeat(64)
    }

    #[test]
    fn token_urlsafe_shape() {
        let t = token_urlsafe(32);
        assert_eq!(t.len(), 43); // 32 bytes → 43 chars no-pad
        assert!(t.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_'));
        assert_ne!(token_urlsafe(32), token_urlsafe(32));
    }

    #[test]
    fn quote_plus_matches_python() {
        assert_eq!(quote_plus("a b"), "a+b");
        assert_eq!(quote_plus("x&y=z"), "x%26y%3Dz");
        assert_eq!(quote_plus("_.-~"), "_.-~");
        // UTF-8 bytes encode per byte, uppercase hex.
        assert_eq!(quote_plus("あ"), "%E3%81%82");
    }

    #[test]
    fn code_exchange_roundtrip_and_one_time() {
        let f = flow();
        let code = f.issue_code(
            Principal {
                email: "a@example.com".into(),
                name: json!("A"),
                picture: json!(""),
            },
            &s256(&v64()),
        );
        let body = json!({
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": v64(),
        });
        let (status, pair) = f.exchange(&body);
        assert_eq!(status, StatusCode::OK);
        assert_eq!(pair["token_type"], "Bearer");
        assert_eq!(pair["expires_in"], 3600);
        assert!(pair["access_token"].as_str().unwrap().len() > 50);
        // replay → consumed
        let (status, _) = f.exchange(&body);
        assert_eq!(status, StatusCode::UNAUTHORIZED);
    }

    #[test]
    fn wrong_verifier_consumes_code() {
        let f = flow();
        let code = f.issue_code(
            Principal {
                email: "a@example.com".into(),
                name: json!("A"),
                picture: json!(""),
            },
            &s256("right"),
        );
        let bad = json!({
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": "wrong",
        });
        assert_eq!(f.exchange(&bad).0, StatusCode::UNAUTHORIZED);
        let good = json!({
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": "right",
        });
        assert_eq!(f.exchange(&good).0, StatusCode::UNAUTHORIZED);
    }

    #[test]
    fn refresh_rotation_kills_old_token() {
        let f = flow();
        let code = f.issue_code(
            Principal {
                email: "a@example.com".into(),
                name: json!("A"),
                picture: json!(""),
            },
            &s256(&v64()),
        );
        let first = json!({
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": v64(),
        });
        let (_, pair) = f.exchange(&first);
        let rt = pair["refresh_token"].as_str().unwrap().to_string();
        let second = json!({"grant_type": "refresh_token", "refresh_token": rt});
        let (status, pair2) = f.exchange(&second);
        assert_eq!(status, StatusCode::OK);
        assert_ne!(pair2["refresh_token"].as_str().unwrap(), rt);
        // replayed old token dies
        assert_eq!(f.exchange(&second).0, StatusCode::UNAUTHORIZED);
    }

    #[test]
    fn grant_and_field_validation() {
        let f = flow();
        assert_eq!(
            f.exchange(&json!("not an object")).0,
            StatusCode::BAD_REQUEST
        );
        assert_eq!(f.exchange(&json!({"grant_type": "bogus"})).0, StatusCode::BAD_REQUEST);
        assert_eq!(
            f.exchange(&json!({"grant_type": "authorization_code", "code": "x"})).0,
            StatusCode::BAD_REQUEST
        );
        assert_eq!(
            f.exchange(&json!({"grant_type": "refresh_token"})).0,
            StatusCode::BAD_REQUEST
        );
        // expired code → 401
        let code = f.issue_code(
            Principal {
                email: "a@example.com".into(),
                name: json!("A"),
                picture: json!(""),
            },
            &s256(&v64()),
        );
        f.codes.lock().unwrap().get_mut(&code).unwrap().exp = 0.0;
        assert_eq!(
            f.exchange(&json!({
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": v64(),
            }))
            .0,
            StatusCode::UNAUTHORIZED
        );
    }

    #[test]
    fn minted_access_passes_bearer_validation() {
        let token = mint_access(
            "test-secret",
            &Principal {
                email: "a@example.com".into(),
                name: json!("A"),
                picture: json!(""),
            },
        );
        let allowlist: HashSet<String> = ["a@example.com".to_string()].into_iter().collect();
        let id = auth::validate_bearer(&token, "test-secret", &allowlist).unwrap();
        assert_eq!(id.email, "a@example.com");
    }

    #[test]
    fn native_params_validation_matrix() {
        let base: HashMap<String, String> = [
            ("client".into(), "native".into()),
            ("code_challenge".into(), "c".repeat(50)),
        ]
        .into_iter()
        .collect();
        assert!(native_params(&base).unwrap().is_some());
        // not native → None
        assert!(native_params(&HashMap::new()).unwrap().is_none());
        // short challenge → 400
        let mut q = base.clone();
        q.insert("code_challenge".into(), "c".repeat(42));
        assert!(native_params(&q).unwrap_err().status() == StatusCode::BAD_REQUEST);
        // plain method → 400
        let mut q = base.clone();
        q.insert("code_challenge_method".into(), "plain".into());
        let resp = native_params(&q).unwrap_err();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
        // oversized state → 400
        let mut q = base.clone();
        q.insert("state".into(), "s".repeat(257));
        assert!(native_params(&q).unwrap_err().status() == StatusCode::BAD_REQUEST);
    }

    #[test]
    fn login_redirects_and_sets_tx_cookie() {
        let f = flow();
        let res = login(&f, &HashMap::new());
        assert_eq!(res.status(), StatusCode::FOUND);
        let loc = res.headers().get(header::LOCATION).unwrap().to_str().unwrap();
        assert!(loc.starts_with(AUTH_ENDPOINT));
        assert!(loc.contains("redirect_uri=https%3A%2F%2Fbookq.example%2Fauth%2Fcallback"));
        assert!(loc.contains("scope=openid+email+profile"));
        let cookie = res
            .headers()
            .get(header::SET_COOKIE)
            .unwrap()
            .to_str()
            .unwrap()
            .to_string();
        assert!(cookie.starts_with("bookq_oauth="));
        assert!(cookie.contains("Max-Age=600"));
        // the tx state must round-trip through unsign_session
        let raw = cookie["bookq_oauth=".len()..].split(';').next().unwrap();
        let payload = auth::unsign_session(&f.session_key, raw, OAUTH_TX_MAX_AGE_S, now_secs_i64())
            .expect("tx cookie verifies");
        let tx: Value = serde_json::from_slice(&payload).unwrap();
        let st = tx["st"].as_str().unwrap();
        assert!(loc.contains(&format!("state={st}")));
    }

    #[test]
    fn session_cookie_mint_verifies() {
        let f = flow();
        let payload = br#"{"user":{"email":"a@example.com","name":"A","picture":""}}"#;
        let val = session_set_value(&f.session_key, payload);
        let raw = val["session=".len()..].split(';').next().unwrap();
        let back = auth::unsign_session(&f.session_key, raw, auth::SESSION_MAX_AGE, now_secs_i64())
            .expect("verifies");
        assert_eq!(&back[..], payload);
        assert!(val.contains("httponly; samesite=lax; secure"));
    }

    #[test]
    fn logout_clears_only_when_session_present() {
        let f = flow();
        let res = logout(&f, &SessionLookup::Absent);
        assert_eq!(res.status(), StatusCode::SEE_OTHER);
        let cookies: Vec<_> = res
            .headers()
            .get_all(header::SET_COOKIE)
            .iter()
            .map(|v| v.to_str().unwrap().to_string())
            .collect();
        assert_eq!(cookies.len(), 1); // bookq_oauth clear only
        assert!(cookies[0].starts_with("bookq_oauth=null"));

        let res = logout(
            &f,
            &SessionLookup::User(json!({"email": "a@example.com"})),
        );
        let cookies: Vec<_> = res
            .headers()
            .get_all(header::SET_COOKIE)
            .iter()
            .map(|v| v.to_str().unwrap().to_string())
            .collect();
        assert_eq!(cookies.len(), 2);
        assert!(cookies[0].starts_with("session=null"));
    }

    #[test]
    fn token_endpoint_response_shape() {
        let f = flow();
        let res = token(&f, b"not json");
        assert_eq!(res.status(), StatusCode::BAD_REQUEST);
        assert_eq!(
            res.headers().get(header::CACHE_CONTROL).unwrap(),
            "no-store"
        );
    }

    #[tokio::test]
    async fn callback_non_get_denies_without_touching_tx() {
        let f = flow();
        // A HEAD callback upstream is authlib's form branch: 303
        // access_denied, tx cookie survives, nothing is minted.
        let query = HashMap::from([
            ("code".to_string(), "any".to_string()),
            ("state".to_string(), "any".to_string()),
        ]);
        let res = callback(
            &f,
            &Method::HEAD,
            &HeaderMap::new(),
            &query,
            &SessionLookup::Absent,
        )
        .await;
        assert_eq!(res.status(), StatusCode::SEE_OTHER);
        assert_eq!(
            res.headers().get(header::LOCATION).unwrap(),
            "/?error=access_denied"
        );
        // No bookq_oauth clear — the in-flight GET flow stays retriable.
        assert!(res.headers().get_all(header::SET_COOKIE).iter().next().is_none());
    }
}
