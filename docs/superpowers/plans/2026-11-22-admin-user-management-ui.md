# /admin ユーザー管理 UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a server-rendered `/admin` UI that lets an admin add, disable, and re-enable `member` users without touching the CLI, while keeping admin rows CLI-only.

**Architecture:** Add two member-only store operations (`add_member`, `disable_member`) that reject admin rows inside the SQLite transaction. A new `admin_ui` module renders the user table, add form, flash messages, and CSRF check. `main.rs` keeps the existing per-request admin gate, adds three POST handlers, and serves the rendered template. No JavaScript, no schema change.

**Tech Stack:** Rust (axum 0.8, rusqlite, tokio), plain HTML forms (`application/x-www-form-urlencoded`), Python pytest integration tests, existing `users_cli` test helper.

---

## File structure

| File | Responsibility |
|---|---|
| `rust/viewer-rs/src/users.rs` | Add `Change::AdminProtected`, `UserStore::add_member`, `UserStore::disable_member`, store unit tests |
| `rust/viewer-rs/src/admin_ui.rs` | **new** — HTML escaping, flash mapping, table/form rendering, POST same-origin check, unit tests |
| `rust/viewer-rs/src/main.rs` | Wire `admin_ui`, GET rendering, POST handlers, routes, CSP, body limit |
| `web/templates/admin.html` | Admin shell with `{{ADMIN_FLASH}}` / `{{ADMIN_USERS}}` placeholders and add form |
| `web/static/style.css` | Admin table, form, buttons, status badges |
| `tests/test_admin.py` | UI HTTP/authorization/CSRF/XSS/membership tests |
| `docs/deployment.md`, `README.md`, `AGENTS.md`, `CLAUDE.md`, `docs/user-state-db.md`, `docs/glossary.md`, `api/user-state-schema.sql` | Document the new member-only HTTP path |

No changes to `users_cli.rs`: its `Ok(other)` catch-all already covers the new enum variant.

---

### Task 1: Member-only store operations

**Files:**
- Modify: `rust/viewer-rs/src/users.rs`
- Test: `rust/viewer-rs/src/users.rs` (existing `mod tests`)

- [ ] **Step 1: Add the failing store test**

Insert this test immediately after the existing `add_change_disable_reactivate` test in `rust/viewer-rs/src/users.rs`:

```rust
    #[test]
    fn add_member_only_manages_members() {
        let dir = temp_dir("member-ui");
        let store = UserStore::new(&dir);

        assert_eq!(
            store.add_member("m@example.com", "admin@example.com").unwrap(),
            Change::Added
        );
        let row = store.find("m@example.com").unwrap().unwrap();
        assert_eq!(row.role, Role::Member);
        assert_eq!(row.created_by, "admin@example.com");
        assert_eq!(row.note, "admin-ui");
        assert_eq!(
            store.add_member("m@example.com", "admin@example.com").unwrap(),
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
            store.add_member("m@example.com", "admin@example.com").unwrap(),
            Change::Activated
        );
        assert!(store.is_active("m@example.com").unwrap());

        store
            .add("a@example.com", Role::Admin, "cli", "")
            .unwrap();
        assert_eq!(
            store.add_member("a@example.com", "admin@example.com").unwrap(),
            Change::AdminProtected
        );
        assert_eq!(
            store.disable_member("a@example.com").unwrap(),
            Change::AdminProtected
        );
        assert!(store.is_admin("a@example.com").unwrap());

        store.disable("a@example.com").unwrap();
        assert_eq!(
            store.add_member("a@example.com", "admin@example.com").unwrap(),
            Change::AdminProtected
        );
        assert_eq!(
            store.disable_member("a@example.com").unwrap(),
            Change::AdminProtected
        );
        assert_eq!(
            store.disable_member("missing@example.com").unwrap(),
            Change::NotFound
        );
        let _ = std::fs::remove_dir_all(&dir);
    }
```

- [ ] **Step 2: Run it and confirm it fails to compile**

```bash
cd rust
cargo test -p viewer-rs users::tests::add_member_only_manages_members
```

Expected: compile error `no variant or associated item named 'AdminProtected'` and `no method named 'add_member'` / `'disable_member'`.

- [ ] **Step 3: Add the enum variant**

In `rust/viewer-rs/src/users.rs`, extend `Change`:

```rust
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Change {
    Added,
    Updated,
    AlreadyPresent,
    Activated,
    Disabled,
    AlreadyDisabled,
    NotFound,
    /// The `/admin` UI only manages members; admins stay CLI-only.
    AdminProtected,
}
```

- [ ] **Step 4: Add the two store methods**

Insert these methods inside `impl UserStore`, immediately before the existing `pub(crate) fn add(`:

```rust
    /// Add or re-enable a `member` from the `/admin` UI.
    ///
    /// Unlike [`Self::add`], this never changes an existing row's role and
    /// refuses any row whose role is not `member` (including disabled
    /// admins) with [`Change::AdminProtected`]. The check and write share one
    /// immediate transaction so a concurrent CLI promotion cannot be
    /// overwritten.
    pub(crate) fn add_member(
        &self,
        email: &str,
        created_by: &str,
    ) -> Result<Change, UserError> {
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

    /// Disable a `member` from the `/admin` UI. Admin rows are refused.
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

```

- [ ] **Step 5: Run the new test and the existing users tests**

```bash
cd rust
cargo test -p viewer-rs users::tests
```

Expected: all `users::tests` pass, including `add_member_only_manages_members` and the existing `add_change_disable_reactivate`.

- [ ] **Step 6: Commit**

```bash
git add rust/viewer-rs/src/users.rs
git commit -m "feat(admin): member 専用の store 操作を追加 (#613)"
```

---

### Task 2: Rendering and CSRF helpers

**Files:**
- Create: `rust/viewer-rs/src/admin_ui.rs`
- Modify: `rust/viewer-rs/src/main.rs` (module declaration only)

- [ ] **Step 1: Create the module and its tests**

Create `rust/viewer-rs/src/admin_ui.rs` with the complete contents below.

