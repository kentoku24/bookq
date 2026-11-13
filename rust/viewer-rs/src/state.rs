//! `/api/v1/reading-positions` + `/api/v1/folder-visibility` — M4 of #163.
//!
//! Ports `app/api_v1.py`'s state endpoints plus the stores behind them:
//!
//! * reading positions persist in the shared `user-state` SQLite volume —
//!   the same DB the Python viewer writes (#375), so a position PUT that
//!   lands on either backend is visible to the other and survives restarts.
//!   Slot rules are ported verbatim from `app/reading_positions.py`
//!   (`PositionBook`) and pinned by `api/vectors/positions-cases.json`,
//!   which the Python tests consume too.
//! * folder visibility persists in the same shared DB — the hidden set is
//!   library-global (every row lives under `GLOBAL_USER_KEY`, matching
//!   `app/folder_visibility.py`). Schema/transaction semantics are ported
//!   verbatim: WAL, busy_timeout 5s, revision bumps only on an actual
//!   change, fresh
//!   connection per operation.
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

pub fn detail(status: StatusCode, msg: &str) -> Response {
    json_response(status, &serde_json::json!({"detail": msg}))
}

/// FastAPI's 422 issue-array shape — the spec's `ValidationError` schema
/// declares `detail` as an array of issue objects, not a string (#465).
pub fn unprocessable(param: &str, ty: &str, msg: &str) -> Response {
    json_response(
        StatusCode::UNPROCESSABLE_ENTITY,
        &serde_json::json!({
            "detail": [{"loc": ["query", param], "msg": msg, "type": ty}]
        }),
    )
}

// ── reading positions ───────────────────────────────────────────────────────
//
// `PositionBook` rules are a verbatim port of `app/reading_positions.py`:
// per-user slot `{folders, tombstone_ts, updated_at}` where a live rec is
// `{folder,page,total,ts,updated_at}` and a folder delete is a tombstone
// `{tombstone,ts,updated_at}`. `seq` reproduces Python dict insertion
// order — rowid order on load, first-insert seq kept on overwrite — so
// ts ties sort identically on both backends.

const STORED: &str = "stored";
const STALE: &str = "stale";
const REJECTED: &str = "rejected";

#[derive(Clone, Debug)]
pub(crate) struct Rec {
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

impl Slot {
    // ── PositionBook reads ──

    /// `_slot_max_ts` — newest ts over every record, tombstones included.
    fn max_ts(&self) -> i64 {
        self.folders
            .values()
            .map(|r| r.ts)
            .max()
            .unwrap_or(0)
            .max(self.tombstone_ts)
    }

    /// `_live_max_ts` — newest ts over non-tombstone records only.
    /// (Reached via `put_v0`; kept for full PositionBook parity.)
    #[allow(dead_code)]
    fn live_max_ts(&self) -> i64 {
        self.folders
            .values()
            .filter(|r| !r.tombstone)
            .map(|r| r.ts)
            .max()
            .unwrap_or(0)
    }

    /// `_latest_position` — newest non-tombstone record; on a ts tie the
    /// FIRST in insertion order wins (Python's `>` keeps the incumbent).
    /// Only the conformance vectors exercise it — v0 routes proxy upstream.
    #[allow(dead_code)]
    fn latest(&self) -> Option<&Rec> {
        self.folders
            .values()
            .filter(|r| !r.tombstone)
            .min_by(|a, b| b.ts.cmp(&a.ts).then(a.seq.cmp(&b.seq)))
    }

    /// `list_positions` — non-tombstone records, ts desc with insertion
    /// order for ties (the stable-sort contract).
    fn list_positions(&self, folder: Option<&str>) -> Vec<Rec> {
        let mut recs: Vec<Rec> = match folder {
            Some(f) => self
                .folders
                .get(f)
                .filter(|r| !r.tombstone)
                .cloned()
                .into_iter()
                .collect(),
            None => self
                .folders
                .values()
                .filter(|r| !r.tombstone)
                .cloned()
                .collect(),
        };
        recs.sort_by(|a, b| b.ts.cmp(&a.ts).then(a.seq.cmp(&b.seq)));
        recs
    }

