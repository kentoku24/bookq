//! `viewer-rs admin …` — the offline admin-registry CLI (#608).
//!
//! This is the **only** writer of `admin_users`. It runs as the same binary
//! as the server, so operators do not need a shell or extra image contents:
//!
//! ```text
//! docker exec bookq-prd-viewer-rs /usr/local/bin/viewer-rs admin add you@example.com
//! ```
//!
//! The store (`admin.rs`) normalizes emails and fails closed; this module
//! owns argv parsing, human-readable output and exit codes. Keeping argv and
//! stdout out of `admin.rs` lets the server module stay testable without
//! touching process globals.
//!
//! Exit codes: 0 ok, 1 not found (remove/check), 2 usage/invalid input,
//! 3 store error. A schema that is newer than this binary panics in the
//! shared store init (matching the server's refusal) and exits 101.

use std::path::Path;
use std::time::{Duration, UNIX_EPOCH};

use crate::admin::{self, AdminError, AdminStore, Change};
use crate::state;

const EXIT_OK: i32 = 0;
const EXIT_NOT_FOUND: i32 = 1;
const EXIT_USAGE: i32 = 2;
const EXIT_STORE: i32 = 3;

const USAGE: &str = "\
usage: viewer-rs admin <command> [options]

commands:
  add <email> [--note TEXT] [--by ACTOR]   grant admin (idempotent)
  remove <email>                           revoke admin
  list                                     list grants (TSV; no output = none)
  check <email>                            exit 0 if admin, 1 if not

env:
  BOOKQ_STATE_DIR   user-state directory (default /var/lib/bookq)

exit codes:
  0 ok / 1 not found / 2 usage or invalid input / 3 store error
";

#[derive(Debug, PartialEq, Eq)]
enum Cmd {
    Add {
        email: String,
        note: String,
        created_by: String,
    },
    Remove {
        email: String,
    },
    List,
    Check {
        email: String,
    },
    Help,
}

fn usage_error(msg: &str) -> (i32, String) {
    (EXIT_USAGE, format!("error: {msg}\n\n{USAGE}"))
}

/// Parse the argv slice after `admin`. Kept pure so the parser matrix is
/// unit-testable without touching the filesystem or process env.
fn parse(args: &[String]) -> Result<Cmd, String> {
    let Some(head) = args.first().map(String::as_str) else {
        return Ok(Cmd::Help);
    };
    if matches!(head, "--help" | "-h" | "help") {
        return Ok(Cmd::Help);
    }
    let rest = &args[1..];
    match head {
        "add" => {
            let mut email: Option<String> = None;
            let mut note = String::new();
            let mut created_by = "cli".to_string();
            let mut i = 0;
            while i < rest.len() {
                let arg = rest[i].as_str();
                if arg == "--help" || arg == "-h" {
                    return Ok(Cmd::Help);
                }
                if arg == "--note" || arg == "--by" {
                    let Some(value) = rest.get(i + 1) else {
                        return Err(format!("{arg} requires a value"));
                    };
                    if arg == "--note" {
                        note = value.clone();
                    } else {
                        created_by = value.clone();
                    }
                    i += 2;
                    continue;
                }
                if let Some(value) = arg.strip_prefix("--note=") {
                    note = value.to_string();
                    i += 1;
                    continue;
                }
                if let Some(value) = arg.strip_prefix("--by=") {
                    created_by = value.to_string();
                    i += 1;
                    continue;
                }
                if arg.starts_with('-') {
                    return Err(format!("unknown option: {arg}"));
                }
                if email.replace(arg.to_string()).is_some() {
                    return Err(format!("unexpected extra argument: {arg}"));
                }
                i += 1;
            }
            let Some(email) = email else {
                return Err("add requires <email>".to_string());
            };
            Ok(Cmd::Add {
                email,
                note,
                created_by,
            })
        }
        "remove" | "check" => {
            if rest.iter().any(|a| a == "--help" || a == "-h") {
                return Ok(Cmd::Help);
            }
            let mut email: Option<String> = None;
            for arg in rest {
                if arg.starts_with('-') {
                    return Err(format!("unknown option: {arg}"));
                }
                if email.replace(arg.clone()).is_some() {
                    return Err(format!("unexpected extra argument: {arg}"));
                }
            }
            let Some(email) = email else {
                return Err(format!("{head} requires <email>"));
            };
            if head == "remove" {
                Ok(Cmd::Remove { email })
            } else {
                Ok(Cmd::Check { email })
            }
        }
        "list" => {
            if rest.iter().any(|a| a == "--help" || a == "-h") {
                return Ok(Cmd::Help);
            }
            if let Some(arg) = rest.first() {
                return Err(format!("list takes no arguments: {arg}"));
            }
            Ok(Cmd::List)
        }
        other => Err(format!("unknown command: {other}")),
    }
}

/// Largest timestamp `httpdate` formats safely (9999-12-31T23:59:59Z).
/// A corrupted/manual DB row with a huge REAL must not panic `admin list`.
const MAX_CREATED_AT: f64 = 253_402_300_799.0;

