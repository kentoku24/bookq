//! viewer-rs — Rust backend for bookq (issue #163, parallel-backend track).
//!
//! The sole production backend (#409, #503): every route is served natively
//! (#532 retired the strangler proxy — there is no `BOOKQ_UPSTREAM`
//! fall-through anymore, so a misconfigured upstream can no longer receive
//! unauthenticated requests).

use axum::{
    body::Body,
    extract::{FromRequest, FromRequestParts, Path as AxumPath, Query, Request, State},
    http::{header, HeaderName, HeaderValue, StatusCode},
    response::{IntoResponse, Response},
    routing::{any, get},
    Router,
};
use percent_encoding::percent_decode_str;
use std::collections::HashMap;
use std::net::SocketAddr;
use std::path::PathBuf;

// serve/tree/folders hold the contract-independent core (archive listing,
// path containment, tree assembly, index cache) that the v1 envelopes
// are wired onto. v0 routes are deprecated and answer 501 via the fallback.
mod admin;
mod admin_ui;
mod auth;
#[cfg(test)]
mod conformance_vectors;
mod folders;
mod guest;
// #573: `--healthcheck` — the image has no shell for a compose probe.
mod healthcheck;
mod library;
mod oauth;
mod pair_report;
mod reader_sync;
mod search;
mod serve;
mod shell;
mod state;
mod thumbs;
mod tree;
mod users;
mod users_cli;

#[derive(Clone)]
pub(crate) struct AppState {
    /// Downloads root, resolved once (symlinks collapsed like the Python side).
    pub(crate) library: PathBuf,
    /// `BOOKQ_TEST_MODE` — mock-user posture for dev/test.
    pub(crate) test_mode: bool,
    /// `/api/folders` index cache (`metadata_index_cache`).
    pub(crate) index: std::sync::Arc<folders::IndexState>,
    /// M4+375: per-user reading positions in the shared user-state SQLite
    /// store (`ReadingPositionStore`).
    pub(crate) position_store: std::sync::Arc<state::PositionStore>,
    /// M4: shared user-state SQLite store (`FolderVisibilityStore`).
    pub(crate) visibility: std::sync::Arc<state::VisibilityStore>,
    /// #608: authoritative membership/role store on the user-state DB. Admins
    /// are written only by the offline `viewer-rs users` CLI; members can also
    /// be added/disabled/re-enabled from the `/admin` UI.
    pub(crate) users: std::sync::Arc<users::UserStore>,
    /// #608: in-memory active members for the hot auth path (1s refresh).
    pub(crate) directory: std::sync::Arc<users::UserDirectory>,
    /// #400: the whole auth surface (`/auth/*` + `/api/v1/auth/token`).
    /// `Some` iff the OAuth config is complete, else the surface answers 501.
    pub(crate) auth_flow: Option<std::sync::Arc<oauth::AuthFlow>>,
    /// `SECRET_KEY` — Bearer JWT verify + visibility owner HMAC.
    pub(crate) secret_key: String,
    /// Session-cookie signing key derived once from `secret_key` (`None`
    /// on empty — the fail-closed guard shared by HTTP and WS auth).
    pub(crate) session_key: Option<auth::SessionKey>,
    /// M7: shared thumb-cache root (`BOOKQ_THUMB_CACHE_DIR`).
    pub(crate) thumb_cache: PathBuf,
    /// `BOOKQ_GIT_SHA` / `BOOKQ_BUILD_DATE` — `/api/v1/version` fields.
    pub(crate) git_sha: String,
    pub(crate) build_date: String,
    /// M8: dual-reader session registry (`app.state.reader_sync`).
    pub(crate) registry: std::sync::Arc<reader_sync::Registry>,
    /// #401: shell/static root (`BOOKQ_WEB_DIR`) holding `static/`,
    /// `templates/`, `perf/`.
    pub(crate) web_dir: PathBuf,
    /// TEST_MODE-only pair-report store (`app/api_v1.py::_PAIR_REPORTS`).
    pub(crate) pair_reports: std::sync::Arc<pair_report::PairReports>,
    /// `<library>/.bookq-uploads` — `Some` when the probe at startup
    /// proved the library writable, else `None` and `/api/v1/library/*`
    /// answers 503 (read-only deploys stay fully functional).
    pub(crate) upload_root: Option<PathBuf>,
    /// Serializes the dedupe+rename placement critical section — without it
    /// two same-name uploads pick the same free slot and `rename` clobbers
    /// the first (Unix rename replaces the destination).
    pub(crate) place_lock: std::sync::Arc<std::sync::Mutex<()>>,
    /// Serializes upload-session mutations (append/finalize/cancel) —
    /// the load→verify→write cycle races otherwise and corrupts the
    /// blob/offset pair.
    pub(crate) session_lock: std::sync::Arc<tokio::sync::Mutex<()>>,
    /// #631: operator-declared extra CSP origins (`BOOKQ_CSP_EXTRA_ORIGINS`)
    /// appended to `img-src`/`connect-src` on HTML responses — exists for
    /// the `?api_base=` mock-dev flow (#237). Empty in production.
    pub(crate) csp_extra: Vec<String>,
    /// Per-IP fixed-window limiter for `POST /api/v1/guest/redeem`.
    pub(crate) guest_redeem_limit: guest::RedeemRateLimit,
}

/// A local 405 carries the RFC 9110 `Allow` header.
pub(crate) fn method_not_allowed(allow: &'static str) -> Response {
    let mut r = state::detail(StatusCode::METHOD_NOT_ALLOWED, "method not allowed");
    if let Ok(v) = HeaderValue::from_str(allow) {
        r.headers_mut().insert(header::ALLOW, v);
    }
    r
}

/// Public, per-user-free asset paths — exempt from both `Vary: Cookie`
/// (starlette parity: these routes never touch `request.session`) and
/// the guest `no-store`/`x-bookq-guest` marking.
fn session_free_path(p: &str) -> bool {
    p == "/sw.js" || p == "/favicon.ico" || p == "/static" || p.starts_with("/static/")
}

/// Unverifiable caller (no session, no/invalid Bearer) answers 401 locally.
fn unauthenticated() -> Response {
    state::detail(StatusCode::UNAUTHORIZED, "authentication required")
}

impl AppState {
    /// The full caller-resolution chain (session → Bearer → guest) with
    /// this app's auth inputs — one place the seven call sites share.
    fn resolve(&self, headers: &axum::http::HeaderMap) -> auth::Caller {
        auth::resolve(
            headers,
            self.test_mode,
            &self.secret_key,
            self.session_key.as_ref(),
            &self.directory,
            self.registry.guest(),
        )
    }

    fn resolve_ws_user(
        &self,
        headers: &axum::http::HeaderMap,
    ) -> Option<(serde_json::Value, bool)> {
        auth::resolve_ws_user(
            headers,
            self.test_mode,
            &self.secret_key,
            self.session_key.as_ref(),
            &self.directory,
            self.registry.guest(),
        )
    }
}

/// Resolve the caller for a route: `None` means unauthenticated. A guest
/// grant resolves to the leader's identity for reads — writes are stopped
/// earlier by `guest_write_guard`.
pub(crate) fn caller(st: &AppState, headers: &axum::http::HeaderMap) -> Option<auth::Identity> {
    match st.resolve(headers) {
        auth::Caller::User(u) | auth::Caller::Guest(u) => Some(u),
        auth::Caller::Delegate => None,
    }
}

/// `Vary` already lists `Cookie` (case-insensitive, comma-separated).
fn vary_lists_cookie(h: &axum::http::HeaderMap) -> bool {
    h.get_all(header::VARY).iter().any(|v| {
        v.to_str()
            .unwrap_or("")
            .split(',')
            .any(|p| p.trim().eq_ignore_ascii_case("cookie"))
    })
}

/// `Vary: Cookie` parity with starlette's SessionMiddleware: it appends
/// the header to every response when the request's session cookie loaded
/// (valid signature, any payload). A shared HTTP cache must still key on
/// the cookie or one user's cached page could answer another's — this
/// matters most for the per-user surfaces (positions, visibility, search).
/// `vary_lists_cookie` dedupes a Vary the handler already set.
async fn vary_session_cookie(
    State(st): State<AppState>,
    req: Request<Body>,
    next: axum::middleware::Next,
) -> Response {
    let signed = !matches!(
        auth::session_lookup(req.headers(), st.session_key.as_ref()),
        auth::SessionLookup::Absent
    ) || auth::cookie_value(req.headers(), "bookq_guest").is_some();
    // starlette only emits `Vary: Cookie` when the route touches
    // `request.session`. The public static/shell assets never do, so a
    // signed-in browser's static fetches carry no Vary —
    // mirror that or every such response would wrongly advertise one.
    let session_free = session_free_path(req.uri().path());
    let mut res = next.run(req).await;
    if signed && !session_free && !vary_lists_cookie(res.headers()) {
        res.headers_mut()
            .append(header::VARY, HeaderValue::from_static("Cookie"));
    }
    res
}

/// Read-only enforcement for `bookq_guest` callers. A guest grant resolves
/// to the pair leader's identity so every read works, but writes, the
/// whole `/admin` surface, and `/api/v1/library/*` answer 403 before the
/// handler runs — pair sync is WebSocket and unaffected. The library
/// surface is denied outright (not just its writes): its GETs have side
/// effects (upload slots/finalize state) a read-only caller must not
/// trigger. `/api/v1/guest/redeem` is exempt: it is how the cookie is
/// minted and refreshed. Resolution goes through `auth::resolve` itself
/// so session→Bearer→guest precedence (including the expired-session-
/// cookie edge) can't drift from the handlers'.
async fn guest_write_guard(
    State(st): State<AppState>,
    req: Request<Body>,
    next: axum::middleware::Next,
) -> Response {
    let path = req.uri().path();
    let safe_method = matches!(
        *req.method(),
        axum::http::Method::GET | axum::http::Method::HEAD | axum::http::Method::OPTIONS
    );
    let admin_path = path == "/admin" || path.starts_with("/admin/");
    let library_path = path == "/api/v1/library" || path.starts_with("/api/v1/library/");
    // Reading positions resolve to the leader's identity for a guest —
    // denying even safe methods keeps their private history off the wire.
    // `== x || starts_with(x + "/")` like the neighbors: any future
    // sub-route (per-work position, history) is private for the same
    // reason and must not slip past the guard unmarked.
    let positions_path = path == "/api/v1/reading-positions"
        || path.starts_with("/api/v1/reading-positions/");
    if (safe_method && !admin_path && !library_path && !positions_path)
        || path == "/api/v1/guest/redeem"
    {
        return next.run(req).await;
    }
    if matches!(
        st.resolve(req.headers()),
        auth::Caller::Guest(_)
    ) {
        return state::detail(StatusCode::FORBIDDEN, "guest access is read-only");
    }
    next.run(req).await
}

