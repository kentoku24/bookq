//! `/api/folders` — index cache machinery + response shaping (R1b).
//!
//! Ports `app/routes.py`'s `_ensure_tree_and_index`/`_folders_response`
//! contract exactly: TTL cache (300s) with stale-serve + single-flight
//! signature-gated background refresh, cold-miss single-flight build with
//! an 0.8s await budget (202 + Retry-After), generation/`since` handling,
//! sha1-ETag body cache, and the live-FS shallow fallback for `depth`
//! requests while the cache is cold.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use axum::{
    body::Body,
    http::{header, HeaderMap, HeaderValue, StatusCode},
    response::Response,
};
use serde_json::Value;
use sha1::{Digest, Sha1};
use tokio::sync::{Mutex, Notify};

use crate::tree;
use bookq_core::ComicRec;

const INDEX_CACHE_TTL_SECS: f64 = 300.0;
const INDEX_BUILD_AWAIT_SECS: f64 = 0.8;
const FOLDERS_CACHE_CONTROL: &str = "private, max-age=60";

pub struct IndexState {
    /// `metadata_index_cache` — check-and-swap only, never held over a build.
    cache: Mutex<Option<Arc<CacheData>>>,
    /// Single-flight cold build: `Some` while a build task is in flight.
    building: Mutex<Option<Arc<Notify>>>,
    /// Single-flight background refresh (`index_refresh_task`).
    refreshing: Mutex<bool>,
}

pub struct CacheData {
    pub tree: Vec<Value>,
    /// `index.comics` — `rebuild_from_tree`'s ComicMetadata rows; M6
    /// search/facets read them straight off the cache like the Python index.
    pub comics: Vec<ComicRec>,
    /// `timestamp` — epoch seconds (float precision as f64 micros); re-stamped
    /// in place by the signature gate on an unchanged tree.
    timestamp_micros: AtomicU64,
    /// `generation` — starts at 1 + archive/pdf node count (the fill-complete
    /// steady state); a verified-change refresh swaps in prev+1.
    pub generation: i64,
    /// `signature` — the post-scan compute_signature the next expiry gates on.
    pub signature: Option<String>,
    /// `folders_body_cache` — (path, depth) → (body, etag). Lives per-swap;
    /// an in-place timestamp re-stamp keeps the tree identical so hits stay
    /// semantically correct.
    body_cache: Mutex<HashMap<(String, Option<i64>), (Vec<u8>, String)>>,
}

impl CacheData {
    fn timestamp(&self) -> f64 {
        self.timestamp_micros.load(Ordering::Relaxed) as f64 / 1e6
    }
    fn restamp(&self) {
        self.timestamp_micros
            .store((now_secs() * 1e6) as u64, Ordering::Relaxed);
    }
    fn fresh(&self) -> bool {
        now_secs() - self.timestamp() <= INDEX_CACHE_TTL_SECS
    }
}

fn now_secs() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

impl IndexState {
    pub fn new() -> Self {
        Self {
            cache: Mutex::new(None),
            building: Mutex::new(None),
            refreshing: Mutex::new(false),
        }
    }

    /// Current cache whatever its freshness (None = cold).
    async fn current(&self) -> Option<Arc<CacheData>> {
        self.cache.lock().await.clone()
    }

    /// Startup warmup — mirrors `warmup_metadata_index`.
    pub fn spawn_warmup(self: &Arc<Self>, downloads: PathBuf) {
        let st = self.clone();
        tokio::spawn(async move {
            let _ = ensure_tree_and_index(&st, &downloads).await;
        });
    }

    /// `_schedule_index_refresh` — single-flight background refresh.
    fn schedule_refresh(self: &Arc<Self>, downloads: &Path) {
        let st = self.clone();
        let d = downloads.to_path_buf();
        tokio::spawn(async move {
            {
                let mut r = st.refreshing.lock().await;
                if *r {
                    return;
                }
                *r = true;
            }
            background_index_refresh(&st, &d).await;
            *st.refreshing.lock().await = false;
        });
    }
}

