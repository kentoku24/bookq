//! viewer-rs — Rust backend for bookq (issue #163, parallel-backend track).
//!
//! Runs on its own hostname/slot next to the untouched Python `viewer`.
//! Requests hit a routing table of natively implemented endpoints; everything
//! not yet ported is proxied to `BOOKQ_UPSTREAM` (the Python viewer) byte-
//! transparently, including WebSocket upgrades — so the backend is usable from
//! day one and endpoints migrate one at a time.

use axum::{
    body::Body,
    extract::{Query, Request, State},
    http::{header, StatusCode, Uri},
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
/// request we cannot positively identify (session cookie, missing/bad
/// Bearer), falls through to upstream — production rollout is per
/// endpoint via config only.
pub(crate) struct Native {
    pub(crate) test_mode: bool,
    all: bool,
    set: std::collections::HashSet<String>,
}

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
        Self { test_mode, all, set }
    }

    pub(crate) fn enabled(&self, ep: &str) -> bool {
        self.all || self.set.contains(ep)
    }
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
    /// M4: per-process reading positions (`app.state.reading_positions`).
    pub(crate) positions: std::sync::Arc<state::Positions>,
    /// M4: shared user-state SQLite store (`FolderVisibilityStore`).
    pub(crate) visibility: std::sync::Arc<state::VisibilityStore>,
    /// `SECRET_KEY` — needed for the visibility owner HMAC.
    pub(crate) secret_key: String,
    /// M7: shared thumb-cache root (`BOOKQ_THUMB_CACHE_DIR`).
    pub(crate) thumb_cache: PathBuf,
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

/// Resolve the caller for a native route: unidentified requests (session
/// cookie present, no/invalid Bearer) always proxy upstream, which applies
/// its own cookie → Bearer → 401 chain.
fn caller_or_proxy(st: &AppState, headers: &axum::http::HeaderMap) -> Option<auth::Identity> {
    match auth::resolve(
        headers,
        st.native.test_mode,
        &st.secret_key,
        &st.allowed_emails,
    ) {
        auth::Caller::User(u) => Some(u),
        auth::Caller::Delegate => None,
    }
}

/// `/api/v1/reading-positions` — M4: per-folder LWW + tombstones, fully
/// native (positions are per-process ephemeral upstream as well).
async fn reading_positions(State(st): State<AppState>, req: Request<Body>) -> Response {
    if !st.native.enabled("positions") {
        return proxy(&st.upstream, req).await;
    }
    let Some(user) = caller_or_proxy(&st, req.headers()) else {
        return proxy(&st.upstream, req).await;
    };
    // `_position_user_key`: str(email or "unknown").lower()
    let key = user.email.to_lowercase();
    let q = Query::<HashMap<String, String>>::try_from_uri(req.uri())
        .map(|q| q.0)
        .unwrap_or_default();
    let headers = req.headers().clone();
    match *req.method() {
        axum::http::Method::GET => {
            let folder = q.get("folder").map(|s| s.as_str());
            serve::maybe_gzip(&headers, state::list_positions(&st.positions, &key, folder), true)
                .await
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
            state::put_position(&st.positions, &key, &folder, &body)
        }
        axum::http::Method::DELETE => {
            state::delete_positions(&st.positions, &key, q.get("folder").map(|s| s.as_str()))
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
    let Some(user) = caller_or_proxy(&st, req.headers()) else {
        return proxy(&st.upstream, req).await;
    };
    let key = user.email.trim().to_lowercase();
    let q = Query::<HashMap<String, String>>::try_from_uri(req.uri())
        .map(|q| q.0)
        .unwrap_or_default();
    let headers = req.headers().clone();
    match search::api_search(&st, &key, &q).await {
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
    let Some(user) = caller_or_proxy(&st, req.headers()) else {
        return proxy(&st.upstream, req).await;
    };
    let key = user.email.trim().to_lowercase();
    let q = Query::<HashMap<String, String>>::try_from_uri(req.uri())
        .map(|q| q.0)
        .unwrap_or_default();
    let headers = req.headers().clone();
    match search::api_facets(&st, &key, &q).await {
        serve::Serve::Done(res) => serve::maybe_gzip(&headers, res, true).await,
        serve::Serve::Proxy => proxy(&st.upstream, req).await,
    }
}

/// `/image/{*path}` — a v1 contract path (media is not versioned): native
/// for plain files and ZIP/CBZ/EPUB entries (M7 completes the surface).
/// PDF pages, RAR/CBR entries and multi-range requests proxy upstream.
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
/// 304 fast path and shared-cache hits; misses proxy to Pillow upstream.
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
/// formats answer shared-cache hits; everything else proxies.
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
    // Same once-at-startup semantics as app/auth.py's _TEST_MODE.
    let native = Native::from_env();
    let allowed_emails: std::collections::HashSet<String> = std::env::var("ALLOWED_EMAILS")
        .unwrap_or_default()
        .split(',')
        .map(|e| e.trim().to_lowercase())
        .filter(|e| !e.is_empty())
        .collect();

    let index = std::sync::Arc::new(folders::IndexState::new());
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
        positions: std::sync::Arc::new(state::Positions::default()),
        visibility: std::sync::Arc::new(state::VisibilityStore::new(&state_dir)),
        secret_key: std::env::var("SECRET_KEY").unwrap_or_default(),
        thumb_cache: PathBuf::from(
            std::env::var("BOOKQ_THUMB_CACHE_DIR").unwrap_or_else(|_| "/tmp/bookq-thumbs".into()),
        ),
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
        .fallback(axum::routing::any(|State(st): State<AppState>, req: Request<Body>| async move {
            proxy(&st.upstream, req).await
        }))
        .with_state(st);

    let addr: SocketAddr = bind.parse().expect("BOOKQ_BIND must be host:port");
    tracing::info!(%addr, %upstream, "viewer-rs listening");
    let listener = tokio::net::TcpListener::bind(addr).await.expect("bind failed");
    axum::serve(listener, app)
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await
        .expect("serve failed");
}