/// `x-bookq-guest: 1` on every response a `bookq_guest` caller receives —
/// the Service Worker and page-side writers refuse to persist marked
/// responses, so a revoked grant can't be re-served out of the device's
/// own caches. `Cache-Control: private, no-store` closes the other half
/// of the hole: the browser's HTTP cache ignores the custom header and
/// would happily re-serve 200/206/304 content past revocation.
///
/// A dead grant resolves to anonymous, not Guest — so 401/403s to a
/// request that carried ONLY a `bookq_guest` cookie (no `session`
/// cookie) get the marker too: that is exactly the revoked-guest case
/// the SW evicts on, while a member's expired-session 401 (no guest
/// cookie presented) stays unmarked and keeps downloaded pages.
async fn guest_response_marker(
    State(st): State<AppState>,
    req: Request<Body>,
    next: axum::middleware::Next,
) -> Response {
    let (is_guest, dead_guest_only) = match st.resolve(req.headers()) {
        auth::Caller::Guest(_) => (true, false),
        auth::Caller::User(_) => (false, false),
        // A guest cookie that resolved to anonymous is a dead grant —
        // base this on the resolution outcome, not cookie presence, so a
        // stale `session` cookie can't mask the revoked-guest eviction.
        auth::Caller::Delegate => (
            false,
            auth::cookie_value(req.headers(), "bookq_guest").is_some(),
        ),
    };
    // Public, per-user-free assets stay cacheable for guests — same set
    // `vary_session_cookie` exempts. Everything else gets no-store.
    let session_free = session_free_path(req.uri().path());
    let mut res = next.run(req).await;
    let mark = is_guest
        || (dead_guest_only
            && matches!(
                res.status(),
                StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN
            ));
    if mark {
        let h = res.headers_mut();
        h.insert(
            HeaderName::from_static("x-bookq-guest"),
            HeaderValue::from_static("1"),
        );
    }
    if is_guest && !session_free {
        res.headers_mut().insert(
            header::CACHE_CONTROL,
            HeaderValue::from_static("private, no-store"),
        );
    }
    res
}

/// CSP for the HTML surfaces (`/`, `/static/*.html`). `unsafe-inline` is
/// required by index.html's per-request boot snippets and the `onerror=`
/// handlers JS-built tiles emit — a nonce-based policy is follow-up work.
/// Everything else stays locked down: no objects, no base-uri hijack, no
/// framing, same-origin forms only. `script-src` is injected per-response
/// (see `app_csp`) so TEST_MODE can add `'unsafe-eval'`.
const APP_CSP_BASE: &str = "default-src 'self'; \
    style-src 'self' 'unsafe-inline'; \
    font-src 'self'; \
    worker-src 'self'; \
    manifest-src 'self'; \
    object-src 'none'; \
    base-uri 'none'; \
    form-action 'self'; \
    frame-ancestors 'none'";

/// The app CSP with operator-declared extra origins (`img-src` +
/// `connect-src`) appended — `BOOKQ_CSP_EXTRA_ORIGINS` exists for the
/// `?api_base=` mock-dev flow (#237), which fetches API + images
/// cross-origin. Request params are never reflected into the policy.
/// `test_mode` adds `'unsafe-eval'` to `script-src`: TEST_MODE is already
/// the auth-bypassing dev posture, and Playwright's `evaluate` /
/// `wait_for_function` drive pages via string eval, which CSP otherwise
/// blocks. The app itself never evals, so every other directive — and the
/// production policy — stays strict.
fn app_csp(extra: &[String], test_mode: bool) -> String {
    let script_src = if test_mode {
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'"
    } else {
        "script-src 'self' 'unsafe-inline'"
    };
    let mut img = String::from("img-src 'self' data: blob:");
    let mut conn = String::from("connect-src 'self' ws: wss:");
    for o in extra {
        img.push(' ');
        img.push_str(o);
        conn.push(' ');
        conn.push_str(o);
    }
    format!("{APP_CSP_BASE}; {script_src}; {img}; {conn}")
}

/// A `BOOKQ_CSP_EXTRA_ORIGINS` entry must be a bare `scheme://host[:port]`
/// origin — the charset whitelist excludes `'`/`;`/space/slash so a bad
/// value can never inject CSP syntax (callers still drop + warn).
fn csp_origin_valid(s: &str) -> bool {
    let s = s.trim();
    let Some(rest) = ["https://", "http://", "wss://", "ws://"]
        .iter()
        .find_map(|p| s.strip_prefix(p))
    else {
        return false;
    };
    !rest.is_empty()
        && rest
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '-' | ':' | '[' | ']'))
}

/// Comma-separated `BOOKQ_CSP_EXTRA_ORIGINS` → validated bare origins.
/// Invalid entries are dropped with a warning.
fn parse_csp_extra_origins(raw: &str) -> Vec<String> {
    let mut out = Vec::new();
    for part in raw.split(',') {
        let o = part.trim();
        if o.is_empty() {
            continue;
        }
        if csp_origin_valid(o) {
            out.push(o.to_string());
        } else {
            tracing::warn!(
                origin = %o,
                "BOOKQ_CSP_EXTRA_ORIGINS entry is not a bare origin — ignored"
            );
        }
    }
    out
}

/// Insert `name: value` unless the handler already set it — the /admin
/// CSP and the SVG sandbox CSP are stricter and must win (#631).
fn set_header_if_absent(h: &mut axum::http::HeaderMap, name: &'static str, value: &'static str) {
    let name = axum::http::HeaderName::from_static(name);
    if !h.contains_key(&name) {
        h.insert(name, HeaderValue::from_static(value));
    }
}

/// Baseline hardening headers on every response (#631, OWASP A05). TLS
/// terminates at the tailscale/cloudflared sidecar, but HSTS still reaches
/// the client on that path and is ignored over plain HTTP, so it is safe
/// to emit unconditionally (no `includeSubDomains` — sibling hosts on a
/// custom domain must not be affected). `X-Frame-Options: DENY` plus CSP
/// `frame-ancestors 'none'` close clickjacking on the upload/delete
/// controls; `nosniff` covers JSON, static assets and error bodies alike.
/// Non-HTML responses get no CSP: `/sw.js`'s own response headers feed the
/// worker global scope, where `default-src 'none'` would break
/// `importScripts`, and XFO already covers framing for media/JSON.
fn apply_security_headers(res: &mut Response, extra_origins: &[String], test_mode: bool) {
    let h = res.headers_mut();
    for (n, v) in [
        ("x-content-type-options", "nosniff"),
        ("referrer-policy", "same-origin"),
        ("x-frame-options", "DENY"),
        ("strict-transport-security", "max-age=31536000"),
    ] {
        set_header_if_absent(h, n, v);
    }
    let is_html = h
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|ct| ct.starts_with("text/html"));
    if is_html {
        // The CSP is built per-response (operator extras), so it cannot
        // ride the &'static insert helper.
        if !h.contains_key(header::CONTENT_SECURITY_POLICY) {
            if let Ok(v) = HeaderValue::from_str(&app_csp(extra_origins, test_mode)) {
                h.insert(header::CONTENT_SECURITY_POLICY, v);
            }
        }
        set_header_if_absent(h, "x-robots-tag", "noindex");
    }
}

async fn security_headers(
    State(st): State<AppState>,
    req: Request<Body>,
    next: axum::middleware::Next,
) -> Response {
    let mut res = next.run(req).await;
    apply_security_headers(&mut res, &st.csp_extra, st.test_mode);
    res
}

/// `/api/v1/reading-positions` — M4+375: per-folder LWW + tombstones,
/// native on the shared user-state SQLite store (same rows the Python
/// viewer reads/writes, so py/rs cannot diverge — #263 resolved).
async fn reading_positions(State(st): State<AppState>, req: Request<Body>) -> Response {
    let Some(user) = caller(&st, req.headers()) else {
        return unauthenticated();
    };
    // `user_key_for_email`: strip().lower() — an empty key is a 403, never
    // a shared bucket (same posture as the Python `_position_user_key`).
    let key = user.email.trim().to_lowercase();
    if key.is_empty() {
        return state::detail(
            axum::http::StatusCode::FORBIDDEN,
            "session has no usable identity",
        );
    }
    let q = Query::<HashMap<String, String>>::try_from_uri(req.uri())
        .map(|q| q.0)
        .unwrap_or_default();
    let headers = req.headers().clone();
    match *req.method() {
        axum::http::Method::GET => {
            let folder = q.get("folder").map(|s| s.as_str());
            let res = state::list_positions(
                st.position_store.clone(),
                &key,
                folder.map(|s| s.to_string()),
            )
            .await;
            serve::maybe_gzip(&headers, res, true).await
        }
        axum::http::Method::PUT => {
            // `folder` is required → missing keeps FastAPI's 422.
            let Some(folder) = q.get("folder").cloned() else {
                return state::unprocessable("folder", "missing", "Field required");
            };
            let (_parts, body) = req.into_parts();
            let body = match http_body_util::BodyExt::collect(body).await {
                Ok(c) => c.to_bytes(),
                Err(_) => {
                    return state::detail(StatusCode::BAD_REQUEST, "invalid body");
                }
            };
            state::put_position(st.position_store.clone(), &key, &folder, &body).await
        }
        axum::http::Method::DELETE => {
            state::delete_positions(
                st.position_store.clone(),
                &key,
                q.get("folder").map(|s| s.as_str()),
            )
            .await
        }
        _ => {
            return method_not_allowed("GET, PUT, DELETE");
        }
    }
}

/// `/api/v1/folder-visibility` — M4: shared SQLite store + owner HMAC.
async fn folder_visibility(State(st): State<AppState>, req: Request<Body>) -> Response {
    let Some(user) = caller(&st, req.headers()) else {
        return unauthenticated();
    };
    // `user_key_for_email`: email.strip().lower()
    let key = user.email.trim().to_lowercase();
    let headers = req.headers().clone();
    match *req.method() {
        axum::http::Method::GET => {
            serve::maybe_gzip(&headers, state::get_folder_visibility(&st, &key).await, true).await
        }
        axum::http::Method::PUT => {
            let (_parts, body) = req.into_parts();
            let body = match http_body_util::BodyExt::collect(body).await {
                Ok(c) => c.to_bytes(),
                Err(_) => {
                    return state::detail(StatusCode::BAD_REQUEST, "invalid body");
                }
            };
            state::put_folder_visibility(&st, &key, &headers, &body).await
        }
        _ => {
            return method_not_allowed("GET, PUT");
        }
    }
}

/// TEST_MODE-only `?hard=1` equivalent — drop the mock user's whole
/// position slot (tombstones too) so the harness's per-test reset starts
/// clean (#469). Always 404 outside test mode, never part of the contract.
async fn test_positions_reset(State(st): State<AppState>, req: Request<Body>) -> Response {
    if !st.test_mode || *req.method() != axum::http::Method::DELETE {
        return state::detail(StatusCode::NOT_FOUND, "not found");
    }
    state::delete_user_positions(st.position_store.clone(), auth::TEST_USER_EMAIL).await
}

/// `/api/v1/tree` — the v1 envelope `{kind, nodes, generation}` over the
/// shared index cache (`_folder_tree(v1=True)`).
async fn api_tree(State(st): State<AppState>, req: Request<Body>) -> Response {
    if req.method() != axum::http::Method::GET {
        return method_not_allowed("GET");
    }
    if caller(&st, req.headers()).is_none() {
        return unauthenticated();
    }
    let q = Query::<HashMap<String, String>>::try_from_uri(req.uri())
        .map(|q| q.0)
        .unwrap_or_default();
    let headers = req.headers().clone();
    let res = folders::api_tree(&st, &headers, q).await;
    serve::maybe_gzip(&headers, res, true).await
}