/// `created_at` (epoch seconds) → RFC 1123 UTC. `httpdate` is already a
/// dependency (HTTP header formatting); no date crate is pulled in.
fn format_created_at(ts: f64) -> String {
    let secs = if ts.is_finite() && ts > 0.0 {
        ts.min(MAX_CREATED_AT)
    } else {
        0.0
    };
    httpdate::fmt_http_date(UNIX_EPOCH + Duration::from_secs_f64(secs))
}

/// Show what was actually stored/looked up, not the raw argv spelling.
/// `add " Alice@Example.COM "` must report the canonical `alice@example.com`
/// so an operator can catch a typo before it becomes a grant.
fn display_email(email: &str) -> String {
    admin::normalize_email(email).unwrap_or_else(|| email.to_string())
}

fn format_row(row: &admin::AdminRow) -> String {
    format!(
        "{}\t{}\t{}\t{}",
        row.email,
        format_created_at(row.created_at),
        row.created_by,
        row.note
    )
}

fn fail(err: &AdminError) -> i32 {
    match err {
        AdminError::InvalidEmail | AdminError::InvalidText(_) => {
            eprintln!("error: {err}\n\n{USAGE}");
            EXIT_USAGE
        }
        AdminError::Sqlite(_) => {
            eprintln!("error: {err}");
            EXIT_STORE
        }
    }
}

fn warn_if_new_db(state_dir: &Path) {
    let path = state::user_state_path(state_dir);
    if !path.exists() {
        eprintln!(
            "warning: user-state DB does not exist yet; creating {} — \
             is BOOKQ_STATE_DIR correct?",
            path.display()
        );
    }
}

/// A grant for an email outside `ALLOWED_EMAILS` is inert: the server's
/// per-request allowlist check rejects the login first. Warn (never refuse) —
/// the allowlist can change later and the CLI may run with a different env.
fn warn_if_not_allowed(email: &str) {
    let Ok(allow) = std::env::var("ALLOWED_EMAILS") else {
        return;
    };
    let Some(normalized) = admin::normalize_email(email) else {
        return;
    };
    let listed = allow
        .split(',')
        .map(|e| e.trim().to_lowercase())
        .any(|e| !e.is_empty() && e == normalized);
    if !listed {
        eprintln!(
            "warning: {normalized} is not in ALLOWED_EMAILS — \
             this grant is inert until the email is added there"
        );
    }
}

