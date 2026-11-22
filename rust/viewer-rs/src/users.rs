//! User directory (#608) — the authoritative membership + role store.
//!
//! Source of truth: the `users` table in the shared user-state SQLite DB
//! (`state::user_state_path`). Admins are granted only by the offline
//! `viewer-rs users` CLI (host/container operator). The `/admin` UI can add,
//! disable and re-enable **members** over HTTP, but never changes an admin
//! row; admin grants stay on the same trust boundary as reading
//! `SECRET_KEY` / the OAuth client secret.
//!
//! Membership is loaded into an in-memory [`UserDirectory`] so the sync
//! `auth::resolve` path does not do SQLite I/O on every image request. A
//! background task refreshes it every second; the `/admin` gate additionally
//! checks the DB per request so admin revocation is immediate.
//!
//! There is no env allowlist. The first user (normally an admin) is enrolled
//! with `viewer-rs users add <email> --role admin`; normal users use the same
//! command with the default `member` role. A fresh DB starts empty, so no one
//! can log in until the operator enrolls at least one user.

use std::collections::HashMap;
use std::fmt;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, RwLock};
use std::time::{SystemTime, UNIX_EPOCH};

use rusqlite::{params, OptionalExtension};

use crate::state;

/// RFC 5321 maximum mailbox length.
pub(crate) const MAX_EMAIL_LEN: usize = 254;
/// Bound for `created_by` / `note`. Matches the RFC 5321 mailbox limit so an
/// admin email can always be recorded as the actor; `users list` stays one
/// TSV row per user.
pub(crate) const MAX_TEXT_LEN: usize = 254;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Role {
    Admin,
    Member,
}

impl Role {
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Self::Admin => "admin",
            Self::Member => "member",
        }
    }

    pub(crate) fn parse(raw: &str) -> Option<Self> {
        match raw.trim().to_ascii_lowercase().as_str() {
            "admin" => Some(Self::Admin),
            "member" => Some(Self::Member),
            _ => None,
        }
    }
}

impl fmt::Display for Role {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

#[derive(Clone, Debug, PartialEq)]
pub(crate) struct UserRow {
    pub(crate) email: String,
    pub(crate) role: Role,
    pub(crate) disabled_at: Option<f64>,
    pub(crate) created_at: f64,
    pub(crate) created_by: String,
    pub(crate) note: String,
}

impl UserRow {
    pub(crate) fn active(&self) -> bool {
        self.disabled_at.is_none()
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Change {
    Added,
    Updated,
    AlreadyPresent,
    Activated,
    Disabled,
    AlreadyDisabled,
    NotFound,
    /// The `/admin` UI only manages members; admins (and corrupt roles) stay CLI-only.
    AdminProtected,
}

#[derive(Debug)]
pub(crate) enum UserError {
    InvalidEmail,
    InvalidText(&'static str),
    Sqlite(rusqlite::Error),
}

impl fmt::Display for UserError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidEmail => write!(f, "invalid email"),
            Self::InvalidText(field) => write!(f, "invalid {field}"),
            Self::Sqlite(e) => write!(f, "user-state store error: {e}"),
        }
    }
}

impl std::error::Error for UserError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Sqlite(e) => Some(e),
            _ => None,
        }
    }
}

impl From<rusqlite::Error> for UserError {
    fn from(e: rusqlite::Error) -> Self {
        Self::Sqlite(e)
    }
}

/// Normalize an identity email: trim, reject anything that cannot be a
/// mailbox, lowercase (the same normalization used for session/Bearer
/// identities and `visibility_owner`). Deliberately not RFC 5322 — OAuth is
/// the real gate; this only catches typos and keeps the DB key canonical.
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
/// back to `default`; control characters are rejected so `users list` stays
/// one TSV row per user.
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

/// Show what was actually stored/looked up, not the raw argv spelling.
pub(crate) fn display_email(email: &str) -> String {
    normalize_email(email).unwrap_or_else(|| email.to_string())
}

fn now_secs() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

fn parse_role(raw: &str) -> Option<Role> {
    Role::parse(raw)
}