/// `/api/v1/images` — `{folder, images[]}` over `_list_folder_images`.
async fn api_images(State(st): State<AppState>, req: Request<Body>) -> Response {
    if req.method() != axum::http::Method::GET {
        return method_not_allowed("GET");
    }
    if caller(&st, req.headers()).is_none() {
        return unauthenticated();
    }
    let q = Query::<HashMap<String, String>>::try_from_uri(req.uri())
        .map(|q| q.0)
        .unwrap_or_default();
    // `folder` is a required Query param → missing keeps the 422.
    let Some(folder) = q.get("folder").cloned() else {
        return state::unprocessable("folder", "missing", "Field required");
    };
    let headers = req.headers().clone();
    let library = st.library.clone();
    let res = tokio::task::spawn_blocking(move || serve::api_images(&library, &folder))
        .await
        .unwrap_or_else(|_| state::detail(StatusCode::INTERNAL_SERVER_ERROR, "task failed"));
    serve::maybe_gzip(&headers, res, true).await
}

/// `/api/v1/search` — relevance + filters over the shared index; hidden
/// roots applied server-side (fail-closed).
async fn api_search(State(st): State<AppState>, req: Request<Body>) -> Response {
    if req.method() != axum::http::Method::GET {
        return method_not_allowed("GET");
    }
    // Hidden roots are library-global; auth still gates the caller.
    let Some(_user) = caller(&st, req.headers()) else {
        return unauthenticated();
    };
    let q = Query::<HashMap<String, String>>::try_from_uri(req.uri())
        .map(|q| q.0)
        .unwrap_or_default();
    let headers = req.headers().clone();
    let res = search::api_search(&st, &q).await;
    serve::maybe_gzip(&headers, res, true).await
}

/// `/api/v1/facets` — `{artists, tags}` scoped to `path`'s subtree; hidden
/// roots applied server-side (fail-closed).
async fn api_facets(State(st): State<AppState>, req: Request<Body>) -> Response {
    if req.method() != axum::http::Method::GET {
        return method_not_allowed("GET");
    }
    // Hidden roots are library-global; auth still gates the caller.
    let Some(_user) = caller(&st, req.headers()) else {
        return unauthenticated();
    };
    let q = Query::<HashMap<String, String>>::try_from_uri(req.uri())
        .map(|q| q.0)
        .unwrap_or_default();
    let headers = req.headers().clone();
    let res = search::api_facets(&st, &q).await;
    serve::maybe_gzip(&headers, res, true).await
}

// ── #401: shell + static ───────────────────────────────────────────────────

/// A signed session cookie whose payload is not a session dict — Python's
/// SessionMiddleware raises on `json.loads` (500) before any route runs,
/// so corrupt sessions answer 500 locally.
pub(crate) fn corrupt_session(st: &AppState, headers: &axum::http::HeaderMap) -> bool {
    matches!(
        auth::session_lookup(headers, st.session_key.as_ref()),
        auth::SessionLookup::Corrupt
    )
}


/// `GET /` — the Jinja shell: viewer page for identified callers, the
/// login page for everyone else (200, not a redirect).
async fn shell_index(State(st): State<AppState>, req: Request<Body>) -> Response {
    if !matches!(
            *req.method(),
            axum::http::Method::GET | axum::http::Method::HEAD
        )
    {
        return method_not_allowed("GET, HEAD");
    }
    let head = *req.method() == axum::http::Method::HEAD;
    let headers = req.headers().clone();
    let thumb_perf = st.test_mode
        && shell::first_query_param(req.uri(), "thumb_perf").as_deref() == Some("1");
    let user = shell::index_caller(
        st.test_mode,
        &st.secret_key,
        st.session_key.as_ref(),
        &st.directory,
        &headers,
        st.registry.guest(),
    );
    // #608: the admin menu entry is cosmetic, but derive it from the same
    // strict identity path the `/admin` gate uses (active membership
    // re-checked), not from the shell's viewer branch. Guests resolve to
    // the leader's identity for reads — the menu must not leak to them
    // (`/admin` itself is already 403 via guest_write_guard).
    let admin_email = match st.resolve(&headers) {
        auth::Caller::User(u) => Some(u.email),
        _ => None,
    };
    // '/' is outside the gzip prefixes.
    let st2 = st.clone();
    tokio::task::spawn_blocking(move || {
        shell::index(
            &st2.web_dir,
            st2.test_mode,
            &st2.secret_key,
            &st2.visibility,
            &st2.directory,
            admin_email.as_deref(),
            &user,
            &headers,
            thumb_perf,
            head,
        )
    })
    .await
    .unwrap_or_else(|_| state::detail(StatusCode::INTERNAL_SERVER_ERROR, "task failed"))
}

/// `GET|HEAD /sw.js` — public FileResponse with `Service-Worker-Allowed: /`.
async fn serve_sw(State(st): State<AppState>, req: Request<Body>) -> Response {
    if corrupt_session(&st, req.headers()) {
        return state::detail(
            StatusCode::INTERNAL_SERVER_ERROR,
            "invalid session",
        );
    }
    if !matches!(
            *req.method(),
            axum::http::Method::GET | axum::http::Method::HEAD
        )
    {
        return method_not_allowed("GET, HEAD");
    }
    let head = *req.method() == axum::http::Method::HEAD;
    let headers = req.headers().clone();
    let req_headers = headers.clone();
    let st2 = st.clone();
    let res = tokio::task::spawn_blocking(move || {
        shell::named_file(
            &st2.web_dir.join("static").join("sw.js"),
            "application/javascript",
            shell::SW_HEADERS,
            "sw.js not found",
            &headers,
            head,
        )
    })
    .await
    .unwrap_or_else(|_| state::detail(StatusCode::INTERNAL_SERVER_ERROR, "task failed"));
    // '/sw.js' is a gzip prefix; application/javascript is not excluded.
    serve::maybe_gzip(&req_headers, res, true).await
}

/// `GET|HEAD /favicon.ico` — public FileResponse, day-long cache.
async fn serve_favicon(State(st): State<AppState>, req: Request<Body>) -> Response {
    if corrupt_session(&st, req.headers()) {
        return state::detail(
            StatusCode::INTERNAL_SERVER_ERROR,
            "invalid session",
        );
    }
    if !matches!(
            *req.method(),
            axum::http::Method::GET | axum::http::Method::HEAD
        )
    {
        return method_not_allowed("GET, HEAD");
    }
    let head = *req.method() == axum::http::Method::HEAD;
    let headers = req.headers().clone();
    let st2 = st.clone();
    // '/favicon.ico' is outside the gzip prefixes.
    tokio::task::spawn_blocking(move || {
        shell::named_file(
            &st2.web_dir.join("static").join("favicon.ico"),
            "image/x-icon",
            shell::FAVICON_HEADERS,
            "favicon.ico not found",
            &headers,
            head,
        )
    })
    .await
    .unwrap_or_else(|_| state::detail(StatusCode::INTERNAL_SERVER_ERROR, "task failed"))
}

/// `GET|HEAD /static/{*path}` (and bare `/static`) — public
/// NoCacheStaticFiles: realpath containment, no-cache, 304 + range.
async fn serve_static(State(st): State<AppState>, req: Request<Body>) -> Response {
    if corrupt_session(&st, req.headers()) {
        return state::detail(
            StatusCode::INTERNAL_SERVER_ERROR,
            "invalid session",
        );
    }
    if !matches!(
            *req.method(),
            axum::http::Method::GET | axum::http::Method::HEAD
        )
    {
        return method_not_allowed("GET, HEAD");
    }
    let head = *req.method() == axum::http::Method::HEAD;
    let rel = match req.uri().path() {
        "/static" => String::new(),
        p => match p.strip_prefix("/static/") {
            Some(raw) => match percent_decode_str(raw).decode_utf8() {
                Ok(d) => d.into_owned(),
                Err(_) => {
                    return state::detail(StatusCode::BAD_REQUEST, "invalid path");
                }
            },
            None => return state::detail(StatusCode::NOT_FOUND, "Not Found"),
        },
    };
    let headers = req.headers().clone();
    let req_headers = headers.clone();
    let st2 = st.clone();
    let res = tokio::task::spawn_blocking(move || {
        shell::static_file(&st2.web_dir.join("static"), &rel, &headers, head)
    })
    .await
    .unwrap_or_else(|_| state::detail(StatusCode::INTERNAL_SERVER_ERROR, "task failed"));
    // '/static/' is a gzip prefix; compression still skips the
    // excluded content types (image/png, font/woff2, video/*, …).
    let compressible = !res
        .headers()
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .is_some_and(shell::gzip_excluded);
    serve::maybe_gzip(&req_headers, res, compressible).await
}

/// `/api/v1/health` — public (`security: []`); `{status:"ok"}`.
async fn api_health(State(_st): State<AppState>, req: Request<Body>) -> Response {
    if req.method() != axum::http::Method::GET {
        return method_not_allowed("GET");
    }
    let headers = req.headers().clone();
    let mut res = Response::builder()
        .status(StatusCode::OK)
        .body(Body::from("{\"status\":\"ok\"}"))
        .unwrap();
    res.headers_mut().insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("application/json"),
    );
    serve::maybe_gzip(&headers, res, true).await
}

/// `/api/v1/version` — authed; `{version,sha,build_date}` + `no-store`.
async fn api_version(State(st): State<AppState>, req: Request<Body>) -> Response {
    if req.method() != axum::http::Method::GET {
        return method_not_allowed("GET");
    }
    if caller(&st, req.headers()).is_none() {
        return unauthenticated();
    }
    // Key order is the wire contract: version, sha, build_date.
    let body = format!(
        "{{\"version\":\"2.0.0\",\"sha\":{},\"build_date\":{}}}",
        serde_json::to_string(&st.git_sha).unwrap_or("\"\"".into()),
        serde_json::to_string(&st.build_date).unwrap_or("\"\"".into())
    );
    let headers = req.headers().clone();
    let mut res = Response::builder()
        .status(StatusCode::OK)
        .body(Body::from(body))
        .unwrap();
    res.headers_mut().insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("application/json"),
    );
    res.headers_mut().insert(
        header::CACHE_CONTROL,
        HeaderValue::from_static("no-store"),
    );
    serve::maybe_gzip(&headers, res, true).await
}

/// `/api/v1/library/files` — POST multipart upload / DELETE file-or-dir.
/// `dir` (upload) and `path` (delete) are query params; the body streams.
/// #633: DELETE is admin-only (destructive on shared library content);
/// uploads stay open to any member.
async fn library_files(State(st): State<AppState>, req: Request<Body>) -> Response {
    let Some(user) = caller(&st, req.headers()) else {
        return unauthenticated();
    };
    match *req.method() {
        axum::http::Method::POST => {
            let q = Query::<HashMap<String, String>>::try_from_uri(req.uri())
                .map(|q| q.0)
                .unwrap_or_default();
            let dir = q.get("dir").cloned().unwrap_or_default();
            let mp = match axum::extract::Multipart::from_request(req, &st).await {
                Ok(mp) => mp,
                Err(e) => return e.into_response(),
            };
            library::post_files(&st, &dir, mp).await
        }
        axum::http::Method::DELETE => {
            if !admin::admin_check(&st, &user.email).await {
                return state::detail(StatusCode::FORBIDDEN, "Forbidden");
            }
            let q = Query::<HashMap<String, String>>::try_from_uri(req.uri())
                .map(|q| q.0)
                .unwrap_or_default();
            let Some(path) = q.get("path").cloned() else {
                return state::unprocessable("path", "missing", "Field required");
            };
            library::delete_file(&st, &path).await
        }
        _ => method_not_allowed("POST, DELETE"),
    }
}

