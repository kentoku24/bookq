//! TEST_MODE-only state bridge for the dual-reader verification runner (#405).
//!
//! Port of `app/api_v1.py`'s pair-report/pair-reset hooks: the in-page
//! `?pair_report=<cid>` hook (`browse/pairSync.js`, test mode only) POSTs its
//! raw PairController/PairedView state here so the runner can read back
//! structured state from clients it cannot JS-eval — iOS Simulator Safari
//! and PWA webclips. Same-origin, so it survives https dev envs where an
//! http:// fixture-server POST would be mixed-content-blocked. Deliberately
//! NOT in `api/openapi.v1.yaml` — it is a test hook, not API contract.
//!
//! * `POST /api/v1/pair-report` — store `{id, gen?, ...state}` keyed by `id`
//!   (truncated to 64 chars); a parked tab from a previous run keeps POSTing
//!   under the same client id, so only the newest generation wins (`rpt_gen`
//!   in the launch URL; gen-less posts lose to any gen'd report).
//! * `GET /api/v1/pair-report?id=` — return `{state}` (`null` when unknown).
//! * `POST /api/v1/pair-reset` — end every live reader-sync session with
//!   `"reset"` so a stale link-drop session can't hijack the next MATCH.
//!
//! All three are gated on `native.test_mode` — outside test mode they 404,
//! exactly like the Python `_pair_report_gate`. No auth: the Python routes
//! carry none either. No `BOOKQ_RS_NATIVE` endpoint id: test mode implies
//! the full native surface, and outside test mode these must not exist.

use std::collections::HashMap;

use axum::{
    body::Body,
    extract::{Query, Request, State},
    http::{HeaderValue, StatusCode},
    response::Response,
};
use serde_json::{Map, Value};

use crate::reader_sync::END_RESET;
use crate::AppState;

/// In-memory reports: client id → stored state (without `id`, with
/// `received_at`). Process-local like Python's `_PAIR_REPORTS`.
pub type PairReports = tokio::sync::Mutex<HashMap<String, Value>>;

fn json_body(status: StatusCode, body: &Value) -> Response {
    let mut res = Response::builder()
        .status(status)
        .body(Body::from(serde_json::to_string(body).unwrap()))
        .unwrap();
    res.headers_mut().insert(
        axum::http::header::CONTENT_TYPE,
        HeaderValue::from_static("application/json"),
    );
    res
}

fn now_secs() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// Python `str(...)[:64]` — char-wise, so multi-byte ids truncate the same.
pub fn truncate_id(s: &str) -> String {
    s.chars().take(64).collect()
}

/// Python `int(body.get("gen") or 0)`: absent/null → 0; bools → 0/1;
/// integral floats truncate; numeric strings parse; anything else → 0.
/// The hook always sends an int or nothing — this only keeps adversarial
/// payloads from diverging between backends.
pub fn gen_of(v: Option<&Value>) -> i64 {
    match v {
        None | Some(Value::Null) => 0,
        Some(Value::Bool(b)) => i64::from(*b),
        Some(Value::Number(n)) => n.as_i64().unwrap_or_else(|| {
            n.as_u64()
                .map(|u| u.min(i64::MAX as u64) as i64)
                .unwrap_or_else(|| n.as_f64().map(|f| f.trunc() as i64).unwrap_or(0))
        }),
        Some(Value::String(s)) => s.trim().parse::<i64>().unwrap_or(0),
        _ => 0,
    }
}

#[derive(Debug, PartialEq)]
pub enum InsertOutcome {
    Stored,
    Dropped,
}

