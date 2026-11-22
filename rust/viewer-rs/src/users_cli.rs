//! `viewer-rs users …` — the offline user-directory CLI (#608).
//!
//! This is the **only** writer of admin rows (members can also be changed
//! from the `/admin` UI). It runs as the same binary as the server, so
//! operators do not need a shell or extra image contents:
//!
//! ```text
//! docker exec bookq-prd-viewer-rs /usr/local/bin/viewer-rs users add you@example.com --role admin
//! ```
//!
//! Membership is DB-authoritative; admins are enrolled only here, while
//! members can also be added/disabled/re-enabled from the `/admin` UI;
//! additions/removals take effect without a restart (the server refreshes
//! its directory every second; `/admin` checks the DB per request).
//!
//! Exit codes: 0 ok, 1 not found/disabled (check/remove), 2 usage/invalid
//! input, 3 store error. A schema newer than this binary panics in the shared
//! store init (matching the server's refusal) and exits 101.

use std::path::Path;
use std::time::{Duration, UNIX_EPOCH};

use crate::state;
use crate::users::{self, Change, Role, UserError, UserRow, UserStore};

const EXIT_OK: i32 = 0;
const EXIT_NOT_FOUND: i32 = 1;
const EXIT_USAGE: i32 = 2;
const EXIT_STORE: i32 = 3;

const USAGE: &str = "\
usage: viewer-rs users <command> [options]

commands:
  add <email> [--role member|admin] [--note TEXT] [--by ACTOR]
                                           add/reactivate a user (idempotent)
  remove <email>                           disable a user (soft delete)
  list                                     list users (TSV; no output = none)
  check <email>                            admin|member|disabled|not found

env:
  BOOKQ_STATE_DIR   user-state directory (default /var/lib/bookq)

exit codes:
  0 ok / 1 not found or disabled / 2 usage or invalid input / 3 store error
";