/// `/api/v1/library/uploads` — POST init a chunked upload session.
/// Init is a tiny JSON; the route-level `DefaultBodyLimit::disable` (the
/// file routes need streaming bodies) removes axum's cap here too, so
/// bound the `collect` explicitly or a fat body buffers unbounded.
const LIBRARY_INIT_BODY_MAX: usize = 64 * 1024;

async fn library_uploads(State(st): State<AppState>, req: Request<Body>) -> Response {
    let Some(user) = caller(&st, req.headers()) else {
        return unauthenticated();
    };
    if *req.method() != axum::http::Method::POST {
        return method_not_allowed("POST");
    }
    let (_parts, body) = req.into_parts();
    let body = http_body_util::Limited::new(body, LIBRARY_INIT_BODY_MAX);
    let body = match http_body_util::BodyExt::collect(body).await {
        Ok(c) => c.to_bytes(),
        Err(_) => {
            return state::detail(StatusCode::PAYLOAD_TOO_LARGE, "request body too large");
        }
    };
    library::uploads_init(&st, &body, &user.email).await
}

/// `/api/v1/library/uploads/{id}` — GET state / PUT chunk / DELETE cancel.
async fn library_upload(
    State(st): State<AppState>,
    AxumPath(id): AxumPath<String>,
    req: Request<Body>,
) -> Response {
    let Some(user) = caller(&st, req.headers()) else {
        return unauthenticated();
    };
    match *req.method() {
        axum::http::Method::GET => library::upload_get(&st, &id, &user.email).await,
        axum::http::Method::PUT => {
            let headers = req.headers().clone();
            let (_parts, body) = req.into_parts();
            library::upload_append(&st, &id, &user.email, &headers, body).await
        }
        axum::http::Method::DELETE => library::upload_cancel(&st, &id, &user.email).await,
        _ => method_not_allowed("GET, PUT, DELETE"),
    }
}

/// `/api/v1/library/uploads/{id}/finalize` — POST commit into the library.
async fn library_upload_finalize(
    State(st): State<AppState>,
    AxumPath(id): AxumPath<String>,
    req: Request<Body>,
) -> Response {
    let Some(user) = caller(&st, req.headers()) else {
        return unauthenticated();
    };
    if *req.method() != axum::http::Method::POST {
        return method_not_allowed("POST");
    }
    library::upload_finalize(&st, &id, &user.email).await
}

/// `/api/v1/index/refresh` — authed POST; synchronous rebuild under the
/// same swap semantics as the background gate, `{status,folders}`.
async fn api_refresh(State(st): State<AppState>, req: Request<Body>) -> Response {
    if req.method() != axum::http::Method::POST {
        return method_not_allowed("POST");
    }
    if caller(&st, req.headers()).is_none() {
        return unauthenticated();
    }
    let headers = req.headers().clone();
    match folders::force_rebuild(&st.index, &st.library).await {
        Some(folders) => {
            let mut res = Response::builder()
                .status(StatusCode::OK)
                .body(Body::from(format!("{{\"status\":\"ok\",\"folders\":{folders}}}")))
                .unwrap();
            res.headers_mut().insert(
                header::CONTENT_TYPE,
                HeaderValue::from_static("application/json"),
            );
            serve::maybe_gzip(&headers, res, true).await
        }
        None => state::detail(
            StatusCode::INTERNAL_SERVER_ERROR,
            "index build failed",
        ),
    }
}

/// `/image/{*path}` — a v1 contract path (media is not versioned) for plain
/// files, ZIP/CBZ/EPUB/RAR/CBR entries, and PDF pages (#402 — pdfium-render;
/// a missing `libpdfium` answers 501).
/// Multi-range is a 200-full contract shrink.
async fn serve_image(State(st): State<AppState>, req: Request<Body>) -> Response {
    if req.method() != axum::http::Method::GET {
        return method_not_allowed("GET");
    }
    // /image requires auth — only positively identified callers are served.
    if caller(&st, req.headers()).is_none() {
        return unauthenticated();
    }
    // The URI path is still percent-encoded; decode per-segment like
    // Starlette does (`%2F` inside a segment becomes "/" and is handled by
    // the same traversal guards downstream).
    let Some(raw) = req.uri().path().strip_prefix("/image/") else {
        return state::detail(StatusCode::NOT_FOUND, "Not Found");
    };
    let Ok(path) = percent_decode_str(raw).decode_utf8() else {
        return state::detail(StatusCode::BAD_REQUEST, "invalid path");
    };
    let headers = req.headers().clone();
    let library = st.library.clone();
    let path = path.into_owned();
    // /image is outside the gzip prefixes.
    tokio::task::spawn_blocking(move || serve::image(&library, &path, &headers))
        .await
        .unwrap_or_else(|_| state::detail(StatusCode::INTERNAL_SERVER_ERROR, "task failed"))
}

/// `/thumb/{*path}` — v1 contract path. Answers validation, the
/// 304 fast path and shared-cache hits; misses render in-process (image
/// crate), unreconstructable sources answer 501.
async fn serve_thumb(State(st): State<AppState>, req: Request<Body>) -> Response {
    if req.method() != axum::http::Method::GET {
        return method_not_allowed("GET");
    }
    if caller(&st, req.headers()).is_none() {
        return unauthenticated();
    }
    let Some(raw) = req.uri().path().strip_prefix("/thumb/") else {
        return state::detail(StatusCode::NOT_FOUND, "Not Found");
    };
    let Ok(path) = percent_decode_str(raw).decode_utf8() else {
        return state::detail(StatusCode::BAD_REQUEST, "invalid path");
    };
    let q = Query::<HashMap<String, String>>::try_from_uri(req.uri())
        .map(|q| q.0)
        .unwrap_or_default();
    let headers = req.headers().clone();
    let library = st.library.clone();
    let cache = st.thumb_cache.clone();
    let path = path.into_owned();
    // /thumb is outside the gzip prefixes.
    tokio::task::spawn_blocking(move || thumbs::thumb(&library, &cache, &path, q.get("w"), &headers))
        .await
        .unwrap_or_else(|_| state::detail(StatusCode::INTERNAL_SERVER_ERROR, "task failed"))
}

/// `/view/{*path}` — passthrough formats serve natively; normalized
/// formats answer shared-cache hits and render misses in-process;
/// unreconstructable sources answer 501. `?w=<px>` opts into the
/// normalized fast-tier branch at that width (#440).
async fn serve_view(State(st): State<AppState>, req: Request<Body>) -> Response {
    if req.method() != axum::http::Method::GET {
        return method_not_allowed("GET");
    }
    if caller(&st, req.headers()).is_none() {
        return unauthenticated();
    }
    let Some(raw) = req.uri().path().strip_prefix("/view/") else {
        return state::detail(StatusCode::NOT_FOUND, "Not Found");
    };
    let Ok(path) = percent_decode_str(raw).decode_utf8() else {
        return state::detail(StatusCode::BAD_REQUEST, "invalid path");
    };
    let q = Query::<HashMap<String, String>>::try_from_uri(req.uri())
        .map(|q| q.0)
        .unwrap_or_default();
    let headers = req.headers().clone();
    let library = st.library.clone();
    let cache = st.thumb_cache.clone();
    let path = path.into_owned();
    // /view is outside the gzip prefixes.
    tokio::task::spawn_blocking(move || {
        thumbs::view(&library, &cache, &path, q.get("w"), &headers)
    })
    .await
    .unwrap_or_else(|_| state::detail(StatusCode::INTERNAL_SERVER_ERROR, "task failed"))
}

/// `/api/v1/thumb-sprite` — v1 contract.
/// Answers validation, 304s and shared-cache hits; misses compose
/// in-process (9-cell strip + `rows` grid) into the shared `sprites/` cache.
async fn api_thumb_sprite(State(st): State<AppState>, req: Request<Body>) -> Response {
    if req.method() != axum::http::Method::GET {
        return method_not_allowed("GET");
    }
    if caller(&st, req.headers()).is_none() {
        return unauthenticated();
    }
    let q = Query::<HashMap<String, String>>::try_from_uri(req.uri())
        .map(|q| q.0)
        .unwrap_or_default();
    let query_string = req.uri().query().unwrap_or("").to_string();
    let headers = req.headers().clone();
    let headers2 = headers.clone();
    let library = st.library.clone();
    let cache = st.thumb_cache.clone();
    let res = tokio::task::spawn_blocking(move || {
        thumbs::thumb_sprite(&library, &cache, &q, &query_string, &headers2)
    })
    .await
    .unwrap_or_else(|_| state::detail(StatusCode::INTERNAL_SERVER_ERROR, "task failed"));
    // image/* responses are excluded from ApiGZipMiddleware by content type.
    let compressible = !res
        .headers()
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|ct| ct.starts_with("image/"));
    serve::maybe_gzip(&headers, res, compressible).await
}

// ── #400: /auth/* + /api/v1/auth/token ─────────────────────────────────────
//
// The callback mints the one-time codes the token endpoint redeems.
// `auth_flow` is `Some` iff the OAuth config is complete, else the surface
// answers 501. A signed session cookie whose payload won't parse gets
// starlette's middleware-level 500, answered locally too.

/// Missing OAuth config answers 501 (no upstream fallback exists anymore).
fn auth_unconfigured() -> Response {
    state::detail(
        StatusCode::NOT_IMPLEMENTED,
        "oauth not configured",
    )
}

/// A corrupt session cookie answers 500 like starlette's middleware.
pub(crate) fn corrupt_session_response() -> Response {
    state::detail(StatusCode::INTERNAL_SERVER_ERROR, "invalid session")
}

async fn auth_login(State(st): State<AppState>, req: Request<Body>) -> Response {
    if !matches!(*req.method(), axum::http::Method::GET | axum::http::Method::HEAD) {
        return method_not_allowed("GET, HEAD");
    }
    // #373: a cross-site initiation must not write the `bookq_oauth` tx
    // cookie (login CSRF plants an attacker PKCE challenge).
    if !oauth::same_site_initiation(req.headers()) {
        return state::detail(StatusCode::FORBIDDEN, "Forbidden");
    }
    let Some(flow) = st.auth_flow.clone() else {
        return auth_unconfigured();
    };
    if matches!(
        auth::session_lookup(req.headers(), st.session_key.as_ref()),
        auth::SessionLookup::Corrupt
    ) {
        return corrupt_session_response();
    }
    let q = Query::<HashMap<String, String>>::try_from_uri(req.uri())
        .map(|q| q.0)
        .unwrap_or_default();
    oauth::login(&flow, &q, &oauth::client_ip(req.headers()))
}