/// Mirror of `v1_post_pair_report`. `Err` carries the 400 detail
/// (`"id required"` matches the Python message verbatim).
pub fn insert_report(
    reports: &mut HashMap<String, Value>,
    mut body: Map<String, Value>,
    now: f64,
) -> Result<InsertOutcome, &'static str> {
    let cid = match body.get("id").and_then(|v| v.as_str()) {
        Some(s) => truncate_id(s),
        None => String::new(),
    };
    if cid.is_empty() {
        return Err("id required");
    }
    let gen = gen_of(body.get("gen"));
    if let Some(cur) = reports.get(&cid) {
        if gen < gen_of(cur.get("gen")) {
            return Ok(InsertOutcome::Dropped);
        }
    }
    body.remove("id");
    body.insert("received_at".into(), Value::from(now));
    reports.insert(cid, Value::Object(body));
    Ok(InsertOutcome::Stored)
}

/// `POST /api/v1/pair-report` + `GET /api/v1/pair-report?id=` — one route,
/// dispatched on method like the other multi-verb natives. Unknown methods
/// proxy upstream (which answers 405, same as the Python router).
pub async fn handle_report(State(st): State<AppState>, req: Request<Body>) -> Response {
    if !st.native.test_mode {
        return crate::state::detail(StatusCode::NOT_FOUND, "Not Found");
    }
    match *req.method() {
        axum::http::Method::POST => {
            let (parts, body) = req.into_parts();
            let bytes = match http_body_util::BodyExt::collect(body).await {
                Ok(c) => c.to_bytes(),
                Err(_) => {
                    return crate::proxy(&st.upstream, Request::from_parts(parts, Body::empty()))
                        .await
                }
            };
            let Ok(Value::Object(map)) = serde_json::from_slice::<Value>(&bytes) else {
                return crate::state::detail(StatusCode::BAD_REQUEST, "invalid JSON body");
            };
            let mut reports = st.pair_reports.lock().await;
            match insert_report(&mut reports, map, now_secs()) {
                Ok(InsertOutcome::Stored) => {
                    json_body(StatusCode::OK, &serde_json::json!({"ok": true}))
                }
                Ok(InsertOutcome::Dropped) => json_body(
                    StatusCode::OK,
                    &serde_json::json!({"ok": true, "dropped": true}),
                ),
                Err(detail) => crate::state::detail(StatusCode::BAD_REQUEST, detail),
            }
        }
        axum::http::Method::GET => {
            let q = Query::<HashMap<String, String>>::try_from_uri(req.uri())
                .map(|q| q.0)
                .unwrap_or_default();
            // `id` is a required Query param upstream — a missing one keeps
            // FastAPI's 422 by proxying instead of inventing a shape here.
            let Some(id) = q.get("id") else {
                return crate::proxy(&st.upstream, req).await;
            };
            let reports = st.pair_reports.lock().await;
            let state = reports.get(id.as_str()).cloned().unwrap_or(Value::Null);
            json_body(StatusCode::OK, &serde_json::json!({"state": state}))
        }
        _ => crate::proxy(&st.upstream, req).await,
    }
}