/// `_background_index_refresh` — signature gate outside the lock; an
/// unchanged tree re-stamps in place, a verified change swaps with prev+1.
async fn background_index_refresh(st: &IndexState, downloads: &Path) {
    let cur = match st.current().await {
        Some(c) => c,
        None => return,
    };
    let sig = tokio::task::spawn_blocking({
        let d = downloads.to_path_buf();
        move || bookq_core::tree_signature(&d)
    })
    .await
    .ok()
    .flatten();
    if sig.is_some() {
        {
            let c = st.cache.lock().await;
            match &*c {
                // Replaced or turned fresh while the walk ran → leave it.
                Some(d) if !Arc::ptr_eq(d, &cur) || d.fresh() => return,
                Some(d) if d.signature.as_deref() == sig.as_deref() => {
                    d.restamp();
                    return;
                }
                _ => {}
            }
        }
    }
    // Full rebuild outside the lock; swap under it.
    let built = tokio::task::spawn_blocking({
        let d = downloads.to_path_buf();
        move || build_cache(&d)
    })
    .await
    .ok()
    .flatten();
    let Some(mut data) = built else { return }; // failure keeps old cache
    {
        let mut c = st.cache.lock().await;
        if matches!(&*c, Some(d) if d.fresh()) {
            return; // turned fresh during the scan
        }
        let prev_gen = c.as_ref().map(|d| d.generation).unwrap_or(1);
        let prev_sig = c.as_ref().and_then(|d| d.signature.clone());
        // Verified change → prev+1; unverifiable gate keeps the generation.
        let changed = sig.is_some()
            && prev_sig.is_some()
            && sig.as_deref() != prev_sig.as_deref();
        data.generation = if changed { prev_gen + 1 } else { prev_gen };
        data.signature = sig;
        *c = Some(Arc::new(data));
    }
}

/// `_scan_and_rebuild` — full tree + metadata + attaches in one walk.
fn build_cache(downloads: &Path) -> Option<CacheData> {
    let mut t = tree::scan_tree(downloads)?;
    // `rebuild_from_tree` (NOT `_walk`): tree-driven parse — archive
    // nodes' internal dirs produce phantom minimal records, and real
    // dirs absent from the tree are never indexed.
    let comics = tree::rebuild_index_from_tree(&t, downloads);
    let lookup: HashMap<String, ComicRec> =
        comics.iter().map(|c| (c.0.clone(), c.clone())).collect();
    tree::attach_metadata(&mut t, &lookup, "");
    let subtree = tree::subtree_facets(&comics);
    tree::attach_subtree_facets(&mut t, &subtree, "");
    let generation = 1 + tree::archive_node_count(&t);
    let signature = bookq_core::tree_signature(downloads);
    Some(CacheData {
        tree: t,
        comics,
        timestamp_micros: AtomicU64::new((now_secs() * 1e6) as u64),
        generation,
        signature,
        body_cache: Mutex::new(HashMap::new()),
    })
}

pub enum Ensure {
    Fresh(Arc<CacheData>),
    /// Stale cache — serve it; a background refresh is already scheduled.
    Stale(Arc<CacheData>),
    /// `_IndexBuilding` — cold and still building past the 0.8s budget.
    Building,
    /// Build failed — Python propagates; the caller proxies upstream.
    Failed,
}

