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
    /// `folders_body_cache` — (key_ns, path, depth) → (body, etag). Lives
    /// per-swap; an in-place timestamp re-stamp keeps the tree identical so
    /// hits stay semantically correct. `key_ns` mirrors Python's namespace
    /// ("v1" payloads vs the "v1-since" stale-poll envelope).
    body_cache: Mutex<HashMap<(String, String, Option<i64>), (Vec<u8>, String)>>,
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
        snapshot_save(&data);
        *c = Some(Arc::new(data));
    }
}

// ── index snapshot (fast repeat boot) ────────────────────────────────────────

/// Persisted CacheData under BOOKQ_STATE_DIR. On boot it is installed as a
/// *stale* cache and the ordinary signature-gated refresh verifies it in the
/// background: unchanged → re-stamp (seconds, done); changed (the user
/// reorganised archives while we were down) → full rebuild + prev+1 swap.
/// Self-healing with a brief stale window — semantically a TTL expiry.
fn snapshot_path() -> PathBuf {
    PathBuf::from(std::env::var("BOOKQ_STATE_DIR").unwrap_or_else(|_| "/var/lib/bookq".into()))
        .join("index-snapshot.json")
}
const SNAPSHOT_VERSION: i64 = 1;

fn snapshot_save(c: &CacheData) {
    let payload = serde_json::json!({
        "version": SNAPSHOT_VERSION,
        "generation": c.generation,
        "signature": c.signature,
        "tree": c.tree,
        "comics": c.comics,
    });
    let Ok(bytes) = serde_json::to_vec(&payload) else { return };
    let path = snapshot_path();
    let tmp = path.with_extension("tmp");
    if std::fs::write(&tmp, &bytes).is_ok() {
        let _ = std::fs::rename(&tmp, &path);
    }
}

fn snapshot_load() -> Option<CacheData> {
    let bytes = std::fs::read(snapshot_path()).ok()?;
    let v: Value = serde_json::from_slice(&bytes).ok()?;
    if v.get("version").and_then(|x| x.as_i64()) != Some(SNAPSHOT_VERSION) {
        return None;
    }
    Some(CacheData {
        tree: serde_json::from_value(v.get("tree")?.clone()).ok()?,
        comics: serde_json::from_value(v.get("comics")?.clone()).ok()?,
        // Stale timestamp → the caller routes through the refresh path,
        // which re-walks the signature and rebuilds only on a real change.
        timestamp_micros: AtomicU64::new(0),
        generation: v.get("generation")?.as_i64()?,
        signature: v.get("signature").and_then(|x| x.as_str()).map(String::from),
        body_cache: Mutex::new(HashMap::new()),
    })
}

/// `refresh_index` — unconditional synchronous rebuild (NOT signature
/// gated): the caller waits for the swap before replying, matching
/// `async with _index_lock` + `_scan_and_rebuild`. Generation follows the
/// same rule as the background path — verified signature change bumps
/// `prev+1`, otherwise `prev` — so `since=` tokens stay honest about a
/// tree that actually changed.
pub async fn force_rebuild(st: &Arc<IndexState>, downloads: &Path) -> Option<usize> {
    let mut data = tokio::task::spawn_blocking({
        let d = downloads.to_path_buf();
        move || build_cache(&d)
    })
    .await
    .ok()
    .flatten()?;
    let folders = data.comics.len();
    {
        let mut c = st.cache.lock().await;
        let prev_gen = c.as_ref().map(|d| d.generation).unwrap_or(1);
        let prev_sig = c.as_ref().and_then(|d| d.signature.clone());
        let changed = prev_sig.is_some()
            && data.signature.as_deref() != prev_sig.as_deref();
        data.generation = if changed { prev_gen + 1 } else { prev_gen };
        *c = Some(Arc::new(data));
    }
    Some(folders)
}

