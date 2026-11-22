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
fn escape_html(raw: &str) -> String {
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
/// `Sec-Fetch-Site` / `Origin`; every present signal must be valid UTF-8 and
/// same-origin. `oauth::same_site_initiation` validates the present values.
pub(crate) fn same_origin_post(headers: &HeaderMap) -> bool {
    let has_signal = headers.contains_key("sec-fetch-site") || headers.contains_key(header::ORIGIN);
    let parsed = headers
        .get("sec-fetch-site")
        .is_none_or(|value| value.to_str().is_ok());
    parsed && has_signal && crate::oauth::same_site_initiation(headers)
}

fn render_row(row: &UserRow) -> String {
    let email = escape_html(&row.email);
    let role = row.role.as_str();
    let state = if row.active() { "active" } else { "disabled" };
    let action = match (row.role, row.active()) {
        (Role::Admin, _) => r#"<span class="admin-note">CLI で管理</span>"#.to_string(),
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

/// Fill the admin shell's `{{ADMIN_FLASH}}` and `{{ADMIN_USERS}}`
/// placeholders. `status` is the query value; only known codes produce a
/// flash, and the rendered message is escaped too. Rows are inserted last so
/// a `{{...}}` sequence in an email cannot be re-substituted.
pub(crate) fn render(template: &str, users: &[UserRow], status: Option<&str>) -> String {
    let flash = match status.and_then(flash_message) {
        Some(message) => format!(
            r#"<p class="admin-flash" role="status">{}</p>"#,
            escape_html(message)
        ),
        None => String::new(),
    };
    let rows: String = users.iter().map(render_row).collect();
    // Flash first, rows last: row content must never be re-scanned for
    // placeholders.
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
        for &(name, value) in pairs {
            map.insert(name, HeaderValue::from_static(value));
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
            user("disabled-admin@example.com", Role::Admin, false),
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

        let disabled_admin = body
            .lines()
            .find(|line| line.contains("disabled-admin@example.com"))
            .unwrap();
        assert!(!disabled_admin.contains(r#"action="/admin/users/"#));
        assert!(disabled_admin.contains("CLI で管理"));
        assert!(disabled_admin.contains("admin-status-disabled"));
    }

    #[test]
    fn escapes_quotes_in_hidden_inputs() {
        let quoted = user(r#"q"uote@example.com"#, Role::Member, true);
        let apostrophe = user("o'neil@example.com", Role::Member, true);
        let body = render("{{ADMIN_USERS}}", &[quoted, apostrophe], None);
        assert!(body.contains(r#"value="q&quot;uote@example.com""#));
        assert!(!body.contains(r#"value="q"uote@example.com"#));
        assert!(body.contains(r#"value="o&#39;neil@example.com""#));
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
        assert!(same_origin_post(&headers(&[(
            "sec-fetch-site",
            "same-origin"
        )])));
        assert!(same_origin_post(&headers(&[("sec-fetch-site", "none")])));
        assert!(!same_origin_post(&headers(&[])));
        assert!(!same_origin_post(&headers(&[(
            "sec-fetch-site",
            "cross-site"
        )])));
        assert!(!same_origin_post(&headers(&[(
            "sec-fetch-site",
            "same-site"
        )])));
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

        let mut invalid_utf8 = HeaderMap::new();
        invalid_utf8.insert("sec-fetch-site", HeaderValue::from_bytes(b"\xff").unwrap());
        assert!(!same_origin_post(&invalid_utf8));
    }
}