/// `POST /api/v1/pair-reset` — end every live reader-sync session with
/// `"reset"`, returning the count. Unknown methods proxy (upstream 405).
pub async fn handle_reset(State(st): State<AppState>, req: Request<Body>) -> Response {
    if !st.native.test_mode {
        return crate::state::detail(StatusCode::NOT_FOUND, "Not Found");
    }
    if *req.method() != axum::http::Method::POST {
        return crate::proxy(&st.upstream, req).await;
    }
    let n = st.registry.end_all(END_RESET).await;
    json_body(StatusCode::OK, &serde_json::json!({"ended": n}))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn obj(pairs: &[(&str, Value)]) -> Map<String, Value> {
        pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.clone()))
            .collect()
    }

    #[test]
    fn post_strips_id_and_stamps_received_at() {
        let mut reports = HashMap::new();
        let body = obj(&[
            ("id", Value::String("pytest-bridge".into())),
            ("pc", serde_json::json!({"phase": "reading"})),
            ("pv", serde_json::json!({"left": "1"})),
        ]);
        let out = insert_report(&mut reports, body, 1700000000.5).unwrap();
        assert!(matches!(out, InsertOutcome::Stored));
        let st = reports.get("pytest-bridge").unwrap();
        assert_eq!(st["pc"], serde_json::json!({"phase": "reading"}));
        assert_eq!(st["pv"], serde_json::json!({"left": "1"}));
        assert!(st.get("id").is_none()); // id is the key, not part of the state
        assert!((st["received_at"].as_f64().unwrap() - 1700000000.5).abs() < f64::EPSILON);
        assert_eq!(gen_of(st.get("gen")), 0);
    }

    #[test]
    fn newest_generation_wins_stale_drops() {
        let mut reports = HashMap::new();
        insert_report(
            &mut reports,
            obj(&[
                ("id", Value::String("c1".into())),
                ("gen", Value::from(2)),
                ("v", Value::from("new")),
            ]),
            1.0,
        )
        .unwrap();
        // Stale gen from a parked tab must not clobber the fresh report.
        let out = insert_report(
            &mut reports,
            obj(&[
                ("id", Value::String("c1".into())),
                ("gen", Value::from(1)),
                ("v", Value::from("stale")),
            ]),
            2.0,
        )
        .unwrap();
        assert!(matches!(out, InsertOutcome::Dropped));
        assert_eq!(reports["c1"]["v"], Value::String("new".into()));
        // Same gen overwrites (Python keeps `>=` — only strictly older drops).
        let out = insert_report(
            &mut reports,
            obj(&[
                ("id", Value::String("c1".into())),
                ("gen", Value::from(2)),
                ("v", Value::from("same")),
            ]),
            3.0,
        )
        .unwrap();
        assert!(matches!(out, InsertOutcome::Stored));
        assert_eq!(reports["c1"]["v"], Value::String("same".into()));
        // Gen-less posts lose to any gen'd report.
        let out = insert_report(
            &mut reports,
            obj(&[
                ("id", Value::String("c1".into())),
                ("v", Value::from("genless")),
            ]),
            4.0,
        )
        .unwrap();
        assert!(matches!(out, InsertOutcome::Dropped));
    }

    #[test]
    fn id_required_and_truncated() {
        let mut reports = HashMap::new();
        let err = insert_report(&mut reports, obj(&[("pc", Value::Object(Map::new()))]), 0.0)
            .unwrap_err();
        assert_eq!(err, "id required");
        let err = insert_report(
            &mut reports,
            obj(&[("id", Value::String(String::new()))]),
            0.0,
        )
        .unwrap_err();
        assert_eq!(err, "id required");

        let long = "x".repeat(100);
        insert_report(
            &mut reports,
            obj(&[("id", Value::String(long.clone())), ("v", Value::from(1))]),
            0.0,
        )
        .unwrap();
        assert_eq!(long.chars().count(), 100);
        assert!(reports.contains_key(&"x".repeat(64)));
        assert_eq!(reports.len(), 1);
    }

    #[test]
    fn gen_coercions_match_python_int_or_zero() {
        assert_eq!(gen_of(None), 0);
        assert_eq!(gen_of(Some(&Value::Null)), 0);
        assert_eq!(gen_of(Some(&Value::from(3))), 3);
        assert_eq!(gen_of(Some(&Value::from(true))), 1);
        assert_eq!(gen_of(Some(&Value::from(false))), 0);
        assert_eq!(gen_of(Some(&serde_json::json!(2.9))), 2);
        assert_eq!(gen_of(Some(&Value::String("7".into()))), 7);
        assert_eq!(gen_of(Some(&Value::String("nope".into()))), 0);
        assert_eq!(gen_of(Some(&serde_json::json!({"n": 1}))), 0);
    }

    #[test]
    fn truncate_id_is_char_wise() {
        let multi = "あ".repeat(100); // 3 bytes each — byte slicing would split chars
        assert_eq!(truncate_id(&multi).chars().count(), 64);
        assert_eq!(truncate_id("abc"), "abc");
    }
}