#[derive(Debug, PartialEq, Eq)]
enum Cmd {
    Add {
        email: String,
        role: Role,
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

/// Parse the argv slice after `users`. Kept pure so the parser matrix is
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
            let mut role = Role::Member;
            let mut note = String::new();
            let mut created_by = "cli".to_string();
            let mut i = 0;
            while i < rest.len() {
                let arg = rest[i].as_str();
                if arg == "--help" || arg == "-h" {
                    return Ok(Cmd::Help);
                }
                if matches!(arg, "--role" | "--note" | "--by") {
                    let Some(value) = rest.get(i + 1) else {
                        return Err(format!("{arg} requires a value"));
                    };
                    match arg {
                        "--role" => {
                            role = Role::parse(value)
                                .ok_or_else(|| format!("invalid role: {value}"))?
                        }
                        "--note" => note = value.clone(),
                        "--by" => created_by = value.clone(),
                        _ => unreachable!(),
                    }
                    i += 2;
                    continue;
                }
                if let Some(value) = arg.strip_prefix("--role=") {
                    role = Role::parse(value).ok_or_else(|| format!("invalid role: {value}"))?;
                    i += 1;
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
                role,
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
/// A corrupted/manual DB row with a huge REAL must not panic `users list`.
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

fn format_row(row: &UserRow) -> String {
    format!(
        "{}\t{}\t{}\t{}\t{}\t{}",
        row.email,
        row.role,
        if row.active() { "active" } else { "disabled" },
        format_created_at(row.created_at),
        row.created_by,
        row.note
    )
}

fn fail(err: &UserError) -> i32 {
    match err {
        UserError::InvalidEmail | UserError::InvalidText(_) => {
            eprintln!("error: {err}\n\n{USAGE}");
            EXIT_USAGE
        }
        UserError::Sqlite(_) => {
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

/// Run one `users` invocation. Returns the process exit code; never panics on
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
    // BOOKQ_STATE_DIR is the easiest way to change a different env.
    eprintln!("state: {}", state::user_state_path(state_dir).display());
    warn_if_new_db(state_dir);
    let store = UserStore::new(state_dir);
    match cmd {
        Cmd::Add {
            email,
            role,
            note,
            created_by,
        } => {
            let display = users::display_email(&email);
            match store.add(&email, role, &created_by, &note) {
                Ok(Change::Added) => {
                    println!("added {display} role={role}");
                    EXIT_OK
                }
                Ok(Change::Updated) => {
                    println!("updated {display} role={role}");
                    EXIT_OK
                }
                Ok(Change::AlreadyPresent) => {
                    println!("already {display} role={role}");
                    EXIT_OK
                }
                Ok(Change::Activated) => {
                    println!("reactivated {display} role={role}");
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
            let display = users::display_email(&email);
            match store.disable(&email) {
                Ok(Change::Disabled) => {
                    println!("disabled {display}");
                    EXIT_OK
                }
                Ok(Change::AlreadyDisabled) => {
                    println!("already disabled: {display}");
                    EXIT_OK
                }
                Ok(Change::NotFound) => {
                    eprintln!("not found: {display}");
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
            if users::normalize_email(&email).is_none() {
                eprintln!("error: invalid email\n\n{USAGE}");
                return EXIT_USAGE;
            }
            match store.find(&email) {
                Ok(Some(row)) if row.active() => {
                    println!("{}", row.role);
                    EXIT_OK
                }
                Ok(Some(_)) => {
                    println!("disabled");
                    EXIT_NOT_FOUND
                }
                Ok(None) => {
                    println!("not found");
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
            "bookq-users-cli-{tag}-{}-{}",
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
                role: Role::Member,
                note: String::new(),
                created_by: "cli".into(),
            })
        );
        assert_eq!(
            parse(&args(&[
                "add",
                "--role",
                "admin",
                "--note",
                "initial",
                "a@b.com",
                "--by=ticket-1"
            ])),
            Ok(Cmd::Add {
                email: "a@b.com".into(),
                role: Role::Admin,
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
            args(&["add", "a@b.com", "--role"]),
            args(&["add", "a@b.com", "--role", "owner"]),
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
    fn add_change_role_disable_and_reactivate() {
        let dir = temp_dir("roundtrip");
        let display = "a@b.com";

        assert_eq!(run(&args(&["add", display]), &dir), EXIT_OK);
        assert_eq!(run(&args(&["check", display]), &dir), EXIT_OK);
        assert_eq!(run(&args(&["add", display]), &dir), EXIT_OK);
        assert_eq!(
            run(&args(&["add", display, "--role", "admin"]), &dir),
            EXIT_OK
        );
        let rows = UserStore::new(&dir).list().unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].role, Role::Admin);
        assert!(rows[0].active());

        assert_eq!(run(&args(&["remove", display]), &dir), EXIT_OK);
        assert_eq!(run(&args(&["check", display]), &dir), EXIT_NOT_FOUND);
        let rows = UserStore::new(&dir).list().unwrap();
        assert!(!rows[0].active());

        assert_eq!(run(&args(&["add", display]), &dir), EXIT_OK);
        let rows = UserStore::new(&dir).list().unwrap();
        assert_eq!(rows[0].role, Role::Member);
        assert!(rows[0].active());

        // Invalid input never reaches the store.
        assert_eq!(run(&args(&["add", "not-an-email"]), &dir), EXIT_USAGE);
        assert_eq!(run(&args(&["check", "not-an-email"]), &dir), EXIT_USAGE);
        assert_eq!(
            run(&args(&["add", display, "--role", "owner"]), &dir),
            EXIT_USAGE
        );
        assert_eq!(run(&args(&["wat"]), &dir), EXIT_USAGE);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn list_is_empty_and_tsv_shape_is_stable() {
        let dir = temp_dir("tsv");
        assert_eq!(run(&args(&["list"]), &dir), EXIT_OK);
        assert_eq!(
            run(
                &args(&[
                    "add", "a@b.com", "--role", "admin", "--note", "initial", "--by", "ticket-1"
                ]),
                &dir
            ),
            EXIT_OK
        );
        let rows = UserStore::new(&dir).list().unwrap();
        assert_eq!(rows.len(), 1);
        let line = format_row(&rows[0]);
        let cols: Vec<&str> = line.split('\t').collect();
        assert_eq!(cols.len(), 6);
        assert_eq!(cols[0], "a@b.com");
        assert_eq!(cols[1], "admin");
        assert_eq!(cols[2], "active");
        assert!(cols[3].ends_with("GMT"));
        assert_eq!(cols[4], "ticket-1");
        assert_eq!(cols[5], "initial");
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
        assert_eq!(
            users::display_email(" Alice@Example.COM "),
            "alice@example.com"
        );
        assert_eq!(users::display_email("not-an-email"), "not-an-email");
    }
}
