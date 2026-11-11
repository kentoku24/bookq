//! `/api/v1/search` + `/api/v1/facets` — M6 of #163.
//!
//! Ports `app/routes.py::_search_impl` / `_facets_impl` over the shared
//! index cache: relevance scoring (`_calculate_relevance`), artist/tag
//! filters (`_matches_filters`), the `(-score, natural_key, name)` ordering,
//! and the fail-closed hidden-root exclusion — the library-global roots are
//! read from the shared visibility SQLite and a failed read answers 503,
//! never an unfiltered list (`hidden_roots_for_request` posture).

use std::collections::HashMap;
use std::sync::Arc;

use axum::body::Body;
use axum::http::{header, HeaderValue, StatusCode};
use axum::response::Response;
use serde_json::Value;

use bookq_core::ComicRec;

use crate::folders::{ensure_tree_and_index, CacheData, Ensure};
use crate::serve::Serve;
use crate::state::{covered_by_roots, VisibilityStore, GLOBAL_USER_KEY};
use crate::tree;
use crate::AppState;

fn json_response(status: StatusCode, body: Vec<u8>) -> Response {
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

fn detail(status: StatusCode, msg: &str) -> Response {
    json_response(
        status,
        serde_json::to_string(&serde_json::json!({"detail": msg}))
            .unwrap()
            .into_bytes(),
    )
}

/// `metadata index building` 202 — identical across the index endpoints.
fn building() -> Response {
    let mut res = json_response(
        StatusCode::ACCEPTED,
        serde_json::to_string(&serde_json::json!({"detail": "metadata index building"}))
            .unwrap()
            .into_bytes(),
    );
    res.headers_mut()
        .insert(header::RETRY_AFTER, HeaderValue::from_static("2"));
    res.headers_mut()
        .insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    res
}

/// `_visibility_hidden_roots` — the library-global hidden roots,
/// fail-closed. Same for every caller; `include_hidden` opts out.
async fn hidden_roots(st: &AppState) -> Option<Vec<String>> {
    let store: Arc<VisibilityStore> = st.visibility.clone();
    tokio::task::spawn_blocking(move || store.read(GLOBAL_USER_KEY))
        .await
        .ok()
        .and_then(|r| r.ok())
        .map(|s| s.hidden_roots)
}

// ── search (_calculate_relevance / _matches_filters / index.search) ───────────

/// `_calculate_relevance` — +10 title / +8 artist / +3 tag per word, +2 per
/// extra matched field. `word in field` is a substring test on lowercased
/// copies (Python `in` on str / `any(w in t for t in tags)`).
fn relevance(query: &str, c: &ComicRec) -> f64 {
    let words: Vec<String> = query.split_whitespace().map(|w| w.to_lowercase()).collect();
    if words.is_empty() {
        return 0.0;
    }
    let title = format!("{} {}", c.2, c.3).to_lowercase();
    let artists: Vec<String> = c.4.iter().map(|a| a.to_lowercase()).collect();
    let tags: Vec<String> = c.5.iter().map(|t| t.to_lowercase()).collect();
    let mut score = 0.0f64;
    let mut hit_title = false;
    let mut hit_artist = false;
    let mut hit_tag = false;
    for w in &words {
        if title.contains(w.as_str()) {
            score += 10.0;
            hit_title = true;
        }
        if artists.iter().any(|a| a.contains(w.as_str())) {
            score += 8.0;
            hit_artist = true;
        }
        if tags.iter().any(|t| t.contains(w.as_str())) {
            score += 3.0;
            hit_tag = true;
        }
    }
    let fields = hit_title as i64 + hit_artist as i64 + hit_tag as i64;
    if fields > 1 {
        score += (fields - 1) as f64 * 2.0;
    }
    score
}

/// `_matches_filters` — OR within a key, AND across keys; substring,
/// case-insensitive. Only `artist`/`tags` keys exist (`_get_field_values`
/// rejects everything else, and an unknown non-empty key filters out).
fn matches_filters(c: &ComicRec, filters: &HashMap<&str, Vec<String>>) -> bool {
    for (key, values) in filters {
        if values.is_empty() {
            continue;
        }
        let comic_values: &[String] = match *key {
            "artist" => &c.4,
            "tags" => &c.5,
            _ => &[],
        };
        if comic_values.is_empty() {
            return false;
        }
        let hit = values.iter().any(|fv| {
            let fvl = fv.to_lowercase();
            comic_values
                .iter()
                .any(|cv| cv.to_lowercase().contains(&fvl))
        });
        if !hit {
            return false;
        }
    }
    true
}

/// `index.search` — hidden-root exclusion → scoring → filters →
/// `(-score, natural_key(folder_name), folder_name)`.
fn search_index(
    data: &CacheData,
    q: &str,
    filters: &HashMap<&str, Vec<String>>,
    roots: &[String],
) -> Vec<Value> {
    let comics: Vec<&ComicRec> = data
        .comics
        .iter()
        .filter(|c| roots.is_empty() || !covered_by_roots(&c.0, roots))
        .collect();
    let mut candidates: Vec<(f64, &ComicRec)> = if q.trim().is_empty() {
        comics.iter().map(|c| (0.0, *c)).collect()
    } else {
        comics
            .iter()
            .filter_map(|c| {
                let s = relevance(q.trim(), c);
                (s > 0.0).then_some((s, *c))
            })
            .collect()
    };
    candidates.retain(|(_, c)| matches_filters(c, filters));
    candidates.sort_by(|(sa, a), (sb, b)| {
        sb.partial_cmp(sa)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then(bookq_core::natural_cmp(&a.1, &b.1))
    });
    candidates.iter().map(|(_, c)| tree::comic_to_json(c)).collect()
}

// ── facets (_build_facets / _facets_for) ──────────────────────────────────────

/// `_build_facets` — `sorted(set, key=str.casefold)`; the existing
/// deterministic tiebreak convention from `subtree_facets` applies.
fn build_facets<'a>(comics: impl Iterator<Item = &'a ComicRec>) -> Value {
    let mut artists: Vec<String> = Vec::new();
    let mut tags: Vec<String> = Vec::new();
    for c in comics {
        artists.extend(c.4.iter().cloned());
        tags.extend(c.5.iter().cloned());
    }
    for v in [&mut artists, &mut tags] {
        v.sort();
        v.dedup();
        v.sort_by(|x, y| x.to_lowercase().cmp(&y.to_lowercase()).then(x.cmp(y)));
    }
    serde_json::json!({"artists": artists, "tags": tags})
}