async fn auth_callback(State(st): State<AppState>, req: Request<Body>) -> Response {
    if !matches!(*req.method(), axum::http::Method::GET | axum::http::Method::HEAD) {
        return method_not_allowed("GET, HEAD");
    }
    let Some(flow) = st.auth_flow.clone() else {
        return auth_unconfigured();
    };
    let lookup = auth::session_lookup(req.headers(), st.session_key.as_ref());
    if matches!(lookup, auth::SessionLookup::Corrupt) {
        return corrupt_session_response();
    }
    let q = Query::<HashMap<String, String>>::try_from_uri(req.uri())
        .map(|q| q.0)
        .unwrap_or_default();
    let mut res = oauth::callback(&flow, req.method(), req.headers(), &q, &lookup).await;
    // A completed WEB login supersedes any stale guest grant cookie —
    // `Vary`/marker plumbing would keep tagging this browser's responses
    // as guest-served (and the cache-exclusion would degrade caching)
    // until the cookie died on its own. Only the browser-success branch
    // mints a `session` cookie: native (bookq://) redirects and denied
    // logins issue no web session, so clearing there would silently kill
    // in-browser guest browsing — e.g. an Android Custom-Tabs app login
    // sharing the browser the guest reads in.
    let has_web_session = res
        .headers()
        .get_all(header::SET_COOKIE)
        .iter()
        .filter_map(|v| v.to_str().ok())
        .any(|v| {
            v.starts_with("session=")
                && !v.starts_with("session=null")
                && !v.starts_with("session=;")
        });
    if has_web_session {
        clear_guest_cookie(&mut res);
    }
    res
}

/// `bookq_guest=; Max-Age=0` — drop the guest grant cookie. Shared by the
/// logout paths (every one of them) and the post-login stale-cookie sweep.
fn clear_guest_cookie(res: &mut Response) {
    res.headers_mut().append(
        header::SET_COOKIE,
        HeaderValue::from_static(
            "bookq_guest=; path=/; Max-Age=0; httponly; samesite=lax; secure",
        ),
    );
}

async fn auth_logout(State(st): State<AppState>, req: Request<Body>) -> Response {
    if !matches!(*req.method(), axum::http::Method::GET | axum::http::Method::HEAD) {
        return method_not_allowed("GET, HEAD");
    }
    // #373: logout is a state-changing GET — refuse cross-site
    // initiations (forced logout CSRF). The guest cookie is NOT cleared
    // on this path: honoring a forged navigation's Set-Cookie would be a
    // forced guest-logout primitive — the same thing this gate exists to
    // prevent.
    if !oauth::same_site_initiation(req.headers()) {
        return state::detail(StatusCode::FORBIDDEN, "Forbidden");
    }
    // The grant is server-side state — kill it, not just the cookie copy
    // in this browser, so a lifted token can't outlive the sign-out.
    if let Some(token) = auth::cookie_value(req.headers(), "bookq_guest") {
        st.registry.guest().revoke_token(&token);
    }
    let Some(flow) = st.auth_flow.clone() else {
        let mut res = auth_unconfigured();
        clear_guest_cookie(&mut res);
        return res;
    };
    let lookup = auth::session_lookup(req.headers(), st.session_key.as_ref());
    if matches!(lookup, auth::SessionLookup::Corrupt) {
        let mut res = corrupt_session_response();
        clear_guest_cookie(&mut res);
        return res;
    }
    let mut res = oauth::logout(&flow, &lookup);
    // Also drop any guest grant cookie — logout is the explicit
    // "I'm done here" gesture and shouldn't leave a live bearer behind.
    clear_guest_cookie(&mut res);
    res
}

async fn auth_token(State(st): State<AppState>, req: Request<Body>) -> Response {
    if *req.method() != axum::http::Method::POST {
        return method_not_allowed("POST");
    }
    let Some(flow) = st.auth_flow.clone() else {
        return auth_unconfigured();
    };
    // starlette loads the session cookie before routing — a corrupt one
    // 500s there, answered locally here too.
    if matches!(
        auth::session_lookup(req.headers(), st.session_key.as_ref()),
        auth::SessionLookup::Corrupt
    ) {
        return corrupt_session_response();
    }
    let headers = req.headers().clone();
    let ip = oauth::client_ip(&headers);
    let (_parts, body) = req.into_parts();
    let body = match http_body_util::BodyExt::collect(body).await {
        Ok(c) => c.to_bytes(),
        Err(_) => {
            return state::detail(StatusCode::BAD_REQUEST, "invalid body");
        }
    };
    let res = oauth::token(&flow, &body, &ip);
    serve::maybe_gzip(&headers, res, true).await
}

/// `POST /api/v1/guest/redeem` — exchange a pair-session guest token for
/// the `bookq_guest` cookie. Already-authenticated callers (session or
/// Bearer → `Caller::User`) get `{"ok":true}` without a cookie so a logged-in
/// user scanning the QR keeps their own identity; guest-cookie holders
/// re-redeeming get last-wins refresh.
/// `POST /api/v1/guest/redeem` body — the invite token to exchange.
#[derive(serde::Deserialize)]
struct GuestRedeemBody {
    token: String,
}

async fn guest_redeem(State(st): State<AppState>, req: Request<Body>) -> Response {
    if *req.method() != axum::http::Method::POST {
        return method_not_allowed("POST");
    }
    // Same cross-site gate as the OAuth initiation endpoints — a foreign
    // page must not mint a guest cookie into this browser.
    if !oauth::same_site_initiation(req.headers()) {
        return state::detail(StatusCode::FORBIDDEN, "Forbidden");
    }
    if matches!(
        auth::session_lookup(req.headers(), st.session_key.as_ref()),
        auth::SessionLookup::Corrupt
    ) {
        return corrupt_session_response();
    }
    if !st.guest_redeem_limit.check(&oauth::client_ip(req.headers())) {
        return state::detail(StatusCode::TOO_MANY_REQUESTS, "too many requests");
    }

    let headers = req.headers().clone();
    // `Bytes::from_request` honors the route-layer DefaultBodyLimit — an
    // unauthenticated endpoint must not slurp an unbounded body.
    let body = match axum::body::Bytes::from_request(req, &st).await {
        Ok(b) => b,
        Err(rej) => return rej.into_response(),
    };
    // Validate the body BEFORE the authed short-circuit — the OpenAPI
    // contract declares requestBody required, and schema-violating
    // requests must get 400s no matter who is calling.
    let token = serde_json::from_slice::<GuestRedeemBody>(&body)
        .ok()
        .map(|b| b.token);
    let Some(token) = token.filter(|t| guest::valid_token_shape(t)) else {
        return state::detail(StatusCode::BAD_REQUEST, "invalid token");
    };
    if matches!(
        st.resolve(&headers),
        auth::Caller::User(_)
    ) {
        let mut res = Response::builder()
            .status(StatusCode::OK)
            .body(Body::from("{\"ok\":true}"))
            .unwrap();
        res.headers_mut().insert(
            header::CONTENT_TYPE,
            HeaderValue::from_static("application/json"),
        );
        return serve::maybe_gzip(&headers, res, true).await;
    }
    match st.registry.guest().status(&token) {
        guest::GrantStatus::Valid(grant) => {
            // Same membership re-check the auth paths run — a grant whose
            // owner was since disabled must not mint a cookie (the shell
            // would reject it anyway and login.js would loop redeeming).
            if !st.directory.is_member(&grant.owner_email) {
                return state::detail(StatusCode::UNAUTHORIZED, "guest link invalid");
            }
            let max_age = grant.remaining_secs();
            let mut res = Response::builder()
                .status(StatusCode::OK)
                .body(Body::from(format!(
                    "{{\"ok\":true,\"expiresInSec\":{max_age}}}"
                )))
                .unwrap();
            res.headers_mut().insert(
                header::CONTENT_TYPE,
                HeaderValue::from_static("application/json"),
            );
            // The response carries a credential (Set-Cookie) — never let
            // a shared cache store it, matching `/api/v1/auth/token`.
            res.headers_mut().insert(
                header::CACHE_CONTROL,
                HeaderValue::from_static("no-store"),
            );
            if let Ok(v) = HeaderValue::from_str(&format!(
                "bookq_guest={token}; path=/; Max-Age={max_age}; httponly; samesite=lax; secure"
            )) {
                res.headers_mut().append(header::SET_COOKIE, v);
            }
            serve::maybe_gzip(&headers, res, true).await
        }
        // Expired and unknown answer identically — distinguishing them
        // would be a token-state oracle. (The WS side's join_guest DOES
        // split code_expired/bad_code: that path is throttled by
        // join_fails so the oracle is bounded, and expired-vs-wrong tells
        // the user to request a fresh invite rather than retype.)
        guest::GrantStatus::Expired | guest::GrantStatus::Unknown => {
            state::detail(StatusCode::UNAUTHORIZED, "guest link invalid")
        }
    }
}

// ── M8: /ws/reader-sync ────────────────────────────────────────────────────

/// `/ws/reader-sync` — non-upgrade requests fall through to the
/// `WebSocketUpgrade` extractor's rejection; unauthenticated handshakes
/// answer HTTP 403.
async fn reader_sync_ws(State(st): State<AppState>, req: Request<Body>) -> Response {
    let Some((user, is_guest)) = st.resolve_ws_user(req.headers()) else {
        return StatusCode::FORBIDDEN.into_response();
    };
    // The grant bearer rides on the link so `tick` can re-validate it —
    // a revoked/expired grant drops the socket rather than coasting on
    // the handshake-time snapshot.
    let guest_token = if is_guest {
        auth::cookie_value(req.headers(), "bookq_guest")
    } else {
        None
    };
    let (mut parts, _body) = req.into_parts();
    match axum::extract::ws::WebSocketUpgrade::from_request_parts(&mut parts, &st).await {
        Ok(wsu) => {
            let st2 = st.clone();
            wsu.on_upgrade(move |socket| {
                reader_sync_conn(socket, st2, user, guest_token)
            })
        }
        Err(rej) => rej.into_response(),
    }
}

/// One link per connection: register → drain task → receive loop → detach.
/// Registry-initiated closes come through the drain's watch channel.
async fn reader_sync_conn(
    socket: axum::extract::ws::WebSocket,
    st: AppState,
    user: serde_json::Value,
    guest_token: Option<String>,
) {
    use axum::extract::ws::Message;
    use futures_util::StreamExt;
    let (ws_tx, mut ws_rx) = socket.split();
    let (outq_tx, outq_rx) = tokio::sync::mpsc::channel(st.registry.send_queue_max());
    let (close_tx, close_rx) = tokio::sync::watch::channel(None);
    let reg = st.registry.clone();
    let link = reader_sync::Link::new(
        outq_tx,
        close_tx,
        Some(user),
        guest_token,
        std::time::Instant::now(),
        reg.heartbeat_interval(),
    );
    let bulk = link.bulk.clone();
    let link_id = {
        let mut g = reg.inner.lock().await;
        g.register_link(link, reg.heartbeat_interval())
    };
    let drain = tokio::spawn(reader_sync_drain(ws_tx, outq_rx, bulk, close_rx));
    let reg2 = reg.clone();
    let lid = link_id.clone();
    let mut recv = tokio::spawn(async move {
        while let Some(msg) = ws_rx.next().await {
            match msg {
                Ok(Message::Text(t)) => reg2.handle_raw(&lid, t.as_bytes()).await,
                Ok(Message::Binary(b)) => reg2.handle_raw(&lid, &b).await,
                Ok(Message::Close(_)) | Err(_) => break,
                _ => {}
            }
        }
    });
    let mut drain = drain;
    tokio::select! {
        _ = &mut recv => { drain.abort(); }
        _ = &mut drain => { recv.abort(); }
    }
    reg.detach_link(&link_id).await;
}