pub async fn ensure_tree_and_index(st: &Arc<IndexState>, downloads: &PathBuf) -> Ensure {
    if let Some(c) = st.current().await {
        if c.fresh() {
            return Ensure::Fresh(c);
        }
        st.schedule_refresh(downloads);
        return Ensure::Stale(c);
    }
    enum Role {
        Follower(Arc<Notify>),
        Builder(Arc<Notify>),
    }
    // Decide the role under the lock, but never hold the guard across the
    // build await — build_and_serve clears the flag itself at the end.
    let role = {
        let mut b = st.building.lock().await;
        match &*b {
            Some(n) => Role::Follower(n.clone()),
            None => {
                let n = Arc::new(Notify::new());
                *b = Some(n.clone());
                Role::Builder(n)
            }
        }
    };
    if let Role::Builder(n) = role {
        return build_and_serve(st, downloads.clone(), n).await;
    }
    let Role::Follower(follower) = role else {
        unreachable!()
    };
    let _ = tokio::time::timeout(
        std::time::Duration::from_secs_f64(INDEX_BUILD_AWAIT_SECS),
        follower.notified(),
    )
    .await;
    match st.current().await {
        Some(c) if c.fresh() => Ensure::Fresh(c),
        Some(c) => Ensure::Stale(c),
        None => Ensure::Building,
    }
}

/// Builder branch: scan outside the lock, swap under it, wake the waiters.
async fn build_and_serve(st: &Arc<IndexState>, downloads: PathBuf, done: Arc<Notify>) -> Ensure {
    let built = tokio::task::spawn_blocking(move || build_cache(&downloads))
        .await
        .ok()
        .flatten();
    let succeeded = built.is_some();
    {
        let mut c = st.cache.lock().await;
        let cur_fresh = matches!(&*c, Some(d) if d.fresh());
        if !cur_fresh {
            if let Some(d) = built {
                *c = Some(Arc::new(d));
            }
        }
    }
    *st.building.lock().await = None;
    done.notify_waiters();
    match st.current().await {
        Some(c) if c.fresh() => Ensure::Fresh(c),
        Some(c) => Ensure::Stale(c),
        None if !succeeded => Ensure::Failed,
        None => Ensure::Building,
    }
}

// ── _folders_response / api_folders ─────────────────────────────────────────

fn detail_response(status: StatusCode, detail: &str) -> Response {
    let body = serde_json::to_string(&serde_json::json!({"detail": detail})).unwrap();
    let mut res = Response::builder()
        .status(status)
        .body(Body::from(body))
        .unwrap();
    res.headers_mut().insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("application/json"),
    );
    res
}

fn json_body_response(body: Vec<u8>, etag: Option<&str>) -> Response {
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
        HeaderValue::from_static(FOLDERS_CACHE_CONTROL),
    );
    if let Some(e) = etag {
        res.headers_mut()
            .insert(header::ETAG, HeaderValue::from_str(e).unwrap());
    }
    res
}

fn not_modified(cache_control: &'static str, etag: Option<&str>) -> Response {
    let mut res = Response::builder()
        .status(StatusCode::NOT_MODIFIED)
        .body(Body::empty())
        .unwrap();
    res.headers_mut().insert(
        header::CACHE_CONTROL,
        HeaderValue::from_static(cache_control),
    );
    if let Some(e) = etag {
        res.headers_mut()
            .insert(header::ETAG, HeaderValue::from_str(e).unwrap());
    }
    res
}

/// `_folders_response` (v1): sha1(f"v1:{path}:{depth}:{body}")[:24] +
/// "-g{gen_int}", 304 on exact If-None-Match. Payload is the v1 envelope
/// `{kind, nodes, generation}` — `key_ns` is part of the digest so the v0
/// and v1 shapes can never collide.
async fn folders_response(
    data: &Arc<CacheData>,
    payload: &Value,
    path: &str,
    depth: Option<i64>,
    headers: &HeaderMap,
) -> Response {
    let key = (path.to_string(), depth);
    let cached = {
        let bc = data.body_cache.lock().await;
        bc.get(&key).cloned()
    };
    let (body, etag) = match cached {
        Some(be) => be,
        None => {
            // json.dumps(ensure_ascii=False, separators=(",", ":")) — serde's
            // to_string already produces that exact byte shape (UTF-8 verbatim).
            let body_str = serde_json::to_string(payload).unwrap();
            let digest = Sha1::digest(
                format!("v1:{path}:{}:{body_str}", depth_repr(depth)).as_bytes(),
            );
            let etag = format!("\"{}-g{}\"", &hex24(&digest), data.generation);
            let body = body_str.into_bytes();
            data.body_cache
                .lock()
                .await
                .insert(key, (body.clone(), etag.clone()));
            (body, etag)
        }
    };
    if headers
        .get(header::IF_NONE_MATCH)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.trim() == etag)
    {
        return not_modified(FOLDERS_CACHE_CONTROL, Some(&etag));
    }
    json_body_response(body, Some(&etag))
}