/// `_facets_for` — `folder_path == path or startswith(path.rstrip("/")+"/")`,
/// minus `covered_by_roots`.
fn facets_for(data: &CacheData, path: &str, roots: &[String]) -> Value {
    if path.is_empty() && roots.is_empty() {
        return build_facets(data.comics.iter());
    }
    let prefix = format!("{}/", path.trim_end_matches('/'));
    build_facets(data.comics.iter().filter(|c| {
        (path.is_empty() || c.0.as_str() == path || c.0.starts_with(prefix.as_str()))
            && !covered_by_roots(&c.0, roots)
    }))
}

// ── handlers ──────────────────────────────────────────────────────────────────

/// FastAPI `bool` query coercion — accepts its true/false token sets;
/// `None` means the value would 422 upstream (the caller proxies).
fn parse_bool(v: Option<&String>) -> Option<bool> {
    match v?.to_lowercase().as_str() {
        "1" | "true" | "t" | "yes" | "y" | "on" => Some(true),
        "0" | "false" | "f" | "no" | "n" | "off" => Some(false),
        _ => None,
    }
}

fn bool_param(q: &HashMap<String, String>, name: &str) -> Option<Option<bool>> {
    match q.get(name) {
        None => Some(Some(false)),
        Some(v) => parse_bool(Some(v)).map(Some),
    }
}

/// Shared ensure+hidden-roots preamble; `None` means a response was already
/// produced (202/503) or the call must proxy (Failed).
async fn index_and_hidden(
    st: &AppState,
    include_hidden: bool,
) -> Result<(Arc<CacheData>, Vec<String>), Response> {
    let data = match ensure_tree_and_index(&st.index, &st.downloads).await {
        Ensure::Fresh(d) | Ensure::Stale(d) => d,
        Ensure::Building => return Err(building()),
        Ensure::Failed => return Err(detail(StatusCode::INTERNAL_SERVER_ERROR, "index build failed")),
    };
    let hidden = if include_hidden {
        Vec::new()
    } else {
        match hidden_roots(st).await {
            Some(h) => h,
            None => {
                return Err(detail(
                    StatusCode::SERVICE_UNAVAILABLE,
                    "visibility store unavailable",
                ))
            }
        }
    };
    Ok((data, hidden))
}

/// `GET /api/v1/search` — `_search_impl`. `sort` is `Literal["relevance"]`
/// upstream: any other value falls through to keep FastAPI's 422.
pub async fn api_search(st: &AppState, q: &HashMap<String, String>) -> Serve {
    if q.get("sort").map(|s| s.as_str()).unwrap_or("relevance") != "relevance" {
        return Serve::Proxy;
    }
    let Some(include_hidden) = bool_param(q, "include_hidden") else {
        return Serve::Proxy;
    };
    let (data, hidden) = match index_and_hidden(st, include_hidden.unwrap()).await {
        Ok(x) => x,
        Err(res) => return Serve::Done(res),
    };
    let mut filters: HashMap<&str, Vec<String>> = HashMap::new();
    let artist = q.get("artist").map(|s| s.as_str()).unwrap_or("");
    if !artist.is_empty() {
        filters.insert("artist", vec![artist.to_string()]);
    }
    let tags: Vec<String> = q
        .get("tag")
        .map(|s| s.as_str())
        .unwrap_or("")
        .split(',')
        .map(|t| t.trim().to_string())
        .filter(|t| !t.is_empty())
        .collect();
    if !tags.is_empty() {
        filters.insert("tags", tags);
    }
    let qv = q.get("q").map(|s| s.as_str()).unwrap_or("");
    let results = search_index(&data, qv, &filters, &hidden);
    Serve::Done(json_response(
        StatusCode::OK,
        serde_json::to_string(&serde_json::json!({
            "query": qv,
            "total": results.len(),
            "results": results,
        }))
        .unwrap()
        .into_bytes(),
    ))
}

/// `GET /api/v1/facets` — `_facets_impl`. `path` is a raw string filter
/// (no traversal check upstream — a non-matching value just empties the set).
pub async fn api_facets(st: &AppState, q: &HashMap<String, String>) -> Serve {
    let Some(include_hidden) = bool_param(q, "include_hidden") else {
        return Serve::Proxy;
    };
    let (data, hidden) = match index_and_hidden(st, include_hidden.unwrap()).await {
        Ok(x) => x,
        Err(res) => return Serve::Done(res),
    };
    let path = q.get("path").map(|s| s.as_str()).unwrap_or("");
    Serve::Done(json_response(
        StatusCode::OK,
        serde_json::to_string(&facets_for(&data, path, &hidden))
            .unwrap()
            .into_bytes(),
    ))
}