/// Outbound pump: control frames + bulk (manifest) blocks + registry-close.
/// Close is polled first (biased) so it bypasses queued frames like the
/// Python drain-cancel + socket-close path.
async fn reader_sync_drain(
    mut ws_tx: futures_util::stream::SplitSink<
        axum::extract::ws::WebSocket,
        axum::extract::ws::Message,
    >,
    mut outq: tokio::sync::mpsc::Receiver<reader_sync::OutItem>,
    bulk: std::sync::Arc<std::sync::Mutex<reader_sync::BulkQ>>,
    mut close: tokio::sync::watch::Receiver<Option<(u16, String)>>,
) {
    use axum::extract::ws::{CloseFrame, Message};
    use futures_util::SinkExt;

    /// Feed one queued item into the sink without flushing — a Bulk
    /// marker expands to every queued bulk frame, in order.
    async fn feed_item(
        ws_tx: &mut futures_util::stream::SplitSink<
            axum::extract::ws::WebSocket,
            Message,
        >,
        bulk: &std::sync::Mutex<reader_sync::BulkQ>,
        item: reader_sync::OutItem,
    ) -> bool {
        match item {
            reader_sync::OutItem::Text(raw) => {
                ws_tx.feed(Message::Text(raw.into())).await.is_ok()
            }
            reader_sync::OutItem::Bulk => {
                bulk.lock().unwrap().pending = false;
                loop {
                    let raw = { bulk.lock().unwrap().pop() };
                    let Some(raw) = raw else { break };
                    if ws_tx.feed(Message::Text(raw.into())).await.is_err() {
                        return false;
                    }
                }
                true
            }
            reader_sync::OutItem::Close(code, reason) => {
                let _ = ws_tx
                    .send(Message::Close(Some(CloseFrame {
                        code,
                        reason: reason.into(),
                    })))
                    .await;
                false
            }
        }
    }

    // The close watch's sender dies when the registry detaches the link —
    // that is NOT a close request: queued outq frames (e.g. END + an
    // ordered Close from end_owner_sessions) must still be delivered, so a
    // dead watch stops polling instead of abandoning the queue.
    let mut close_dead = false;
    loop {
        tokio::select! {
            biased;
            changed = close.changed(), if !close_dead => {
                match changed {
                    Ok(()) => {
                        let val = close.borrow().clone();
                        if let Some((code, reason)) = val {
                            let _ = ws_tx
                                .send(Message::Close(Some(CloseFrame {
                                    code,
                                    reason: reason.into(),
                                })))
                                .await;
                        }
                        break;
                    }
                    Err(_) => {
                        close_dead = true;
                        continue;
                    }
                }
            }
            item = outq.recv() => {
                match item {
                    None => break,
                    Some(it) => {
                        if !feed_item(&mut ws_tx, &bulk, it).await { break; }
                        // Collapse bursts: frames enqueued by one dispatch
                        // (ACK→STATE→MANIFEST) are fed back-to-back and
                        // flushed in a single write — mirroring the Python
                        // drain, where a handler's outbound burst leaves
                        // the transport in one event-loop turn. A close
                        // request still preempts remaining frames.
                        while let Ok(next) = outq.try_recv() {
                            if close.has_changed().unwrap_or(false) { break; }
                            if !feed_item(&mut ws_tx, &bulk, next).await { return; }
                        }
                        if ws_tx.flush().await.is_err() { break; }
                    }
                }
            }
        }
    }
}

/// Accepted sockets get TCP_NODELAY — small WS frames must not wait on
/// Nagle's delayed-ACK timer (asyncio/uvicorn do this by default).
struct NoDelayListener(tokio::net::TcpListener);

impl axum::serve::Listener for NoDelayListener {
    type Io = tokio::net::TcpStream;
    type Addr = SocketAddr;

    async fn accept(&mut self) -> (Self::Io, Self::Addr) {
        loop {
            match self.0.accept().await {
                Ok((io, addr)) if io.set_nodelay(true).is_ok() => return (io, addr),
                Ok(_) => {}
                Err(_) => tokio::task::yield_now().await,
            }
        }
    }

    fn local_addr(&self) -> std::io::Result<Self::Addr> {
        self.0.local_addr()
    }
}

#[tokio::main]
async fn main() {
    // #573: distroless has no shell/curl, so the compose healthcheck runs
    // this binary with `--healthcheck` instead of a `bash -c` one-liner.
    // Handled before any tracing/runtime work so the probe stays quiet.
    if std::env::args().skip(1).any(|a| a == "--healthcheck") {
        std::process::exit(healthcheck::run());
    }

    // #608: the users CLI is the only writer of admin rows (the `/admin` UI
    // covers members). Dispatch it before tracing/index/store setup — it
    // needs only BOOKQ_STATE_DIR.
    let argv: Vec<String> = std::env::args().skip(1).collect();
    if argv.first().map(String::as_str) == Some("users") {
        let state_dir = PathBuf::from(
            std::env::var("BOOKQ_STATE_DIR").unwrap_or_else(|_| "/var/lib/bookq".into()),
        );
        std::process::exit(users_cli::run(&argv[1..], &state_dir));
    }

    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "viewer_rs=info".into()),
        )
        .init();

    let bind = std::env::var("BOOKQ_BIND").unwrap_or_else(|_| "0.0.0.0:8080".into());
    let library = PathBuf::from(std::env::var("LIBRARY_DIR").unwrap_or_else(|_| "/app/library".into()));
    // Canonicalize once — `is_within` treats this as the canonical base so
    // per-request checks canonicalize only the target path.
    let library = std::fs::canonicalize(&library).unwrap_or(library);
    // Same once-at-startup semantics as app/auth.py's _TEST_MODE.
    let test_mode = std::env::var("BOOKQ_TEST_MODE")
        .unwrap_or_default()
        .eq_ignore_ascii_case("true");
    let state_dir = PathBuf::from(
        std::env::var("BOOKQ_STATE_DIR").unwrap_or_else(|_| "/var/lib/bookq".into()),
    );
    // #632: SECRET_KEY signs session cookies, verifies Bearer JWTs and keys
    // the visibility owner HMAC. The .env.example placeholder is public
    // knowledge — refuse to boot with it; empty stays fail-closed (warned),
    // short keys warn (dev's `dev-not-a-secret` trips this by design).
    let secret_key = std::env::var("SECRET_KEY").unwrap_or_default();
    match auth::secret_key_posture(&secret_key) {
        auth::SecretKeyPosture::Public => {
            tracing::error!(
                "SECRET_KEY is the .env.example placeholder — generate a random key \
                 (openssl rand -hex 32) and set it in .env"
            );
            std::process::exit(1);
        }
        auth::SecretKeyPosture::Missing => {
            tracing::warn!(
                "SECRET_KEY unset — session cookies and Bearer JWT auth are disabled \
                 (fail-closed); set it outside BOOKQ_TEST_MODE"
            );
        }
        auth::SecretKeyPosture::Weak => {
            tracing::warn!(
                "SECRET_KEY is shorter than 32 chars — use `openssl rand -hex 32` \
                 for anything beyond local dev"
            );
        }
        auth::SecretKeyPosture::Strong => {}
    }
    // No env allowlist: the users table is the only membership source.
    // A fresh DB starts empty; enroll the first admin with `viewer-rs users`.
    let users_store = std::sync::Arc::new(users::UserStore::new(&state_dir));
    let directory = std::sync::Arc::new(users::UserDirectory::new(users_store.clone()));
    // #400: the auth surface needs the full OAuth config — missing config
    // answers 501 (warned at startup).
    let auth_flow = match oauth::AuthFlow::from_env(users_store.clone(), directory.clone()) {
        oauth::Configured::Ready(flow) => Some(std::sync::Arc::new(flow)),
        oauth::Configured::Missing(missing) => {
            tracing::warn!(
                missing = ?missing,
                "OAuth config missing — /auth/* and /api/v1/auth/token answer 501"
            );
            None
        }
    };

    let index = std::sync::Arc::new(folders::IndexState::new());
    let registry =
        reader_sync::Registry::new(index.clone(), library.clone(), Some(directory.clone()));
    {
        let reg_ticker = registry.clone();
        tokio::spawn(async move { reg_ticker.run_ticker().await });
    }
    // #631 follow-up: explicit operator opt-in for the `?api_base=` mock-dev
    // flow (#237) — validated bare origins only, never the request param.
    let csp_extra = parse_csp_extra_origins(
        &std::env::var("BOOKQ_CSP_EXTRA_ORIGINS").unwrap_or_default(),
    );
    let st = AppState {
        library: library.clone(),
        test_mode,
        auth_flow,
        index: index.clone(),
        position_store: std::sync::Arc::new(state::PositionStore::new(&state_dir)),
        visibility: std::sync::Arc::new(state::VisibilityStore::new(&state_dir)),
        users: users_store,
        directory: directory.clone(),
        session_key: auth::session_key(&secret_key),
        secret_key,
        registry: registry.clone(),
        pair_reports: std::sync::Arc::new(pair_report::PairReports::new(
            std::collections::HashMap::new(),
        )),
        thumb_cache: PathBuf::from(
            std::env::var("BOOKQ_THUMB_CACHE_DIR").unwrap_or_else(|_| "/tmp/bookq-thumbs".into()),
        ),
        // config.py getenv defaults are "unknown" (env unset); the image's
        // ARG ""→ENV path yields "" — same two-state contract.
        git_sha: std::env::var("BOOKQ_GIT_SHA").unwrap_or_else(|_| "unknown".into()),
        build_date: std::env::var("BOOKQ_BUILD_DATE").unwrap_or_else(|_| "unknown".into()),
        // #401: the image ships `web/{static,templates,perf}` at /web; dev
        // points this at the worktree's `web/` for live asset edits.
        web_dir: PathBuf::from(
            std::env::var("BOOKQ_WEB_DIR").unwrap_or_else(|_| "/web".into()),
        ),
        // Library write surface: probe once — a read-only deploy keeps every
        // read route but `/api/v1/library/*` answers 503.
        upload_root: library::probe_upload_root(&library),
        place_lock: std::sync::Arc::new(std::sync::Mutex::new(())),
        session_lock: std::sync::Arc::new(tokio::sync::Mutex::new(())),
        csp_extra,
        guest_redeem_limit: guest::RedeemRateLimit::from_env(),
    };
    // #608: TEST_MODE trusts every caller as the mock user. An admin role in
    // that posture makes `/admin` reachable by anyone who can hit the URL, so
    // surface the combination loudly at boot (dev/test only by design).
    if st.test_mode {
        match st.users.list() {
            Ok(rows) => {
                let admins = rows
                    .iter()
                    .filter(|r| r.active() && r.role == users::Role::Admin)
                    .count();
                if admins > 0 {
                    tracing::warn!(
                        admins,
                        "BOOKQ_TEST_MODE is enabled with admin users — \
                         /admin trusts the mock user; remove admin roles after dev checks"
                    );
                }
            }
            _ => {}
        }
    }

    // Keep the hot auth path in memory; the CLI writes the DB, so a 1s
    // refresh bounds member changes without a per-request SQLite read.
    {
        let dir = directory.clone();
        tokio::spawn(async move {
            let mut ticker = tokio::time::interval(std::time::Duration::from_secs(1));
            ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
            loop {
                ticker.tick().await;
                let dir = dir.clone();
                if let Ok(Err(e)) =
                    tokio::task::spawn_blocking(move || dir.refresh()).await
                {
                    tracing::debug!(error = %e, "user directory refresh failed");
                }
            }
        });
    }

    // warmup_metadata_index — lifespan kicks the same cold-miss path.
    index.spawn_warmup(library.clone());
    tracing::info!(
        test_mode = st.test_mode,
        library = %st.library.display(),
        "all routes native"
    );

    let app = Router::new()
        .route("/healthz", get(|| async { "ok\n" }))
        // #401: shell + static surfaces — public.
        .route("/", any(shell_index))
        // #608: admin-only page. Both spellings map to the same gate so
        // `/admin/` does not fall through to the 501 fallback.
        .route("/admin", any(admin::admin_page))
        .route("/admin/", any(admin::admin_page))
        // #613: member-only mutations. The body limit is scoped here so the
        // three tiny form posts cannot be used as a memory sink.
        .merge(
            Router::new()
                .route("/admin/users/add", any(admin::admin_users_add))
                .route("/admin/users/disable", any(admin::admin_users_disable))
                .route("/admin/users/enable", any(admin::admin_users_enable))
                .route_layer(axum::extract::DefaultBodyLimit::max(8 * 1024)),
        )
        .route("/sw.js", any(serve_sw))
        .route("/favicon.ico", any(serve_favicon))
        .route("/static", any(serve_static))
        .route("/static/{*path}", any(serve_static))
        // /image/{*path} is a v1 contract path (media, not versioned).
        .route("/image/{*path}", any(serve_image))
        .route("/thumb/{*path}", any(serve_thumb))
        .route("/view/{*path}", any(serve_view))
        .route("/api/v1/thumb-sprite", any(api_thumb_sprite))
        .route("/api/v1/tree", any(api_tree))
        .route("/api/v1/images", any(api_images))
        .route("/api/v1/search", any(api_search))
        .route("/api/v1/facets", any(api_facets))
        .route("/api/v1/reading-positions", any(reading_positions))
        .route("/api/v1/folder-visibility", any(folder_visibility))
        .route("/api/v1/health", any(api_health))
        .route("/api/v1/version", any(api_version))
        .route("/api/v1/index/refresh", any(api_refresh))
        // #400: the auth surface moves as one unit — the callback's
        // one-time codes are redeemed by the token endpoint in the same
        // process. `auth_flow.is_none()` answers 501 (unconfigured).
        .route("/auth/login", any(auth_login))
        .route("/auth/callback", any(auth_callback))
        .route("/auth/logout", any(auth_logout))
        .route("/api/v1/auth/token", any(auth_token))
        // Unauthenticated endpoint: cap the body well under the 2MiB
        // default so redeem can't be a memory sink.
        .merge(
            Router::new()
                .route("/api/v1/guest/redeem", any(guest_redeem))
                .route_layer(axum::extract::DefaultBodyLimit::max(2048)),
        )
        // TEST_MODE-only dual-reader verification hooks (#405) — always
        // 404 outside test mode, never part of the OpenAPI contract.
        .route("/api/v1/pair-report", any(pair_report::handle_report))
        .route("/api/v1/pair-reset", any(pair_report::handle_reset))
        // #469: TEST_MODE-only `?hard=1` equivalent — wipe the mock user's
        // whole position slot incl. tombstones (harness per-test reset).
        .route("/api/v1/test-positions-reset", any(test_positions_reset))
        // M8: dual-reader sync socket.
        .route("/ws/reader-sync", any(reader_sync_ws))
        // Library edit: multipart upload + delete, chunked upload sessions.
        // `Multipart::from_request` calls `with_limited_body`, which honors
        // the 2MiB DefaultBodyLimit even for manual extractor calls — the
        // disable must be a route_layer scoped to these four routes, or
        // uploads >2MiB answer 413.
        .merge(
            Router::new()
                .route("/api/v1/library/files", any(library_files))
                .route("/api/v1/library/uploads", any(library_uploads))
                .route("/api/v1/library/uploads/{id}", any(library_upload))
                .route(
                    "/api/v1/library/uploads/{id}/finalize",
                    any(library_upload_finalize),
                )
                .route_layer(axum::extract::DefaultBodyLimit::disable()),
        )
        .fallback(axum::routing::any(|State(_st): State<AppState>, req: Request<Body>| async move {
            tracing::warn!(path = %req.uri().path(), "unknown route: answered 501");
            state::detail(StatusCode::NOT_IMPLEMENTED, "not found")
        }))
        // Innermost: runs after vary_session_cookie so its 403s still get
        // `Vary: Cookie`, and before every handler.
        .layer(axum::middleware::from_fn_with_state(
            st.clone(),
            guest_write_guard,
        ))
        // Marks guest responses for the SW cache policy — must wrap every
        // route, so it sits between the guard and vary layers.
        .layer(axum::middleware::from_fn_with_state(
            st.clone(),
            guest_response_marker,
        ))
        .layer(axum::middleware::from_fn_with_state(
            st.clone(),
            vary_session_cookie,
        ))
        // #631: baseline hardening headers — outermost so it sees the
        // final response (post-gzip) and never clobbers stricter per-route
        // values (admin CSP, SVG sandbox).
        .layer(axum::middleware::from_fn_with_state(
            st.clone(),
            security_headers,
        ))
        .with_state(st);

    let addr: SocketAddr = bind.parse().expect("BOOKQ_BIND must be host:port");
    tracing::info!(%addr, "viewer-rs listening (all routes native)");
    let listener = tokio::net::TcpListener::bind(addr).await.expect("bind failed");
    axum::serve(NoDelayListener(listener), app)
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await
        .expect("serve failed");
}

