//! Admin registry (#608) — the library-global set of privileged users.
//!
//! Source of truth: the `admin_users` table in the shared user-state SQLite
//! DB (`state::user_state_path`). Rows are written **only** by the offline
//! `viewer-rs admin` CLI (host/container operator); there is no HTTP write
//! path. Granting admin therefore sits on the same trust boundary as reading
//! `SECRET_KEY` / the OAuth client secret.
//!
//! Authorization is an AND on top of normal auth: a caller must have a valid
//! session/Bearer, pass `ALLOWED_EMAILS`, and have a row here. Every lookup is
//! a per-request PK SELECT, so a removed row is rejected on the next request
//! even while an old signed cookie is still valid. Any store error is treated
//! as "not admin" by the callers (fail closed).
//!
//! The CLI lives in `admin_cli.rs`; this module stays free of argv/stdout.

use std::fmt;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use rusqlite::OptionalExtension;

use crate::state;

/// RFC 5321 maximum mailbox length.
pub(crate) const MAX_EMAIL_LEN: usize = 254;
/// Bound for `created_by` / `note` — keeps `admin list` TSV rows readable.
pub(crate) const MAX_TEXT_LEN: usize = 200;

#[derive(Clone, Debug, PartialEq)]
pub(crate) struct AdminRow {
    pub(crate) email: String,
    pub(crate) created_at: f64,
    pub(crate) created_by: String,
    pub(crate) note: String,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Change {
    Added,
    AlreadyPresent,
    Removed,
    NotFound,
}

#[derive(Debug)]
pub(crate) enum AdminError {
    InvalidEmail,
    InvalidText(&'static str),
    Sqlite(rusqlite::Error),
}

impl fmt::Display for AdminError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidEmail => write!(f, "invalid email"),
            Self::InvalidText(field) => write!(f, "invalid {field}"),
            Self::Sqlite(e) => write!(f, "user-state store error: {e}"),
        }
    }
}

impl std::error::Error for AdminError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Sqlite(e) => Some(e),
            _ => None,
        }
    }
}

impl From<rusqlite::Error> for AdminError {
    fn from(e: rusqlite::Error) -> Self {
        Self::Sqlite(e)
    }
}

/// Normalize an identity email: trim, reject anything that cannot be a
/// mailbox, lowercase (the same normalization used for session/Bearer
/// identities and `visibility_owner`). Deliberately not RFC 5322 — OAuth +
/// `ALLOWED_EMAILS` are the real gate; this only catches typos and keeps the
/// DB key canonical.
pub(crate) fn normalize_email(raw: &str) -> Option<String> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return None;
    }
    if trimmed.chars().any(|c| c.is_whitespace() || c.is_control()) {
        return None;
    }
    let mut parts = trimmed.split('@');
    let local = parts.next().unwrap_or("");
    let domain = parts.next().unwrap_or("");
    if local.is_empty() || domain.is_empty() || parts.next().is_some() {
        return None;
    }
    // Lowercasing can expand a character (e.g. U+0130 → "i" + U+0307), so the
    // 254-byte limit is checked on the value that is actually stored.
    let normalized = trimmed.to_lowercase();
    (normalized.len() <= MAX_EMAIL_LEN).then_some(normalized)
}

/// Normalize free-form metadata (`created_by` / `note`). Empty values fall
/// back to `default`; control characters are rejected so `admin list` stays
/// one TSV row per grant.
fn normalize_text(raw: &str, default: &str) -> Option<String> {
    let trimmed = raw.trim();
    let value = if trimmed.is_empty() { default } else { trimmed };
    if value.len() > MAX_TEXT_LEN {
        return None;
    }
    if value.chars().any(|c| c.is_control()) {
        return None;
    }
    Some(value.to_string())
}

fn now_secs() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

pub(crate) struct AdminStore {
    path: PathBuf,
    init: Mutex<bool>,
}

impl AdminStore {
    pub(crate) fn new(state_dir: &Path) -> Self {
        Self {
            path: state::user_state_path(state_dir),
            init: Mutex::new(false),
        }
    }

    fn connect(&self) -> Result<rusqlite::Connection, AdminError> {
        Ok(state::connect_user_state(&self.path, &self.init)?)
    }

