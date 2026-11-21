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
    let data = match ensure_tree_and_index(&st.index, &st.library).await {
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

/// `GET /api/v1/search` — `_search_impl`. `sort` is `Literal["relevance"]`;
/// any other value answers FastAPI's 422 locally.
pub async fn api_search(st: &AppState, q: &HashMap<String, String>) -> Response {
    if q.get("sort").map(|s| s.as_str()).unwrap_or("relevance") != "relevance" {
        return crate::serve::unprocessable(
            "sort",
            "value_error",
            "invalid sort",
        );
    }
    let Some(include_hidden) = bool_param(q, "include_hidden") else {
        return crate::serve::unprocessable(
            "include_hidden",
            "value_error",
            "invalid include_hidden",
        );
    };
    let (data, hidden) = match index_and_hidden(st, include_hidden.unwrap()).await {
        Ok(x) => x,
        Err(res) => return res,
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
    json_response(
        StatusCode::OK,
        serde_json::to_string(&serde_json::json!({
            "query": qv,
            "total": results.len(),
            "results": results,
        }))
        .unwrap()
        .into_bytes(),
    )
}

/// `GET /api/v1/facets` — `_facets_impl`. `path` is a raw string filter
/// (no traversal check — a non-matching value just empties the set).
pub async fn api_facets(st: &AppState, q: &HashMap<String, String>) -> Response {
    let Some(include_hidden) = bool_param(q, "include_hidden") else {
        return crate::serve::unprocessable(
            "include_hidden",
            "value_error",
            "invalid include_hidden",
        );
    };
    let (data, hidden) = match index_and_hidden(st, include_hidden.unwrap()).await {
        Ok(x) => x,
        Err(res) => return res,
    };
    let path = q.get("path").map(|s| s.as_str()).unwrap_or("");
    json_response(
        StatusCode::OK,
        serde_json::to_string(&facets_for(&data, path, &hidden))
            .unwrap()
            .into_bytes(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::folders::test_cache_data;

    fn comic(path: &str, name: &str, tj: &str, te: &str, artists: &[&str], tags: &[&str]) -> ComicRec {
        (
            path.to_string(),
            name.to_string(),
            tj.to_string(),
            te.to_string(),
            artists.iter().map(|s| s.to_string()).collect(),
            tags.iter().map(|s| s.to_string()).collect(),
            "test".to_string(),
            true,
            10,
        )
    }

    fn paths_of(results: &[Value]) -> Vec<String> {
        results
            .iter()
            .map(|r| r["folder_path"].as_str().unwrap().to_string())
            .collect()
    }

    // ── relevance ──────────────────────────────────────────────────────

    #[test]
    fn relevance_empty_query_scores_zero() {
        let c = comic("a", "n", "t", "", &[], &[]);
        assert_eq!(relevance("", &c), 0.0);
        assert_eq!(relevance("   ", &c), 0.0);
    }

    #[test]
    fn relevance_weights_title_artist_tag() {
        let c = comic("a", "n", "のだめ", "nodame", &["person A"], &["romance"]);
        // title_jp + title_en are concatenated into one haystack
        assert_eq!(relevance("nodame", &c), 10.0);
        assert_eq!(relevance("person", &c), 8.0);  // artist substring
        assert_eq!(relevance("romance", &c), 3.0); // tag substring
    }

    #[test]
    fn relevance_multi_field_bonus() {
        let c = comic("a", "n", "alpha", "beta", &["gamma"], &["delta"]);
        // "a" hits title? "alpha" contains "a" → +10; artist "gamma" no; tag "delta" no.
        // Use a word present in title AND artist AND tag.
        let c2 = comic("a", "n", "foo title", "", &["foo artist"], &["foo tag"]);
        // per word: title +10, artist +8, tag +3 = 21 + fields(3)-1 *2 = 25
        assert_eq!(relevance("foo", &c2), 25.0);
        let _ = c;
    }

    #[test]
    fn relevance_is_case_insensitive_substring() {
        let c = comic("a", "n", "TitleCase", "", &["SomeArtist"], &[]);
        assert!(relevance("titlecase", &c) > 0.0);
        assert!(relevance("ARTIST", &c) > 0.0);
        // substring, not word-boundary
        assert_eq!(relevance("tle", &c), 10.0);
    }

    #[test]
    fn relevance_multi_word_accumulates() {
        let c = comic("a", "n", "red blue", "", &[], &[]);
        // "red" +10, "blue" +10 → 20; same field hit twice → fields=1 → no bonus
        assert_eq!(relevance("red blue", &c), 20.0);
        // unknown word contributes nothing
        assert_eq!(relevance("red zzz", &c), 10.0);
    }

    // ── matches_filters ────────────────────────────────────────────────

    fn filters<'a>(pairs: &'a [(&'a str, &'a [&'a str])]) -> HashMap<&'a str, Vec<String>> {
        pairs
            .iter()
            .map(|(k, vs)| (*k, vs.iter().map(|s| s.to_string()).collect()))
            .collect()
    }

    #[test]
    fn filters_artist_substring_case_insensitive() {
        let c = comic("a", "n", "", "", &["CLAMP Artist"], &[]);
        assert!(matches_filters(&c, &filters(&[("artist", &["clamp"])]) ));
        assert!(matches_filters(&c, &filters(&[("artist", &["ARTIST"])])));
        assert!(!matches_filters(&c, &filters(&[("artist", &["other"])]) ));
    }

    #[test]
    fn filters_or_within_key_and_across_keys() {
        let c = comic("a", "n", "", "", &["A"], &["x", "y"]);
        // OR within tags
        assert!(matches_filters(&c, &filters(&[("tags", &["x", "z"])]) ));
        // AND across keys: artist A ok but missing tag w → false
        assert!(!matches_filters(&c, &filters(&[("artist", &["A"]), ("tags", &["w"])]) ));
        // empty value list → key skipped
        assert!(matches_filters(&c, &filters(&[("artist", &[] as &[&str])]) ));
        // unknown key filters everything out
        assert!(!matches_filters(&c, &filters(&[("genre", &["x"])]) ));
        // comic with no artists + artist filter → false
        let c2 = comic("a", "n", "", "", &[], &["x"]);
        assert!(!matches_filters(&c2, &filters(&[("artist", &["A"])]) ));
    }

    // ── search_index ───────────────────────────────────────────────────

    fn data(comics: Vec<ComicRec>) -> CacheData {
        test_cache_data(comics)
    }

    #[test]
    fn search_index_empty_query_returns_all_visible_sorted() {
        let d = data(vec![
            comic("lib/b", "Beta", "", "", &[], &[]),
            comic("lib/a", "Alpha", "", "", &[], &[]),
        ]);
        let out = search_index(&d, "", &HashMap::new(), &[]);
        // empty query → score 0 for all; natural_cmp on folder_name
        assert_eq!(paths_of(&out), vec!["lib/a", "lib/b"]);
    }

    #[test]
    fn search_index_ranks_by_score_then_name() {
        let d = data(vec![
            comic("lib/x", "zzz", "", "needle", &[], &[]),   // title_en hit → 10
            comic("lib/y", "yyy", "", "", &["needle"], &[]), // artist hit → 8
            comic("lib/z", "needle2", "", "", &[], &[]),    // folder_name not scored! title_jp empty
        ]);
        // "needle" hits title_en of lib/x (10) and artist of lib/y (8);
        // lib/z's folder_name isn't part of relevance haystack → score 0 → excluded
        let out = search_index(&d, "needle", &HashMap::new(), &[]);
        assert_eq!(paths_of(&out), vec!["lib/x", "lib/y"]);
    }

    #[test]
    fn search_index_excludes_hidden_roots() {
        // Privacy contract: hidden roots must not appear in results even when
        // the query matches — include_hidden is the only opt-out (handler level).
        let d = data(vec![
            comic("secret/comic", "Hidden Hit", "needle", "", &[], &[]),
            comic("lib/ok", "Visible Hit", "needle", "", &[], &[]),
            comic("secret/deep/x", "Deep Hidden", "needle", "", &[], &[]),
        ]);
        let roots = vec!["secret".to_string()];
        let out = search_index(&d, "needle", &HashMap::new(), &roots);
        assert_eq!(paths_of(&out), vec!["lib/ok"]);
        // empty query also excludes hidden
        let out_all = search_index(&d, "", &HashMap::new(), &roots);
        assert_eq!(paths_of(&out_all), vec!["lib/ok"]);
        // no roots → everything visible
        let out_none = search_index(&d, "needle", &HashMap::new(), &[]);
        assert_eq!(out_none.len(), 3);
    }

    #[test]
    fn search_index_applies_filters_after_scoring() {
        let d = data(vec![
            comic("lib/a", "A", "needle", "", &["X"], &[]),
            comic("lib/b", "B", "needle", "", &["Y"], &[]),
        ]);
        let f = filters(&[("artist", &["x"])]);
        let out = search_index(&d, "needle", &f, &[]);
        assert_eq!(paths_of(&out), vec!["lib/a"]);
    }

    #[test]
    fn search_index_result_shape_matches_comic_to_json() {
        let d = data(vec![comic("lib/a", "A", "題", "Title", &["Ar"], &["tg"],)]);
        let out = search_index(&d, "", &HashMap::new(), &[]);
        assert_eq!(out.len(), 1);
        let r = &out[0];
        for k in ["folder_path","folder_name","title_jp","title_en","artist","tags","source","has_metadata","total_pages"] {
            assert!(r.get(k).is_some(), "missing key {k}");
        }
        assert_eq!(r["total_pages"], 10);
        assert_eq!(r["has_metadata"], true);
    }

    // ── build_facets / facets_for ──────────────────────────────────────

    #[test]
    fn build_facets_dedups_exact_and_sorts_case_insensitive() {
        // Contract (ports Python `sorted(set(...), key=str.casefold)`):
        // dedup is EXACT — "ALPHA"/"Alpha" are distinct entries — while the
        // ordering is case-insensitive with original-string tiebreak.
        let comics = vec![
            comic("a", "n", "", "", &["beta", "Alpha"], &["z", "a"]),
            comic("b", "n", "", "", &["ALPHA", "gamma"], &["m"]),
        ];
        let v = build_facets(comics.iter());
        assert_eq!(
            v["artists"],
            serde_json::json!(["ALPHA", "Alpha", "beta", "gamma"])
        );
        assert_eq!(v["tags"], serde_json::json!(["a", "m", "z"]));
    }

    #[test]
    fn facets_for_scopes_to_path_prefix() {
        let d = data(vec![
            comic("lib/a/x", "n", "", "", &["in-a"], &[]),
            comic("lib/ab/y", "n", "", "", &["in-ab"], &[]),
            comic("lib/b/z", "n", "", "", &["in-b"], &[]),
            comic("other/w", "n", "", "", &["other"], &[]),
        ]);
        // path "lib/a" must NOT match "lib/ab" (prefix needs the "/" boundary)
        let v = facets_for(&d, "lib/a", &[]);
        assert_eq!(v["artists"], serde_json::json!(["in-a"]));
        // exact folder match included
        let v2 = facets_for(&d, "lib/b/z", &[]);
        assert_eq!(v2["artists"], serde_json::json!(["in-b"]));
        // empty path → all
        let v3 = facets_for(&d, "", &[]);
        assert_eq!(v3["artists"].as_array().unwrap().len(), 4);
        // trailing slash tolerated
        let v4 = facets_for(&d, "lib/a/", &[]);
        assert_eq!(v4["artists"], serde_json::json!(["in-a"]));
    }

    #[test]
    fn facets_for_excludes_hidden_roots() {
        // Hidden works' artists/tags must not leak into the facet list —
        // same fail-closed contract as search_index.
        let d = data(vec![
            comic("secret/x", "n", "", "", &["secret-artist"], &["secret-tag"]),
            comic("lib/y", "n", "", "", &["public-artist"], &["public-tag"]),
        ]);
        let roots = vec!["secret".to_string()];
        let v = facets_for(&d, "", &roots);
        assert_eq!(v["artists"], serde_json::json!(["public-artist"]));
        assert_eq!(v["tags"], serde_json::json!(["public-tag"]));
    }

    // ── parse_bool / bool_param ────────────────────────────────────────

    #[test]
    fn parse_bool_accepts_fastapi_token_sets() {
        let s = |v: &str| Some(v.to_string());
        for t in ["1","true","t","yes","y","on","TRUE","Yes"] {
            assert_eq!(parse_bool(s(t).as_ref()), Some(true), "{t}");
        }
        for t in ["0","false","f","no","n","off","FALSE","No"] {
            assert_eq!(parse_bool(s(t).as_ref()), Some(false), "{t}");
        }
        for t in ["", "maybe", "2", "enable"] {
            assert_eq!(parse_bool(s(t).as_ref()), None, "{t}");
        }
        assert_eq!(parse_bool(None), None);
    }

    #[test]
    fn bool_param_defaults_false_and_rejects_invalid() {
        let mut q = HashMap::new();
        assert_eq!(bool_param(&q, "include_hidden"), Some(Some(false)));
        q.insert("include_hidden".to_string(), "true".to_string());
        assert_eq!(bool_param(&q, "include_hidden"), Some(Some(true)));
        q.insert("include_hidden".to_string(), "junk".to_string());
        assert_eq!(bool_param(&q, "include_hidden"), None);
    }
}
