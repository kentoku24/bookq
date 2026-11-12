//! viewer-rs — Rust backend for bookq (issue #163, parallel-backend track).
//!
//! Runs on its own hostname/slot next to the untouched Python `viewer`.
//! Requests hit a routing table of natively implemented endpoints; everything
//! not yet ported is proxied to `BOOKQ_UPSTREAM` (the Python viewer) byte-
//! transparently, including WebSocket upgrades — so the backend is usable from
//! day one and endpoints migrate one at a time.

use axum::{
    body::Body,
    extract::{FromRequestParts, Query, Request, State},
    http::{header, HeaderValue, StatusCode, Uri},
    response::{IntoResponse, Response},
    routing::{any, get},
    Router,
};
use percent_encoding::percent_decode_str;
use hyper_util::{
    client::legacy::{connect::HttpConnector, Client},
    rt::{TokioExecutor, TokioIo},
};
use std::collections::HashMap;
use std::net::SocketAddr;
use std::path::PathBuf;

// v1 retarget (issue #163 M3+): only /api/v1/* endpoints get native
// handlers — v0 routes are deprecated upstream and must always proxy.
// serve/tree/folders hold the contract-independent core (archive listing,
// path containment, tree assembly, index cache) that M4–M7 wire onto the
// v1 envelopes; until then their exports are intentionally unused.
mod auth;
mod folders;
mod reader_sync;
mod search;
mod serve;
mod state;
mod thumbs;
mod tree;

#[derive(Clone)]
struct Upstream {
    client: Client<HttpConnector, Body>,
    authority: hyper::http::uri::Authority,
}

/// Which native surfaces are live. `BOOKQ_TEST_MODE=true` enables every
/// native route with the mock user (unchanged dev/test posture). Outside
/// test mode, `BOOKQ_RS_NATIVE` is a comma-separated list of endpoint ids
/// (`positions`, `visibility`, `image`, `tree`, …); `all`/`1`/`true`/`*`
/// enables the full native surface. Anything not enabled, plus any
/// request we cannot positively identify (no verifiable session cookie,
/// missing/bad Bearer), falls through to upstream — production rollout
/// is per endpoint via config only.
pub(crate) struct Native {
    pub(crate) test_mode: bool,
    all: bool,
    set: std::collections::HashSet<String>,
}

/// Endpoint groups that answer from the same in-process tree/index snapshot
/// and therefore have to be cut over together (#281).
///
/// `tree` / `search` / `facets` / `images` all read [`folders::IndexState`]:
/// enabling only some of them makes browse and search report different
/// generations from different backends (#261). `reader-sync`'s catalog
/// resolver builds its manifest from the same tree, so a native socket with a
/// proxied tree resolves `OPEN_WORK` against the other backend's flatten/fill
/// state.
const NATIVE_GROUPS: [&[&str]; 2] = [
    &["tree", "search", "facets", "images"],
    &["tree", "reader-sync"],
];

impl Native {
    fn from_env() -> Self {
        let test_mode = std::env::var("BOOKQ_TEST_MODE")
            .unwrap_or_default()
            .eq_ignore_ascii_case("true");
        let mut all = test_mode;
        let mut set = std::collections::HashSet::new();
        for tok in std::env::var("BOOKQ_RS_NATIVE")
            .unwrap_or_default()
            .split(',')
            .map(|t| t.trim().to_lowercase())
            .filter(|t| !t.is_empty())
        {
            match tok.as_str() {
                "1" | "true" | "all" | "*" => all = true,
                other => {
                    set.insert(other.to_string());
                }
            }
        }
        Self::from_parts(test_mode, all, set)
    }

    /// Env-free constructor: the gate below is unit-tested against it instead
    /// of mutating process-global `BOOKQ_RS_NATIVE` (which cargo's parallel
    /// test threads share).
    fn from_parts(test_mode: bool, all: bool, set: std::collections::HashSet<String>) -> Self {
        Self { test_mode, all, set }
    }

    pub(crate) fn enabled(&self, ep: &str) -> bool {
        self.all || self.set.contains(ep)
    }

    /// Groups where at least one member is native and at least one is not.
    ///
    /// All-or-nothing per group is the only safe cutover state: a partial one
    /// silently splits the index between the two backends. Groups nobody has
    /// enabled are fine — that is the "all proxy" fallback posture.
    pub(crate) fn partial_groups(&self) -> Vec<&'static [&'static str]> {
        NATIVE_GROUPS
            .iter()
            .copied()
            .filter(|group| {
                let enabled = group.iter().filter(|ep| self.enabled(ep)).count();
                enabled > 0 && enabled < group.len()
            })
            .collect()
    }
}

/// `BOOKQ_RS_NATIVE_STRICT=1` promotes the partial-group warning to a start-up
/// failure, so a cutover runbook can gate on "the gateway refused a split
/// configuration" instead of grepping logs.
fn native_strict() -> bool {
    matches!(
        std::env::var("BOOKQ_RS_NATIVE_STRICT")
            .unwrap_or_default()
            .trim()
            .to_ascii_lowercase()
            .as_str(),
        "1" | "true" | "yes"
    )
}

#[derive(Clone)]
pub(crate) struct AppState {
    upstream: Upstream,
    /// Downloads root, resolved once (symlinks collapsed like the Python side).
    pub(crate) downloads: PathBuf,
    /// Native-surface gate — see `Native`.
    pub(crate) native: std::sync::Arc<Native>,
    /// `ALLOWED_EMAILS` (trim+lowercase) — Bearer allowlist, fail-closed.
    pub(crate) allowed_emails: std::sync::Arc<std::collections::HashSet<String>>,
    /// `/api/folders` index cache (`metadata_index_cache`).
    pub(crate) index: std::sync::Arc<folders::IndexState>,
    /// M4+375: per-user reading positions in the shared user-state SQLite
    /// store (`ReadingPositionStore`) — same DB the Python viewer writes.
    pub(crate) position_store: std::sync::Arc<state::PositionStore>,
    /// M4: shared user-state SQLite store (`FolderVisibilityStore`).
    pub(crate) visibility: std::sync::Arc<state::VisibilityStore>,
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
    /// TEST_MODE-only pair-report state bridge (#405) — in-memory map keyed
    /// by client id, the native port of `app/api_v1.py::_PAIR_REPORTS`.
    pub(crate) pair_reports: std::sync::Arc<std::sync::Mutex<HashMap<String, serde_json::Value>>>,
}