#[cfg(test)]
mod handler_tests {
    use super::*;

    /// #465 follow-up: a local 405 must carry the RFC 9110 `Allow` header
    /// naming the endpoint's declared methods (schemathesis ST204).
    #[test]
    fn method_not_allowed_carries_allow() {
        let res = method_not_allowed("GET, PUT, DELETE");
        assert_eq!(res.status(), StatusCode::METHOD_NOT_ALLOWED);
        assert_eq!(
            res.headers().get(header::ALLOW).unwrap().to_str().unwrap(),
            "GET, PUT, DELETE"
        );
    }

    /// Unverifiable callers answer 401 locally (#532: no upstream chain left).
    #[test]
    fn unauthenticated_answers_401() {
        assert_eq!(
            unauthenticated().status(),
            StatusCode::UNAUTHORIZED
        );
    }

    /// #631: every response gets the baseline headers; HTML additionally
    /// gets the app CSP + noindex, and a stricter handler-set CSP wins.
    #[test]
    fn security_headers_baseline_and_html_csp() {
        // Non-HTML (JSON/error shape): baseline only, no CSP — /sw.js's
        // response headers feed the worker scope where default-src 'none'
        // would break importScripts.
        let mut res = state::detail(StatusCode::OK, "ok");
        apply_security_headers(&mut res, &[], false);
        assert_eq!(res.headers()["x-content-type-options"], "nosniff");
        assert_eq!(res.headers()["x-frame-options"], "DENY");
        assert_eq!(res.headers()["referrer-policy"], "same-origin");
        assert_eq!(
            res.headers()["strict-transport-security"],
            "max-age=31536000"
        );
        assert!(!res.headers().contains_key("content-security-policy"));

        // HTML: app CSP + noindex on top of the baseline.
        let mut res = Response::builder()
            .status(StatusCode::OK)
            .header(header::CONTENT_TYPE, "text/html; charset=utf-8")
            .body(Body::empty())
            .unwrap();
        apply_security_headers(&mut res, &[], false);
        let csp = res.headers()["content-security-policy"]
            .to_str()
            .unwrap()
            .to_string();
        for needle in [
            "default-src 'self'",
            "frame-ancestors 'none'",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'self'",
        ] {
            assert!(csp.contains(needle), "CSP missing {needle}: {csp}");
        }
        assert_eq!(res.headers()["x-robots-tag"], "noindex");

        // TEST_MODE relaxes only script-src with 'unsafe-eval' — Playwright
        // drives pages via string eval; every other directive is unchanged.
        let mut res = Response::builder()
            .status(StatusCode::OK)
            .header(header::CONTENT_TYPE, "text/html; charset=utf-8")
            .body(Body::empty())
            .unwrap();
        apply_security_headers(&mut res, &[], true);
        let csp = res.headers()["content-security-policy"]
            .to_str()
            .unwrap()
            .to_string();
        assert!(csp.contains("script-src 'self' 'unsafe-inline' 'unsafe-eval'"));
        assert!(csp.contains("object-src 'none'"));
        assert!(csp.contains("frame-ancestors 'none'"));

        // A handler-set CSP (admin's stricter policy, SVG sandbox) is
        // never clobbered.
        let mut res = Response::builder()
            .status(StatusCode::OK)
            .header(header::CONTENT_TYPE, "text/html; charset=utf-8")
            .header("content-security-policy", "default-src 'none'")
            .body(Body::empty())
            .unwrap();
        apply_security_headers(&mut res, &[], false);
        assert_eq!(
            res.headers()["content-security-policy"],
            "default-src 'none'"
        );
    }

    /// #631 follow-up: `BOOKQ_CSP_EXTRA_ORIGINS` entries reach only
    /// img-src/connect-src on HTML responses — operator opt-in for the
    /// `?api_base=` mock flow (#237); the request param itself is never
    /// reflected.
    #[test]
    fn csp_extra_origins_extend_only_html_directives() {
        let extra = vec!["http://127.0.0.1:4010".to_string()];
        let mut res = Response::builder()
            .status(StatusCode::OK)
            .header(header::CONTENT_TYPE, "text/html; charset=utf-8")
            .body(Body::empty())
            .unwrap();
        apply_security_headers(&mut res, &extra, false);
        let csp = res.headers()["content-security-policy"]
            .to_str()
            .unwrap()
            .to_string();
        assert!(csp.contains("img-src 'self' data: blob: http://127.0.0.1:4010"));
        assert!(csp.contains("connect-src 'self' ws: wss: http://127.0.0.1:4010"));
        // Extras must not leak into unrelated directives.
        assert!(!csp.contains("script-src 'self' 'unsafe-inline' http"));
        assert!(!csp.contains("default-src 'self' http"));

        // Non-HTML responses never carry CSP, extras or not.
        let mut res = state::detail(StatusCode::OK, "ok");
        apply_security_headers(&mut res, &extra, false);
        assert!(!res.headers().contains_key("content-security-policy"));
    }

    /// #631 follow-up: only bare scheme://host[:port] origins survive —
    /// anything that could inject CSP syntax or smuggle a path is dropped.
    #[test]
    fn csp_extra_origin_validation() {
        for ok in [
            "http://127.0.0.1:4010",
            "https://mock.example.com",
            "ws://localhost:4010",
            "wss://[::1]:8443",
            "http://192.168.1.20:4010",
        ] {
            assert!(csp_origin_valid(ok), "{ok} should be valid");
        }
        for bad in [
            "http://127.0.0.1:4010/api",   // path
            "http://127.0.0.1:4010?x=1",   // query
            "javascript:alert(1)",          // wrong scheme
            "http://",                      // no host
            "http://host';object-src *",    // CSP injection attempt
            "http://user@host:1",           // userinfo
            "127.0.0.1:4010",               // no scheme
            "http://host name:1",           // space
        ] {
            assert!(!csp_origin_valid(bad), "{bad} should be invalid");
        }
        // Comma-separated parse keeps valid, drops invalid.
        let parsed = parse_csp_extra_origins(
            " http://127.0.0.1:4010 , bogus entry ,https://ok.example.com ",
        );
        assert_eq!(
            parsed,
            vec![
                "http://127.0.0.1:4010".to_string(),
                "https://ok.example.com".to_string()
            ]
        );
    }

