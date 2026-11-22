//! User directory (#608) — the authoritative membership + role store.
//!
//! Source of truth: the `users` table in the shared user-state SQLite DB
//! (`state::user_state_path`). Rows are written **only** by the offline
//! `viewer-rs users` CLI (host/container operator); there is no HTTP write
//! path. Granting membership/admin therefore sits on the same trust boundary
//! as reading `SECRET_KEY` / the OAuth client secret.
//!
//! Membership is loaded into an in-memory [`UserDirectory`] so the sync
//! `auth::resolve` path does not do SQLite I/O on every image request. A
//! background task refreshes it every second; the `/admin` gate additionally
//! checks the DB per request so admin revocation is immediate.
//!
//! `ALLOWED_EMAILS` is a **one-time seed** (`settings.users_seeded` marker):
//! the first init inserts those emails as `member`, then the DB is
//! authoritative. Re-seeding after an intentional removal is prevented by the
//! marker; a lost DB re-seeds from the env on recovery.

use std::collections::{HashMap, HashSet};
use std::fmt;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, RwLock};
use std::time::{SystemTime, UNIX_EPOCH};

use rusqlite::{params, OptionalExtension};

use crate::state;

/// RFC 5321 maximum mailbox length.
pub(crate) const MAX_EMAIL_LEN: usize = 254;
/// Bound for `created_by` / `note` — keeps `users list` TSV rows readable.
pub(crate) const MAX_TEXT_LEN: usize = 200;

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

fn table_exists(conn: &rusqlite::Connection, name: &str) -> rusqlite::Result<bool> {
    let found = conn
        .query_row(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?1",
            [name],
            |_| Ok(()),
        )
        .optional()?;
    Ok(found.is_some())
}

fn parse_role(raw: &str) -> Option<Role> {
    Role::parse(raw)
}

pub(crate) struct UserStore {
    path: PathBuf,
    init: Mutex<bool>,
    /// `Some` for the server (seed/migrate on first init); `None` for the CLI
    /// so a CLI-first bootstrap cannot consume the one-time seed marker.
    seed: Option<Vec<String>>,
}

impl UserStore {
    pub(crate) fn new(state_dir: &Path, seed: Option<Vec<String>>) -> Self {
        Self {
            path: state::user_state_path(state_dir),
            init: Mutex::new(false),
            seed,
        }
    }

    fn connect(&self) -> Result<rusqlite::Connection, UserError> {
        let seed = self.seed.as_deref();
        Ok(state::connect_user_state_with(
            &self.path,
            &self.init,
            |conn| match seed {
                Some(seed) => Self::ensure_seed(conn, seed),
                None => Ok(()),
            },
        )?)
    }