/// RFC 7230 hop-by-hop headers, minus `upgrade`/`connection` which the upgrade
/// path handles separately.
const HOP_BY_HOP: [header::HeaderName; 7] = [
    header::HeaderName::from_static("keep-alive"),
    header::PROXY_AUTHENTICATE,
    header::PROXY_AUTHORIZATION,
    header::TE,
    header::TRAILER,
    header::TRANSFER_ENCODING,
    header::HeaderName::from_static("proxy-connection"),
];

fn strip_hop_by_hop(headers: &mut axum::http::HeaderMap) {
    for h in HOP_BY_HOP {
        headers.remove(h);
    }
}

/// `Connection: upgrade` + `Upgrade: <proto>` → the client wants a protocol
/// switch (WebSocket in bookq's case: `/ws/reader-sync`).
fn is_upgrade(req: &Request) -> bool {
    let wants = req
        .headers()
        .get(header::CONNECTION)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.split(',').any(|t| t.trim().eq_ignore_ascii_case("upgrade")));
    wants && req.headers().contains_key(header::UPGRADE)
}

fn bad_gateway(err: impl std::fmt::Display) -> Response {
    (StatusCode::BAD_GATEWAY, format!("upstream error: {err}\n")).into_response()
}

/// Forward `req` (already rewritten to the upstream authority) and map the
/// response back. For upgrade requests, splice the two sockets once the
/// upstream agrees (101) so frames flow transparently both ways.
async fn forward(mut req: Request<Body>, st: &Upstream) -> Response {
    let upgrade = is_upgrade(&req);
    let client_upgrade = upgrade.then(|| hyper::upgrade::on(&mut req));

    if !upgrade {
        strip_hop_by_hop(req.headers_mut());
    }
    let mut res = match st.client.request(req).await {
        Ok(res) => res,
        Err(e) => return bad_gateway(e),
    };

    // Grab the upstream upgrade token before consuming the response head.
    let server_upgrade = upgrade.then(|| hyper::upgrade::on(&mut res));

    let (mut parts, body) = res.into_parts();
    strip_hop_by_hop(&mut parts.headers);
    let out = Response::from_parts(parts, Body::new(body));

    if upgrade && out.status() == StatusCode::SWITCHING_PROTOCOLS {
        tokio::spawn(async move {
            if let (Some(client_upgrade), Some(server_upgrade)) = (client_upgrade, server_upgrade) {
                if let (Ok(client), Ok(server)) = (client_upgrade.await, server_upgrade.await) {
                    let mut client = TokioIo::new(client);
                    let mut server = TokioIo::new(server);
                    let _ = tokio::io::copy_bidirectional(&mut client, &mut server).await;
                }
            }
        });
    }
    out
}

async fn proxy(st: &Upstream, mut req: Request<Body>) -> Response {
    let mut parts = req.uri().clone().into_parts();
    parts.scheme = Some("http".parse().expect("static scheme"));
    parts.authority = Some(st.authority.clone());
    match Uri::from_parts(parts) {
        Ok(uri) => *req.uri_mut() = uri,
        Err(e) => return bad_gateway(e),
    }
    forward(req, st).await
}