fn hex24(digest: &[u8]) -> String {
    digest[..12].iter().map(|b| format!("{b:02x}")).collect()
}

/// `f"{path}:{depth}:{body}"` — depth rendered the Python repr way.
fn depth_repr(depth: Option<i64>) -> String {
    match depth {
        None => "None".to_string(),
        Some(d) => d.to_string(),
    }
}

/// Live-FS fallback tail of `_folder_tree` (cold cache + depth): a single
/// iterdir level, suffix-classified, no body cache, etag without -g suffix.
/// v1: `{"kind":"children", nodes, generation: fallback_gen}` where
/// fallback_gen is the cache's generation or 0 when there is no cache.
fn live_fs_response(
    downloads: &Path,
    norm: &str,
    depth: i64,
    fallback_gen: i64,
    headers: &HeaderMap,
) -> Response {
    let target = if norm.is_empty() {
        downloads.to_path_buf()
    } else {
        downloads.join(norm)
    };
    let shallow = if norm.is_empty() || target.is_dir() {
        tree::shallow_live(&target).unwrap_or_default()
    } else if !target.exists() {
        return detail_response(StatusCode::NOT_FOUND, "folder not found");
    } else {
        Vec::new() // existing non-dir (archive/pdf file) → []
    };
    let payload = serde_json::json!({
        "kind": "children", "nodes": shallow, "generation": fallback_gen
    });
    let body_str = serde_json::to_string(&payload).unwrap();
    let digest = Sha1::digest(format!("v1:{norm}:{depth}:{body_str}").as_bytes());
    let etag = format!("\"{}\"", hex24(&digest));
    if headers
        .get(header::IF_NONE_MATCH)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.trim() == etag)
    {
        return not_modified(FOLDERS_CACHE_CONTROL, Some(&etag));
    }
    json_body_response(body_str.into_bytes(), Some(&etag))
}

// ── handler ─────────────────────────────────────────────────────────────────

use crate::serve::{is_within, Serve};
use crate::AppState;

fn parse_i64(v: Option<&String>) -> Result<Option<i64>, ()> {
    match v {
        None => Ok(None),
        Some(s) => s.parse::<i64>().map(Some).map_err(|_| ()),
    }
}