    /// One-time seed + legacy `admin_users` migration. Runs before the
    /// `init` flag is set; an error leaves the store uninitialized so the
    /// next call retries (the DDL is idempotent).
    fn ensure_seed(conn: &rusqlite::Connection, seed: &[String]) -> rusqlite::Result<()> {
        let tx = conn.unchecked_transaction()?;
        let seeded = tx
            .query_row(
                "SELECT 1 FROM settings WHERE key = 'users_seeded'",
                [],
                |_| Ok(()),
            )
            .optional()?
            .is_some();
        if seeded {
            tx.commit()?;
            return Ok(());
        }

        let seed_set: HashSet<String> = seed.iter().filter_map(|e| normalize_email(e)).collect();
        let now = now_secs();

        // Unmerged-branch DBs had `admin_users`. Copy only grants that were
        // actually reachable (in ALLOWED_EMAILS); dormant rows are skipped
        // rather than silently activated. Production (v2) has no such table.
        if table_exists(&tx, "admin_users")? {
            let rows: Vec<(String, f64, String, String)> = {
                let mut stmt =
                    tx.prepare("SELECT email, created_at, created_by, note FROM admin_users")?;
                let rows = stmt
                    .query_map([], |r| {
                        Ok((
                            r.get::<_, String>(0)?,
                            r.get::<_, f64>(1)?,
                            r.get::<_, String>(2)?,
                            r.get::<_, String>(3)?,
                        ))
                    })?
                    .collect::<rusqlite::Result<Vec<_>>>()?;
                rows
            };
            for (email, created_at, created_by, note) in rows {
                let Some(email) = normalize_email(&email) else {
                    continue;
                };
                if seed_set.contains(&email) {
                    tx.execute(
                        "INSERT OR IGNORE INTO users \
                         (email, role, disabled_at, created_at, created_by, note) \
                         VALUES (?1, 'admin', NULL, ?2, ?3, ?4)",
                        params![email, created_at, created_by, note],
                    )?;
                } else {
                    tracing::warn!(
                        email = %email,
                        "skipping legacy admin grant absent from ALLOWED_EMAILS seed"
                    );
                }
            }
            tx.execute("DROP TABLE admin_users", [])?;
        }

        for email in &seed_set {
            tx.execute(
                "INSERT OR IGNORE INTO users \
                 (email, role, disabled_at, created_at, created_by, note) \
                 VALUES (?1, 'member', NULL, ?2, 'seed', '')",
                params![email, now],
            )?;
        }
        tx.execute(
            "INSERT INTO settings (key, value) VALUES ('users_seeded', '1')",
            [],
        )?;
        tx.commit()?;
        Ok(())
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

    fn seeded(dir: &Path, emails: &[&str]) -> UserStore {
        UserStore::new(dir, Some(emails.iter().map(|e| e.to_string()).collect()))
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
    fn seed_members_and_marker_is_one_time() {
        let dir = temp_dir("seed");
        let store = seeded(&dir, &["a@example.com", "b@example.com"]);
        let rows = store.list().unwrap();
        assert_eq!(rows.len(), 2);
        assert!(rows.iter().all(|r| r.role == Role::Member && r.active()));

        // A new seed must not be applied after the marker exists (an
        // intentional removal must survive a restart).
        let store2 = seeded(&dir, &["c@example.com"]);
        let rows = store2.list().unwrap();
        assert_eq!(rows.len(), 2);
        assert!(!rows.iter().any(|r| r.email == "c@example.com"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn marker_prevents_reseed_after_all_users_are_removed() {
        let dir = temp_dir("no-reseed");
        let store = seeded(&dir, &["a@example.com"]);
        assert!(store.is_active("a@example.com").unwrap());
        store.disable("a@example.com").unwrap();
        // Re-open with a new seed; the marker must keep the directory empty.
        let store2 = seeded(&dir, &["b@example.com"]);
        let rows = store2.list().unwrap();
        assert_eq!(rows.len(), 1);
        assert!(!rows[0].active());
        assert!(!store2.is_active("b@example.com").unwrap());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn cli_first_does_not_consume_the_server_seed_marker() {
        let dir = temp_dir("cli-first");
        let cli_store = UserStore::new(&dir, None);
        assert_eq!(
            cli_store
                .add("admin@example.com", Role::Admin, "cli", "")
                .unwrap(),
            Change::Added
        );
        // Server starts later: its seed still runs and keeps the admin role.
        let server_store = seeded(&dir, &["member@example.com"]);
        let rows = server_store.list().unwrap();
        assert_eq!(rows.len(), 2);
        assert_eq!(
            rows.iter()
                .find(|r| r.email == "admin@example.com")
                .unwrap()
                .role,
            Role::Admin
        );
        assert_eq!(
            rows.iter()
                .find(|r| r.email == "member@example.com")
                .unwrap()
                .role,
            Role::Member
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn add_change_disable_reactivate() {
        let dir = temp_dir("crud");
        let store = seeded(&dir, &[]);
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
    fn invalid_input_does_not_write() {
        let dir = temp_dir("invalid");
        let store = seeded(&dir, &[]);
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
        assert!(store.list().unwrap().is_empty());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn legacy_admin_users_are_migrated_filtered_by_seed() {
        let dir = temp_dir("legacy-admin");
        let path = state::user_state_path(&dir);
        {
            let conn = rusqlite::Connection::open(&path).unwrap();
            conn.execute_batch(
                "CREATE TABLE admin_users (
                     email TEXT NOT NULL COLLATE NOCASE PRIMARY KEY,
                     created_at REAL NOT NULL,
                     created_by TEXT NOT NULL,
                     note TEXT NOT NULL DEFAULT ''
                 );
                 INSERT INTO admin_users VALUES ('a@example.com', 11.0, 'cli', 'legacy');
                 INSERT INTO admin_users VALUES ('outside@example.com', 12.0, 'cli', 'dormant');
                 PRAGMA user_version = 3;",
            )
            .unwrap();
        }
        let store = seeded(&dir, &["a@example.com", "member@example.com"]);
        let rows = store.list().unwrap();
        assert_eq!(rows.len(), 2);
        let admin = rows.iter().find(|r| r.email == "a@example.com").unwrap();
        assert_eq!(admin.role, Role::Admin);
        assert_eq!(admin.note, "legacy");
        assert!(rows.iter().any(|r| r.email == "member@example.com"));
        assert!(!rows.iter().any(|r| r.email == "outside@example.com"));
        let conn = rusqlite::Connection::open(&path).unwrap();
        let legacy: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='admin_users'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(legacy, 0);
        let marker: String = conn
            .query_row(
                "SELECT value FROM settings WHERE key='users_seeded'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(marker, "1");
        drop(conn);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn v2_db_seeds_members_without_losing_visibility_rows() {
        let dir = temp_dir("v2-seed");
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
        let store = seeded(&dir, &["a@example.com"]);
        assert!(store.is_active("a@example.com").unwrap());
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
        let store = std::sync::Arc::new(seeded(&dir, &["member@example.com"]));
        let directory = UserDirectory::new(store.clone());
        assert!(directory.is_member("member@example.com"));
        assert!(!directory.is_admin("member@example.com"));
        assert!(!directory.is_member("outside@example.com"));

        store
            .add("admin@example.com", Role::Admin, "cli", "")
            .unwrap();
        directory.refresh().unwrap();
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
        let store = UserStore::new(&dir, Some(vec![]));
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
        let store = UserStore::new(&dir, Some(vec![]));
        let result =
            std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| store.is_active("a@b.com")));
        assert!(result.is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }
}