```rust
//! Server-rendered `/admin` user management (#613).
//!
//! The admin surface is plain HTML forms: no JavaScript, no JSON API. Every
//! dynamic value goes through [`escape_html`], and flash messages are selected
//! from fixed codes. The only mutation-level CSRF gate is
//! [`same_origin_post`] plus the session cookie's `SameSite=Lax`.

use axum::http::{header, HeaderMap};

use crate::users::{Role, UserRow};

fn flash_message(code: &str) -> Option<&'static str> {
    match code {
        "added" => Some("ユーザーを追加しました。"),
        "reactivated" => Some("ユーザーを再有効化しました。"),
        "exists" => Some("そのユーザーは既に登録されています。"),
        "disabled" => Some("ログインを停止しました。"),
        "already-disabled" => Some("そのユーザーは既に停止されています。"),
        "not-found" => Some("ユーザーが見つかりません。"),
        "admin-readonly" => Some("admin はこの画面から変更できません。CLI を使ってください。"),
        "invalid" => Some("email の形式が正しくありません。"),
        _ => None,
    }
}

/// Escape a value for HTML text and double-quoted attributes.
pub(crate) fn escape_html(raw: &str) -> String {
    let mut out = String::with_capacity(raw.len());
    for c in raw.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&#39;"),
            _ => out.push(c),
        }
    }
    out
}

/// CSRF gate for `/admin/users/*`: the request must carry at least one of
/// `Sec-Fetch-Site` / `Origin`, and every signal present must be same-origin.
/// `oauth::same_site_initiation` already validates present signals; requiring
/// one closes the "bare HTTP client / stripped header" case for mutations.
pub(crate) fn same_origin_post(headers: &HeaderMap) -> bool {
    let has_signal =
        headers.contains_key("sec-fetch-site") || headers.contains_key(header::ORIGIN);
    has_signal && crate::oauth::same_site_initiation(headers)
}

fn render_row(row: &UserRow) -> String {
    let email = escape_html(&row.email);
    let role = row.role.as_str();
    let state = if row.active() { "active" } else { "disabled" };
    let action = match (row.role, row.active()) {
        (Role::Admin, _) => {
            r#"<span class="admin-note">CLI で管理</span>"#.to_string()
        }
        (Role::Member, true) => format!(
            r#"<form method="post" action="/admin/users/disable" class="admin-inline-form"><input type="hidden" name="email" value="{email}"><button type="submit" class="admin-btn admin-btn-danger">ログイン停止</button></form>"#
        ),
        (Role::Member, false) => format!(
            r#"<form method="post" action="/admin/users/enable" class="admin-inline-form"><input type="hidden" name="email" value="{email}"><button type="submit" class="admin-btn">再有効化</button></form>"#
        ),
    };
    format!(
        "<tr><td class=\"admin-email\">{email}</td>\
         <td><span class=\"admin-role admin-role-{role}\">{role}</span></td>\
         <td><span class=\"admin-status admin-status-{state}\">{state}</span></td>\
         <td>{action}</td></tr>\n"
    )
}

/// Fill the admin shell's two placeholders. `status` is the query value; only
/// known codes produce a flash, and the rendered message is escaped too.
pub(crate) fn render(template: &str, users: &[UserRow], status: Option<&str>) -> String {
    let flash = match status.and_then(flash_message) {
        Some(message) => format!(
            r#"<p class="admin-flash" role="status">{}</p>"#,
            escape_html(message)
        ),
        None => String::new(),
    };
    let rows: String = users.iter().map(render_row).collect();
    template
        .replace("{{ADMIN_FLASH}}", &flash)
        .replace("{{ADMIN_USERS}}", &rows)
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::http::HeaderValue;

    fn user(email: &str, role: Role, active: bool) -> UserRow {
        UserRow {
            email: email.to_string(),
            role,
            disabled_at: if active { None } else { Some(0.0) },
            created_at: 0.0,
            created_by: "admin@example.com".to_string(),
            note: "admin-ui".to_string(),
        }
    }

    fn headers(pairs: &[(&'static str, &'static str)]) -> HeaderMap {
        let mut map = HeaderMap::new();
        for (name, value) in pairs {
            map.insert(*name, HeaderValue::from_static(value));
        }
        map
    }

    #[test]
    fn escape_html_matrix() {
        assert_eq!(
            escape_html(r#"<a href="x">'&"#),
            "&lt;a href=&quot;x&quot;&gt;&#39;&amp;"
        );
        assert_eq!(escape_html("plain@example.com"), "plain@example.com");
    }

    #[test]
    fn renders_member_actions_and_admin_readonly() {
        let template = "{{ADMIN_FLASH}}|{{ADMIN_USERS}}";
        let users = [
            user("member@example.com", Role::Member, true),
            user("disabled@example.com", Role::Member, false),
            user("admin@example.com", Role::Admin, true),
        ];
        let body = render(template, &users, Some("added"));
        assert!(body.contains("ユーザーを追加しました。"));
        assert!(body.contains("admin-flash"));

        let member = body
            .lines()
            .find(|line| line.contains("member@example.com"))
            .unwrap();
        assert!(member.contains(r#"action="/admin/users/disable""#));
        assert!(!member.contains(r#"action="/admin/users/enable""#));

        let disabled = body
            .lines()
            .find(|line| line.contains("disabled@example.com"))
            .unwrap();
        assert!(disabled.contains(r#"action="/admin/users/enable""#));
        assert!(!disabled.contains(r#"action="/admin/users/disable""#));

        let admin = body
            .lines()
            .find(|line| line.contains("admin@example.com"))
            .unwrap();
        assert!(!admin.contains(r#"action="/admin/users/"#));
        assert!(admin.contains("CLI で管理"));
    }

    #[test]
    fn unknown_flash_code_renders_nothing() {
        assert!(flash_message("added").is_some());
        assert!(flash_message("bogus").is_none());
        let body = render("{{ADMIN_FLASH}}", &[], Some("bogus"));
        assert_eq!(body, "");
    }

    #[test]
    fn same_origin_post_matrix() {
        assert!(same_origin_post(&headers(&[
            ("sec-fetch-site", "same-origin")
        ])));
        assert!(same_origin_post(&headers(&[("sec-fetch-site", "none")])));
        assert!(!same_origin_post(&headers(&[])));
        assert!(!same_origin_post(&headers(&[
            ("sec-fetch-site", "cross-site")
        ])));
        assert!(!same_origin_post(&headers(&[
            ("sec-fetch-site", "same-site")
        ])));
        assert!(same_origin_post(&headers(&[
            ("origin", "https://bookq.example"),
            ("host", "bookq.example"),
        ])));
        assert!(!same_origin_post(&headers(&[
            ("origin", "https://evil.example"),
            ("host", "bookq.example"),
        ])));
        assert!(!same_origin_post(&headers(&[
            ("origin", "null"),
            ("host", "bookq.example"),
        ])));
        // Both signals present: each one must pass.
        assert!(!same_origin_post(&headers(&[
            ("sec-fetch-site", "cross-site"),
            ("origin", "https://bookq.example"),
            ("host", "bookq.example"),
        ])));
    }
}
```

- [ ] **Step 2: Declare the module and run the tests**

Add `mod admin_ui;` to `rust/viewer-rs/src/main.rs` immediately after `mod auth;`:

```rust
mod auth;
mod admin_ui;
```

Run:

```bash
cd rust
cargo test -p viewer-rs admin_ui
```

Expected: 4 tests pass.

- [ ] **Step 3: Commit**

```bash
git add rust/viewer-rs/src/admin_ui.rs rust/viewer-rs/src/main.rs
git commit -m "feat(admin): 管理画面の描画と CSRF ヘルパーを追加 (#613)"
```

---

### Task 3: GET `/admin` with list and add form

**Files:**
- Modify: `tests/test_admin.py`
- Modify: `web/templates/admin.html`
- Modify: `web/static/style.css`
- Modify: `rust/viewer-rs/src/main.rs`

- [ ] **Step 1: Add the failing GET tests**

In `tests/test_admin.py`, replace the existing `_request` with a body-capable version:

```python
def _request(server, method, path, headers=None, body=None):
    """One raw HTTP request — no redirect following, so 303 is observable."""
    u = urllib.parse.urlsplit(server)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
    finally:
        conn.close()
```

Add these helpers after `_cookie_header`:

```python
def _form_headers(server, **overrides):
    host = urllib.parse.urlsplit(server).netloc
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": f"http://{host}",
        "Sec-Fetch-Site": "same-origin",
    }
    headers.update(overrides)
    return headers


def _post_admin(server, action, email, headers=None):
    data = urllib.parse.urlencode({"email": email}).encode("utf-8")
    return _request(
        server,
        "POST",
        f"/admin/users/{action}",
        headers if headers is not None else _form_headers(server),
        data,
    )


def _row(body, email):
    needle = email.encode("utf-8")
    for line in body.splitlines():
        if needle in line and line.lstrip().startswith(b"<tr>"):
            return line
    raise AssertionError(f"row not found: {email}")
```

Add the new fixture and GET tests immediately before the existing `# ── real-auth server` section:

```python
# ── #613: /admin user management UI ────────────────────────────────────────

NEW_USER = "new-member@example.com"
CSRF_USER = "csrf-member@example.com"
XSS_USER = "<img@example.com>"


@pytest.fixture(scope="module")
def ui_state(tmp_path_factory):
    return tmp_path_factory.mktemp("admin-ui-state")


@pytest.fixture(scope="module")
def ui_server(library_tree, ui_state):
    proc, base_url = spawn_backend(library_dir=library_tree, state_dir=ui_state)
    yield base_url
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture
def ui_reset(ui_state, ui_server):
    """Admin actor + a member target; restore a safe state after each test."""
    users_cli(ui_state, "add", TEST_EMAIL, "--role", "admin")
    users_cli(ui_state, "add", OUTSIDE)
    users_cli(ui_state, "remove", NEW_USER)
    users_cli(ui_state, "remove", CSRF_USER)
    users_cli(ui_state, "remove", XSS_USER)
    yield
    users_cli(ui_state, "add", TEST_EMAIL, "--role", "member")
    users_cli(ui_state, "remove", OUTSIDE)
    users_cli(ui_state, "remove", NEW_USER)
    users_cli(ui_state, "remove", CSRF_USER)
    users_cli(ui_state, "remove", XSS_USER)


def test_admin_page_lists_users_and_forms(ui_server, ui_reset):
    status, headers, body = _request(ui_server, "GET", "/admin")
    assert status == 200
    assert headers["cache-control"] == "no-store"
    csp = headers["content-security-policy"]
    assert "form-action 'self'" in csp
    assert "script-src 'none'" in csp
    assert b'action="/admin/users/add"' in body
    assert b'name="email"' in body
    assert b"admin-users" in body

    member_row = _row(body, OUTSIDE)
    assert b'action="/admin/users/disable"' in member_row
    admin_row = _row(body, TEST_EMAIL)
    assert b"/admin/users/disable" not in admin_row
    assert b"/admin/users/enable" not in admin_row
    assert b"CLI" in admin_row


def test_admin_status_message_and_unknown_ignored(ui_server, ui_reset):
    _, _, body = _request(ui_server, "GET", "/admin?status=added")
    assert "ユーザーを追加しました".encode("utf-8") in body
    _, _, body = _request(ui_server, "GET", "/admin?status=bogus")
    assert b"admin-flash" not in body
```

Update the existing `test_admin_role_opens_page_and_menu` assertion:

```python
    assert "ユーザーを追加".encode("utf-8") in body
```

replacing:

```python
    assert "管理機能はまだありません".encode("utf-8") in body
```

Update `test_admin_template_is_not_public` to use the new page marker:

```python
        assert "ユーザーを追加".encode("utf-8") not in body
```

replacing:

```python
        assert "管理機能".encode("utf-8") not in body
```

- [ ] **Step 2: Run the GET tests and confirm they fail**

```bash
uv run --with pytest --with playwright --with-requirements requirements-dev.txt \
  pytest tests/test_admin.py -q -k "admin_page_lists or status_message"
```

Expected: FAIL because the page still says `管理機能はまだありません。` and has no form.

- [ ] **Step 3: Replace the admin template**

Replace the `<main>` block in `web/templates/admin.html` with:

```html
<main id="admin-content" class="admin-content">
  {{ADMIN_FLASH}}
  <section class="admin-section" aria-labelledby="admin-add-title">
    <h2 id="admin-add-title">ユーザーを追加</h2>
    <form class="admin-add" method="post" action="/admin/users/add">
      <label for="admin-add-email">Google アカウントのメールアドレス</label>
      <div class="admin-add-row">
        <input id="admin-add-email" name="email" type="email" autocomplete="off"
               spellcheck="false" maxlength="254" required>
        <button type="submit" class="admin-btn admin-btn-primary">追加</button>
      </div>
    </form>
    <p class="admin-hint">追加したユーザーは member として登録されます。admin の変更は CLI で行います。</p>
  </section>
  <section class="admin-section" aria-labelledby="admin-users-title">
    <h2 id="admin-users-title">ユーザー一覧</h2>
    <div class="admin-table-wrap">
      <table class="admin-users">
        <thead>
          <tr>
            <th scope="col">email</th>
            <th scope="col">role</th>
            <th scope="col">status</th>
            <th scope="col">操作</th>
          </tr>
        </thead>
        <tbody>
{{ADMIN_USERS}}        </tbody>
      </table>
    </div>
  </section>
</main>
```

- [ ] **Step 4: Add the admin UI styles**

Replace the existing `/* ── admin (empty admin page, #608) ── */` block in `web/static/style.css` with:

```css
/* ── admin (#608, user management #613) ── */
html,body.admin{height:100%;background:#0d0d0d;color:#ccc}
body.admin{position:static;inset:auto;overflow-y:auto;user-select:text;-webkit-user-select:text}
.admin-header{position:sticky;top:0;z-index:2;display:flex;align-items:center;gap:12px;min-height:52px;padding:4px 16px;background:rgba(20,20,20,.94);border-bottom:1px solid #2a2a2a}
.admin-header h1{flex:1;font-size:16px;font-weight:600;color:#e0e0e0;letter-spacing:-.01em}
.admin-header a{display:inline-flex;align-items:center;min-height:44px;padding:0 4px;color:#8ab4f8;font-size:13px;text-decoration:none}
.admin-header a:hover{text-decoration:underline}
.admin-content{max-width:720px;margin:0 auto;padding:32px 20px 48px}
.admin-section{margin:0 0 28px}
.admin-section h2{font-size:14px;font-weight:600;color:#e0e0e0;margin:0 0 12px}
.admin-flash{margin:0 0 20px;padding:12px 14px;border:1px solid #2f4f3a;border-radius:8px;background:rgba(46,160,67,.12);color:#b7e4c7;font-size:13px;line-height:1.6}
.admin-add label{display:block;margin:0 0 6px;font-size:12px;color:#aaa}
.admin-add-row{display:flex;gap:8px}
.admin-add input{flex:1;min-width:0;min-height:44px;padding:0 12px;border:1px solid #3a3a3a;border-radius:8px;background:#151515;color:#eee;font-size:15px}
.admin-add input:focus{outline:2px solid #4a7ac4;outline-offset:1px}
.admin-btn{min-height:44px;padding:0 14px;border:1px solid #3a3a3a;border-radius:8px;background:#1d1d1d;color:#ddd;font-size:13px;cursor:pointer;white-space:nowrap}
.admin-btn:hover{background:#262626}
.admin-btn-primary{border-color:#3b6ea5;background:#1d3a5c;color:#dce9fb}
.admin-btn-danger{border-color:#6b3030;background:#3a1d1d;color:#f3c1c1}
.admin-table-wrap{overflow-x:auto;border:1px solid #2a2a2a;border-radius:8px}
.admin-users{width:100%;border-collapse:collapse;font-size:13px}
.admin-users th,.admin-users td{padding:10px 12px;border-bottom:1px solid #242424;text-align:left;vertical-align:middle}
.admin-users th{background:#151515;color:#999;font-weight:500;white-space:nowrap}
.admin-users tr:last-child td{border-bottom:none}
.admin-email{word-break:break-all;color:#ddd}
.admin-role,.admin-status{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;line-height:1.6;white-space:nowrap}
.admin-role-admin{background:#2a3f5c;color:#bcd6f7}
.admin-role-member{background:#242424;color:#bbb}
.admin-status-active{background:#1e3b2a;color:#a8dfb9}
.admin-status-disabled{background:#3b2424;color:#e6b3b3}
.admin-inline-form{display:inline;margin:0}
.admin-note{font-size:12px;color:#777}
.admin-hint{margin:10px 0 0;font-size:12px;line-height:1.6;color:#777}
@media (max-width:480px){.admin-add-row{flex-direction:column}.admin-add-row .admin-btn{width:100%}}
```

- [ ] **Step 5: Rewrite `admin_page` and add shared admin helpers**

In `rust/viewer-rs/src/main.rs`, replace the whole `async fn admin_page(...) { ... }` with the following helpers and function. Keep the existing `admin_private_headers`, `admin_redirect_login`, `admin_not_found`, and `admin_check` above it.

```rust
/// Shared admin gate for the page and every mutation handler. Returns the
/// acting identity on success; any failure is already a complete response.
async fn admin_actor(
    st: &AppState,
    headers: &axum::http::HeaderMap,
) -> Result<auth::Identity, Response> {
    if corrupt_session(st, headers) {
        return Err(corrupt_session_response());
    }
    let Some(user) = caller(st, headers) else {
        return Err(admin_redirect_login());
    };
    if !admin_check(st, &user.email).await {
        return Err(admin_not_found());
    }
    Ok(user)
}

fn admin_internal_error(msg: &'static str) -> Response {
    let mut res = state::detail(StatusCode::INTERNAL_SERVER_ERROR, msg);
    admin_private_headers(&mut res);
    res
}

/// `GET|HEAD /admin` — user list + add form (#613).
async fn admin_page(State(st): State<AppState>, req: Request<Body>) -> Response {
    if !matches!(
        *req.method(),
        axum::http::Method::GET | axum::http::Method::HEAD
    ) {
        return method_not_allowed("GET, HEAD");
    }
    let head = *req.method() == axum::http::Method::HEAD;
    let headers = req.headers().clone();
    if let Err(res) = admin_actor(&st, &headers).await {
        return res;
    }
    let status = Query::<HashMap<String, String>>::try_from_uri(req.uri())
        .ok()
        .and_then(|q| q.0.get("status").cloned());

    let store = st.users.clone();
    let users = match tokio::task::spawn_blocking(move || store.list()).await {
        Ok(Ok(users)) => users,
        Ok(Err(e)) => {
            tracing::warn!(error = %e, "admin user list unavailable");
            return admin_internal_error("admin store unavailable");
        }
        Err(e) => {
            tracing::warn!(error = %e, "admin user list task failed");
            return admin_internal_error("admin store unavailable");
        }
    };

    let web_dir = st.web_dir.clone();
    let template = match tokio::task::spawn_blocking(move || {
        std::fs::read_to_string(web_dir.join("templates").join("admin.html"))
    })
    .await
    {
        Ok(Ok(template)) => template,
        Ok(Err(e)) => {
            tracing::warn!(error = %e, "admin template unavailable");
            return admin_internal_error("admin template unavailable");
        }
        Err(e) => {
            tracing::warn!(error = %e, "admin template task failed");
            return admin_internal_error("admin template unavailable");
        }
    };

    let body = admin_ui::render(&template, &users, status.as_deref());
    let mut res = Response::builder()
        .status(StatusCode::OK)
        .header(header::CONTENT_TYPE, "text/html; charset=utf-8")
        .body(if head { Body::empty() } else { Body::from(body) })
        .unwrap();
    admin_private_headers(&mut res);
    res.headers_mut()
        .insert("x-frame-options", HeaderValue::from_static("DENY"));
    // Forms post to this same origin; scripts and inline styles stay off.
    res.headers_mut().insert(
        "content-security-policy",
        HeaderValue::from_static(
            "default-src 'self'; script-src 'none'; style-src 'self'; \
             img-src 'self'; object-src 'none'; base-uri 'none'; \
             form-action 'self'; frame-ancestors 'none'",
        ),
    );
    res.headers_mut()
        .insert("x-robots-tag", HeaderValue::from_static("noindex"));
    res
}
```

- [ ] **Step 6: Run the GET tests**

```bash
uv run --with pytest --with playwright --with-requirements requirements-dev.txt \
  pytest tests/test_admin.py -q -k "admin_page_lists or status_message"
cd rust && cargo test -p viewer-rs admin_ui
```

Expected: the two Python tests pass; the 4 `admin_ui` tests still pass.

- [ ] **Step 7: Commit**

```bash
git add rust/viewer-rs/src/main.rs web/templates/admin.html web/static/style.css tests/test_admin.py
git commit -m "feat(admin): /admin にユーザー一覧と追加フォームを表示 (#613)"
```

---

### Task 4: Add / disable / enable POST handlers

**Files:**
- Modify: `tests/test_admin.py`
- Modify: `rust/viewer-rs/src/main.rs`

- [ ] **Step 1: Add the failing POST tests**

Append these tests after `test_admin_status_message_and_unknown_ignored`:

```python
def test_admin_add_member_then_disable_and_enable(ui_server, ui_state, ui_reset):
    status, headers, _ = _post_admin(ui_server, "add", NEW_USER)
    assert status == 303
    assert headers["location"] == "/admin?status=added"
    cp = users_cli(ui_state, "check", NEW_USER)
    assert cp.returncode == 0 and cp.stdout.strip() == "member"

    _, _, body = _request(ui_server, "GET", "/admin")
    row = _row(body, NEW_USER)
    assert b"/admin/users/disable" in row
    assert b"admin-status-active" in row

    status, headers, _ = _post_admin(ui_server, "disable", NEW_USER)
    assert status == 303
    assert headers["location"] == "/admin?status=disabled"
    cp = users_cli(ui_state, "check", NEW_USER)
    assert cp.returncode == 1 and cp.stdout.strip() == "disabled"
    _, _, body = _request(ui_server, "GET", "/admin")
    row = _row(body, NEW_USER)
    assert b"/admin/users/enable" in row
    assert b"admin-status-disabled" in row

    status, headers, _ = _post_admin(ui_server, "enable", NEW_USER)
    assert status == 303
    assert headers["location"] == "/admin?status=reactivated"
    cp = users_cli(ui_state, "check", NEW_USER)
    assert cp.returncode == 0 and cp.stdout.strip() == "member"


def test_admin_add_existing_member_is_idempotent(ui_server, ui_state, ui_reset):
    status, headers, _ = _post_admin(ui_server, "add", OUTSIDE)
    assert status == 303
    assert headers["location"] == "/admin?status=exists"
    cp = users_cli(ui_state, "check", OUTSIDE)
    assert cp.returncode == 0 and cp.stdout.strip() == "member"


def test_admin_add_admin_is_rejected(ui_server, ui_state, ui_reset):
    status, headers, _ = _post_admin(ui_server, "add", TEST_EMAIL)
    assert status == 303
    assert headers["location"] == "/admin?status=admin-readonly"
    cp = users_cli(ui_state, "check", TEST_EMAIL)
    assert cp.returncode == 0 and cp.stdout.strip() == "admin"


def test_admin_disable_admin_is_rejected(ui_server, ui_state, ui_reset):
    status, headers, _ = _post_admin(ui_server, "disable", TEST_EMAIL)
    assert status == 303
    assert headers["location"] == "/admin?status=admin-readonly"
    cp = users_cli(ui_state, "check", TEST_EMAIL)
    assert cp.returncode == 0 and cp.stdout.strip() == "admin"


def test_admin_invalid_email_and_xss_are_safe(ui_server, ui_state, ui_reset):
    status, headers, _ = _post_admin(ui_server, "add", "not-an-email")
    assert status == 303
    assert headers["location"] == "/admin?status=invalid"

    status, headers, _ = _post_admin(ui_server, "add", XSS_USER)
    assert status == 303
    assert headers["location"] == "/admin?status=added"
    _, _, body = _request(ui_server, "GET", "/admin")
    assert b"<img@example.com>" not in body
    assert b"&lt;img@example.com&gt;" in body


def test_admin_mutations_require_admin(ui_server, ui_state):
    users_cli(ui_state, "add", TEST_EMAIL, "--role", "member")
    try:
        status, _, _ = _post_admin(ui_server, "add", NEW_USER)
        assert status == 404
    finally:
        users_cli(ui_state, "add", TEST_EMAIL, "--role", "admin")


def test_admin_action_get_is_405(ui_server, ui_reset):
    status, headers, _ = _request(ui_server, "GET", "/admin/users/add")
    assert status == 405
    assert headers["allow"] == "POST"


def test_admin_csrf_matrix(ui_server, ui_reset):
    status, _, _ = _request(
        ui_server,
        "POST",
        "/admin/users/add",
        {"Content-Type": "application/x-www-form-urlencoded"},
        b"email=csrf-missing%40example.com",
    )
    assert status == 403

    for override in (
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
    ):
        status, _, _ = _post_admin(
            ui_server, "add", NEW_USER, _form_headers(ui_server, **override)
        )
        assert status == 403, override

    status, headers, _ = _post_admin(ui_server, "add", CSRF_USER)
    assert status == 303
    assert headers["location"] == "/admin?status=added"


def test_admin_post_body_limit(ui_server, ui_reset):
    status, _, _ = _post_admin(ui_server, "add", "a" * 9000 + "@example.com")
    assert status == 413
```

Add these real-auth tests after `test_auth_bearer_member_and_admin` (or immediately before `test_auth_corrupt_signed_session_is_500`):

```python
def test_admin_post_without_cookie_goes_to_login(auth_server):
    host = urllib.parse.urlsplit(auth_server).netloc
    status, headers, _ = _request(
        auth_server,
        "POST",
        "/admin/users/add",
        {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": f"http://{host}",
            "Sec-Fetch-Site": "same-origin",
        },
        b"email=x%40example.com",
    )
    assert status == 303
    assert headers["location"] == "/"


def test_admin_post_as_member_is_404(auth_server, auth_reset):
    headers = _cookie_header(TEST_EMAIL)
    headers.update(_form_headers(auth_server))
    status, _, _ = _request(
        auth_server,
        "POST",
        "/admin/users/add",
        headers,
        urllib.parse.urlencode({"email": OUTSIDE}).encode(),
    )
    assert status == 404


def test_admin_ui_disable_blocks_login(auth_server, auth_state):
    try:
        users_cli(auth_state, "add", TEST_EMAIL, "--role", "admin")
        users_cli(auth_state, "add", OUTSIDE)
        time.sleep(1.2)
        admin_headers = _cookie_header(TEST_EMAIL)
        admin_headers.update(_form_headers(auth_server))
        outside_headers = _cookie_header(OUTSIDE)
        _wait_api(auth_server, outside_headers, 200)

        status, headers, _ = _request(
            auth_server,
            "POST",
            "/admin/users/disable",
            admin_headers,
            urllib.parse.urlencode({"email": OUTSIDE}).encode(),
        )
        assert status == 303
        assert headers["location"] == "/admin?status=disabled"
        _wait_api(auth_server, outside_headers, 401)

        status, headers, _ = _request(
            auth_server,
            "POST",
            "/admin/users/enable",
            admin_headers,
            urllib.parse.urlencode({"email": OUTSIDE}).encode(),
        )
        assert status == 303
        assert headers["location"] == "/admin?status=reactivated"
        _wait_api(auth_server, outside_headers, 200)
    finally:
        users_cli(auth_state, "add", TEST_EMAIL, "--role", "member")
        users_cli(auth_state, "remove", OUTSIDE)
```

- [ ] **Step 2: Run the POST tests and confirm they fail**

```bash
uv run --with pytest --with playwright --with-requirements requirements-dev.txt \
  pytest tests/test_admin.py -q -k "admin_add_member_then or admin_add_admin or admin_disable_admin or admin_csrf or admin_action_get or admin_ui_disable or admin_post"
```

Expected: FAIL with 404/405 because the routes do not exist yet.

- [ ] **Step 3: Add the form type, action enum, and handlers**

In `rust/viewer-rs/src/main.rs`, insert this block immediately after the `admin_page` function (before `/// GET /`):

```rust
#[derive(serde::Deserialize)]
struct AdminEmailForm {
    email: String,
}

#[derive(Clone, Copy)]
enum MemberAction {
    Add,
    Disable,
    Enable,
}

fn admin_forbidden() -> Response {
    let mut res = state::detail(StatusCode::FORBIDDEN, "Forbidden");
    admin_private_headers(&mut res);
    res
}

fn admin_bad_request() -> Response {
    let mut res = state::detail(StatusCode::BAD_REQUEST, "invalid form");
    admin_private_headers(&mut res);
    res
}

fn admin_payload_too_large() -> Response {
    let mut res = state::detail(StatusCode::PAYLOAD_TOO_LARGE, "payload too large");
    admin_private_headers(&mut res);
    res
}

fn admin_redirect_status(status: &'static str) -> Response {
    let mut res = Response::builder()
        .status(StatusCode::SEE_OTHER)
        .header(header::LOCATION, format!("/admin?status={status}"))
        .body(Body::empty())
        .unwrap();
    admin_private_headers(&mut res);
    res
}

async fn admin_users_add(State(st): State<AppState>, req: Request<Body>) -> Response {
    admin_member_action(st, req, MemberAction::Add).await
}

async fn admin_users_disable(State(st): State<AppState>, req: Request<Body>) -> Response {
    admin_member_action(st, req, MemberAction::Disable).await
}

async fn admin_users_enable(State(st): State<AppState>, req: Request<Body>) -> Response {
    admin_member_action(st, req, MemberAction::Enable).await
}

async fn admin_member_action(
    st: AppState,
    req: Request<Body>,
    action: MemberAction,
) -> Response {
    if *req.method() != axum::http::Method::POST {
        return method_not_allowed("POST");
    }
    let headers = req.headers().clone();
    let actor = match admin_actor(&st, &headers).await {
        Ok(actor) => actor,
        Err(res) => return res,
    };
    if !admin_ui::same_origin_post(&headers) {
        return admin_forbidden();
    }
    let form = match axum::extract::Form::<AdminEmailForm>::from_request(req, &()).await {
        Ok(form) => form.0,
        Err(rejection) => {
            if rejection.into_response().status() == StatusCode::PAYLOAD_TOO_LARGE {
                return admin_payload_too_large();
            }
            return admin_bad_request();
        }
    };

    let store = st.users.clone();
    let email = form.email;
    let actor_email = actor.email;
    let result = tokio::task::spawn_blocking(move || match action {
        MemberAction::Add | MemberAction::Enable => {
            store.add_member(&email, &actor_email)
        }
        MemberAction::Disable => store.disable_member(&email),
    })
    .await;

    let change = match result {
        Ok(Ok(change)) => change,
        Ok(Err(users::UserError::InvalidEmail)) => return admin_redirect_status("invalid"),
        Ok(Err(e)) => {
            tracing::warn!(error = %e, "admin user update failed");
            return admin_internal_error("user update failed");
        }
        Err(e) => {
            tracing::warn!(error = %e, "admin user update task failed");
            return admin_internal_error("user update failed");
        }
    };

    let status = match (action, change) {
        (MemberAction::Add | MemberAction::Enable, users::Change::Added) => "added",
        (MemberAction::Add | MemberAction::Enable, users::Change::Activated) => {
            "reactivated"
        }
        (MemberAction::Add | MemberAction::Enable, users::Change::AlreadyPresent) => {
            "exists"
        }
        (MemberAction::Add | MemberAction::Enable, users::Change::AdminProtected) => {
            "admin-readonly"
        }
        (MemberAction::Disable, users::Change::Disabled) => "disabled",
        (MemberAction::Disable, users::Change::AlreadyDisabled) => "already-disabled",
        (MemberAction::Disable, users::Change::NotFound) => "not-found",
        (MemberAction::Disable, users::Change::AdminProtected) => "admin-readonly",
        (_, other) => {
            tracing::error!(?other, "unexpected admin store result");
            return admin_internal_error("user update failed");
        }
    };
    admin_redirect_status(status)
}
```

- [ ] **Step 4: Register the routes with an 8KiB body limit**

In the router in `rust/viewer-rs/src/main.rs`, immediately after the two `/admin` routes, add:

```rust
        // #613: member-only mutations. The body limit is scoped here so the
        // three tiny form posts cannot be used as a memory sink.
        .merge(
            Router::new()
                .route("/admin/users/add", any(admin_users_add))
                .route("/admin/users/disable", any(admin_users_disable))
                .route("/admin/users/enable", any(admin_users_enable))
                .route_layer(axum::extract::DefaultBodyLimit::max(8 * 1024)),
        )
```

- [ ] **Step 5: Run the POST tests**

```bash
uv run --with pytest --with playwright --with-requirements requirements-dev.txt \
  pytest tests/test_admin.py -q
```

Expected: all tests in `tests/test_admin.py` pass, including the existing auth matrix and the new UI tests.

- [ ] **Step 6: Run Rust tests**

```bash
cd rust
cargo test -p viewer-rs
```

Expected: all viewer-rs tests pass.

- [ ] **Step 7: Commit**

```bash
git add rust/viewer-rs/src/main.rs tests/test_admin.py
git commit -m "feat(admin): member の追加・無効化・再有効化 POST を追加 (#613)"
```

---

### Task 5: Documentation and source comments

**Files:**
- Modify: `rust/viewer-rs/src/users.rs`
- Modify: `api/user-state-schema.sql`
- Modify: `docs/user-state-db.md`
- Modify: `docs/deployment.md`
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `CLAUDE.md`
- Modify: `docs/glossary.md`

- [ ] **Step 1: Update the module and schema comments**

In `rust/viewer-rs/src/users.rs`, replace the header paragraph:

```rust
//! (`state::user_state_path`). Rows are written **only** by the offline
//! `viewer-rs users` CLI (host/container operator); there is no HTTP write
//! path. Granting membership/admin therefore sits on the same trust boundary
//! as reading `SECRET_KEY` / the OAuth client secret.
```

with:

```rust
//! (`state::user_state_path`). Admins are granted only by the offline
//! `viewer-rs users` CLI (host/container operator). The `/admin` UI can add,
//! disable and re-enable **members** over HTTP, but never changes an admin
//! row; admin grants stay on the same trust boundary as reading
//! `SECRET_KEY` / the OAuth client secret.
```

In `api/user-state-schema.sql`, replace:

```sql
-- Rows are written only by the offline CLI (`viewer-rs users`); there is no
-- HTTP write path and no env allowlist. `email` is normalized
```

with:

```sql
-- Admins are written only by the offline CLI (`viewer-rs users`). The
-- `/admin` UI can add/disable members over HTTP but cannot change an admin
-- row. `email` is normalized
```

In `docs/user-state-db.md`, replace:

```markdown
  Rows are written **only** by the offline `viewer-rs users` CLI; there
  is no HTTP write path. `email` is normalized (trim + lowercase)
```

with:

```markdown
  Admins are written **only** by the offline `viewer-rs users` CLI. The
  `/admin` UI can add, disable and re-enable members over HTTP, but it
  cannot change an admin row. `email` is normalized (trim + lowercase)
```

- [ ] **Step 2: Update the deployment guide**

In `docs/deployment.md`:

Replace the auth table cell:

```markdown
| 認証 | Google OAuth + `users` テーブル（CLI で登録） | なし（`BOOKQ_TEST_MODE=true`） |
```

with:

```markdown
| 認証 | Google OAuth + `users` テーブル（CLI / `/admin`） | なし（`BOOKQ_TEST_MODE=true`） |
```

Replace the first bullet of the user-management section:

```markdown
- 登録・role 変更・失効は **`viewer-rs users` CLI のみ**。HTTP の書込み経路はない。実行にはホスト/コンテナ操作権限が要る（`SECRET_KEY` と同じ信頼境界。distroless なので shell 不要）。
```

with:

```markdown
- admin の登録・role 変更・失効は **`viewer-rs users` CLI のみ**。HTTP から admin 行は変更できない。CLI の実行にはホスト/コンテナ操作権限が要る（`SECRET_KEY` と同じ信頼境界。distroless なので shell 不要）。
- member の追加・無効化・再有効化は `/admin` の UI からも行える（admin でログイン →「管理」）。UI の追加は常に member、既存 admin を指定した場合は拒否される。無効化は `disabled_at` による soft delete で、同じ画面から再有効化できる。POST は same-origin の Fetch Metadata / Origin を検証し、`/admin` は `Cache-Control: no-store`。
```

After the CLI examples block, add:

```markdown
`/admin` の UI で member を追加・無効化しても、`users list` / `users check` の結果は同じ DB に反映される。UI から追加した行は `created_by` に操作した admin の email、`note` に `admin-ui` が入る。
```

- [ ] **Step 3: Update README, AGENTS, CLAUDE, glossary**

In `README.md`, replace:

```markdown
fresh DB の `users` は空なので、**ログイン検証の前に** 少なくとも admin を 1 名登録する（サーバー再起動は不要）。登録は稼働中のコンテナで CLI から行う — HTTP の書込み経路はない:
```

with:

```markdown
fresh DB の `users` は空なので、**ログイン検証の前に** 少なくとも admin を 1 名登録する（サーバー再起動は不要）。admin の登録は稼働中のコンテナで CLI から行う（HTTP から admin は作成できない）:
```

Replace:

```markdown
一般ユーザーは `users add family@example.com`（既定 role=member）、失効は `users remove <email>`（ログイン自体を停止、`users add` で再活性化）。env の allowlist は廃止済みで、`users` CLI が唯一の登録経路。詳細は [docs/deployment.md](docs/deployment.md)「ユーザーと admin の管理」を参照。
```

with:

```markdown
一般ユーザーは `users add family@example.com`（既定 role=member）、失効は `users remove <email>`（ログイン自体を停止、`users add` で再活性化）。admin でログイン後は `/admin` から member の追加・無効化・再有効化もできる（admin の変更は CLI のみ）。env の allowlist は廃止済み。詳細は [docs/deployment.md](docs/deployment.md)「ユーザーと admin の管理」を参照。
```

In `AGENTS.md`, replace the user/admin bullet with:

```markdown
- ユーザーと admin は同じ DB の `users` (v3、`role=admin|member`)。**env のメール allowlist は廃止済み**。admin は `viewer-rs users add|remove|list|check` (HTTP 書込みなし、`docker exec` で実行、再起動不要) でのみ変更し、member は CLI に加えて `/admin` の UI から追加・無効化・再有効化できる (admin 行は UI から変更不可)。fresh DB は users が空なので、ログイン前に `users add <email> --role admin` で最初の admin を登録する。`/admin` は admin 以外 404・未認証は 303 `/`。dev は `BOOKQ_TEST_MODE=true` で mock user が常に認証済みになるため、dev に admin を付けると URL 到達者全員が `/admin` に入れる — 検証後は `users add test@example.com --role member` に戻す。詳細は `docs/deployment.md`「ユーザーと admin の管理」。
```

In `CLAUDE.md`, replace:

```markdown
Access control is the `users` table (`role=admin|member`), enrolled only through `viewer-rs users`; there is no env allowlist.
```

with:

```markdown
Access control is the `users` table (`role=admin|member`); admins are enrolled only through `viewer-rs users`, while the `/admin` UI can add/disable/re-enable members (never admin rows). There is no env allowlist.
```

In `docs/glossary.md`, replace:

```markdown
/ `users` テーブル（CLI enrollment）
```

with:

```markdown
/ `users` テーブル（admin は CLI enrollment、member は CLI または `/admin`）
```

- [ ] **Step 4: Run the full non-browser suite**

```bash
uv run --with pytest --with playwright --with-requirements requirements-dev.txt \
  pytest tests/ -q -n auto -m "not browser"
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add rust/viewer-rs/src/users.rs api/user-state-schema.sql docs/user-state-db.md \
  docs/deployment.md README.md AGENTS.md CLAUDE.md docs/glossary.md
git commit -m "docs(admin): 管理画面の member 操作と admin の CLI 限定を明記 (#613)"
```

---

### Task 6: Full verification and PR

**Files:** none (verification only)

- [ ] **Step 1: Format and run Rust tests**

```bash
cd rust
cargo fmt --all
cargo test -p viewer-rs
```

Expected: `test result: ok` for all viewer-rs tests.

- [ ] **Step 2: Run the browser suite**

```bash
uv run --with pytest --with playwright --with-requirements requirements-dev.txt \
  pytest tests/ -q -n auto -m browser --reruns 2 --reruns-delay 2
```

Expected: all browser tests pass.

- [ ] **Step 3: Optional live regression**

If a live library root is available:

```bash
BOOKQ_LIVE_ROOT=<path> ./scripts/live-regress.sh
```

Record the result in the PR. Do not put folder names in the PR or logs.

- [ ] **Step 4: Commit any formatting changes**

```bash
git status --short
git add -A
git commit -m "chore(admin): cargo fmt (#613)"
```

Only run this if `cargo fmt` changed files.

- [ ] **Step 5: Push and open the PR**

```bash
git push -u origin ai/613-admin-user-management-ui

gh pr create --base main --head ai/613-admin-user-management-ui \
  --title "feat(ai): /admin に通常ユーザーの追加・無効化 UI を追加 (#613)" \
  --body-file - <<'EOF'
Closes #613

## 変更内容

- `/admin` にユーザー一覧（email / role / status / 操作）と member 追加フォームを追加
- `POST /admin/users/add|disable|enable` を追加（HTML form + 303、JS なし）
- admin 行は読み取り専用。admin の role 変更・無効化は CLI のみ
- 無効化は既存 CLI と同じ `disabled_at`（soft delete）、再有効化可能
- POST は per-request の admin DB 確認 + Fetch Metadata / Origin 検証
- store 層で admin 行への操作を拒否し、UI の事前チェックに依存しない
- email は HTML エスケープ、本文は 8KiB 上限

## テスト

- Rust: `cargo test -p viewer-rs`
- Python: `pytest tests/ -q -n auto -m "not browser"`
- Browser: `pytest tests/ -q -n auto -m browser --reruns 2 --reruns-delay 2`
- `tests/test_admin.py`: 一覧 / 追加 / 重複 / 無効化 / 再有効化 / admin 保護 / 不正 email / XSS / CSRF / 認可 / body limit
- live 実データ回帰: 未実施（必要なら実施結果を追記）

## 影響範囲

- `users` テーブルの書き込み経路が CLI + `/admin`（member のみ）に増える
- schema 変更なし。既存ユーザー・role・disabled 状態は保持
EOF
```

- [ ] **Step 6: Wait for CI**

```bash
gh pr checks --watch
```

If a check fails, fix it in a new commit on the same branch and push again.

---

## Self-review checklist

- **Spec coverage:** member add/disable/enable → Task 1 + Task 4; admin read-only → Task 1 (store) + Task 2 (render) + Task 4 (handler); CSRF/XSS/body limit → Task 2 + Task 4; GET list/form → Task 3; docs → Task 5; full CI → Task 6.
- **No placeholders:** every code step contains the actual code or the exact replacement text.
- **Type consistency:** `Change::AdminProtected`, `add_member(email, created_by)`, `disable_member(email)`, `admin_ui::render(template, users, status)`, `admin_ui::same_origin_post(headers)`, `MemberAction::{Add,Disable,Enable}` are used consistently across tasks.