    /// True only for a normalized email that has a row. An unusable email
    /// (empty / malformed) is simply not an admin, not an error.
    pub(crate) fn is_admin(&self, email: &str) -> Result<bool, AdminError> {
        let Some(email) = normalize_email(email) else {
            return Ok(false);
        };
        let conn = self.connect()?;
        let found = conn
            .query_row(
                "SELECT 1 FROM admin_users WHERE email = ?1",
                [&email],
                |_| Ok(()),
            )
            .optional()?;
        Ok(found.is_some())
    }

    pub(crate) fn list(&self) -> Result<Vec<AdminRow>, AdminError> {
        let conn = self.connect()?;
        let mut stmt = conn.prepare(
            "SELECT email, created_at, created_by, note \
             FROM admin_users ORDER BY email",
        )?;
        let rows = stmt
            .query_map([], |r| {
                Ok(AdminRow {
                    email: r.get(0)?,
                    created_at: r.get(1)?,
                    created_by: r.get(2)?,
                    note: r.get(3)?,
                })
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(rows)
    }

    /// Idempotent grant. An existing row keeps its original created_at /
    /// created_by / note (no silent metadata rewrite) and reports
    /// `AlreadyPresent`.
    pub(crate) fn add(
        &self,
        email: &str,
        created_by: &str,
        note: &str,
    ) -> Result<Change, AdminError> {
        let email = normalize_email(email).ok_or(AdminError::InvalidEmail)?;
        let created_by =
            normalize_text(created_by, "cli").ok_or(AdminError::InvalidText("created_by"))?;
        let note = normalize_text(note, "").ok_or(AdminError::InvalidText("note"))?;
        let conn = self.connect()?;
        let changed = conn.execute(
            "INSERT INTO admin_users (email, created_at, created_by, note) \
             VALUES (?1, ?2, ?3, ?4) \
             ON CONFLICT(email) DO NOTHING",
            rusqlite::params![email, now_secs(), created_by, note],
        )?;
        Ok(if changed == 0 {
            Change::AlreadyPresent
        } else {
            Change::Added
        })
    }

    pub(crate) fn remove(&self, email: &str) -> Result<Change, AdminError> {
        let email = normalize_email(email).ok_or(AdminError::InvalidEmail)?;
        let conn = self.connect()?;
        let changed = conn.execute("DELETE FROM admin_users WHERE email = ?1", [&email])?;
        Ok(if changed == 0 {
            Change::NotFound
        } else {
            Change::Removed
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_dir(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "bookq-admin-{tag}-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn normalize_email_matrix() {
        assert_eq!(
            normalize_email(" Alice@Example.COM "),
            Some("alice@example.com".to_string())
        );
        assert_eq!(normalize_email("a@b"), Some("a@b".to_string()));
        let long = format!("{}@example.com", "x".repeat(MAX_EMAIL_LEN));
        let cases = [
            "",
            "   ",
            "alice",
            "@example.com",
            "alice@",
            "a@b@c",
            "a b@example.com",
            "a\tb@example.com",
            "a\u{0}b@example.com",
            long.as_str(),
        ];
        for raw in cases {
            assert_eq!(normalize_email(raw), None, "{raw:?}");
        }

        // Lowercasing can grow the byte length: U+0130 is 2 bytes, but its
        // lowercase form is "i" + U+0307 (3 bytes). Reject on the stored form.
        let raw = format!("{}@a.com", "İ".repeat(85));
        assert!(raw.len() < MAX_EMAIL_LEN);
        assert_eq!(normalize_email(&raw), None);
    }

    #[test]
    fn normalize_text_rejects_multiline_and_long_values() {
        assert_eq!(normalize_text("", "cli"), Some("cli".to_string()));
        assert_eq!(
            normalize_text("  ticket-123 ", ""),
            Some("ticket-123".to_string())
        );
        assert_eq!(normalize_text("line\nbreak", ""), None);
        assert_eq!(normalize_text("tab\there", ""), None);
        let long = "x".repeat(MAX_TEXT_LEN + 1);
        assert_eq!(normalize_text(&long, ""), None);
    }

    #[test]
    fn store_crud_and_persistence() {
        let dir = temp_dir("crud");
        let store = AdminStore::new(&dir);
        assert!(!store.is_admin("alice@example.com").unwrap());
        assert!(store.list().unwrap().is_empty());

        assert_eq!(
            store.add(" Alice@Example.COM ", "cli", "initial").unwrap(),
            Change::Added
        );
        assert!(store.is_admin("alice@example.com").unwrap());
        // COLLATE NOCASE + normalization: any casing resolves.
        assert!(store.is_admin("ALICE@EXAMPLE.COM").unwrap());
        assert_eq!(
            store.add("alice@example.com", "cli", "dup").unwrap(),
            Change::AlreadyPresent
        );

        let rows = store.list().unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].email, "alice@example.com");
        assert_eq!(rows[0].created_by, "cli");
        assert_eq!(rows[0].note, "initial");
        assert!(rows[0].created_at > 0.0);

        // "Restart": a fresh store on the same file sees the row.
        let store2 = AdminStore::new(&dir);
        assert!(store2.is_admin("alice@example.com").unwrap());
        assert_eq!(store2.remove("ALICE@EXAMPLE.COM").unwrap(), Change::Removed);
        assert!(!store2.is_admin("alice@example.com").unwrap());
        assert_eq!(
            store2.remove("alice@example.com").unwrap(),
            Change::NotFound
        );
        assert!(store2.list().unwrap().is_empty());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn invalid_input_does_not_write() {
        let dir = temp_dir("invalid");
        let store = AdminStore::new(&dir);
        assert!(matches!(
            store.add("not-an-email", "cli", ""),
            Err(AdminError::InvalidEmail)
        ));
        assert!(matches!(
            store.add("a@b.com", "cli", "line\nbreak"),
            Err(AdminError::InvalidText("note"))
        ));
        assert!(matches!(
            store.add("a@b.com", "line\nbreak", ""),
            Err(AdminError::InvalidText("created_by"))
        ));
        assert!(matches!(
            store.remove("not-an-email"),
            Err(AdminError::InvalidEmail)
        ));
        assert!(store.list().unwrap().is_empty());
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// Any store failure must surface as an error (the handler turns it into
    /// "not admin"). Opening a directory as the DB file is a deterministic
    /// failure without touching permissions.
    #[test]
    fn store_failure_is_err_not_admin() {
        let dir = temp_dir("openfail");
        std::fs::create_dir(dir.join("folder-visibility.sqlite3")).unwrap();
        let store = AdminStore::new(&dir);
        assert!(store.is_admin("a@b.com").is_err());
        assert!(store.list().is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// v2 → v3 is additive: existing visibility rows survive and the admin
    /// table appears. Mirrors the real upgrade (the server applies the DDL on
    /// first connect).
    #[test]
    fn schema_v2_migrates_without_data_loss() {
        let dir = temp_dir("migrate");
        let path = state::user_state_path(&dir);
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            conn.execute_batch(
                "CREATE TABLE visibility_users ( \
                     user_key TEXT PRIMARY KEY, \
                     revision INTEGER NOT NULL DEFAULT 0 \
                 ); \
                 CREATE TABLE hidden_roots ( \
                     user_key TEXT NOT NULL, \
                     root TEXT NOT NULL, \
                     PRIMARY KEY (user_key, root) \
                 ); \
                 INSERT INTO visibility_users (user_key, revision) VALUES ('*', 4); \
                 INSERT INTO hidden_roots (user_key, root) VALUES ('*', 'secret'); \
                 PRAGMA user_version = 2;",
            )
            .unwrap();
        }

        let store = AdminStore::new(&dir);
        assert_eq!(
            store.add("alice@example.com", "cli", "").unwrap(),
            Change::Added
        );

        let conn = rusqlite::Connection::open(&path).unwrap();
        let version: i64 = conn
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(version, state::USER_STATE_SCHEMA_VERSION);
        let revision: i64 = conn
            .query_row(
                "SELECT revision FROM visibility_users WHERE user_key = '*'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(revision, 4);
        let root: String = conn
            .query_row(
                "SELECT root FROM hidden_roots WHERE user_key = '*'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(root, "secret");
        let tables: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master \
                 WHERE type = 'table' AND name = 'admin_users'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(tables, 1);
        drop(conn);
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// The existing "a newer DB refuses to run" guard covers the admin store
    /// too — a downgraded binary must not silently interpret a future schema.
    #[test]
    fn schema_too_new_refuses() {
        let dir = temp_dir("toonew");
        let path = state::user_state_path(&dir);
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            conn.pragma_update(None, "user_version", 99).unwrap();
        }
        let store = AdminStore::new(&dir);
        let result =
            std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| store.is_admin("a@b.com")));
        assert!(result.is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }
}