    // ── PositionBook writes ──

    fn future_ts(&self, ts: i64, now: f64) -> bool {
        ts > (now * 1000.0) as i64 + POSITION_TS_FUTURE_SLACK_MS
    }

    /// `put_v0` — accept only a global newest write (v0 single-slot view).
    /// Only the conformance vectors exercise it — v0 routes proxy upstream.
    #[allow(dead_code)]
    fn put_v0(&mut self, folder: &str, page: i64, total: i64, ts: i64, now: f64) -> &'static str {
        if self.future_ts(ts, now) {
            return REJECTED;
        }
        if ts <= self.tombstone_ts {
            return STALE;
        }
        if let Some(ex) = self.folders.get(folder) {
            if ex.tombstone && ts <= ex.ts {
                return STALE;
            }
        }
        if ts < self.live_max_ts() {
            return STALE;
        }
        self.write(folder, page, total, ts, now);
        STORED
    }

    /// `put_v1` — per-folder last-writer-wins.
    fn put_v1(&mut self, folder: &str, page: i64, total: i64, ts: i64, now: f64) -> &'static str {
        if self.future_ts(ts, now) {
            return REJECTED;
        }
        if ts <= self.tombstone_ts {
            return STALE;
        }
        if let Some(ex) = self.folders.get(folder) {
            if ex.tombstone {
                if ts <= ex.ts {
                    return STALE;
                }
            } else if ts < ex.ts {
                return STALE;
            }
        }
        self.write(folder, page, total, ts, now);
        STORED
    }

    fn write(&mut self, folder: &str, page: i64, total: i64, ts: i64, now: f64) {
        let seq = match self.folders.get(folder) {
            Some(old) => old.seq,
            None => {
                let s = self.next_seq;
                self.next_seq += 1;
                s
            }
        };
        self.folders.insert(
            folder.to_string(),
            Rec {
                folder: folder.to_string(),
                page,
                total,
                ts,
                tombstone: false,
                updated_at: now,
                seq,
            },
        );
        self.updated_at = now;
        self.trim();
    }

    /// `delete_folder` — per-folder tombstone carrying the wiped ts.
    fn delete_folder(&mut self, folder: &str, now: f64) {
        self.updated_at = now;
        let prev_ts = self.folders.get(folder).map(|r| r.ts).unwrap_or(0);
        // A tombstone on an existing folder keeps its insertion position
        // (dict overwrite), a new one appends — same as Python.
        let seq = match self.folders.get(folder) {
            Some(old) => old.seq,
            None => {
                let s = self.next_seq;
                self.next_seq += 1;
                s
            }
        };
        self.folders.insert(
            folder.to_string(),
            Rec {
                folder: folder.to_string(),
                page: 0,
                total: 0,
                ts: prev_ts,
                tombstone: true,
                updated_at: now,
                seq,
            },
        );
    }

    /// `delete_all` — wipe every record, keep the newest wiped ts as the
    /// slot tombstone so stale writes can't resurrect them.
    fn delete_all(&mut self, now: f64) {
        self.tombstone_ts = self.max_ts();
        self.folders.clear();
        self.updated_at = now;
        self.next_seq = 0;
    }

    /// `_trim` — bound the folder map; evict the least-recently-written
    /// entry (first minimum in insertion order on updated_at ties).
    fn trim(&mut self) {
        while self.folders.len() > POSITION_MAX_FOLDERS {
            let stalest = self
                .folders
                .values()
                .min_by(|a, b| {
                    a.updated_at
                        .partial_cmp(&b.updated_at)
                        .unwrap_or(std::cmp::Ordering::Equal)
                        .then(a.seq.cmp(&b.seq))
                })
                .map(|r| r.folder.clone());
            match stalest {
                Some(f) => {
                    self.folders.remove(&f);
                }
                None => break,
            }
        }
    }
}

