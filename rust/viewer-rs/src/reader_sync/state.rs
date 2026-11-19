//! dual-reader authoritative shared state + sequencer — port of
//! `app/reader_sync/state.py`. Pure and transport-free.

use std::collections::HashMap;

use serde_json::{Map, Value};

use super::model::{self, Spread};
use super::{Link, PairSession};

// reject reasons for sequenced ops (wire-stable strings)
pub const REASON_STALE_EPOCH: &str = "stale_epoch";
pub const REASON_STALE_REVISION: &str = "stale_revision";
pub const REASON_STALE_MANIFEST: &str = "stale_manifest";
pub const REASON_STALE_CHAPTER: &str = "stale_chapter";
pub const REASON_STALE_REQUEST: &str = "stale_request";
pub const REASON_NOT_READY: &str = "not_ready";
pub const REASON_NO_WORK: &str = "no_work";
pub const REASON_BAD_PHASE: &str = "bad_phase";
pub const REASON_RANGE: &str = "range";
pub const REASON_BAD_ORIGIN: &str = "bad_origin";
pub const REASON_NO_INITIAL_PAGE: &str = "no_initial_page";
#[allow(dead_code)]
pub const REASON_UNAVAILABLE: &str = "unavailable";
pub const REASON_PEER_OFFLINE: &str = "peer_offline";
pub const REASON_NOT_LEADER: &str = "not_leader";
pub const REASON_MALFORMED: &str = "malformed";

pub const ABSOLUTE_TYPES: &[&str] = &["GOTO", "SET_OFFSET", "SWAP_SIDES"];
pub const SEQUENCED_TYPES: &[&str] = &[
    "TURN",
    "GOTO",
    "SET_OFFSET",
    "SWAP_SIDES",
    "OPEN_WORK",
    "READY",
    "APPLIED",
];

/// Manifest failure code → REJECT/ERROR reason vocabulary.
pub fn manifest_err_reason(code: &str) -> &'static str {
    match code {
        "ERR_FORBIDDEN" => "forbidden",
        "ERR_NOT_FOUND" => "not_found",
        "ERR_EMPTY_WORK" => "empty_work",
        "ERR_BROKEN_ARCHIVE" => "broken_archive",
        "ERR_PAGE_NOT_FOUND" => "page_not_found",
        "ERR_NO_INITIAL_PAGE" => "no_initial_page",
        "ERR_RANGE" => "range",
        "ERR_SIZE" => "size",
        "ERR_MALFORMED" => "malformed",
        "ERR_TIMEOUT" => "timeout",
        _ => "malformed",
    }
}

/// Validation failure inside the sequencer — raised before any mutation.
#[derive(Debug)]
pub struct Reject {
    pub reason: &'static str,
}
impl std::fmt::Display for Reject {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.reason)
    }
}
impl std::error::Error for Reject {}

fn reject<T>(reason: &'static str) -> Result<T, Reject> {
    Err(Reject { reason })
}

/// Python `==` between a JSON value and an int: `True == 1`, `1.0 == 1`,
/// everything else (strings, null, objects) is never equal.
pub fn py_num_eq(v: &Value, n: i64) -> bool {
    match v {
        Value::Number(x) => x.as_f64() == Some(n as f64),
        Value::Bool(b) => (*b as i64) == n,
        _ => false,
    }
}

/// Python int coercion for arithmetic: bools -> 0/1, ints -> themselves.
pub fn py_i64(v: &Value) -> Option<i64> {
    match v {
        Value::Number(n) => n.as_i64(),
        Value::Bool(b) => Some(*b as i64),
        _ => None,
    }
}

// ── phase derivation (§3.3 vocabulary) ─────────────────────────────

pub fn all_ready(session: &PairSession) -> bool {
    !session.members.is_empty()
        && session
            .members
            .iter()
            .all(|m| session.ready.contains(&m.id))
}

