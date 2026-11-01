//! `/api/v1/reading-positions` + `/api/v1/folder-visibility` — M4 of #163.
//!
//! Ports `app/api_v1.py`'s state endpoints plus the stores behind them:
//!
//! * reading positions are per-process ephemeral (`app.state.reading_positions`
//!   is an in-memory dict — a restart wipes it, so a Rust-side map has the
//!   same contract while every request on this backend routes through it).
//! * folder visibility persists in the shared `user-state` SQLite volume —
//!   the same DB the Python viewer writes, so one writer's rows are the
//!   other's reads. Schema/transaction semantics are ported verbatim
//!   (`app/folder_visibility.py`): WAL, busy_timeout 5s, revision bumps only
//!   on an actual change, fresh connection per operation.
//!
//! Caller identity: handlers take the resolved user key (`_position_user_key`
//! / `user_key_for_email` equivalents) — test mode's mock user or a Bearer
//! JWT's `sub` (see `auth.rs`). Unidentified callers never reach this file:
//! main.rs delegates them upstream.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use axum::{
    body::Body,
    http::{header, HeaderMap, HeaderValue, StatusCode},
    response::Response,
};
use hmac::{Hmac, Mac};
use rusqlite::OptionalExtension;
use serde_json::Value;
use sha2::Sha256;

const POSITION_MAX_USERS: usize = 100;
const POSITION_MAX_FOLDERS: usize = 512;
const POSITION_TS_FUTURE_SLACK_MS: i64 = 300_000;
const MAX_ROOT_LENGTH: usize = 2048;
const BUSY_TIMEOUT_MS: u64 = 5000;

fn now_secs() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

fn json_response(status: StatusCode, body: &Value) -> Response {
    let mut res = Response::builder()
        .status(status)
        .body(Body::from(serde_json::to_string(body).unwrap()))
        .unwrap();
    res.headers_mut().insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("application/json"),
    );
    res
}

fn detail(status: StatusCode, msg: &str) -> Response {
    json_response(status, &serde_json::json!({"detail": msg}))
}

// ── reading positions ───────────────────────────────────────────────────────

#[derive(Clone)]
struct Rec {
    folder: String,
    page: i64,
    total: i64,
    ts: i64,
    tombstone: bool,
    updated_at: f64,
    /// Insertion sequence — Python's dict order is insertion-ordered and a
    /// stable `sorted(ts, reverse=True)` keeps it for equal ts.
    seq: u64,
}

#[derive(Default)]
struct Slot {
    folders: HashMap<String, Rec>,
    tombstone_ts: i64,
    updated_at: f64,
    next_seq: u64,
}

#[derive(Default)]
pub struct Positions {
    map: Mutex<HashMap<String, Slot>>,
}

fn slot_max_ts(slot: &Slot) -> i64 {
    slot.folders
        .values()
        .map(|r| r.ts)
        .max()
        .unwrap_or(0)
        .max(slot.tombstone_ts)
}

/// `_validate_folder_param` — non-empty, ≤2048, no leading `/`, no NUL.
fn valid_folder_param(folder: &str) -> bool {
    !folder.is_empty() && folder.len() <= 2048 && !folder.starts_with('/') && !folder.contains('\0')
}

/// `_as_int` — JSON integer admits integral floats; bools stay rejected.
fn as_int(v: &Value) -> Option<i64> {
    if v.is_boolean() {
        return None;
    }
    if let Some(i) = v.as_i64() {
        return Some(i);
    }
    if v.is_f64() {
        let f = v.as_f64().unwrap();
        if f.is_finite() && f.fract() == 0.0 && f >= i64::MIN as f64 && f <= i64::MAX as f64 {
            return Some(f as i64);
        }
    }
    None
}

pub fn list_positions(pos: &Positions, user_key: &str, folder: Option<&str>) -> Response {
    let map = pos.map.lock().unwrap();
    let slot = map.get(user_key);
    let recs: Vec<&Rec> = match (slot, folder) {
        (Some(s), Some(f)) => s
            .folders
            .get(f)
            .filter(|r| !r.tombstone)
            .into_iter()
            .collect(),
        (Some(s), None) => s.folders.values().filter(|r| !r.tombstone).collect(),
        (None, _) => Vec::new(),
    };
    let mut recs = recs;
    // ts desc, insertion seq asc for ties (stable sorted()).
    recs.sort_by(|a, b| b.ts.cmp(&a.ts).then(a.seq.cmp(&b.seq)));
    let items: Vec<Value> = recs
        .iter()
        .map(|r| {
            serde_json::json!({
                "folder": r.folder, "page": r.page, "total": r.total, "ts": r.ts
            })
        })
        .collect();
    let mut res = json_response(StatusCode::OK, &serde_json::json!({"positions": items}));
    res.headers_mut().insert(
        header::CACHE_CONTROL,
        HeaderValue::from_static("no-store"),
    );
    res
}