/// `ReadingPositionStore` — SQLite persistence on the shared user-state
/// DB. Every op is one IMMEDIATE transaction: load slot → apply book →
/// rewrite the user's rows in insertion (seq) order so rowid reproduces
/// dict order for the other backend's reads.
pub struct PositionStore {
    path: PathBuf,
    init: Mutex<bool>,
}

impl PositionStore {
    pub fn new(state_dir: &Path) -> Self {
        Self {
            path: state_dir.join("folder-visibility.sqlite3"),
            init: Mutex::new(false),
        }
    }

    fn connect(&self) -> rusqlite::Result<rusqlite::Connection> {
        connect_user_state(&self.path, &self.init)
    }

    /// Rebuild `{folders, tombstone_ts, updated_at}`; folder rows come back
    /// in rowid order → seq 0..n, matching Python dict insertion order.
    fn load_slot(
        conn: &rusqlite::Connection,
        user_key: &str,
    ) -> rusqlite::Result<Option<Slot>> {
        let user_row = conn
            .query_row(
                "SELECT tombstone_ts, updated_at FROM position_users WHERE user_key = ?1",
                [user_key],
                |r| Ok((r.get::<_, i64>(0)?, r.get::<_, f64>(1)?)),
            )
            .optional()?;
        let mut stmt = conn.prepare(
            "SELECT folder, page, total, ts, tombstone, updated_at \
             FROM position_folders WHERE user_key = ?1 ORDER BY rowid",
        )?;
        let rows = stmt
            .query_map([user_key], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, Option<i64>>(1)?,
                    r.get::<_, Option<i64>>(2)?,
                    r.get::<_, i64>(3)?,
                    r.get::<_, i64>(4)?,
                    r.get::<_, f64>(5)?,
                ))
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        drop(stmt);
        if user_row.is_none() && rows.is_empty() {
            return Ok(None);
        }
        let n = rows.len() as u64;
        let mut slot = Slot {
            next_seq: n,
            ..Slot::default()
        };
        for (seq, (folder, page, total, ts, tombstone, updated_at)) in
            rows.into_iter().enumerate()
        {
            slot.folders.insert(
                folder.clone(),
                Rec {
                    folder,
                    page: page.unwrap_or(0),
                    total: total.unwrap_or(0),
                    ts,
                    tombstone: tombstone != 0,
                    updated_at,
                    seq: seq as u64,
                },
            );
        }
        if let Some((tomb_ts, upd)) = user_row {
            slot.tombstone_ts = tomb_ts;
            slot.updated_at = upd;
        }
        Ok(Some(slot))
    }

    fn save_slot(
        conn: &rusqlite::Connection,
        user_key: &str,
        slot: &Slot,
    ) -> rusqlite::Result<()> {
        conn.execute(
            "INSERT INTO position_users (user_key, tombstone_ts, updated_at) \
             VALUES (?1, ?2, ?3) \
             ON CONFLICT(user_key) DO UPDATE SET \
             tombstone_ts = excluded.tombstone_ts, \
             updated_at = excluded.updated_at",
            rusqlite::params![user_key, slot.tombstone_ts, slot.updated_at],
        )?;
        conn.execute(
            "DELETE FROM position_folders WHERE user_key = ?1",
            [user_key],
        )?;
        let mut recs: Vec<&Rec> = slot.folders.values().collect();
        recs.sort_by_key(|r| r.seq);
        for r in recs {
            if r.tombstone {
                conn.execute(
                    "INSERT INTO position_folders \
                     (user_key, folder, page, total, ts, tombstone, updated_at) \
                     VALUES (?1, ?2, NULL, NULL, ?3, 1, ?4)",
                    rusqlite::params![user_key, r.folder, r.ts, r.updated_at],
                )?;
            } else {
                conn.execute(
                    "INSERT INTO position_folders \
                     (user_key, folder, page, total, ts, tombstone, updated_at) \
                     VALUES (?1, ?2, ?3, ?4, ?5, 0, ?6)",
                    rusqlite::params![
                        user_key, r.folder, r.page, r.total, r.ts, r.updated_at
                    ],
                )?;
            }
        }
        Ok(())
    }

    /// MAX_USERS bound: drop the least-recently-updated user slot.
    fn evict_stalest_user(conn: &rusqlite::Connection) -> rusqlite::Result<()> {
        let key: Option<String> = conn
            .query_row(
                "SELECT user_key FROM position_users \
                 ORDER BY updated_at ASC, rowid ASC LIMIT 1",
                [],
                |r| r.get(0),
            )
            .optional()?;
        if let Some(key) = key {
            conn.execute(
                "DELETE FROM position_folders WHERE user_key = ?1",
                [&key],
            )?;
            conn.execute("DELETE FROM position_users WHERE user_key = ?1", [&key])?;
        }
        Ok(())
    }

    /// One transaction: load slot → apply book op → persist on STORED.
    /// MAX_USERS eviction runs only for a successful write by a user with
    /// no slot yet — a rejected PUT must not evict anyone.
    fn write<F>(&self, user_key: &str, op: F) -> rusqlite::Result<&'static str>
    where
        F: FnOnce(&mut Slot) -> &'static str,
    {
        let mut conn = self.connect()?;
        let tx = conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let existing = Self::load_slot(&tx, user_key)?;
        let is_new_user = existing.is_none();
        let mut slot = existing.unwrap_or_default();
        let result = op(&mut slot);
        if result == STORED {
            if is_new_user {
                let n: i64 = tx.query_row(
                    "SELECT COUNT(*) FROM position_users",
                    [],
                    |r| r.get(0),
                )?;
                if n as usize >= POSITION_MAX_USERS {
                    Self::evict_stalest_user(&tx)?;
                }
            }
            Self::save_slot(&tx, user_key, &slot)?;
        }
        tx.commit()?;
        Ok(result)
    }

    pub fn list_positions(
        &self,
        user_key: &str,
        folder: Option<&str>,
    ) -> rusqlite::Result<Vec<Rec>> {
        let conn = self.connect()?;
        let slot = Self::load_slot(&conn, user_key)?;
        Ok(slot.map(|s| s.list_positions(folder)).unwrap_or_default())
    }

    pub fn put_v1(
        &self,
        user_key: &str,
        folder: &str,
        page: i64,
        total: i64,
        ts: i64,
    ) -> rusqlite::Result<&'static str> {
        self.write(user_key, |slot| {
            slot.put_v1(folder, page, total, ts, now_secs())
        })
    }

    pub fn delete_folder(&self, user_key: &str, folder: &str) -> rusqlite::Result<()> {
        self.write(user_key, |slot| {
            slot.delete_folder(folder, now_secs());
            STORED
        })
        .map(|_| ())
    }

    pub fn delete_all(&self, user_key: &str) -> rusqlite::Result<()> {
        self.write(user_key, |slot| {
            slot.delete_all(now_secs());
            STORED
        })
        .map(|_| ())
    }
}