/// Run one `admin` invocation. Returns the process exit code; never panics on
/// user input. `state_dir` is passed explicitly so tests do not mutate env.
pub(crate) fn run(args: &[String], state_dir: &Path) -> i32 {
    let cmd = match parse(args) {
        Ok(cmd) => cmd,
        Err(msg) => {
            let (code, text) = usage_error(&msg);
            eprintln!("{text}");
            return code;
        }
    };
    if matches!(cmd, Cmd::Help) {
        print!("{USAGE}");
        return EXIT_OK;
    }

    // Make the target explicit: with prod/dev volumes on one host, a wrong
    // BOOKQ_STATE_DIR is the easiest way to grant admin to a different env.
    eprintln!("state: {}", state::user_state_path(state_dir).display());
    warn_if_new_db(state_dir);
    let store = AdminStore::new(state_dir);
    match cmd {
        Cmd::Add {
            email,
            note,
            created_by,
        } => {
            warn_if_not_allowed(&email);
            let display = display_email(&email);
            match store.add(&email, &created_by, &note) {
                Ok(Change::Added) => {
                    println!("added {display}");
                    EXIT_OK
                }
                Ok(Change::AlreadyPresent) => {
                    println!("already admin: {display}");
                    EXIT_OK
                }
                Ok(other) => {
                    eprintln!("error: unexpected store result: {other:?}");
                    EXIT_STORE
                }
                Err(e) => fail(&e),
            }
        }
        Cmd::Remove { email } => {
            let display = display_email(&email);
            match store.remove(&email) {
                Ok(Change::Removed) => {
                    println!("removed {display}");
                    EXIT_OK
                }
                Ok(Change::NotFound) => {
                    eprintln!("not admin: {display}");
                    EXIT_NOT_FOUND
                }
                Ok(other) => {
                    eprintln!("error: unexpected store result: {other:?}");
                    EXIT_STORE
                }
                Err(e) => fail(&e),
            }
        }
        Cmd::List => match store.list() {
            Ok(rows) => {
                for row in rows {
                    println!("{}", format_row(&row));
                }
                EXIT_OK
            }
            Err(e) => fail(&e),
        },
        Cmd::Check { email } => {
            // `is_admin` treats a malformed email as "not an admin" (the
            // server wants that fail-closed behavior). The CLI distinguishes
            // a typo from a real non-admin so scripts can tell them apart.
            if admin::normalize_email(&email).is_none() {
                eprintln!("error: invalid email\n\n{USAGE}");
                return EXIT_USAGE;
            }
            match store.is_admin(&email) {
                Ok(true) => {
                    // The grant exists, but it is inert if the server's
                    // allowlist (and therefore login) cannot admit the email.
                    warn_if_not_allowed(&email);
                    println!("admin");
                    EXIT_OK
                }
                Ok(false) => {
                    println!("not admin");
                    EXIT_NOT_FOUND
                }
                Err(e) => fail(&e),
            }
        }
        Cmd::Help => EXIT_OK,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn args(items: &[&str]) -> Vec<String> {
        items.iter().map(|s| s.to_string()).collect()
    }

    fn temp_dir(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "bookq-admin-cli-{tag}-{}-{}",
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
    fn parse_matrix() {
        assert_eq!(parse(&[]), Ok(Cmd::Help));
        assert_eq!(parse(&args(&["--help"])), Ok(Cmd::Help));
        assert_eq!(
            parse(&args(&["add", "a@b.com"])),
            Ok(Cmd::Add {
                email: "a@b.com".into(),
                note: String::new(),
                created_by: "cli".into(),
            })
        );
        assert_eq!(
            parse(&args(&[
                "add",
                "--note",
                "initial",
                "a@b.com",
                "--by=ticket-1"
            ])),
            Ok(Cmd::Add {
                email: "a@b.com".into(),
                note: "initial".into(),
                created_by: "ticket-1".into(),
            })
        );
        assert_eq!(
            parse(&args(&["remove", "A@B.com"])),
            Ok(Cmd::Remove {
                email: "A@B.com".into()
            })
        );
        assert_eq!(
            parse(&args(&["check", "a@b.com"])),
            Ok(Cmd::Check {
                email: "a@b.com".into()
            })
        );
        assert_eq!(parse(&args(&["list"])), Ok(Cmd::List));

        for bad in [
            args(&["add"]),
            args(&["add", "a@b.com", "extra"]),
            args(&["add", "a@b.com", "--bogus"]),
            args(&["add", "a@b.com", "--note"]),
            args(&["remove"]),
            args(&["remove", "a@b.com", "extra"]),
            args(&["check", "a@b.com", "--by", "x"]),
            args(&["list", "extra"]),
            args(&["wat"]),
        ] {
            assert!(parse(&bad).is_err(), "{bad:?}");
        }
    }

    #[test]
    fn add_check_remove_roundtrip_and_exit_codes() {
        let dir = temp_dir("roundtrip");
        assert_eq!(run(&args(&["add", "a@b.com"]), &dir), EXIT_OK);
        assert!(AdminStore::new(&dir).is_admin("a@b.com").unwrap());
        assert_eq!(run(&args(&["add", "A@B.com"]), &dir), EXIT_OK);
        assert_eq!(run(&args(&["check", "a@b.com"]), &dir), EXIT_OK);
        assert_eq!(run(&args(&["list"]), &dir), EXIT_OK);
        assert_eq!(run(&args(&["remove", "a@b.com"]), &dir), EXIT_OK);
        assert_eq!(run(&args(&["check", "a@b.com"]), &dir), EXIT_NOT_FOUND);
        assert_eq!(run(&args(&["remove", "a@b.com"]), &dir), EXIT_NOT_FOUND);
        // Invalid input never reaches the store.
        assert_eq!(run(&args(&["add", "not-an-email"]), &dir), EXIT_USAGE);
        assert_eq!(run(&args(&["check", "not-an-email"]), &dir), EXIT_USAGE);
        assert_eq!(run(&args(&["wat"]), &dir), EXIT_USAGE);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn list_is_empty_and_tsv_shape_is_stable() {
        let dir = temp_dir("tsv");
        assert_eq!(run(&args(&["list"]), &dir), EXIT_OK);
        assert_eq!(
            run(
                &args(&["add", "a@b.com", "--note", "initial", "--by", "ticket-1"]),
                &dir
            ),
            EXIT_OK
        );
        let rows = AdminStore::new(&dir).list().unwrap();
        assert_eq!(rows.len(), 1);
        let line = format_row(&rows[0]);
        let cols: Vec<&str> = line.split('\t').collect();
        assert_eq!(cols.len(), 4);
        assert_eq!(cols[0], "a@b.com");
        assert!(cols[1].ends_with("GMT"));
        assert_eq!(cols[2], "ticket-1");
        assert_eq!(cols[3], "initial");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn created_at_formatting_handles_bad_values() {
        assert_eq!(format_created_at(0.0), "Thu, 01 Jan 1970 00:00:00 GMT");
        assert_eq!(format_created_at(-1.0), "Thu, 01 Jan 1970 00:00:00 GMT");
        assert_eq!(format_created_at(f64::NAN), "Thu, 01 Jan 1970 00:00:00 GMT");
        // A corrupted huge REAL must not panic Duration::from_secs_f64.
        assert_eq!(format_created_at(1e30), format_created_at(MAX_CREATED_AT));
        assert!(format_created_at(1e30).ends_with("GMT"));
    }

    #[test]
    fn output_email_is_normalized() {
        assert_eq!(display_email(" Alice@Example.COM "), "alice@example.com");
        assert_eq!(display_email("not-an-email"), "not-an-email");
    }
}