pub fn put_position(pos: &Positions, user_key: &str, folder: &str, body_bytes: &[u8]) -> Response {
    if !valid_folder_param(folder) {
        return detail(StatusCode::BAD_REQUEST, "invalid folder");
    }
    let Ok(body) = serde_json::from_slice::<Value>(body_bytes) else {
        return detail(StatusCode::BAD_REQUEST, "invalid JSON body");
    };
    if !body.is_object() {
        return detail(StatusCode::BAD_REQUEST, "expected JSON object");
    }
    let page = match as_int(&body["page"]) {
        Some(v) if v >= 0 => v,
        _ => return detail(StatusCode::BAD_REQUEST, "invalid page"),
    };
    let total = match as_int(&body["total"]) {
        Some(v) if v >= 1 => v,
        _ => return detail(StatusCode::BAD_REQUEST, "invalid total"),
    };
    let ts = match as_int(&body["ts"]) {
        Some(v) if v >= 0 => v,
        _ => return detail(StatusCode::BAD_REQUEST, "invalid ts"),
    };
    if ts > now_ms() + POSITION_TS_FUTURE_SLACK_MS {
        return detail(StatusCode::BAD_REQUEST, "ts too far in the future");
    }
    let updated_at = now_secs();
    let mut map = pos.map.lock().unwrap();
    if !map.contains_key(user_key) && map.len() >= POSITION_MAX_USERS {
        let stalest = map
            .iter()
            .min_by(|a, b| a.1.updated_at.partial_cmp(&b.1.updated_at).unwrap())
            .map(|(k, _)| k.clone())
            .unwrap();
        map.remove(&stalest);
    }
    let slot = map.entry(user_key.to_string()).or_default();
    // _store_position_v1: reject at-or-below the delete-all tombstone, and
    // older/equal ts for the SAME folder (per-folder last-writer-wins).
    let stale = if ts <= slot.tombstone_ts {
        true
    } else {
        match slot.folders.get(folder) {
            Some(ex) if ex.tombstone && ts <= ex.ts => true,
            Some(ex) if !ex.tombstone && ts < ex.ts => true,
            _ => false,
        }
    };
    if stale {
        return json_response(StatusCode::OK, &serde_json::json!({"status": "stale"}));
    }
    let seq = match slot.folders.get(folder) {
        Some(old) => old.seq,
        None => {
            let s = slot.next_seq;
            slot.next_seq += 1;
            s
        }
    };
    slot.folders.insert(
        folder.to_string(),
        Rec {
            folder: folder.to_string(),
            page,
            total,
            ts,
            tombstone: false,
            updated_at,
            seq,
        },
    );
    slot.updated_at = updated_at;
    // _trim_position_slot — evict least-recently-written beyond the bound.
    while slot.folders.len() > POSITION_MAX_FOLDERS {
        if let Some(stalest) = slot
            .folders
            .iter()
            .min_by(|a, b| a.1.updated_at.partial_cmp(&b.1.updated_at).unwrap())
            .map(|(k, _)| k.clone())
        {
            slot.folders.remove(&stalest);
        } else {
            break;
        }
    }
    json_response(StatusCode::OK, &serde_json::json!({"status": "ok"}))
}

pub fn delete_positions(pos: &Positions, user_key: &str, folder: Option<&str>) -> Response {
    if let Some(f) = folder {
        if !valid_folder_param(f) {
            return detail(StatusCode::BAD_REQUEST, "invalid folder");
        }
    }
    let mut map = pos.map.lock().unwrap();
    let slot = map.entry(user_key.to_string()).or_default();
    slot.updated_at = now_secs();
    match folder {
        Some(f) => {
            let prev_ts = slot.folders.get(f).map(|r| r.ts).unwrap_or(0);
            let seq = slot.next_seq;
            slot.next_seq += 1;
            slot.folders.insert(
                f.to_string(),
                Rec {
                    folder: f.to_string(),
                    page: 0,
                    total: 0,
                    ts: prev_ts,
                    tombstone: true,
                    updated_at: now_secs(),
                    seq,
                },
            );
        }
        None => {
            let ts = slot_max_ts(slot);
            let updated = slot.updated_at;
            *slot = Slot {
                folders: HashMap::new(),
                tombstone_ts: ts,
                updated_at: updated,
                next_seq: 0,
            };
        }
    }
    json_response(StatusCode::OK, &serde_json::json!({"status": "ok"}))
}