// ── endpoint handlers ────────────────────────────────────────────────────────

/// `_validate_folder_param` — non-empty, ≤2048 chars, no leading `/`, no NUL.
/// `chars().count()` counts code points like Python's `len(str)`.
fn valid_folder_param(folder: &str) -> bool {
    !folder.is_empty()
        && folder.chars().count() <= 2048
        && !folder.starts_with('/')
        && !folder.contains('\0')
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

fn store_error() -> Response {
    detail(
        StatusCode::SERVICE_UNAVAILABLE,
        "reading-position store unavailable",
    )
}

/// GET /api/v1/reading-positions — `{positions: [...]}` newest first.
pub async fn list_positions(
    store: std::sync::Arc<PositionStore>,
    user_key: &str,
    folder: Option<String>,
) -> Response {
    let key = user_key.to_string();
    let folder = folder.map(|f| f.to_string());
    let recs = tokio::task::spawn_blocking(move || store.list_positions(&key, folder.as_deref()))
        .await
        .ok()
        .and_then(|r| r.ok());
    let Some(recs) = recs else {
        return store_error();
    };
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

/// PUT /api/v1/reading-positions — per-folder LWW; body `{page,total,ts}`,
/// folder from the query param (FastAPI contract).
pub async fn put_position(
    store: std::sync::Arc<PositionStore>,
    user_key: &str,
    folder: &str,
    body_bytes: &[u8],
) -> Response {
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
    let key = user_key.to_string();
    let folder = folder.to_string();
    let result = tokio::task::spawn_blocking(move || {
        store.put_v1(&key, &folder, page, total, ts)
    })
    .await
    .ok()
    .and_then(|r| r.ok());
    match result {
        // REJECTED = far-future ts — the wrong-clock guard lives in the
        // book so py/rs cannot diverge on it.
        Some(REJECTED) => detail(StatusCode::BAD_REQUEST, "ts too far in the future"),
        Some(STALE) => json_response(StatusCode::OK, &serde_json::json!({"status": "stale"})),
        Some(STORED) => json_response(StatusCode::OK, &serde_json::json!({"status": "ok"})),
        _ => store_error(),
    }
}

/// DELETE /api/v1/reading-positions — one folder's tombstone, or the
/// delete-all tombstone when `folder` is omitted.
pub async fn delete_positions(
    store: std::sync::Arc<PositionStore>,
    user_key: &str,
    folder: Option<&str>,
) -> Response {
    if let Some(f) = folder {
        if !valid_folder_param(f) {
            return detail(StatusCode::BAD_REQUEST, "invalid folder");
        }
    }
    let key = user_key.to_string();
    let folder = folder.map(|f| f.to_string());
    let result = tokio::task::spawn_blocking(move || match folder {
        Some(f) => store.delete_folder(&key, &f),
        None => store.delete_all(&key),
    })
    .await
    .ok()
    .and_then(|r| r.ok());
    match result {
        Some(()) => json_response(StatusCode::OK, &serde_json::json!({"status": "ok"})),
        None => store_error(),
    }
}

// ── folder visibility (shared SQLite store) ──────────────────────────────────

// The DDL lives in one shared file (#200) — api/user-state-schema.sql is
// the single source FastAPI and viewer-rs both apply. USER_STATE_SCHEMA_VERSION
// must equal the file's `PRAGMA user_version` stamp; a DB stamped newer
// than it is refused rather than silently downgraded. v2 added the reading-
// position tables (#375) — the constant versions the whole user-state DB.
const VIS_SCHEMA: &str = include_str!("../../../api/user-state-schema.sql");
const USER_STATE_SCHEMA_VERSION: i64 = 2;

/// Shared user-state DB connect: WAL + busy_timeout, lazy idempotent
/// schema apply under `init`, fresh connection per call. Used by both
/// `VisibilityStore` and `PositionStore` on the same file.
fn connect_user_state(
    path: &Path,
    init: &Mutex<bool>,
) -> rusqlite::Result<rusqlite::Connection> {
    {
        let mut done = init.lock().unwrap();
        if !*done {
            if let Some(parent) = path.parent() {
                let _ = std::fs::create_dir_all(parent);
                let _ = std::fs::set_permissions(
                    parent,
                    std::os::unix::fs::PermissionsExt::from_mode(0o700),
                );
            }
            let conn = rusqlite::Connection::open(path)?;
            conn.pragma_update(None, "journal_mode", "WAL")?;
            conn.pragma_update(None, "busy_timeout", BUSY_TIMEOUT_MS)?;
            let current: i64 = conn.query_row("PRAGMA user_version", [], |r| r.get(0))?;
            assert!(
                current <= USER_STATE_SCHEMA_VERSION,
                "user-state schema v{current} is newer than supported \
                 v{USER_STATE_SCHEMA_VERSION} — refusing to downgrade"
            );
            conn.execute_batch(VIS_SCHEMA)?;
            let stamped: i64 =
                conn.query_row("PRAGMA user_version", [], |r| r.get(0))?;
            assert_eq!(
                stamped, USER_STATE_SCHEMA_VERSION,
                "user-state schema file stamped v{stamped}; this build \
                 expects v{USER_STATE_SCHEMA_VERSION} — bump \
                 USER_STATE_SCHEMA_VERSION with the DDL"
            );
            drop(conn);
            let _ = std::fs::set_permissions(
                path,
                std::os::unix::fs::PermissionsExt::from_mode(0o600),
            );
            *done = true;
        }
    }
    let conn = rusqlite::Connection::open(path)?;
    conn.pragma_update(None, "busy_timeout", BUSY_TIMEOUT_MS)?;
    Ok(conn)
}

/// Library-global hidden-set key — must equal `GLOBAL_USER_KEY` in
/// `app/folder_visibility.py`. `*` can never collide with an email-derived
/// user key; the per-user `user_key` param still feeds the owner HMAC only.
pub const GLOBAL_USER_KEY: &str = "*";

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
        connect_user_state(&self.path, &self.init)
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

/// GET /api/v1/folder-visibility — the library-global snapshot; `user_key`
/// feeds only the per-user owner token.
pub async fn get_folder_visibility(st: &AppState, user_key: &str) -> Response {
    let store = st.visibility.clone();
    let snap = tokio::task::spawn_blocking(move || store.read(GLOBAL_USER_KEY))
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
            move || store.read(GLOBAL_USER_KEY)
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
            let member = data
                .tree
                .iter()
                .any(|n| matches!(n, crate::tree::Node::Folder(f) if &*f.name == root));
            if !member {
                return vis_detail(StatusCode::NOT_FOUND, "no such top-level folder or archive");
            }
        }
    }
    let root2 = root.to_string();
    let snap =
        tokio::task::spawn_blocking(move || store.set_hidden(GLOBAL_USER_KEY, &root2, hidden))
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
        assert_eq!(stamped, USER_STATE_SCHEMA_VERSION);
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
        assert_eq!(stamped, USER_STATE_SCHEMA_VERSION);
        let _ = std::fs::remove_dir_all(&dir);
    }

    // ── PositionBook conformance (#375) ─────────────────────────────────
    // Same vectors the Python suite runs — py/rs cannot drift on slot
    // rules. `api/vectors/positions-cases.json` is the single source.

    const POSITION_CASES: &str =
        include_str!("../../../api/vectors/positions-cases.json");

    fn subset_match(expected: &Value, actual: &Rec) -> bool {
        let folder = expected.get("folder").and_then(|v| v.as_str());
        let page = expected.get("page").and_then(|v| v.as_i64());
        let total = expected.get("total").and_then(|v| v.as_i64());
        let ts = expected.get("ts").and_then(|v| v.as_i64());
        folder.is_none_or(|f| actual.folder == f)
            && page.is_none_or(|p| actual.page == p)
            && total.is_none_or(|t| actual.total == t)
            && ts.is_none_or(|t| actual.ts == t)
    }

    #[test]
    fn position_book_conformance_cases() {
        let doc: Value = serde_json::from_str(POSITION_CASES).unwrap();
        for case in doc["cases"].as_array().unwrap() {
            let name = case["name"].as_str().unwrap();
            let mut slot = Slot::default();
            for (i, op) in case["ops"].as_array().unwrap().iter().enumerate() {
                let now = op.get("now").and_then(|v| v.as_f64()).unwrap_or(0.0);
                let kind = op["op"].as_str().unwrap();
                let where_ = format!("{name} op[{i}] {kind}");
                match kind {
                    "put_v0" | "put_v1" => {
                        let rec = &op["rec"];
                        let folder = rec["folder"].as_str().unwrap();
                        let page = as_int(&rec["page"]).unwrap();
                        let total = as_int(&rec["total"]).unwrap();
                        let ts = as_int(&rec["ts"]).unwrap();
                        let result = if kind == "put_v0" {
                            slot.put_v0(folder, page, total, ts, now)
                        } else {
                            slot.put_v1(folder, page, total, ts, now)
                        };
                        assert_eq!(
                            result,
                            op["expect"].as_str().unwrap(),
                            "{where_}: expected {:?}, got {result:?}",
                            op["expect"]
                        );
                    }
                    "delete_folder" => {
                        slot.delete_folder(op["folder"].as_str().unwrap(), now);
                    }
                    "delete_all" => slot.delete_all(now),
                    "list" => {
                        let folder = op.get("folder").and_then(|v| v.as_str());
                        let recs = slot.list_positions(folder);
                        let expected = op["expect"].as_array().unwrap();
                        assert_eq!(
                            recs.len(),
                            expected.len(),
                            "{where_}: expected {} recs, got {}: {recs:?}",
                            expected.len(),
                            recs.len()
                        );
                        for (exp, act) in expected.iter().zip(recs.iter()) {
                            assert!(
                                subset_match(exp, act),
                                "{where_}: expected subset {exp}, got folder={} page={} total={} ts={}",
                                act.folder, act.page, act.total, act.ts
                            );
                        }
                    }
                    "latest" => {
                        let latest = slot.latest();
                        if op["expect"].is_null() {
                            assert!(latest.is_none(), "{where_}: expected null");
                        } else {
                            let exp = &op["expect"];
                            assert!(
                                latest.is_some_and(|r| subset_match(exp, r)),
                                "{where_}: expected subset {exp}"
                            );
                        }
                    }
                    "check" => {
                        let recs = slot.list_positions(None);
                        if let Some(len) = op.get("len").and_then(|v| v.as_u64()) {
                            assert_eq!(recs.len() as u64, len, "{where_}: len");
                        }
                        if let Some(first) = op.get("first") {
                            assert!(
                                recs.first().is_some_and(|r| subset_match(first, r)),
                                "{where_}: first mismatch"
                            );
                        }
                        for f in op
                            .get("absent")
                            .and_then(|v| v.as_array())
                            .into_iter()
                            .flatten()
                        {
                            let f = f.as_str().unwrap();
                            assert!(
                                recs.iter().all(|r| r.folder != f),
                                "{where_}: {f} should have been evicted"
                            );
                        }
                    }
                    other => panic!("{where_}: unknown op {other:?}"),
                }
            }
        }
    }

    #[test]
    fn position_store_roundtrip_and_tombstone() {
        let dir = std::env::temp_dir().join(format!(
            "bookq-pos-test-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let store = PositionStore::new(&dir);
        assert_eq!(
            store
                .put_v1("a@x.com", "vol1", 3, 10, 100)
                .unwrap(),
            STORED
        );
        store.put_v1("a@x.com", "vol2", 7, 10, 200).unwrap();
        store.delete_folder("a@x.com", "vol1").unwrap();
        // "Restart": a fresh store on the same file sees the same rows.
        let store2 = PositionStore::new(&dir);
        let recs = store2.list_positions("a@x.com", None).unwrap();
        assert_eq!(recs.len(), 1);
        assert_eq!(recs[0].folder, "vol2");
        // The persisted folder tombstone rejects the wiped ts.
        assert_eq!(
            store2.put_v1("a@x.com", "vol1", 1, 10, 100).unwrap(),
            STALE
        );
        assert_eq!(
            store2.put_v1("a@x.com", "vol1", 8, 10, 300).unwrap(),
            STORED
        );
        // Per-user isolation.
        store2.put_v1("b@x.com", "vol1", 9, 10, 400).unwrap();
        assert_eq!(
            store2.list_positions("b@x.com", None).unwrap()[0].page,
            9
        );
        let _ = std::fs::remove_dir_all(&dir);
    }
}