/// GET /api/v1/tree — native port of `_folder_tree(v1=True)`.
pub async fn api_tree(st: &AppState, headers: &HeaderMap, q: HashMap<String, String>) -> Serve {
    // Query types: path:str, depth:int|None, since:int|None. Non-integer
    // depth/since → FastAPI 422 — fall through to the upstream renderer.
    let path = q.get("path").cloned().unwrap_or_default();
    let depth = match parse_i64(q.get("depth")) {
        Ok(d) => d,
        Err(()) => return Serve::Proxy,
    };
    let since = match parse_i64(q.get("since")) {
        Ok(s) => s,
        Err(()) => return Serve::Proxy,
    };
    if let Some(d) = depth {
        if d < 1 {
            return Serve::Done(detail_response(
                StatusCode::BAD_REQUEST,
                "depth must be >= 1",
            ));
        }
    }

    if depth.is_none() {
        // ── Full tree mode — `path` is ignored by the Python handler.
        let data = match ensure_tree_and_index(&st.index, &st.downloads).await {
            Ensure::Fresh(c) | Ensure::Stale(c) => c,
            Ensure::Building => {
                let mut res = Response::builder()
                    .status(StatusCode::ACCEPTED)
                    .body(Body::from(
                        serde_json::to_string(
                            &serde_json::json!({"detail": "metadata index building"}),
                        )
                        .unwrap(),
                    ))
                    .unwrap();
                res.headers_mut().insert(
                    header::CONTENT_TYPE,
                    HeaderValue::from_static("application/json"),
                );
                res.headers_mut()
                    .insert(header::RETRY_AFTER, HeaderValue::from_static("2"));
                res.headers_mut().insert(
                    header::CACHE_CONTROL,
                    HeaderValue::from_static("no-store"),
                );
                return Serve::Done(res);
            }
            Ensure::Failed => return Serve::Proxy,
        };
        let gen_int = data.generation;
        if since == Some(gen_int) {
            return Serve::Done(not_modified(FOLDERS_CACHE_CONTROL, None));
        }
        if since.is_some() {
            // Stale poll → {"kind":"tree","nodes":tree,"generation":gen} (no ETag).
            let payload = serde_json::json!({
                "kind": "tree", "nodes": data.tree, "generation": gen_int
            });
            let mut res = Response::builder()
                .status(StatusCode::OK)
                .body(Body::from(serde_json::to_string(&payload).unwrap()))
                .unwrap();
            res.headers_mut().insert(
                header::CONTENT_TYPE,
                HeaderValue::from_static("application/json"),
            );
            return Serve::Done(res);
        }
        let payload = serde_json::json!({
            "kind": "tree", "nodes": data.tree, "generation": gen_int
        });
        return Serve::Done(folders_response(&data, &payload, "", None, &headers).await);
    }

    // ── Shallow mode (depth >= 1).
    let depth = depth.unwrap();
    let norm = path.trim_end_matches('/').to_string();
    let target = if norm.is_empty() {
        st.downloads.clone()
    } else {
        st.downloads.join(&norm)
    };
    if !is_within(&st.downloads, &target) {
        return Serve::Done(detail_response(
            StatusCode::FORBIDDEN,
            "path outside downloads dir",
        ));
    }
    match st.index.current().await {
        Some(data) if data.fresh() => {
            let children: &[Value] = if norm.is_empty() {
                &data.tree
            } else {
                match tree::find_tree_node(&data.tree, &norm, "") {
                    Some(n) if n["type"] == "folder" => {
                        n["children"].as_array().map(|v| v.as_slice()).unwrap_or(&[])
                    }
                    _ => {
                        return Serve::Done(detail_response(
                            StatusCode::NOT_FOUND,
                            "folder not found",
                        ))
                    }
                }
            };
            let shallow = tree::shallow_from_tree(children);
            let payload = serde_json::json!({
                "kind": "children", "nodes": shallow, "generation": data.generation
            });
            Serve::Done(folders_response(&data, &payload, &norm, Some(depth), &headers).await)
        }
        // Cold/stale → live-FS one-level listing (never waits on the scan).
        // fallback_gen = cache's generation when a (stale) cache exists, else 0.
        other => {
            let fallback_gen = match &other {
                Some(d) => d.generation,
                None => 0,
            };
            Serve::Done(live_fs_response(
                &st.downloads,
                &norm,
                depth,
                fallback_gen,
                &headers,
            ))
        }
    }
}

/// `wait=True` variant — PUT handlers can't answer 202, so they ride an
/// in-flight build to completion (no await budget).
pub async fn ensure_tree_and_index_wait(
    st: &Arc<IndexState>,
    downloads: &PathBuf,
) -> Option<Arc<CacheData>> {
    match ensure_tree_and_index(st, downloads).await {
        Ensure::Fresh(c) | Ensure::Stale(c) => return Some(c),
        Ensure::Failed => return None,
        Ensure::Building => {}
    }
    // Build already in flight by someone else — wait on its Notify.
    let follower = {
        let b = st.building.lock().await;
        b.as_ref().cloned()
    };
    if let Some(n) = follower {
        n.notified().await;
    }
    st.current().await
}