/// `_scan_and_rebuild` — full tree + metadata + attaches in one walk.
fn build_cache(downloads: &Path) -> Option<CacheData> {
    // The signature walk is independent of the scan/metadata walk — run it
    // on its own thread so the two full-tree traversals overlap instead of
    // serialising back to back (each is a complete stat-walk of the volume).
    let signature_walk = {
        let d = downloads.to_path_buf();
        std::thread::spawn(move || bookq_core::tree_signature(&d))
    };
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
    let signature = signature_walk.join().ok().flatten();
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
        // Repeat-boot fast path: install a persisted snapshot as stale, then
        // let the signature-gated refresh verify it — unchanged re-stamps in
        // ~seconds, a reorganised library triggers the full rebuild+swap.
        let snap = tokio::task::spawn_blocking(snapshot_load).await.ok().flatten();
        if let Some(data) = snap {
            {
                let mut c = st.cache.lock().await;
                if c.is_none() {
                    *c = Some(Arc::new(data));
                }
            }
            *st.building.lock().await = None;
            n.notify_waiters();
            st.schedule_refresh(&downloads);
            return match st.current().await {
                Some(c) => Ensure::Stale(c),
                None => Ensure::Building,
            };
        }
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
                snapshot_save(&d);
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

/// `_folders_response` (v1): sha1(f"{key_ns}:{path}:{depth}:{body}")[:24] +
/// "-g{gen_int}", 304 on exact If-None-Match. Payload is the v1 envelope
/// `{kind, nodes, generation}` — `key_ns` is part of the digest so the v0
/// and v1 shapes can never collide, and so the "v1-since" stale-poll
/// envelope never aliases the bare full-tree body cached under "v1".
async fn folders_response(
    data: &Arc<CacheData>,
    payload: &Value,
    path: &str,
    depth: Option<i64>,
    headers: &HeaderMap,
    key_ns: &str,
) -> Response {
    let key = (key_ns.to_string(), path.to_string(), depth);
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
                format!("{key_ns}:{path}:{}:{body_str}", depth_repr(depth)).as_bytes(),
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
            // Stale poll → same envelope, but through the body cache
            // (#221 port): the mismatch body is the whole tree plus the
            // generation — identical for every poll within a generation —
            // so serialize+sha1 once instead of a multi-MB json.dumps per
            // poller. The "v1-since" namespace keeps this dict envelope
            // from aliasing the bare-list full-tree body cached under
            // "v1"; a generation change swaps CacheData wholesale so a
            // stale body can't be served under new coordinates. The newly
            // attached ETag/Cache-Control is safe for the fill-poller —
            // it only reads body.generation and treats 304 as unchanged.
            let payload = serde_json::json!({
                "kind": "tree", "nodes": data.tree, "generation": gen_int
            });
            return Serve::Done(
                folders_response(&data, &payload, "", None, &headers, "v1-since").await,
            );
        }
        let payload = serde_json::json!({
            "kind": "tree", "nodes": data.tree, "generation": gen_int
        });
        return Serve::Done(folders_response(&data, &payload, "", None, &headers, "v1").await);
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
            Serve::Done(folders_response(&data, &payload, &norm, Some(depth), &headers, "v1").await)
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

#[cfg(test)]
mod tests {
    use super::*;
    use http_body_util::BodyExt;

    fn test_data(tree: Vec<Value>, generation: i64) -> Arc<CacheData> {
        Arc::new(CacheData {
            tree,
            comics: Vec::new(),
            timestamp_micros: AtomicU64::new((now_secs() * 1e6) as u64),
            generation,
            signature: None,
            body_cache: Mutex::new(HashMap::new()),
        })
    }

    async fn body_bytes(res: Response) -> Vec<u8> {
        res.into_body().collect().await.unwrap().to_bytes().to_vec()
    }

    /// #221 port: the ?since stale-poll envelope rides the body cache —
    /// one serialization per generation, ETag/304/Cache-Control attached,
    /// namespaced so it can never alias the bare full-tree body.
    #[tokio::test]
    async fn since_payload_is_cached_with_conditional_headers() {
        let data = test_data(vec![serde_json::json!({"name": "a", "type": "folder"})], 7);
        let payload = serde_json::json!({
            "kind": "tree", "nodes": data.tree, "generation": data.generation
        });
        let empty = HeaderMap::new();

        // First stale poll: serialized + etag minted.
        let r1 = folders_response(&data, &payload, "", None, &empty, "v1-since").await;
        assert_eq!(r1.status(), StatusCode::OK);
        let etag = r1
            .headers()
            .get(header::ETAG)
            .unwrap()
            .to_str()
            .unwrap()
            .to_string();
        assert!(etag.starts_with('"') && etag.ends_with("-g7\""), "{etag}");
        assert_eq!(
            r1.headers().get(header::CACHE_CONTROL).unwrap(),
            FOLDERS_CACHE_CONTROL
        );
        let body1 = body_bytes(r1).await;
        let v1: Value = serde_json::from_slice(&body1).unwrap();
        assert_eq!(v1["kind"], "tree");
        assert_eq!(v1["generation"], 7);

        // Second poll: the cached body is served byte-identically.
        let r2 = folders_response(&data, &payload, "", None, &empty, "v1-since").await;
        assert_eq!(r2.headers().get(header::ETAG).unwrap(), &etag);
        assert_eq!(body_bytes(r2).await, body1);
        assert_eq!(data.body_cache.lock().await.len(), 1);

        // If-None-Match → 304 with the generation-suffixed etag echoed.
        let mut inm = HeaderMap::new();
        inm.insert(header::IF_NONE_MATCH, HeaderValue::from_str(&etag).unwrap());
        let r3 = folders_response(&data, &payload, "", None, &inm, "v1-since").await;
        assert_eq!(r3.status(), StatusCode::NOT_MODIFIED);
        assert_eq!(r3.headers().get(header::ETAG).unwrap(), &etag);
        assert!(body_bytes(r3).await.is_empty());
    }

    /// The "v1-since" envelope can never alias the bare full-tree body:
    /// identical payload under a different namespace yields a different ETag.
    #[tokio::test]
    async fn namespace_separates_cached_bodies() {
        let data = test_data(vec![], 3);
        let payload = serde_json::json!({"kind": "tree", "nodes": [], "generation": 3});
        let empty = HeaderMap::new();

        let rs = folders_response(&data, &payload, "", None, &empty, "v1-since").await;
        let rv = folders_response(&data, &payload, "", None, &empty, "v1").await;
        let e_since = rs
            .headers()
            .get(header::ETAG)
            .unwrap()
            .to_str()
            .unwrap()
            .to_string();
        let e_v1 = rv
            .headers()
            .get(header::ETAG)
            .unwrap()
            .to_str()
            .unwrap()
            .to_string();
        assert_ne!(e_since, e_v1);
        assert_eq!(data.body_cache.lock().await.len(), 2);
    }
}
