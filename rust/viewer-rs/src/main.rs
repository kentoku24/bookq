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
#[allow(dead_code)]
mod folders;
#[allow(dead_code)]
mod serve;
mod state;
#[allow(dead_code)]
mod tree;

#[derive(Clone)]
struct Upstream {
    client: Client<HttpConnector, Body>,
    authority: hyper::http::uri::Authority,
}

#[derive(Clone)]
pub(crate) struct AppState {
    upstream: Upstream,
    /// Downloads root, resolved once (symlinks collapsed like the Python side).
    pub(crate) downloads: PathBuf,
    /// `BOOKQ_TEST_MODE=true` — native endpoints only activate when the
    /// Python side would also skip auth; everything else proxies upstream.
    native: bool,
    /// `/api/folders` index cache (`metadata_index_cache`).
    pub(crate) index: std::sync::Arc<folders::IndexState>,
    /// M4: per-process reading positions (`app.state.reading_positions`).
    pub(crate) positions: std::sync::Arc<state::Positions>,
    /// M4: shared user-state SQLite store (`FolderVisibilityStore`).
    pub(crate) visibility: std::sync::Arc<state::VisibilityStore>,
    /// `SECRET_KEY` — needed for the visibility owner HMAC.
    pub(crate) secret_key: String,
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

/// `/api/v1/reading-positions` — M4: per-folder LWW + tombstones, fully
/// native (positions are per-process ephemeral upstream as well).
async fn reading_positions(State(st): State<AppState>, req: Request<Body>) -> Response {
    if !st.native {
        return proxy(&st.upstream, req).await;
    }
    let q = Query::<HashMap<String, String>>::try_from_uri(req.uri())
        .map(|q| q.0)
        .unwrap_or_default();
    match *req.method() {
        axum::http::Method::GET => {
            let folder = q.get("folder").map(|s| s.as_str());
            state::list_positions(&st.positions, folder)
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
            state::put_position(&st.positions, &folder, &body)
        }
        axum::http::Method::DELETE => {
            state::delete_positions(&st.positions, q.get("folder").map(|s| s.as_str()))
        }
        _ => return proxy(&st.upstream, req).await,
    }
}

/// `/api/v1/folder-visibility` — M4: shared SQLite store + owner HMAC.
async fn folder_visibility(State(st): State<AppState>, req: Request<Body>) -> Response {
    if !st.native {
        return proxy(&st.upstream, req).await;
    }
    match *req.method() {
        axum::http::Method::GET => state::get_folder_visibility(&st).await,
        axum::http::Method::PUT => {
            let headers = req.headers().clone();
            let (parts, body) = req.into_parts();
            let body = match http_body_util::BodyExt::collect(body).await {
                Ok(c) => c.to_bytes(),
                Err(_) => {
                    return proxy(&st.upstream, Request::from_parts(parts, Body::empty()))
                        .await
                }
            };
            state::put_folder_visibility(&st, &headers, &body).await
        }
        _ => return proxy(&st.upstream, req).await,
    }
}

/// `/image/{*path}` — a v1 contract path (media is not versioned): native
/// for plain files and ZIP/CBZ/EPUB entries (M7 completes the surface).
/// PDF pages, RAR/CBR entries and multi-range requests proxy upstream.
async fn serve_image(State(st): State<AppState>, req: Request<Body>) -> Response {
    if !st.native || req.method() != axum::http::Method::GET {
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
    let native = std::env::var("BOOKQ_TEST_MODE")
        .unwrap_or_default()
        .eq_ignore_ascii_case("true");

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
        native,
        index: index.clone(),
        positions: std::sync::Arc::new(state::Positions::default()),
        visibility: std::sync::Arc::new(state::VisibilityStore::new(&state_dir)),
        secret_key: std::env::var("SECRET_KEY").unwrap_or_default(),
    };
    if native {
        // warmup_metadata_index — lifespan kicks the same cold-miss path.
        index.spawn_warmup(downloads.clone());
    }
    tracing::info!(native = st.native, downloads = %st.downloads.display(), "read-path endpoints");

    let app = Router::new()
        .route("/healthz", get(|| async { "ok\n" }))
        // M3 skeleton: no v1 native handlers yet — everything proxies.
        // /image/{*path} is a v1 contract path (media, not versioned).
        .route("/image/{*path}", any(serve_image))
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
