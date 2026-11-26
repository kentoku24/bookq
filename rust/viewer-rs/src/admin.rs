//! #608: `/admin` — member management UI. Admins are CLI-only
//! (`viewer-rs users`); members can be added/disabled/re-enabled here.
//! Handlers are registered from `main.rs`'s router; `admin_actor` is the
//! shared gate (`caller()` + admin-role check) every route composes.

use std::collections::HashMap;

use axum::{
    body::Body,
    extract::{FromRequest, Query, Request, State},
    http::{header, HeaderValue, StatusCode},
    response::{IntoResponse, Response},
};

use crate::{
    admin_ui, auth, caller, corrupt_session, corrupt_session_response,
    method_not_allowed, state, users, AppState,
};

// ── #608: /admin ───────────────────────────────────────────────────────────

/// Headers shared by every `/admin` response: keep per-user HTML out of
/// shared/private caches, and stop MIME sniffing / referrer leakage even on
/// the 404 path.
fn admin_private_headers(res: &mut Response) {
    // `no-transform` also stops Cloudflare Email Obfuscation from replacing
    // the user list's addresses with "[email protected]" (#625). The CSP keeps
    // Cloudflare's decode script disabled, so the obfuscated text would stick.
    res.headers_mut().insert(
        header::CACHE_CONTROL,
        HeaderValue::from_static("no-store, no-transform"),
    );
    res.headers_mut()
        .insert(header::VARY, HeaderValue::from_static("Cookie"));
    res.headers_mut().insert(
        "x-content-type-options",
        HeaderValue::from_static("nosniff"),
    );
    res.headers_mut()
        .insert("referrer-policy", HeaderValue::from_static("same-origin"));
}

/// Unauthenticated browser navigation → the login page at `/`.
/// `/admin` is a page, not an API: a 401 JSON would be a dead end.
fn admin_redirect_login() -> Response {
    let mut res = Response::builder()
        .status(StatusCode::SEE_OTHER)
        .header(header::LOCATION, "/")
        .body(Body::empty())
        .unwrap();
    admin_private_headers(&mut res);
    res
}

/// A non-admin gets the same generic 404 shape as a missing page — the
/// admin surface is not advertised. The store error path uses this too
/// (fail closed) and only the server log records the cause.
fn admin_not_found() -> Response {
    let mut res = state::detail(StatusCode::NOT_FOUND, "Not Found");
    admin_private_headers(&mut res);
    res
}

/// Per-request admin lookup. Any failure (store error, blocking-task join
/// error) denies; the caller never sees the difference from "not an admin".
///
/// Role lookup only: `admin_actor` below composes `caller()` + this check and
/// is the shared gate for the `/admin` page and every mutation route.
pub(crate) async fn admin_check(st: &AppState, email: &str) -> bool {
    let email = email.trim().to_lowercase();
    if email.is_empty() {
        return false;
    }
    let store = st.users.clone();
    match tokio::task::spawn_blocking(move || store.is_admin(&email)).await {
        Ok(Ok(v)) => v,
        Ok(Err(e)) => {
            tracing::warn!(error = %e, "admin store unavailable — denying admin access");
            false
        }
        Err(e) => {
            tracing::warn!(error = %e, "admin check task failed — denying admin access");
            false
        }
    }
}

/// Shared admin gate for the page and every mutation handler. Returns the
/// acting identity on success; any failure is already a complete response.
async fn admin_actor(
    st: &AppState,
    headers: &axum::http::HeaderMap,
) -> Result<auth::Identity, Response> {
    if corrupt_session(st, headers) {
        let mut res = corrupt_session_response();
        admin_private_headers(&mut res);
        return Err(res);
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
pub(crate) async fn admin_page(State(st): State<AppState>, req: Request<Body>) -> Response {
    if !matches!(
        *req.method(),
        axum::http::Method::GET | axum::http::Method::HEAD
    ) {
        return admin_method_not_allowed("GET, HEAD");
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

/// The only field the three mutation forms carry.
#[derive(serde::Deserialize)]
struct AdminEmailForm {
    email: String,
}

/// The three member mutations. Admins are never targets (the store layer
/// refuses them with `Change::AdminProtected`) — they stay CLI-only.
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

fn admin_method_not_allowed(allow: &'static str) -> Response {
    let mut res = method_not_allowed(allow);
    admin_private_headers(&mut res);
    res
}

fn admin_payload_too_large() -> Response {
    let mut res = state::detail(StatusCode::PAYLOAD_TOO_LARGE, "payload too large");
    admin_private_headers(&mut res);
    res
}

/// 303 back to the list with a flash code (`admin_ui::flash_message` renders
/// it; unknown codes are silently ignored). POST-redirect-GET keeps reloads
/// from re-submitting the mutation.
fn admin_redirect_status(status: &'static str) -> Response {
    let mut res = Response::builder()
        .status(StatusCode::SEE_OTHER)
        .header(header::LOCATION, format!("/admin?status={status}"))
        .body(Body::empty())
        .unwrap();
    admin_private_headers(&mut res);
    res
}

pub(crate) async fn admin_users_add(State(st): State<AppState>, req: Request<Body>) -> Response {
    admin_member_action(st, req, MemberAction::Add).await
}

pub(crate) async fn admin_users_disable(State(st): State<AppState>, req: Request<Body>) -> Response {
    admin_member_action(st, req, MemberAction::Disable).await
}

pub(crate) async fn admin_users_enable(State(st): State<AppState>, req: Request<Body>) -> Response {
    admin_member_action(st, req, MemberAction::Enable).await
}

async fn admin_member_action(
    st: AppState,
    req: Request<Body>,
    action: MemberAction,
) -> Response {
    // Gate order matters: identity → role → CSRF → body. Nothing is read
    // (or written) for a caller who is not an admin on this same origin.
    if *req.method() != axum::http::Method::POST {
        return admin_method_not_allowed("POST");
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
    // `end_owner_sessions` matches stored (normalized) owner emails — a raw
    // form value with case/space differences would silently skip revocation.
    let target_email =
        users::normalize_email(&email).unwrap_or_else(|| email.trim().to_lowercase());
    let result = tokio::task::spawn_blocking(move || match action {
        MemberAction::Add | MemberAction::Enable => {
            store.add_member(&email, &actor_email)
        }
        MemberAction::Disable => store.disable_member(&email, &actor_email),
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
        // The actor was demoted/disabled after the page was rendered (the
        // store re-checks inside the write transaction): fail closed with the
        // same generic 404 a non-admin gets, so the surface stays hidden.
        (_, users::Change::NotAuthorized) => return admin_not_found(),
        (_, other) => {
            tracing::error!(?other, "unexpected admin store result");
            return admin_internal_error("user update failed");
        }
    };
    if matches!(action, MemberAction::Disable) && matches!(change, users::Change::Disabled) {
        // Grants are bearer material, not capability lookups — a disable
        // must revoke them outright or disable→re-enable would resurrect
        // live guest links. Their sessions end too (a guest's whole
        // reading surface is the pair session).
        let ended = st.registry.end_owner_sessions(&target_email).await;
        if ended > 0 {
            tracing::info!(email = %target_email, ended, "ended pair sessions for disabled member");
        }
    }
    admin_redirect_status(status)
}