/// Pure phase projection — recomputed for every STATE.
/// `links` resolves member liveness (m.link.alive in Python).
pub fn derive_phase(session: &PairSession, links: &HashMap<String, Link>) -> &'static str {
    if session.pending.is_some() || session.members.len() != 2 {
        return "waiting";
    }
    if session.members.iter().any(|m| {
        m.link_id
            .as_ref()
            .and_then(|id| links.get(id))
            .is_none_or(|l| !l.alive)
            || m.status != "active"
    }) {
        return "paused";
    }
    // An accepted pick is "loading" even before its manifest commits.
    if session.pending_load.is_some() {
        return "loading";
    }
    if session.selecting || session.manifest.is_none() {
        return "selecting";
    }
    if !all_ready(session) {
        return "loading";
    }
    "reading"
}

/// The committed snapshot — embedded verbatim in REJECT frames.
pub fn snapshot(session: &PairSession, links: &HashMap<String, Link>) -> Value {
    let mut m = Map::new();
    m.insert("sessionId".into(), Value::String(session.id.clone()));
    m.insert("revision".into(), Value::from(session.revision));
    m.insert("workEpoch".into(), Value::from(session.work_epoch));
    m.insert(
        "manifestVersion".into(),
        Value::from(session.manifest_version),
    );
    m.insert(
        "phase".into(),
        Value::String(derive_phase(session, links).into()),
    );
    m.insert(
        "spreadId".into(),
        session
            .spread_id
            .clone()
            .map(Value::String)
            .unwrap_or(Value::Null),
    );
    m.insert("offset".into(), Value::from(session.offset));
    m.insert(
        "leaderSide".into(),
        Value::String(session.leader_side.clone()),
    );
    m.insert(
        "dataPlane".into(),
        Value::String(session.data_plane.clone()),
    );
    Value::Object(m)
}

/// Reconstruct spread objects from a committed manifest's spreadTable.
pub fn plan_from_manifest(manifest: &Value) -> Result<Vec<Spread>, model::ModelError> {
    let table = manifest
        .get("spreadTable")
        .and_then(|v| v.as_array())
        .ok_or(model::ModelError {
            code: "ERR_MALFORMED",
            message: "manifest.spreadTable must be a list".into(),
        })?;
    let mut plan = Vec::with_capacity(table.len());
    for s in table {
        let id = s.get("id").and_then(|v| v.as_str());
        let cf = s.get("chapterFolder").and_then(|v| v.as_str());
        let right = s.get("right");
        let left = s.get("left");
        let (id, cf) = match (id, cf) {
            (Some(i), Some(c)) => (i, c),
            _ => {
                return Err(model::ModelError {
                    code: "ERR_MALFORMED",
                    message: "spread needs id+chapterFolder".into(),
                })
            }
        };
        let parse_ref = |r: &Value| -> Result<model::PageRef, model::ModelError> {
            let f = r.get("folder").and_then(|v| v.as_str());
            let n = r.get("name").and_then(|v| v.as_str());
            match (f, n) {
                (Some(f), Some(n)) => Ok(model::PageRef {
                    folder: f.into(),
                    name: n.into(),
                }),
                _ => Err(model::ModelError {
                    code: "ERR_MALFORMED",
                    message: "spread ref needs folder+name".into(),
                }),
            }
        };
        let right = parse_ref(right.ok_or(model::ModelError {
            code: "ERR_MALFORMED",
            message: "spread missing right".into(),
        })?)?;
        let left = match left {
            Some(Value::Null) | None => None,
            Some(l) => Some(parse_ref(l)?),
        };
        plan.push(Spread {
            id: id.into(),
            chapter_folder: cf.into(),
            right,
            left,
        });
    }
    Ok(plan)
}

/// Index of the committed spread inside `session.plan`; -1 with no position.
pub fn current_index(session: &PairSession) -> i64 {
    match (&session.plan, &session.spread_id) {
        (Some(plan), Some(id)) => model::spread_index_by_id(plan, id),
        _ => -1,
    }
}

// ── request-frame checks ───────────────────────────────────────────

pub fn check_epoch(session: &PairSession, req: &Map<String, Value>) -> Result<(), Reject> {
    if !py_num_eq(&req["workEpoch"], session.work_epoch) {
        return reject(REASON_STALE_EPOCH);
    }
    Ok(())
}