// ── folder visibility (shared SQLite store) ──────────────────────────────────

// The DDL lives in one shared file (#200) — api/user-state-schema.sql is
// the single source FastAPI and viewer-rs both apply. VIS_SCHEMA_VERSION
// must equal the file's `PRAGMA user_version` stamp; a DB stamped newer
// than it is refused rather than silently downgraded.
const VIS_SCHEMA: &str = include_str!("../../../api/user-state-schema.sql");
const VIS_SCHEMA_VERSION: i64 = 1;

pub struct VisibilityStore {
    path: PathBuf,
    init: Mutex<bool>,
}

pub struct Snapshot {
    pub revision: i64,
    pub hidden_roots: Vec<String>,
}

impl VisibilityStore {
    pub fn new(state_dir: &Path) -> Self {
        Self {
            path: state_dir.join("folder-visibility.sqlite3"),
            init: Mutex::new(false),
        }
    }

    fn connect(&self) -> rusqlite::Result<rusqlite::Connection> {
        {
            let mut done = self.init.lock().unwrap();
            if !*done {
                if let Some(parent) = self.path.parent() {
                    let _ = std::fs::create_dir_all(parent);
                    let _ = std::fs::set_permissions(
                        parent,
                        std::os::unix::fs::PermissionsExt::from_mode(0o700),
                    );
                }
                let conn = rusqlite::Connection::open(&self.path)?;
                conn.pragma_update(None, "journal_mode", "WAL")?;
                conn.pragma_update(None, "busy_timeout", BUSY_TIMEOUT_MS)?;
                let current: i64 =
                    conn.query_row("PRAGMA user_version", [], |r| r.get(0))?;
                assert!(
                    current <= VIS_SCHEMA_VERSION,
                    "user-state schema v{current} is newer than supported \
                     v{VIS_SCHEMA_VERSION} — refusing to downgrade"
                );
                conn.execute_batch(VIS_SCHEMA)?;
                let stamped: i64 =
                    conn.query_row("PRAGMA user_version", [], |r| r.get(0))?;
                assert_eq!(
                    stamped, VIS_SCHEMA_VERSION,
                    "user-state schema file stamped v{stamped}; this build \
                     expects v{VIS_SCHEMA_VERSION} — bump VIS_SCHEMA_VERSION \
                     with the DDL"
                );
                drop(conn);
                let _ = std::fs::set_permissions(
                    &self.path,
                    std::os::unix::fs::PermissionsExt::from_mode(0o600),
                );
                *done = true;
            }
        }
        let conn = rusqlite::Connection::open(&self.path)?;
        conn.pragma_update(None, "busy_timeout", BUSY_TIMEOUT_MS)?;
        Ok(conn)
    }