    /// Missing OAuth config answers 501 locally (#532: no proxy fallback).
    #[test]
    fn auth_unconfigured_answers_501() {
        assert_eq!(
            auth_unconfigured().status(),
            StatusCode::NOT_IMPLEMENTED
        );
    }

    /// #359: `/api/v1/version` requires auth; health stays public.
    #[tokio::test]
    async fn version_requires_auth_health_stays_public() {
        // Unauthenticated version is 401 — sha/build_date must not leak.
        let st = hook_state(false);
        let (status, body) = body_json(
            api_version(State(st.clone()), http_req("GET", "/api/v1/version", "")).await,
        )
        .await;
        assert_eq!(status, StatusCode::UNAUTHORIZED);
        assert_eq!(body, serde_json::json!({"detail": "authentication required"}));
        // Health stays public for uptime probes.
        let (status, body) =
            body_json(api_health(State(st), http_req("GET", "/api/v1/health", "")).await).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body, serde_json::json!({"status": "ok"}));
        // Authed (test-mode mock user) version is 200 with no-store.
        let st = hook_state(true);
        let res = api_version(State(st), http_req("GET", "/api/v1/version", "")).await;
        assert_eq!(res.status(), StatusCode::OK);
        assert_eq!(
            res.headers()
                .get(header::CACHE_CONTROL)
                .unwrap()
                .to_str()
                .unwrap(),
            "no-store"
        );
        let (status, body) = body_json(res).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body["version"], serde_json::json!("2.0.0"));
        assert!(body.get("sha").is_some());
        assert!(body.get("build_date").is_some());
    }

    /// Minimal AppState for the test-hook handlers: nothing here touches
    /// the network (pair-report/reset are served from memory/the registry)
    /// or the filesystem (the SQLite stores are never consulted).
    fn hook_state(test_mode: bool) -> AppState {
        let dir = std::env::temp_dir().join(format!(
            "bookq-pair-hook-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let index = std::sync::Arc::new(folders::IndexState::new());
        let registry = reader_sync::Registry::new(index.clone(), dir.join("dl"), None);
        let users_store = std::sync::Arc::new(users::UserStore::new(&dir));
        let directory = std::sync::Arc::new(users::UserDirectory::new(users_store.clone()));
        AppState {
            library: dir.join("dl"),
            test_mode,
            index,
            position_store: std::sync::Arc::new(state::PositionStore::new(&dir)),
            visibility: std::sync::Arc::new(state::VisibilityStore::new(&dir)),
            users: users_store,
            directory,
            session_key: None,
            secret_key: "test-secret".into(),
            auth_flow: None,
            registry,
            web_dir: dir.join("web"),
            thumb_cache: dir.join("thumbs"),
            git_sha: "test".into(),
            build_date: "test".into(),
            pair_reports: std::sync::Arc::new(pair_report::PairReports::new(
                std::collections::HashMap::new(),
            )),
            upload_root: None,
            place_lock: std::sync::Arc::new(std::sync::Mutex::new(())),
            session_lock: std::sync::Arc::new(tokio::sync::Mutex::new(())),
            csp_extra: Vec::new(),
            guest_redeem_limit: guest::RedeemRateLimit::default(),
        }
    }

    fn http_req(method: &str, uri: &str, body: &str) -> Request<Body> {
        Request::builder()
            .method(method)
            .uri(uri)
            .header("content-type", "application/json")
            .body(Body::from(body.to_string()))
            .unwrap()
    }

    async fn body_json(res: Response) -> (StatusCode, serde_json::Value) {
        let status = res.status();
        let bytes = http_body_util::BodyExt::collect(res.into_body())
            .await
            .unwrap()
            .to_bytes();
        (status, serde_json::from_slice(&bytes).unwrap())
    }

    #[tokio::test]
    async fn pair_report_roundtrip_native_in_test_mode() {
        let st = hook_state(true);
        // POST stores (id stripped, received_at stamped) …
        let (status, body) = body_json(
            pair_report::handle_report(
                State(st.clone()),
                http_req(
                    "POST",
                    "/api/v1/pair-report",
                    r#"{"id":"pytest-bridge","gen":1,"pc":{"phase":"reading"},"pv":{"left":"1"}}"#,
                ),
            )
            .await,
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body, serde_json::json!({"ok": true}));
        // … GET reads it back …
        let (status, body) = body_json(
            pair_report::handle_report(
                State(st.clone()),
                http_req("GET", "/api/v1/pair-report?id=pytest-bridge", ""),
            )
            .await,
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body["state"]["pc"], serde_json::json!({"phase": "reading"}));
        assert_eq!(body["state"]["pv"], serde_json::json!({"left": "1"}));
        assert!(body["state"].get("id").is_none());
        assert!(body["state"]["received_at"].as_f64().unwrap() > 0.0);
        // … a stale gen from a parked tab drops …
        let (status, body) = body_json(
            pair_report::handle_report(
                State(st.clone()),
                http_req(
                    "POST",
                    "/api/v1/pair-report",
                    r#"{"id":"pytest-bridge","gen":0,"pc":{"phase":"stale"}}"#,
                ),
            )
            .await,
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body, serde_json::json!({"ok": true, "dropped": true}));
        // … unknown ids read back null …
        let (status, body) = body_json(
            pair_report::handle_report(
                State(st.clone()),
                http_req("GET", "/api/v1/pair-report?id=never-posted", ""),
            )
            .await,
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body, serde_json::json!({"state": null}));
        // … and a missing id is a 400 like the Python router.
        let (status, _) = body_json(
            pair_report::handle_report(
                State(st.clone()),
                http_req("POST", "/api/v1/pair-report", r#"{"pc":{}}"#),
            )
            .await,
        )
        .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
    }

    #[tokio::test]
    async fn pair_hooks_404_outside_test_mode() {
        let st = hook_state(false);
        for (handler, req) in [
            (
                "report-post",
                http_req("POST", "/api/v1/pair-report", r#"{"id":"x"}"#),
            ),
            ("report-get", http_req("GET", "/api/v1/pair-report?id=x", "")),
        ] {
            let (status, body) =
                body_json(pair_report::handle_report(State(st.clone()), req).await).await;
            assert_eq!(status, StatusCode::NOT_FOUND, "{handler}");
            assert_eq!(body, serde_json::json!({"detail": "Not Found"}), "{handler}");
        }
        let (status, body) = body_json(
            pair_report::handle_reset(
                State(st.clone()),
                http_req("POST", "/api/v1/pair-reset", ""),
            )
            .await,
        )
        .await;
        assert_eq!(status, StatusCode::NOT_FOUND);
        assert_eq!(body, serde_json::json!({"detail": "Not Found"}));
    }

    #[tokio::test]
    async fn pair_reset_ends_live_session_through_handler() {
        let st = hook_state(true);
        // One live session behind a registered link.
        let (tx, mut rx) =
            tokio::sync::mpsc::channel::<reader_sync::OutItem>(st.registry.send_queue_max());
        let (close_tx, _close_rx) = tokio::sync::watch::channel(None);
        let link = reader_sync::Link::new(
            tx,
            close_tx,
            None,
            None,
            std::time::Instant::now(),
            st.registry.heartbeat_interval(),
        );
        let lid = st
            .registry
            .inner
            .lock()
            .await
            .register_link(link, st.registry.heartbeat_interval());
        let create = reader_sync::protocol::encode_frame(&reader_sync::protocol::frame(&[
            (
                "v",
                serde_json::Value::from(reader_sync::protocol::PROTOCOL_VERSION),
            ),
            ("type", serde_json::Value::String("CREATE".into())),
            ("requestId", serde_json::Value::String("r1".into())),
            ("sessionId", serde_json::Value::String(String::new())),
            ("workEpoch", serde_json::Value::from(0)),
            ("baseRevision", serde_json::Value::from(0)),
            ("payload", serde_json::Value::Object(serde_json::Map::new())),
        ]))
        .unwrap();
        st.registry.handle_raw(&lid, create.as_bytes()).await;
        // The handler ends it with "reset" …
        let (status, body) = body_json(
            pair_report::handle_reset(
                State(st.clone()),
                http_req("POST", "/api/v1/pair-reset", ""),
            )
            .await,
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body, serde_json::json!({"ended": 1}));
        // … and the member link observes the END frame …
        let mut saw_end = false;
        while let Ok(item) = rx.try_recv() {
            if let reader_sync::OutItem::Text(raw) = item {
                let v: serde_json::Value = serde_json::from_str(&raw).unwrap();
                if v["type"] == "END" {
                    assert_eq!(v["reason"], "reset");
                    saw_end = true;
                }
            }
        }
        assert!(saw_end);
        assert_eq!(st.registry.session_count().await, 0);
        // … a second reset ends nothing.
        let (status, body) = body_json(
            pair_report::handle_reset(
                State(st.clone()),
                http_req("POST", "/api/v1/pair-reset", ""),
            )
            .await,
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body, serde_json::json!({"ended": 0}));
    }

    /// `/api/v1/thumb-sprite` — the only HTTP-level coverage of the
    /// handler: method gate, auth, query plumbing into `thumb_sprite`,
    /// and the image/* gzip exclusion (#582).
    #[tokio::test]
    async fn thumb_sprite_route_serves_sheet() {
        let mut st = hook_state(true);
        std::fs::create_dir_all(&st.library).unwrap();
        std::fs::create_dir_all(&st.thumb_cache).unwrap();
        // /var → /private/var on macOS: is_within compares canonical paths.
        st.library = st.library.canonicalize().unwrap();
        st.thumb_cache = st.thumb_cache.canonicalize().unwrap();
        std::fs::write(
            st.library.join("p.avif"),
            include_bytes!("../tests/fixtures/ok-48x64.avif"),
        )
        .unwrap();
        // GET with Accept-Encoding: gzip — image/* must stay identity.
        let req = Request::builder()
            .method("GET")
            .uri("/api/v1/thumb-sprite?folder=.&names=p.avif")
            .header("accept-encoding", "gzip")
            .body(Body::from(""))
            .unwrap();
        let res = api_thumb_sprite(State(st.clone()), req).await;
        assert_eq!(res.status(), StatusCode::OK);
        assert_eq!(res.headers()[header::CONTENT_TYPE], "image/jpeg");
        assert!(res.headers().get(header::CONTENT_ENCODING).is_none());
        // Method gate → 405; bad folder → 400 through the same plumbing.
        let res = api_thumb_sprite(
            State(st.clone()),
            http_req("POST", "/api/v1/thumb-sprite?folder=.&names=p.avif", ""),
        )
        .await;
        assert_eq!(res.status(), StatusCode::METHOD_NOT_ALLOWED);
        let res = api_thumb_sprite(
            State(st),
            http_req("GET", "/api/v1/thumb-sprite?folder=/x&names=p.avif", ""),
        )
        .await;
        assert_eq!(res.status(), StatusCode::BAD_REQUEST);
    }
}