pub fn check_manifest_version(
    session: &PairSession,
    payload: &Map<String, Value>,
) -> Result<(), Reject> {
    if let Some(mv) = payload.get("manifestVersion") {
        // `mv is not None and mv != session.manifest_version`
        if !mv.is_null() && !py_num_eq(mv, session.manifest_version) {
            return reject(REASON_STALE_MANIFEST);
        }
    }
    Ok(())
}

pub fn check_base(session: &PairSession, req: &Map<String, Value>) -> Result<(), Reject> {
    if !py_num_eq(&req["baseRevision"], session.revision) {
        return reject(REASON_STALE_REVISION);
    }
    Ok(())
}

/// Reject reason for TURN/GOTO under the current derived phase.
pub fn gate_page_turn(
    session: &PairSession,
    links: &HashMap<String, Link>,
) -> Option<&'static str> {
    match derive_phase(session, links) {
        "reading" => None,
        "paused" => Some(REASON_PEER_OFFLINE),
        "loading" => Some(REASON_NOT_READY),
        _ => Some(if session.manifest.is_none() {
            REASON_NO_WORK
        } else {
            REASON_BAD_PHASE
        }),
    }
}

// ── committed transitions (validate first, then mutate) ────────────

fn map_model_error(e: &model::ModelError) -> &'static str {
    match e.code {
        "ERR_RANGE" => REASON_RANGE,
        "ERR_SIZE" => "size",
        _ => REASON_MALFORMED,
    }
}

/// TURN: ±1 spread step with chapter-limit stale guard.
pub fn apply_turn(session: &mut PairSession, payload: &Map<String, Value>) -> Result<i64, Reject> {
    let idx = current_index(session);
    if idx < 0 {
        return reject(REASON_NO_WORK);
    }
    if !payload
        .get("crossChapter")
        .and_then(|v| v.as_bool())
        .unwrap_or(false)
    {
        if let Some(claimed) = payload.get("fromChapter").and_then(|v| v.as_str()) {
            let plan = session.plan.as_ref().unwrap();
            if claimed != plan[idx as usize].chapter_folder {
                return reject(REASON_STALE_CHAPTER);
            }
        }
    }
    let plan = session.plan.as_ref().unwrap();
    let delta = py_i64(&payload["delta"]).unwrap_or(0);
    let cross = payload
        .get("crossChapter")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    let nxt = model::turn_index(plan, idx, delta, cross).map_err(|e| Reject {
        reason: map_model_error(&e),
    })?;
    session.spread_id = Some(plan[nxt as usize].id.clone());
    Ok(nxt)
}

/// GOTO: the shared spread containing the requested PageRef.
pub fn apply_goto(session: &mut PairSession, payload: &Map<String, Value>) -> Result<i64, Reject> {
    if session.plan.as_ref().is_none_or(|p| p.is_empty()) {
        return reject(REASON_NO_WORK);
    }
    let page = payload.get("page").cloned().unwrap_or(Value::Null);
    let plan = session.plan.as_ref().unwrap();
    let nxt = model::goto_index(plan, &page).map_err(|e| Reject {
        reason: map_model_error(&e),
    })?;
    session.spread_id = Some(plan[nxt].id.clone());
    Ok(nxt as i64)
}

/// SWAP_SIDES: shared left/right assignment.
pub fn apply_swap_sides(
    session: &mut PairSession,
    payload: &Map<String, Value>,
) -> Result<String, Reject> {
    let side = match payload.get("leaderSide").and_then(|v| v.as_str()) {
        Some(s) if super::protocol::SIDES.contains(&s) => s.to_string(),
        _ => return reject(REASON_RANGE),
    };
    session.leader_side = side.clone();
    Ok(side)
}

/// Deterministic order for a simultaneous request batch on the
/// leader-sequenced webrtc-dc plane (port kept for completeness).
#[allow(dead_code)]
pub fn order_batch(pairs: &[(String, Value)]) -> Vec<(String, Value)> {
    let rank = |role: &str| match role {
        "leader" => 0,
        "follower" => 1,
        _ => 2,
    };
    let mut idx: Vec<usize> = (0..pairs.len()).collect();
    idx.sort_by_key(|&i| (rank(&pairs[i].0), i));
    idx.into_iter().map(|i| pairs[i].clone()).collect()
}