    /// `read` — one transaction: (revision, roots ORDER BY root).
    pub fn read(&self, user_key: &str) -> rusqlite::Result<Snapshot> {
        let mut conn = self.connect()?;
        let tx = conn.transaction()?;
        let revision: i64 = tx
            .query_row(
                "SELECT revision FROM visibility_users WHERE user_key = ?1",
                [user_key],
                |r| r.get(0),
            )
            .unwrap_or(0);
        let mut stmt = tx.prepare("SELECT root FROM hidden_roots WHERE user_key = ?1 ORDER BY root")?;
        let roots = stmt
            .query_map([user_key], |r| r.get::<_, String>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        drop(stmt);
        tx.commit()?;
        Ok(Snapshot {
            revision,
            hidden_roots: roots,
        })
    }

    /// `set_hidden` — bump revision only when the hidden set actually changes.
    pub fn set_hidden(&self, user_key: &str, root: &str, hidden: bool) -> rusqlite::Result<Snapshot> {
        let mut conn = self.connect()?;
        let tx = conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let existing: bool = tx
            .query_row(
                "SELECT 1 FROM hidden_roots WHERE user_key = ?1 AND root = ?2",
                rusqlite::params![user_key, root],
                |_| Ok(()),
            )
            .optional()?
            .is_some();
        if existing != hidden {
            if hidden {
                tx.execute(
                    "INSERT INTO hidden_roots (user_key, root) VALUES (?1, ?2)",
                    rusqlite::params![user_key, root],
                )?;
            } else {
                tx.execute(
                    "DELETE FROM hidden_roots WHERE user_key = ?1 AND root = ?2",
                    rusqlite::params![user_key, root],
                )?;
            }
            tx.execute(
                "INSERT INTO visibility_users (user_key, revision) VALUES (?1, 0) \
                 ON CONFLICT(user_key) DO NOTHING",
                [user_key],
            )?;
            tx.execute(
                "UPDATE visibility_users SET revision = revision + 1 WHERE user_key = ?1",
                [user_key],
            )?;
        }
        let revision: i64 = tx
            .query_row(
                "SELECT revision FROM visibility_users WHERE user_key = ?1",
                [user_key],
                |r| r.get(0),
            )
            .unwrap_or(0);
        let mut stmt = tx.prepare("SELECT root FROM hidden_roots WHERE user_key = ?1 ORDER BY root")?;
        let roots = stmt
            .query_map([user_key], |r| r.get::<_, String>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        drop(stmt);
        tx.commit()?;
        Ok(Snapshot {
            revision,
            hidden_roots: roots,
        })
    }
}

/// `visibility_owner` — purpose-scoped HMAC-SHA256 hex of the user key.
pub fn visibility_owner(user_key: &str, secret: &str) -> String {
    let mut mac =
        <Hmac<Sha256> as Mac>::new_from_slice(secret.as_bytes()).expect("hmac takes any key");
    mac.update(format!("folder-visibility:v1:{user_key}").as_bytes());
    mac.finalize().into_bytes().iter().map(|b| format!("{b:02x}")).collect()
}

/// `validate_root` — normalized top-level path, no empty/../dot segments.
fn validate_root(root: &str) -> bool {
    if root.is_empty() || root.len() > MAX_ROOT_LENGTH || root.contains('\\') || root.contains('\0')
    {
        return false;
    }
    root.split('/')
        .all(|seg| !seg.is_empty() && seg != "." && seg != ".." && !seg.starts_with('.'))
}

const PRIVATE_HEADERS: [(&str, &str); 2] = [
    ("cache-control", "private, no-store"),
    ("vary", "Cookie"),
];

fn vis_response(owner: &str, snap: &Snapshot) -> Response {
    let body = serde_json::json!({
        "owner": owner,
        "revision": snap.revision,
        "hiddenRoots": snap.hidden_roots,
    });
    let mut res = json_response(StatusCode::OK, &body);
    for (k, v) in PRIVATE_HEADERS {
        res.headers_mut().insert(k, HeaderValue::from_static(v));
    }
    res
}

fn vis_detail(status: StatusCode, msg: &str) -> Response {
    let mut res = detail(status, msg);
    for (k, v) in PRIVATE_HEADERS {
        res.headers_mut().insert(k, HeaderValue::from_static(v));
    }
    res
}

/// `covered_by_roots` — `path == K` or `path` under `K + "/"`.
/// Consumed by M6's search/facets hidden-root filtering.
#[allow(dead_code)]
pub fn covered_by_roots(path: &str, roots: &[String]) -> bool {
    if path.is_empty() {
        return false;
    }
    roots
        .iter()
        .any(|k| !k.is_empty() && (path == k || path.starts_with(&format!("{k}/"))))
}

// ── endpoint handlers ────────────────────────────────────────────────────────

use crate::folders::ensure_tree_and_index_wait;
use crate::AppState;

fn owner_token(st: &AppState, user_key: &str) -> String {
    visibility_owner(user_key, &st.secret_key)
}

/// GET /api/v1/folder-visibility
pub async fn get_folder_visibility(st: &AppState, user_key: &str) -> Response {
    let store = st.visibility.clone();
    let key = user_key.to_string();
    let snap = tokio::task::spawn_blocking(move || store.read(&key))
        .await
        .ok()
        .and_then(|r| r.ok());
    match snap {
        Some(s) => vis_response(&owner_token(st, user_key), &s),
        None => vis_detail(StatusCode::SERVICE_UNAVAILABLE, "visibility store unavailable"),
    }
}

/// PUT /api/v1/folder-visibility — owner echo + body validation + tree
/// membership + revision-bump semantics of the v0 handler.
pub async fn put_folder_visibility(
    st: &AppState,
    user_key: &str,
    headers: &HeaderMap,
    body: &[u8],
) -> Response {
    let owner = owner_token(st, user_key);
    let header_owner = headers.get("x-bookq-visibility-owner");
    let Some(header_owner) = header_owner else {
        return vis_detail(StatusCode::FORBIDDEN, "visibility owner header required");
    };
    // Latin-1 header bytes vs ASCII owner — constant-time compare.
    let hb = header_owner.as_bytes();
    let ob = owner.as_bytes();
    let equal = hb.len() == ob.len()
        && hb.iter().zip(ob).fold(0u8, |a, (x, y)| a | (x ^ y)) == 0;
    if !equal {
        return vis_detail(StatusCode::CONFLICT, "visibility owner mismatch");
    }
    let ctype = headers
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .split(';')
        .next()
        .unwrap_or("")
        .trim()
        .to_lowercase();
    if ctype != "application/json" {
        return vis_detail(StatusCode::UNSUPPORTED_MEDIA_TYPE, "expected application/json");
    }
    let Ok(body) = serde_json::from_slice::<Value>(body) else {
        return vis_detail(StatusCode::BAD_REQUEST, "invalid JSON body");
    };
    if !body.is_object() {
        return vis_detail(StatusCode::BAD_REQUEST, "expected JSON object");
    }
    let obj = body.as_object().unwrap();
    if obj.len() != 2 || !obj.contains_key("root") || !obj.contains_key("hidden") {
        return vis_detail(StatusCode::BAD_REQUEST, "expected exactly 'root' and 'hidden'");
    }
    let Some(hidden) = body["hidden"].as_bool() else {
        return vis_detail(StatusCode::BAD_REQUEST, "hidden must be a boolean");
    };
    let Some(root) = body["root"].as_str().filter(|r| validate_root(r)) else {
        return vis_detail(StatusCode::BAD_REQUEST, "invalid root");
    };

    let store = st.visibility.clone();
    if hidden {
        // Read first; only a root not already hidden needs tree membership.
        let snap = tokio::task::spawn_blocking({
            let store = store.clone();
            let key = user_key.to_string();
            move || store.read(&key)
        })
        .await
        .ok()
        .and_then(|r| r.ok());
        let Some(snap) = snap else {
            return vis_detail(
                StatusCode::SERVICE_UNAVAILABLE,
                "visibility store unavailable",
            );
        };
        if !snap.hidden_roots.iter().any(|r| r == root) {
            // wait=True: no 202 available for a PUT — ride the build.
            let data = ensure_tree_and_index_wait(&st.index, &st.downloads).await;
            let Some(data) = data else {
                return vis_detail(StatusCode::SERVICE_UNAVAILABLE, "library tree unavailable");
            };
            let member = data.tree.iter().any(|n| {
                n["type"] == "folder" && n["name"].as_str() == Some(root)
            });
            if !member {
                return vis_detail(StatusCode::NOT_FOUND, "no such top-level folder or archive");
            }
        }
    }
    let root2 = root.to_string();
    let key = user_key.to_string();
    let snap = tokio::task::spawn_blocking(move || store.set_hidden(&key, &root2, hidden))
        .await
        .ok()
        .and_then(|r| r.ok());
    match snap {
        Some(s) => vis_response(&owner, &s),
        None => vis_detail(StatusCode::SERVICE_UNAVAILABLE, "visibility store unavailable"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn vis_schema_version_matches_file_stamp() {
        let needle = "PRAGMA user_version = ";
        let pos = VIS_SCHEMA.find(needle).expect("schema file must stamp user_version");
        let digits: String = VIS_SCHEMA[pos + needle.len()..]
            .chars()
            .take_while(|c| c.is_ascii_digit())
            .collect();
        let stamped: i64 = digits.parse().expect("user_version stamp must be an integer");
        assert_eq!(stamped, VIS_SCHEMA_VERSION);
    }

    #[test]
    fn store_init_stamps_schema_version() {
        let dir = std::env::temp_dir().join(format!(
            "bookq-vis-test-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let store = VisibilityStore::new(&dir);
        let snap = store.read("alice@example.com").unwrap();
        assert_eq!(snap.revision, 0);
        let conn = rusqlite::Connection::open(dir.join("folder-visibility.sqlite3")).unwrap();
        let stamped: i64 = conn
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(stamped, VIS_SCHEMA_VERSION);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