/// Resolve the caller for a native route: unidentified requests (no
/// verifiable session cookie, no/invalid Bearer) always proxy upstream,
/// which applies its own cookie → Bearer → 401 chain.
fn caller_or_proxy(st: &AppState, headers: &axum::http::HeaderMap) -> Option<auth::Identity> {
    match auth::resolve(
        headers,
        st.native.test_mode,
        &st.secret_key,
        st.session_key.as_ref(),
        &st.allowed_emails,
    ) {
        auth::Caller::User(u) => Some(u),
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
/// (valid signature, any payload). Now that cookie-authenticated
/// requests are served natively, a shared HTTP cache must still key on
/// the cookie or one user's cached page could answer another's — this
/// matters most once per-user surfaces (positions, visibility, search)
/// are enabled. Proxied responses carry upstream's own Vary, which
/// `vary_lists_cookie` dedupes.
async fn vary_session_cookie(
    State(st): State<AppState>,
    req: Request<Body>,
    next: axum::middleware::Next,
) -> Response {
    let signed = !matches!(
        auth::session_lookup(req.headers(), st.session_key.as_ref()),
        auth::SessionLookup::Absent
    );
    let mut res = next.run(req).await;
    if signed && !vary_lists_cookie(res.headers()) {
        res.headers_mut()
            .append(header::VARY, HeaderValue::from_static("Cookie"));
    }
    res
}

/// `/api/v1/reading-positions` — M4+375: per-folder LWW + tombstones,
/// native on the shared user-state SQLite store (same rows the Python
/// viewer reads/writes, so py/rs cannot diverge — #263 resolved).
async fn reading_positions(State(st): State<AppState>, req: Request<Body>) -> Response {
    if !st.native.enabled("positions") {
        return proxy(&st.upstream, req).await;
    }
    let Some(user) = caller_or_proxy(&st, req.headers()) else {
        return proxy(&st.upstream, req).await;
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
            // `folder` is required → missing keeps FastAPI's 422 upstream.
            let Some(folder) = q.get("folder").cloned() else {
                return proxy(&st.upstream, req).await;
            };
            let (parts, body) = req.into_parts();
            let body = match http_body_util::BodyExt::collect(body).await {
                Ok(c) => c.to_bytes(),
                Err(_) => {
                    return proxy(&st.upstream, Request::from_parts(parts, Body::empty()))
                        .await
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
        _ => return proxy(&st.upstream, req).await,
    }
}

/// `/api/v1/folder-visibility` — M4: shared SQLite store + owner HMAC.
async fn folder_visibility(State(st): State<AppState>, req: Request<Body>) -> Response {
    if !st.native.enabled("visibility") {
        return proxy(&st.upstream, req).await;
    }
    let Some(user) = caller_or_proxy(&st, req.headers()) else {
        return proxy(&st.upstream, req).await;
    };
    // `user_key_for_email`: email.strip().lower()
    let key = user.email.trim().to_lowercase();
    let headers = req.headers().clone();
    match *req.method() {
        axum::http::Method::GET => {
            serve::maybe_gzip(&headers, state::get_folder_visibility(&st, &key).await, true).await
        }
        axum::http::Method::PUT => {
            let (parts, body) = req.into_parts();
            let body = match http_body_util::BodyExt::collect(body).await {
                Ok(c) => c.to_bytes(),
                Err(_) => {
                    return proxy(&st.upstream, Request::from_parts(parts, Body::empty()))
                        .await
                }
            };
            state::put_folder_visibility(&st, &key, &headers, &body).await
        }
        _ => return proxy(&st.upstream, req).await,
    }
}

/// `/api/v1/tree` — the v1 envelope `{kind, nodes, generation}` over the
/// shared index cache (`_folder_tree(v1=True)`).
async fn api_tree(State(st): State<AppState>, req: Request<Body>) -> Response {
    if !st.native.enabled("tree") || req.method() != axum::http::Method::GET {
        return proxy(&st.upstream, req).await;
    }
    if caller_or_proxy(&st, req.headers()).is_none() {
        return proxy(&st.upstream, req).await;
    }
    let q = Query::<HashMap<String, String>>::try_from_uri(req.uri())
        .map(|q| q.0)
        .unwrap_or_default();
    let headers = req.headers().clone();
    match folders::api_tree(&st, &headers, q).await {
        serve::Serve::Done(res) => serve::maybe_gzip(&headers, res, true).await,
        serve::Serve::Proxy => proxy(&st.upstream, req).await,
    }
}

/// `/api/v1/images` — `{folder, images[]}` over `_list_folder_images`.
async fn api_images(State(st): State<AppState>, req: Request<Body>) -> Response {
    if !st.native.enabled("images") || req.method() != axum::http::Method::GET {
        return proxy(&st.upstream, req).await;
    }
    if caller_or_proxy(&st, req.headers()).is_none() {
        return proxy(&st.upstream, req).await;
    }
    let q = Query::<HashMap<String, String>>::try_from_uri(req.uri())
        .map(|q| q.0)
        .unwrap_or_default();
    // `folder` is a required Query param upstream → missing keeps the 422.
    let Some(folder) = q.get("folder").cloned() else {
        return proxy(&st.upstream, req).await;
    };
    let headers = req.headers().clone();
    let downloads = st.downloads.clone();
    let out = tokio::task::spawn_blocking(move || serve::api_images(&downloads, &folder))
        .await
        .unwrap_or(serve::Serve::Proxy);
    match out {
        serve::Serve::Done(res) => serve::maybe_gzip(&headers, res, true).await,
        serve::Serve::Proxy => proxy(&st.upstream, req).await,
    }
}

/// `/api/v1/search` — relevance + filters over the shared index; hidden
/// roots applied server-side (fail-closed).
async fn api_search(State(st): State<AppState>, req: Request<Body>) -> Response {
    if !st.native.enabled("search") || req.method() != axum::http::Method::GET {
        return proxy(&st.upstream, req).await;
    }
    // `_user` still gates auth upstream; hidden roots are library-global.
    let Some(_user) = caller_or_proxy(&st, req.headers()) else {
        return proxy(&st.upstream, req).await;
    };
    let q = Query::<HashMap<String, String>>::try_from_uri(req.uri())
        .map(|q| q.0)
        .unwrap_or_default();
    let headers = req.headers().clone();
    match search::api_search(&st, &q).await {
        serve::Serve::Done(res) => serve::maybe_gzip(&headers, res, true).await,
        serve::Serve::Proxy => proxy(&st.upstream, req).await,
    }
}

/// `/api/v1/facets` — `{artists, tags}` scoped to `path`'s subtree; hidden
/// roots applied server-side (fail-closed).
async fn api_facets(State(st): State<AppState>, req: Request<Body>) -> Response {
    if !st.native.enabled("facets") || req.method() != axum::http::Method::GET {
        return proxy(&st.upstream, req).await;
    }
    // `_user` still gates auth upstream; hidden roots are library-global.
    let Some(_user) = caller_or_proxy(&st, req.headers()) else {
        return proxy(&st.upstream, req).await;
    };
    let q = Query::<HashMap<String, String>>::try_from_uri(req.uri())
        .map(|q| q.0)
        .unwrap_or_default();
    let headers = req.headers().clone();
    match search::api_facets(&st, &q).await {
        serve::Serve::Done(res) => serve::maybe_gzip(&headers, res, true).await,
        serve::Serve::Proxy => proxy(&st.upstream, req).await,
    }
}

/// `/api/v1/health` — public (`security: []`); `{status:"ok"}`.
async fn api_health(State(st): State<AppState>, req: Request<Body>) -> Response {
    if !st.native.enabled("health") || req.method() != axum::http::Method::GET {
        return proxy(&st.upstream, req).await;
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
    if !st.native.enabled("version") || req.method() != axum::http::Method::GET {
        return proxy(&st.upstream, req).await;
    }
    if caller_or_proxy(&st, req.headers()).is_none() {
        return proxy(&st.upstream, req).await;
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

/// `/api/v1/index/refresh` — authed POST; synchronous rebuild under the
/// same swap semantics as the background gate, `{status,folders}`.
async fn api_refresh(State(st): State<AppState>, req: Request<Body>) -> Response {
    if !st.native.enabled("refresh") || req.method() != axum::http::Method::POST {
        return proxy(&st.upstream, req).await;
    }
    if caller_or_proxy(&st, req.headers()).is_none() {
        return proxy(&st.upstream, req).await;
    }
    let headers = req.headers().clone();
    match folders::force_rebuild(&st.index, &st.downloads).await {
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
        // Build failure propagates upstream in Python — proxy for parity.
        None => proxy(&st.upstream, req).await,
    }
}

// ── TEST_MODE hooks: /api/v1/pair-report + /api/v1/pair-reset ────────────────
// Native ports of `app/api_v1.py`'s `_PAIR_REPORTS` map and its two test
// hooks (#405): the in-page `?pair_report=<cid>` hook (pairSync.js, test
// mode only) POSTs PairController/PairedView state here so the dual-reader
// verification runner can read structured state back over HTTP from clients
// it cannot JS-eval (iOS Simulator Safari, PWA webclips), and `pair-reset`
// ends every live reader-sync session so a stale link-dropped session can't
// hijack the next MATCH. Gated on `native.test_mode` only — never on
// `enabled(...)`: outside test mode both answer 404, mirroring
// `_pair_report_gate`. Deliberately NOT in `api/openapi.v1.yaml` — a test
// hook, not API contract (axum generates no schema, so nothing to exclude).

fn pair_json(status: StatusCode, body: serde_json::Value) -> Response {
    let mut res = Response::builder()
        .status(status)
        .body(Body::from(serde_json::to_string(&body).unwrap()))
        .unwrap();
    res.headers_mut().insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("application/json"),
    );
    res
}

/// `_pair_report_gate` — FastAPI renders `HTTPException(404)` with no
/// explicit detail as exactly this body; the native gate mirrors it so the
/// hook's 404 probe can't tell native from Python.
fn pair_gate_denied() -> Response {
    pair_json(
        StatusCode::NOT_FOUND,
        serde_json::json!({"detail": "Not Found"}),
    )
}

/// `str(body.get("id") or "")[:64]` — falsy ids (missing, null, "", 0,
/// false, empty containers) become "" and 400 below. Truncation is by chars,
/// like Python's slice.
fn report_cid(v: Option<&serde_json::Value>) -> String {
    use serde_json::Value;
    let s = match v {
        None | Some(Value::Null) => return String::new(),
        Some(Value::Bool(false)) => return String::new(),
        Some(Value::Bool(true)) => "True".to_string(),
        Some(Value::Number(n)) => {
            if n.as_i64() == Some(0) || n.as_f64() == Some(0.0) {
                return String::new();
            }
            n.to_string()
        }
        Some(Value::String(s)) => {
            if s.is_empty() {
                return String::new();
            }
            s.clone()
        }
        // Non-empty containers have no hook-id use; treat as missing.
        Some(Value::Array(_)) | Some(Value::Object(_)) => return String::new(),
    };
    s.chars().take(64).collect()
}

/// `int(body.get("gen") or 0)` — a falsy gen (missing, null, 0, "", false)
/// is generation 0; floats truncate like Python's `int()`.
fn report_gen(v: Option<&serde_json::Value>) -> i64 {
    use serde_json::Value;
    match v {
        None | Some(Value::Null) | Some(Value::Bool(false)) => 0,
        Some(Value::Bool(true)) => 1,
        Some(Value::Number(n)) => n
            .as_i64()
            .or_else(|| n.as_f64().map(|f| f.trunc() as i64))
            .unwrap_or(0),
        Some(Value::String(s)) => s.trim().parse::<i64>().unwrap_or_else(|_| {
            s.trim()
                .parse::<f64>()
                .map(|f| f.trunc() as i64)
                .unwrap_or(0)
        }),
        Some(Value::Array(_)) | Some(Value::Object(_)) => 0,
    }
}

fn pair_now_secs() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// `POST /api/v1/pair-report` — store `{id, gen?, ...state}` keyed by client
/// id. A parked tab from a previous run keeps POSTing under the same id, so
/// only the newest generation wins (`rpt_gen` from the launch URL); gen-less
/// posts lose to any gen'd report.
async fn pair_report_post(st: &AppState, req: Request<Body>) -> Response {
    let body = req.into_body();
    let bytes = match http_body_util::BodyExt::collect(body).await {
        Ok(c) => c.to_bytes(),
        Err(_) => {
            return pair_json(
                StatusCode::BAD_REQUEST,
                serde_json::json!({"detail": "invalid JSON body"}),
            )
        }
    };
    let mut obj = match serde_json::from_slice::<serde_json::Value>(&bytes) {
        Ok(serde_json::Value::Object(m)) => m,
        Ok(_) => {
            return pair_json(
                StatusCode::BAD_REQUEST,
                serde_json::json!({"detail": "expected JSON object"}),
            )
        }
        Err(_) => {
            return pair_json(
                StatusCode::BAD_REQUEST,
                serde_json::json!({"detail": "invalid JSON body"}),
            )
        }
    };
    let cid = report_cid(obj.get("id"));
    if cid.is_empty() {
        return pair_json(
            StatusCode::BAD_REQUEST,
            serde_json::json!({"detail": "id required"}),
        );
    }
    obj.remove("id");
    let gen = report_gen(obj.get("gen"));
    let dominated = {
        let mut map = st.pair_reports.lock().unwrap();
        let dominated = map
            .get(&cid)
            .is_some_and(|cur| gen < report_gen(cur.as_object().and_then(|m| m.get("gen"))));
        if !dominated {
            obj.insert(
                "received_at".to_string(),
                serde_json::json!(pair_now_secs()),
            );
            map.insert(cid, serde_json::Value::Object(obj));
        }
        dominated
    };
    if dominated {
        pair_json(
            StatusCode::OK,
            serde_json::json!({"ok": true, "dropped": true}),
        )
    } else {
        pair_json(StatusCode::OK, serde_json::json!({"ok": true}))
    }
}

/// `GET /api/v1/pair-report?id=` — read the stored state back (`null` when
/// the id was never posted). A missing `id` keeps FastAPI's 422 upstream.
async fn pair_report_get(st: &AppState, req: Request<Body>) -> Response {
    let q = Query::<HashMap<String, String>>::try_from_uri(req.uri())
        .map(|q| q.0)
        .unwrap_or_default();
    let Some(id) = q.get("id") else {
        return proxy(&st.upstream, req).await;
    };
    let state = st
        .pair_reports
        .lock()
        .unwrap()
        .get(id)
        .cloned()
        .unwrap_or(serde_json::Value::Null);
    pair_json(StatusCode::OK, serde_json::json!({"state": state}))
}

async fn pair_report(State(st): State<AppState>, req: Request<Body>) -> Response {
    if !st.native.test_mode {
        return pair_gate_denied();
    }
    if req.method() == axum::http::Method::POST {
        return pair_report_post(&st, req).await;
    }
    if req.method() == axum::http::Method::GET {
        return pair_report_get(&st, req).await;
    }
    proxy(&st.upstream, req).await
}

/// `POST /api/v1/pair-reset` — end every live reader-sync session with
/// "reset". A simulator client that went idle by link-drop never LEAVEd —
/// its server-side session lingers in the resume window and hijacks the
/// next MATCH.
async fn pair_reset(State(st): State<AppState>, req: Request<Body>) -> Response {
    if !st.native.test_mode {
        return pair_gate_denied();
    }
    if req.method() != axum::http::Method::POST {
        return proxy(&st.upstream, req).await;
    }
    let ended = st.registry.end_all("reset").await;
    pair_json(StatusCode::OK, serde_json::json!({"ended": ended}))
}

/// `/image/{*path}` — a v1 contract path (media is not versioned): native
/// for plain files and ZIP/CBZ/EPUB/RAR/CBR entries.
/// PDF pages and multi-range requests proxy upstream.
async fn serve_image(State(st): State<AppState>, req: Request<Body>) -> Response {
    if !st.native.enabled("image") || req.method() != axum::http::Method::GET {
        return proxy(&st.upstream, req).await;
    }
    // /image requires auth upstream too — only positively identified
    // callers may be served natively.
    if caller_or_proxy(&st, req.headers()).is_none() {
        return proxy(&st.upstream, req).await;
    }
    // The URI path is still percent-encoded; decode per-segment like
    // Starlette does (`%2F` inside a segment becomes "/" and is handled by
    // the same traversal guards downstream).
    let Some(raw) = req.uri().path().strip_prefix("/image/") else {
        return proxy(&st.upstream, req).await;
    };
    let Ok(path) = percent_decode_str(raw).decode_utf8() else {
        return proxy(&st.upstream, req).await;
    };
    let headers = req.headers().clone();
    let downloads = st.downloads.clone();
    let path = path.into_owned();
    let out = tokio::task::spawn_blocking(move || serve::image(&downloads, &path, &headers))
        .await
        .unwrap_or(serve::Serve::Proxy);
    match out {
        serve::Serve::Proxy => proxy(&st.upstream, req).await,
        serve::Serve::Done(res) => res, // /image is outside the gzip prefixes
    }
}

/// `/thumb/{*path}` — v1 contract path. Native answers validation, the
/// 304 fast path and shared-cache hits; misses render in-process (image
/// crate) with upstream fallback for sources/decodes it can't reach.
async fn serve_thumb(State(st): State<AppState>, req: Request<Body>) -> Response {
    if !st.native.enabled("thumb") || req.method() != axum::http::Method::GET {
        return proxy(&st.upstream, req).await;
    }
    if caller_or_proxy(&st, req.headers()).is_none() {
        return proxy(&st.upstream, req).await;
    }
    let Some(raw) = req.uri().path().strip_prefix("/thumb/") else {
        return proxy(&st.upstream, req).await;
    };
    let Ok(path) = percent_decode_str(raw).decode_utf8() else {
        return proxy(&st.upstream, req).await;
    };
    let q = Query::<HashMap<String, String>>::try_from_uri(req.uri())
        .map(|q| q.0)
        .unwrap_or_default();
    let headers = req.headers().clone();
    let downloads = st.downloads.clone();
    let cache = st.thumb_cache.clone();
    let path = path.into_owned();
    let out =
        tokio::task::spawn_blocking(move || thumbs::thumb(&downloads, &cache, &path, q.get("w"), &headers))
            .await
            .unwrap_or(serve::Serve::Proxy);
    match out {
        serve::Serve::Proxy => proxy(&st.upstream, req).await,
        serve::Serve::Done(res) => res, // /thumb is outside the gzip prefixes
    }
}

/// `/view/{*path}` — passthrough formats serve natively; normalized
/// formats answer shared-cache hits and render misses in-process;
/// undecodable/unreachable sources still proxy.
async fn serve_view(State(st): State<AppState>, req: Request<Body>) -> Response {
    if !st.native.enabled("view") || req.method() != axum::http::Method::GET {
        return proxy(&st.upstream, req).await;
    }
    if caller_or_proxy(&st, req.headers()).is_none() {
        return proxy(&st.upstream, req).await;
    }
    let Some(raw) = req.uri().path().strip_prefix("/view/") else {
        return proxy(&st.upstream, req).await;
    };
    let Ok(path) = percent_decode_str(raw).decode_utf8() else {
        return proxy(&st.upstream, req).await;
    };
    let headers = req.headers().clone();
    let downloads = st.downloads.clone();
    let cache = st.thumb_cache.clone();
    let path = path.into_owned();
    let out = tokio::task::spawn_blocking(move || thumbs::view(&downloads, &cache, &path, &headers))
        .await
        .unwrap_or(serve::Serve::Proxy);
    match out {
        serve::Serve::Proxy => proxy(&st.upstream, req).await,
        serve::Serve::Done(res) => res, // /view is outside the gzip prefixes
    }
}

/// `/api/v1/thumb-sprite` — v1 contract (v0 `/api/thumb-sprite` proxies).
/// Native answers validation, 304s and shared-cache hits; misses proxy.
async fn api_thumb_sprite(State(st): State<AppState>, req: Request<Body>) -> Response {
    if !st.native.enabled("thumb-sprite") || req.method() != axum::http::Method::GET {
        return proxy(&st.upstream, req).await;
    }
    if caller_or_proxy(&st, req.headers()).is_none() {
        return proxy(&st.upstream, req).await;
    }
    let q = Query::<HashMap<String, String>>::try_from_uri(req.uri())
        .map(|q| q.0)
        .unwrap_or_default();
    let query_string = req.uri().query().unwrap_or("").to_string();
    let headers = req.headers().clone();
    let headers2 = headers.clone();
    let downloads = st.downloads.clone();
    let cache = st.thumb_cache.clone();
    let out = tokio::task::spawn_blocking(move || {
        thumbs::thumb_sprite(&downloads, &cache, &q, &query_string, &headers2)
    })
    .await
    .unwrap_or(serve::Serve::Proxy);
    match out {
        serve::Serve::Proxy => proxy(&st.upstream, req).await,
        // image/* responses are excluded from ApiGZipMiddleware by content type.
        serve::Serve::Done(res) => {
            let compressible = !res
                .headers()
                .get(header::CONTENT_TYPE)
                .and_then(|v| v.to_str().ok())
                .is_some_and(|ct| ct.starts_with("image/"));
            serve::maybe_gzip(&headers, res, compressible).await
        }
    }
}

// ── M8: /ws/reader-sync ────────────────────────────────────────────────────

/// Native `/ws/reader-sync` — gated by "reader-sync"; a non-upgrade or
/// gated-out request goes upstream so Python's own socket serves it.
/// Unauthenticated handshakes get starlette's pre-accept close → HTTP 403.
async fn reader_sync_ws(State(st): State<AppState>, req: Request<Body>) -> Response {
    if !st.native.enabled("reader-sync") || !is_upgrade(&req) {
        return proxy(&st.upstream, req).await;
    }
    let Some(user) = auth::resolve_ws_user(
        req.headers(),
        st.native.test_mode,
        &st.secret_key,
        st.session_key.as_ref(),
        &st.allowed_emails,
    ) else {
        return StatusCode::FORBIDDEN.into_response();
    };
    let (mut parts, _body) = req.into_parts();
    match axum::extract::ws::WebSocketUpgrade::from_request_parts(&mut parts, &st).await {
        Ok(wsu) => {
            let st2 = st.clone();
            wsu.on_upgrade(move |socket| reader_sync_conn(socket, st2, user))
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
        }
    }

    loop {
        tokio::select! {
            biased;
            changed = close.changed() => {
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
                    Err(_) => break,
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
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "viewer_rs=info".into()),
        )
        .init();

    let upstream = std::env::var("BOOKQ_UPSTREAM").unwrap_or_else(|_| "http://viewer:8080".into());
    let bind = std::env::var("BOOKQ_BIND").unwrap_or_else(|_| "0.0.0.0:8080".into());
    let authority: hyper::http::uri::Authority = upstream
        .strip_prefix("http://")
        .unwrap_or(&upstream)
        .parse()
        .expect("BOOKQ_UPSTREAM must be http://host[:port]");
    let downloads = PathBuf::from(std::env::var("DOWNLOADS_DIR").unwrap_or_else(|_| "/app/downloads".into()));
    // Canonicalize once — `is_within` treats this as the canonical base so
    // per-request checks canonicalize only the target path.
    let downloads = std::fs::canonicalize(&downloads).unwrap_or(downloads);
    // Same once-at-startup semantics as app/auth.py's _TEST_MODE.
    let native = Native::from_env();
    // #281: a partial group is a silent split-brain — browse answers from one
    // backend's index, search from the other's. Warn only, because production
    // deliberately walks through partial states while rolling out; the strict
    // flag is for cutover runbooks and CI, where a split config must not boot.
    let partial_groups = native.partial_groups();
    for group in &partial_groups {
        tracing::warn!(
            group = ?group,
            native_set = ?native.set,
            native_all = native.all,
            "partial native cutover: group members read one tree/index snapshot and must be enabled together (#281)"
        );
    }
    if !partial_groups.is_empty() && native_strict() {
        tracing::error!(
            groups = ?partial_groups,
            native_set = ?native.set,
            "BOOKQ_RS_NATIVE_STRICT: refusing a partial native cutover"
        );
        std::process::exit(2);
    }
    let allowed_emails: std::collections::HashSet<String> = std::env::var("ALLOWED_EMAILS")
        .unwrap_or_default()
        .split(',')
        .map(|e| e.trim().to_lowercase())
        .filter(|e| !e.is_empty())
        .collect();

    let index = std::sync::Arc::new(folders::IndexState::new());
    let registry = reader_sync::Registry::new(index.clone(), downloads.clone());
    {
        let reg_ticker = registry.clone();
        tokio::spawn(async move { reg_ticker.run_ticker().await });
    }
    let state_dir = PathBuf::from(
        std::env::var("BOOKQ_STATE_DIR").unwrap_or_else(|_| "/var/lib/bookq".into()),
    );
    let st = AppState {
        upstream: Upstream {
            client: Client::builder(TokioExecutor::new()).build(HttpConnector::new()),
            authority,
        },
        downloads: downloads.clone(),
        native: std::sync::Arc::new(native),
        allowed_emails: std::sync::Arc::new(allowed_emails),
        index: index.clone(),
        position_store: std::sync::Arc::new(state::PositionStore::new(&state_dir)),
        visibility: std::sync::Arc::new(state::VisibilityStore::new(&state_dir)),
        session_key: auth::session_key(&std::env::var("SECRET_KEY").unwrap_or_default()),
        secret_key: std::env::var("SECRET_KEY").unwrap_or_default(),
        registry: registry.clone(),
        pair_reports: std::sync::Arc::new(std::sync::Mutex::new(HashMap::new())),
        thumb_cache: PathBuf::from(
            std::env::var("BOOKQ_THUMB_CACHE_DIR").unwrap_or_else(|_| "/tmp/bookq-thumbs".into()),
        ),
        // config.py getenv defaults are "unknown" (env unset); the image's
        // ARG ""→ENV path yields "" — same two-state contract.
        git_sha: std::env::var("BOOKQ_GIT_SHA").unwrap_or_else(|_| "unknown".into()),
        build_date: std::env::var("BOOKQ_BUILD_DATE").unwrap_or_else(|_| "unknown".into()),
    };
    if st.native.enabled("visibility") || st.native.enabled("tree") {
        // warmup_metadata_index — lifespan kicks the same cold-miss path.
        index.spawn_warmup(downloads.clone());
    }
    tracing::info!(
        test_mode = st.native.test_mode,
        native_all = st.native.all,
        native_set = ?st.native.set,
        downloads = %st.downloads.display(),
        "read-path endpoints"
    );

    let app = Router::new()
        .route("/healthz", get(|| async { "ok\n" }))
        // M3 skeleton: no v1 native handlers yet — everything proxies.
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
        // TEST_MODE-only dual-reader verification hooks (#405) — native
        // under `test_mode`, 404 otherwise; never in the OpenAPI contract.
        .route("/api/v1/pair-report", any(pair_report))
        .route("/api/v1/pair-reset", any(pair_reset))
        // M8: dual-reader sync socket — native under "reader-sync" gate;
        // otherwise the upgrade is spliced to Python unchanged.
        .route("/ws/reader-sync", any(reader_sync_ws))
        // /api/v1/auth/token is proxy-only by design: its auth-code and
        // refresh stores are written by the OAuth callback in app/auth.py
        // (app.state); sharing them natively would require touching the
        // Python backend, which is out of bounds.
        .fallback(axum::routing::any(|State(st): State<AppState>, req: Request<Body>| async move {
            proxy(&st.upstream, req).await
        }))
        .layer(axum::middleware::from_fn_with_state(
            st.clone(),
            vary_session_cookie,
        ))
        .with_state(st);

    let addr: SocketAddr = bind.parse().expect("BOOKQ_BIND must be host:port");
    tracing::info!(%addr, %upstream, "viewer-rs listening");
    let listener = tokio::net::TcpListener::bind(addr).await.expect("bind failed");
    axum::serve(NoDelayListener(listener), app)
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await
        .expect("serve failed");
}

#[cfg(test)]
mod native_gate_tests {
    use super::*;
    use std::collections::HashSet;

    fn native(set: &[&str]) -> Native {
        Native::from_parts(false, false, set.iter().map(|e| e.to_string()).collect())
    }

    fn all_native() -> Native {
        Native::from_parts(false, true, HashSet::new())
    }

    #[test]
    fn partial_index_group_is_flagged() {
        // The live production posture this issue is about: tree + images are
        // native while search/facets still answer from the Python index.
        let partial = native(&["tree", "images"]).partial_groups();
        assert_eq!(partial, vec![NATIVE_GROUPS[0], NATIVE_GROUPS[1]]);
    }

    #[test]
    fn single_index_member_is_flagged_once() {
        // `search` alone splits INDEX_GROUP; MANIFEST_GROUP has nobody enabled
        // and must stay quiet (all-proxy is a valid posture).
        let partial = native(&["search"]).partial_groups();
        assert_eq!(partial, vec![NATIVE_GROUPS[0]]);
    }

    #[test]
    fn complete_groups_are_quiet() {
        let n = native(&["tree", "search", "facets", "images", "reader-sync"]);
        assert!(n.partial_groups().is_empty());
    }

    #[test]
    fn all_proxy_and_all_native_are_quiet() {
        assert!(native(&[]).partial_groups().is_empty());
        assert!(all_native().partial_groups().is_empty());
    }

    #[test]
    fn ungrouped_endpoints_never_warn() {
        // thumb/view/image/visibility/positions/health/version/refresh are
        // independent of the shared snapshot: no group covers them.
        let n = native(&["thumb", "view", "image", "visibility", "positions"]);
        assert!(n.partial_groups().is_empty());
    }
}

#[cfg(test)]
mod pair_hook_tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn report_cid_shapes() {
        // `str(body.get("id") or "")`: falsy ids are missing.
        assert_eq!(report_cid(None), "");
        assert_eq!(report_cid(Some(&json!(null))), "");
        assert_eq!(report_cid(Some(&json!(""))), "");
        assert_eq!(report_cid(Some(&json!(0))), "");
        assert_eq!(report_cid(Some(&json!(false))), "");
        assert_eq!(report_cid(Some(&json!([]))), "");
        assert_eq!(report_cid(Some(&json!({}))), "");
        assert_eq!(report_cid(Some(&json!("abc"))), "abc");
        assert_eq!(report_cid(Some(&json!(7))), "7");
        // `[:64]` truncates by chars, not bytes.
        assert_eq!(report_cid(Some(&json!("x".repeat(100)))).len(), 64);
        assert_eq!(
            report_cid(Some(&json!("あ".repeat(100)))).chars().count(),
            64
        );
    }

    #[test]
    fn report_gen_shapes() {
        // `int(body.get("gen") or 0)`: falsy gens are generation 0.
        assert_eq!(report_gen(None), 0);
        assert_eq!(report_gen(Some(&json!(null))), 0);
        assert_eq!(report_gen(Some(&json!(0))), 0);
        assert_eq!(report_gen(Some(&json!(""))), 0);
        assert_eq!(report_gen(Some(&json!(false))), 0);
        assert_eq!(report_gen(Some(&json!(true))), 1);
        assert_eq!(report_gen(Some(&json!(3))), 3);
        assert_eq!(report_gen(Some(&json!("12"))), 12);
        assert_eq!(report_gen(Some(&json!(2.9))), 2);
    }

    fn test_state(test_mode: bool) -> AppState {
        let index = std::sync::Arc::new(folders::IndexState::new());
        let downloads = PathBuf::from("/nonexistent-test-downloads");
        let dir = PathBuf::from("/nonexistent-test-state");
        AppState {
            upstream: Upstream {
                client: Client::builder(TokioExecutor::new()).build(HttpConnector::new()),
                authority: "example.com:8080".parse().unwrap(),
            },
            downloads: downloads.clone(),
            native: std::sync::Arc::new(Native::from_parts(
                test_mode,
                test_mode,
                std::collections::HashSet::new(),
            )),
            allowed_emails: std::sync::Arc::new(std::collections::HashSet::new()),
            index: index.clone(),
            position_store: std::sync::Arc::new(state::PositionStore::new(&dir)),
            visibility: std::sync::Arc::new(state::VisibilityStore::new(&dir)),
            secret_key: "test-secret".to_string(),
            session_key: None,
            thumb_cache: PathBuf::from("/nonexistent-test-thumbs"),
            git_sha: "test".to_string(),
            build_date: "test".to_string(),
            registry: reader_sync::Registry::new(index, downloads),
            pair_reports: std::sync::Arc::new(std::sync::Mutex::new(HashMap::new())),
        }
    }

    fn post_req(body: serde_json::Value) -> Request<Body> {
        Request::builder()
            .method("POST")
            .uri("/api/v1/pair-report")
            .body(Body::from(serde_json::to_string(&body).unwrap()))
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
    async fn post_get_roundtrip_and_gen_drop() {
        let st = test_state(true);
        // Gen-less report stores; the id key is popped, received_at stamped.
        let (status, body) = body_json(
            pair_report_post(
                &st,
                post_req(json!({"id": "c1", "pc": {"phase": "reading"}})),
            )
            .await,
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body, json!({"ok": true}));

        let get = Request::builder()
            .method("GET")
            .uri("/api/v1/pair-report?id=c1")
            .body(Body::empty())
            .unwrap();
        let (status, body) = body_json(pair_report_get(&st, get).await).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body["state"]["pc"], json!({"phase": "reading"}));
        assert!(body["state"].get("id").is_none());
        assert!(body["state"]["received_at"].as_f64().unwrap() > 0.0);

        // Newer generation wins; an older repost from a parked tab drops.
        let (status, body) = body_json(
            pair_report_post(&st, post_req(json!({"id": "c1", "gen": 5, "pc": 1}))).await,
        )
        .await;
        assert_eq!((status, body), (StatusCode::OK, json!({"ok": true})));
        let (status, body) = body_json(
            pair_report_post(&st, post_req(json!({"id": "c1", "gen": 3, "pc": 2}))).await,
        )
        .await;
        assert_eq!(
            (status, body),
            (StatusCode::OK, json!({"ok": true, "dropped": true}))
        );
        let get = Request::builder()
            .method("GET")
            .uri("/api/v1/pair-report?id=c1")
            .body(Body::empty())
            .unwrap();
        let (_, body) = body_json(pair_report_get(&st, get).await).await;
        assert_eq!(body["state"]["pc"], json!(1));
        assert_eq!(body["state"]["gen"], json!(5));

        // Unknown id reads back null, like the Python `dict.get`.
        let get = Request::builder()
            .method("GET")
            .uri("/api/v1/pair-report?id=never-posted")
            .body(Body::empty())
            .unwrap();
        let (status, body) = body_json(pair_report_get(&st, get).await).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body, json!({"state": null}));
    }

    #[tokio::test]
    async fn post_requires_id() {
        let st = test_state(true);
        let (status, body) =
            body_json(pair_report_post(&st, post_req(json!({"pc": {}}))).await).await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        assert_eq!(body, json!({"detail": "id required"}));
    }

    /// Outside test mode the hooks must not exist (404) — even with every
    /// native surface enabled, mirroring `_pair_report_gate`.
    #[tokio::test]
    async fn gate_denies_outside_test_mode() {
        let st = test_state(false);
        let post = Request::builder()
            .method("POST")
            .uri("/api/v1/pair-report")
            .body(Body::from(r#"{"id":"c1"}"#))
            .unwrap();
        assert_eq!(
            pair_report(State(st.clone()), post).await.status(),
            StatusCode::NOT_FOUND
        );
        let get = Request::builder()
            .method("GET")
            .uri("/api/v1/pair-report?id=c1")
            .body(Body::empty())
            .unwrap();
        assert_eq!(
            pair_report(State(st.clone()), get).await.status(),
            StatusCode::NOT_FOUND
        );
        let reset = Request::builder()
            .method("POST")
            .uri("/api/v1/pair-reset")
            .body(Body::empty())
            .unwrap();
        let res = pair_reset(State(st.clone()), reset).await;
        assert_eq!(res.status(), StatusCode::NOT_FOUND);
        let (_, body) = body_json(res).await;
        assert_eq!(body, json!({"detail": "Not Found"}));
    }

    #[tokio::test]
    async fn reset_reports_ended_count() {
        // Empty registry — the handler delegates counting to `end_all`
        // (covered with live sessions in reader_sync's own tests).
        let st = test_state(true);
        let reset = Request::builder()
            .method("POST")
            .uri("/api/v1/pair-reset")
            .body(Body::empty())
            .unwrap();
        let (status, body) = body_json(pair_reset(State(st), reset).await).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body, json!({"ended": 0}));
    }
}