pub(crate) struct UserStore {
    path: PathBuf,
    init: Mutex<bool>,
}

impl UserStore {
    pub(crate) fn new(state_dir: &Path) -> Self {
        Self {
            path: state::user_state_path(state_dir),
            init: Mutex::new(false),
        }
    }

    fn connect(&self) -> Result<rusqlite::Connection, UserError> {
        Ok(state::connect_user_state(&self.path, &self.init)?)
    }

    /// Active email → role. Invalid roles from manual SQL are skipped
    /// (fail closed) and logged.
    pub(crate) fn load_active(&self) -> Result<HashMap<String, Role>, UserError> {
        let conn = self.connect()?;
        let mut stmt =
            conn.prepare("SELECT email, role FROM users WHERE disabled_at IS NULL ORDER BY email")?;
        let rows = stmt
            .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        let mut out = HashMap::with_capacity(rows.len());
        for (email, role) in rows {
            match parse_role(&role) {
                Some(role) => {
                    out.insert(email, role);
                }
                None => tracing::warn!(email = %email, role = %role, "skipping unknown role"),
            }
        }
        Ok(out)
    }

    pub(crate) fn list(&self) -> Result<Vec<UserRow>, UserError> {
        let conn = self.connect()?;
        let mut stmt = conn.prepare(
            "SELECT email, role, disabled_at, created_at, created_by, note \
             FROM users ORDER BY email",
        )?;
        let rows = stmt
            .query_map([], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, Option<f64>>(2)?,
                    r.get::<_, f64>(3)?,
                    r.get::<_, String>(4)?,
                    r.get::<_, String>(5)?,
                ))
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        let mut out = Vec::with_capacity(rows.len());
        for (email, role, disabled_at, created_at, created_by, note) in rows {
            match parse_role(&role) {
                Some(role) => out.push(UserRow {
                    email,
                    role,
                    disabled_at,
                    created_at,
                    created_by,
                    note,
                }),
                None => tracing::warn!(email = %email, role = %role, "skipping unknown role"),
            }
        }
        Ok(out)
    }

    pub(crate) fn find(&self, email: &str) -> Result<Option<UserRow>, UserError> {
        let Some(email) = normalize_email(email) else {
            return Ok(None);
        };
        let conn = self.connect()?;
        let row = conn
            .query_row(
                "SELECT email, role, disabled_at, created_at, created_by, note \
                 FROM users WHERE email = ?1",
                [&email],
                |r| {
                    Ok((
                        r.get::<_, String>(0)?,
                        r.get::<_, String>(1)?,
                        r.get::<_, Option<f64>>(2)?,
                        r.get::<_, f64>(3)?,
                        r.get::<_, String>(4)?,
                        r.get::<_, String>(5)?,
                    ))
                },
            )
            .optional()?;
        let Some((email, role, disabled_at, created_at, created_by, note)) = row else {
            return Ok(None);
        };
        let Some(role) = parse_role(&role) else {
            tracing::warn!(email = %email, role = %role, "skipping unknown role");
            return Ok(None);
        };
        Ok(Some(UserRow {
            email,
            role,
            disabled_at,
            created_at,
            created_by,
            note,
        }))
    }

    pub(crate) fn is_active(&self, email: &str) -> Result<bool, UserError> {
        let Some(email) = normalize_email(email) else {
            return Ok(false);
        };
        let conn = self.connect()?;
        let found = conn
            .query_row(
                "SELECT 1 FROM users WHERE email = ?1 AND disabled_at IS NULL",
                [&email],
                |_| Ok(()),
            )
            .optional()?;
        Ok(found.is_some())
    }

    pub(crate) fn is_admin(&self, email: &str) -> Result<bool, UserError> {
        let Some(email) = normalize_email(email) else {
            return Ok(false);
        };
        let conn = self.connect()?;
        let found = conn
            .query_row(
                "SELECT 1 FROM users \
                 WHERE email = ?1 AND disabled_at IS NULL AND role = 'admin'",
                [&email],
                |_| Ok(()),
            )
            .optional()?;
        Ok(found.is_some())
    }

    /// Add or re-enable a `member` from the `/admin` UI.
    ///
    /// Unlike [`Self::add`], this never changes an existing row's role and
    /// refuses any row whose role is not `member` (including disabled
    /// admins) with [`Change::AdminProtected`]. The check and write share one
    /// immediate transaction so a concurrent CLI promotion cannot be
    /// overwritten.
    pub(crate) fn add_member(&self, email: &str, created_by: &str) -> Result<Change, UserError> {
        let email = normalize_email(email).ok_or(UserError::InvalidEmail)?;
        let created_by =
            normalize_text(created_by, "admin-ui").ok_or(UserError::InvalidText("created_by"))?;
        let mut conn = self.connect()?;
        let tx = conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let existing: Option<(String, Option<f64>)> = tx
            .query_row(
                "SELECT role, disabled_at FROM users WHERE email = ?1",
                [&email],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()?;
        let change = match existing {
            None => {
                tx.execute(
                    "INSERT INTO users \
                     (email, role, disabled_at, created_at, created_by, note) \
                     VALUES (?1, 'member', NULL, ?2, ?3, 'admin-ui')",
                    params![email, now_secs(), created_by],
                )?;
                Change::Added
            }
            Some((role, disabled_at)) => match parse_role(&role) {
                Some(Role::Member) if disabled_at.is_some() => {
                    tx.execute(
                        "UPDATE users SET disabled_at = NULL WHERE email = ?1",
                        [&email],
                    )?;
                    Change::Activated
                }
                Some(Role::Member) => Change::AlreadyPresent,
                _ => Change::AdminProtected,
            },
        };
        tx.commit()?;
        Ok(change)
    }

    /// Disable a `member` from the `/admin` UI. Any row whose role is not
    /// `member` (admins, corrupt roles) is refused with
    /// [`Change::AdminProtected`]; unknown emails are [`Change::NotFound`].
    pub(crate) fn disable_member(&self, email: &str) -> Result<Change, UserError> {
        let email = normalize_email(email).ok_or(UserError::InvalidEmail)?;
        let mut conn = self.connect()?;
        let tx = conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let existing: Option<(String, Option<f64>)> = tx
            .query_row(
                "SELECT role, disabled_at FROM users WHERE email = ?1",
                [&email],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()?;
        let change = match existing {
            None => Change::NotFound,
            Some((role, disabled_at)) => match parse_role(&role) {
                Some(Role::Member) if disabled_at.is_some() => Change::AlreadyDisabled,
                Some(Role::Member) => {
                    tx.execute(
                        "UPDATE users SET disabled_at = ?2 WHERE email = ?1",
                        params![email, now_secs()],
                    )?;
                    Change::Disabled
                }
                _ => Change::AdminProtected,
            },
        };
        tx.commit()?;
        Ok(change)
    }

    /// Idempotent grant/role set. An existing active row with the same role
    /// is `AlreadyPresent`; a different role updates it; a disabled row is
    /// reactivated. Metadata (`created_at`/`created_by`/`note`) is kept from
    /// the first insert.
    pub(crate) fn add(
        &self,
        email: &str,
        role: Role,
        created_by: &str,
        note: &str,
    ) -> Result<Change, UserError> {
        let email = normalize_email(email).ok_or(UserError::InvalidEmail)?;
        let created_by =
            normalize_text(created_by, "cli").ok_or(UserError::InvalidText("created_by"))?;
        let note = normalize_text(note, "").ok_or(UserError::InvalidText("note"))?;
        let mut conn = self.connect()?;
        let tx = conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let existing: Option<(String, Option<f64>)> = tx
            .query_row(
                "SELECT role, disabled_at FROM users WHERE email = ?1",
                [&email],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()?;
        let change = match existing {
            None => {
                tx.execute(
                    "INSERT INTO users \
                     (email, role, disabled_at, created_at, created_by, note) \
                     VALUES (?1, ?2, NULL, ?3, ?4, ?5)",
                    params![email, role.as_str(), now_secs(), created_by, note],
                )?;
                Change::Added
            }
            Some((_, Some(_))) => {
                tx.execute(
                    "UPDATE users SET role = ?2, disabled_at = NULL WHERE email = ?1",
                    params![email, role.as_str()],
                )?;
                Change::Activated
            }
            Some((existing_role, None)) => match parse_role(&existing_role) {
                Some(existing_role) if existing_role == role => Change::AlreadyPresent,
                Some(_) => {
                    tx.execute(
                        "UPDATE users SET role = ?2 WHERE email = ?1",
                        params![email, role.as_str()],
                    )?;
                    Change::Updated
                }
                None => {
                    tx.execute(
                        "UPDATE users SET role = ?2 WHERE email = ?1",
                        params![email, role.as_str()],
                    )?;
                    Change::Updated
                }
            },
        };
        tx.commit()?;
        Ok(change)
    }

    pub(crate) fn disable(&self, email: &str) -> Result<Change, UserError> {
        let email = normalize_email(email).ok_or(UserError::InvalidEmail)?;
        let mut conn = self.connect()?;
        let tx = conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let existing: Option<Option<f64>> = tx
            .query_row(
                "SELECT disabled_at FROM users WHERE email = ?1",
                [&email],
                |r| r.get(0),
            )
            .optional()?;
        let change = match existing {
            None => Change::NotFound,
            Some(Some(_)) => Change::AlreadyDisabled,
            Some(None) => {
                tx.execute(
                    "UPDATE users SET disabled_at = ?2 WHERE email = ?1",
                    params![email, now_secs()],
                )?;
                Change::Disabled
            }
        };
        tx.commit()?;
        Ok(change)
    }
}

/// In-memory active membership for the hot auth path. The server refreshes it
/// every second; tests can construct it without a DB via [`Self::test_with`].
pub(crate) struct UserDirectory {
    store: Option<Arc<UserStore>>,
    users: RwLock<HashMap<String, Role>>,
}

impl UserDirectory {
    pub(crate) fn new(store: Arc<UserStore>) -> Self {
        let dir = Self {
            store: Some(store),
            users: RwLock::new(HashMap::new()),
        };
        if let Err(e) = dir.refresh() {
            tracing::warn!(error = %e, "user directory initial load failed — denying all members");
        }
        dir
    }

    #[cfg(test)]
    pub(crate) fn test_with(entries: &[(&str, Role)]) -> Self {
        let users = entries
            .iter()
            .filter_map(|(email, role)| normalize_email(email).map(|e| (e, *role)))
            .collect();
        Self {
            store: None,
            users: RwLock::new(users),
        }
    }

    #[cfg(test)]
    pub(crate) fn empty() -> Self {
        Self::test_with(&[])
    }

    pub(crate) fn refresh(&self) -> Result<(), UserError> {
        let Some(store) = &self.store else {
            return Ok(());
        };
        let users = store.load_active()?;
        *self.users.write().unwrap() = users;
        Ok(())
    }

    pub(crate) fn is_member(&self, email: &str) -> bool {
        let Some(email) = normalize_email(email) else {
            return false;
        };
        self.users.read().unwrap().contains_key(&email)
    }

    pub(crate) fn is_admin(&self, email: &str) -> bool {
        let Some(email) = normalize_email(email) else {
            return false;
        };
        matches!(self.users.read().unwrap().get(&email), Some(Role::Admin))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_dir(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "bookq-users-{tag}-{}-{}",
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
        // Lowercasing can grow the byte length (U+0130 → 3 bytes).
        let raw = format!("{}@a.com", "İ".repeat(85));
        assert!(raw.len() < MAX_EMAIL_LEN);
        assert_eq!(normalize_email(&raw), None);
    }

    #[test]
    fn role_parse_and_display() {
        assert_eq!(Role::parse("ADMIN"), Some(Role::Admin));
        assert_eq!(Role::parse(" member "), Some(Role::Member));
        assert_eq!(Role::parse("owner"), None);
        assert_eq!(Role::Admin.as_str(), "admin");
        assert_eq!(Role::Member.to_string(), "member");
    }

    #[test]
    fn add_change_disable_reactivate() {
        let dir = temp_dir("crud");
        let store = UserStore::new(&dir);
        assert_eq!(
            store
                .add("a@example.com", Role::Member, "cli", "note")
                .unwrap(),
            Change::Added
        );
        assert!(store.is_active("a@example.com").unwrap());
        assert!(!store.is_admin("a@example.com").unwrap());
        assert_eq!(
            store.add("a@example.com", Role::Member, "cli", "").unwrap(),
            Change::AlreadyPresent
        );
        assert_eq!(
            store.add("a@example.com", Role::Admin, "cli", "").unwrap(),
            Change::Updated
        );
        assert!(store.is_admin("a@example.com").unwrap());
        assert_eq!(store.disable("a@example.com").unwrap(), Change::Disabled);
        assert!(!store.is_active("a@example.com").unwrap());
        assert_eq!(
            store.disable("a@example.com").unwrap(),
            Change::AlreadyDisabled
        );
        assert_eq!(
            store.add("a@example.com", Role::Member, "cli", "").unwrap(),
            Change::Activated
        );
        assert!(!store.is_admin("a@example.com").unwrap());
        let row = store.find("a@example.com").unwrap().unwrap();
        assert_eq!(row.created_by, "cli");
        assert_eq!(row.note, "note");
        assert_eq!(
            store.disable("missing@example.com").unwrap(),
            Change::NotFound
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn add_member_only_manages_members() {
        let dir = temp_dir("member-ui");
        let store = UserStore::new(&dir);

        assert_eq!(
            store
                .add_member("m@example.com", "admin@example.com")
                .unwrap(),
            Change::Added
        );
        let row = store.find("m@example.com").unwrap().unwrap();
        assert_eq!(row.role, Role::Member);
        assert_eq!(row.created_by, "admin@example.com");
        assert_eq!(row.note, "admin-ui");
        assert_eq!(
            store
                .add_member("m@example.com", "admin@example.com")
                .unwrap(),
            Change::AlreadyPresent
        );

        assert_eq!(
            store.disable_member("m@example.com").unwrap(),
            Change::Disabled
        );
        assert!(!store.is_active("m@example.com").unwrap());
        assert_eq!(
            store.disable_member("m@example.com").unwrap(),
            Change::AlreadyDisabled
        );
        assert_eq!(
            store
                .add_member("m@example.com", "admin@example.com")
                .unwrap(),
            Change::Activated
        );
        assert!(store.is_active("m@example.com").unwrap());

        store.add("a@example.com", Role::Admin, "cli", "").unwrap();
        assert_eq!(
            store
                .add_member("a@example.com", "admin@example.com")
                .unwrap(),
            Change::AdminProtected
        );
        assert_eq!(
            store.disable_member("a@example.com").unwrap(),
            Change::AdminProtected
        );
        assert!(store.is_admin("a@example.com").unwrap());

        store.disable("a@example.com").unwrap();
        assert_eq!(
            store
                .add_member("a@example.com", "admin@example.com")
                .unwrap(),
            Change::AdminProtected
        );
        assert_eq!(
            store.disable_member("a@example.com").unwrap(),
            Change::AdminProtected
        );
        assert!(!store.is_active("a@example.com").unwrap());
        assert_eq!(
            store.disable_member("missing@example.com").unwrap(),
            Change::NotFound
        );

        let long_admin = format!("{}@example.com", "a".repeat(201));
        assert_eq!(
            store.add_member("long@example.com", &long_admin).unwrap(),
            Change::Added
        );
        assert_eq!(
            store.find("long@example.com").unwrap().unwrap().created_by,
            long_admin
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn invalid_input_does_not_write() {
        let dir = temp_dir("invalid");
        let store = UserStore::new(&dir);
        assert!(matches!(
            store.add("not-an-email", Role::Member, "cli", ""),
            Err(UserError::InvalidEmail)
        ));
        assert!(matches!(
            store.add("a@b.com", Role::Member, "cli", "line\nbreak"),
            Err(UserError::InvalidText("note"))
        ));
        assert!(matches!(
            store.add("a@b.com", Role::Member, "line\nbreak", ""),
            Err(UserError::InvalidText("created_by"))
        ));
        assert!(matches!(
            store.disable("not-an-email"),
            Err(UserError::InvalidEmail)
        ));
        assert!(matches!(
            store.add_member("not-an-email", "a@b.com"),
            Err(UserError::InvalidEmail)
        ));
        assert!(matches!(
            store.add_member("c@d.com", "line\nbreak"),
            Err(UserError::InvalidText("created_by"))
        ));
        assert!(store.list().unwrap().is_empty());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn v2_db_upgrades_without_users_and_preserves_visibility_rows() {
        let dir = temp_dir("v2-upgrade");
        let path = state::user_state_path(&dir);
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            conn.execute_batch(
                "CREATE TABLE visibility_users (
                     user_key TEXT PRIMARY KEY,
                     revision INTEGER NOT NULL DEFAULT 0
                 );
                 CREATE TABLE hidden_roots (
                     user_key TEXT NOT NULL,
                     root TEXT NOT NULL,
                     PRIMARY KEY (user_key, root)
                 );
                 INSERT INTO visibility_users (user_key, revision) VALUES ('*', 4);
                 INSERT INTO hidden_roots (user_key, root) VALUES ('*', 'secret');
                 PRAGMA user_version = 2;",
            )
            .unwrap();
        }
        let store = UserStore::new(&dir);
        // No env seed: a pre-users DB upgrades with an empty directory; the
        // operator must enroll users via the CLI.
        assert!(store.list().unwrap().is_empty());
        assert!(!store.is_active("a@example.com").unwrap());
        let conn = rusqlite::Connection::open(&path).unwrap();
        let version: i64 = conn
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(version, state::USER_STATE_SCHEMA_VERSION);
        let root: String = conn
            .query_row(
                "SELECT root FROM hidden_roots WHERE user_key='*'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(root, "secret");
        drop(conn);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn directory_tracks_refresh_and_roles() {
        let dir = temp_dir("directory");
        let store = std::sync::Arc::new(UserStore::new(&dir));
        let directory = UserDirectory::new(store.clone());
        assert!(!directory.is_member("member@example.com"));

        store
            .add("member@example.com", Role::Member, "cli", "")
            .unwrap();
        store
            .add("admin@example.com", Role::Admin, "cli", "")
            .unwrap();
        directory.refresh().unwrap();
        assert!(directory.is_member("member@example.com"));
        assert!(!directory.is_admin("member@example.com"));
        assert!(!directory.is_member("outside@example.com"));
        assert!(directory.is_admin("admin@example.com"));
        store.disable("member@example.com").unwrap();
        directory.refresh().unwrap();
        assert!(!directory.is_member("member@example.com"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn directory_test_helper_normalizes() {
        let directory = UserDirectory::test_with(&[
            (" A@Example.com ", Role::Member),
            ("b@example.com", Role::Admin),
        ]);
        assert!(directory.is_member("a@example.com"));
        assert!(!directory.is_admin("a@example.com"));
        assert!(directory.is_admin("B@EXAMPLE.COM"));
        assert!(UserDirectory::empty().is_member("a@example.com") == false);
    }

    #[test]
    fn store_failure_is_err_not_member() {
        let dir = temp_dir("openfail");
        std::fs::create_dir(dir.join("folder-visibility.sqlite3")).unwrap();
        let store = UserStore::new(&dir);
        assert!(store.is_active("a@b.com").is_err());
        assert!(store.list().is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn schema_too_new_refuses() {
        let dir = temp_dir("toonew");
        let path = state::user_state_path(&dir);
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            conn.pragma_update(None, "user_version", 99).unwrap();
        }
        let store = UserStore::new(&dir);
        let result =
            std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| store.is_active("a@b.com")));
        assert!(result.is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }
}
