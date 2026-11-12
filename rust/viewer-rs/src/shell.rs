//! `/`, `/sw.js`, `/favicon.ico`, `/static/*` — the Jinja shell and the
//! starlette `StaticFiles`/`FileResponse` surface (#401).
//!
//! `GET /` renders `app/templates/index.html`: the `{% if not user %}`
//! branch is a login page (200, not a redirect), the `{% else %}` branch is
//! the viewer shell with per-user globals embedded. The static routes are
//! public — they answer regardless of session state (a corrupt signed
//! session still delegates: Python's middleware 500s on it before any route
//! runs). Static responses carry starlette's stat headers (etag =
//! `md5("{st_mtime}-{st_size}")`), conditional 304, single/multi-range 206,
//! and the `NoCacheStaticFiles` cache-control. `/static/*` and `/sw.js` sit
//! under the gzip prefixes; `/` and `/favicon.ico` do not.

use crate::{
    auth,
    serve::{self, Serve},
    state,
};
use axum::{
    body::Body,
    http::{header, HeaderMap, HeaderName, HeaderValue, StatusCode, Uri},
    response::Response,
};
use serde_json::Value;
use std::collections::HashSet;
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};

// ── caller for `/` ──────────────────────────────────────────────────────────

/// `get_current_user` reduced to the three outcomes the shell template
/// cares about. Unlike the API handlers, an unidentified caller is NOT a
/// delegate — upstream answers the login page (200), and so do we.
pub(crate) enum ShellUser {
    /// Falsy session/bearer user (or none) → the `{% if not user %}` page.
    Login,
    /// Truthy user. `user_key` is `user_key_for_email(user.get("email"))`:
    /// `strip().lower()` of a string email, else `None` — the viewer shell
    /// still renders, with `null` embedded globals.
    Viewer { user_key: Option<String> },
    /// We cannot decide natively (a signed session whose payload is not a
    /// dict — Python's middleware raises on `json.loads`, producing a 500
    /// upstream). Defer so the error surface matches.
    Proxy,
}

/// Order mirrors `user_from_connection`: TEST_MODE mock → session `user` →
/// Bearer. A signed non-dict/undecodable session defers (upstream 500).
pub(crate) fn index_caller(
    test_mode: bool,
    secret: &str,
    session_key: Option<&auth::SessionKey>,
    allowlist: &HashSet<String>,
    headers: &HeaderMap,
) -> ShellUser {
    if test_mode {
        return ShellUser::Viewer {
            user_key: Some(auth::TEST_USER_EMAIL.to_string()),
        };
    }
    match auth::session_lookup(headers, session_key) {
        auth::SessionLookup::Corrupt => return ShellUser::Proxy,
        auth::SessionLookup::User(u) => {
            // `{% if not user %}`: the session value's own truthiness decides
            // the branch — an empty object renders the login page, any
            // truthy non-dict renders the viewer with null globals.
            if !json_truthy(&u) {
                return ShellUser::Login;
            }
            return ShellUser::Viewer {
                user_key: user_key(&u),
            };
        }
        auth::SessionLookup::Absent | auth::SessionLookup::NoUser => {}
    }
    match auth::bearer_token(headers).and_then(|t| auth::validate_bearer(t, secret, allowlist)) {
        Some(id) => {
            let key = id.email.trim().to_lowercase();
            ShellUser::Viewer {
                user_key: (!key.is_empty()).then_some(key),
            }
        }
        None => ShellUser::Login,
    }
}

/// `user_key_for_email(user.get("email"))` for a truthy session user:
/// string email → `strip().lower()` (None when empty); anything else → None.
fn user_key(user: &Value) -> Option<String> {
    let email = user.get("email")?.as_str()?;
    let key = email.trim().to_lowercase();
    (!key.is_empty()).then_some(key)
}

/// Python/Jinja truthiness: falsy = null, false, 0, "", [], {}.
fn json_truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_some_and(|f| f != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(m) => !m.is_empty(),
    }
}

// ── Jinja rendering (literal-marker substitution) ───────────────────────────
//
// index.html uses a small fixed Jinja surface, rendered here by literal
// replacement so the template file stays the single source. Markers:
//   {% if not user %} … {% else %} … {% endif %}   (outer branch, EOF-end)
//   {{ 'ua-android' if ua_android else '' }}
//   {{ 'true' if test_mode else 'false' }}
//   {{ <name> | tojson }}                          (htmlsafe_json_dumps)
//   {% if thumbnail_perf %} … {% endif %}          (two flat blocks)
//   {% include "thumbnail_perf.html" %}
//   {{ thumbnail_probe | safe }}                   (verbatim insert)
//   {# … #}                                        (comments, stripped)
// Anything left over after substitution (`{{`/`{%`/`{#`) means the template
// grew a construct we don't port — the caller falls back to upstream
// rather than emit raw Jinja.

pub(crate) struct IndexCtx<'a> {
    /// `user_key_for_email` — feeds `visibility_owner` + `progress_user_key`.
    pub user_key: Option<String>,
    pub owner: Option<String>,
    /// Ordered `{"owner", "revision", "hiddenRoots"}` or null.
    pub snapshot: Value,
    pub test_mode: bool,
    pub ua_android: bool,
    /// `(thumbnail_probe_js, thumbnail_perf_html)` when `?thumb_perf=1`.
    pub perf: Option<(&'a str, &'a str)>,
}

const IF_NOT_USER: &str = "{% if not user %}";
const ELSE_TAG: &str = "{% else %}";
const ENDIF_TAG: &str = "{% endif %}";
const UA_EXPR: &str = "{{ 'ua-android' if ua_android else '' }}";
const TM_EXPR: &str = "{{ 'true' if test_mode else 'false' }}";
const OWNER_EXPR: &str = "{{ visibility_owner | tojson }}";
const SNAP_EXPR: &str = "{{ visibility_snapshot | tojson }}";
const PUKEY_EXPR: &str = "{{ progress_user_key | tojson }}";
const PERF_IF: &str = "{% if thumbnail_perf %}";
const PROBE_EXPR: &str = "{{ thumbnail_probe | safe }}";
const INCLUDE_EXPR: &str = "{% include \"thumbnail_perf.html\" %}";

/// Render `index.html` for one caller state. `None` when the template uses
/// Jinja constructs we don't port — the handler proxies upstream instead.
pub(crate) fn render_index(src: &str, ctx: &IndexCtx) -> Option<String> {
    let src = strip_comments(src)?;
    let rest = src.strip_prefix(IF_NOT_USER)?;
    let else_at = rest.find(ELSE_TAG)?;
    let login = &rest[..else_at];
    let tail = &rest[else_at + ELSE_TAG.len()..];
    let end = tail.rfind(ENDIF_TAG)?;
    let viewer = &tail[..end];

    let _ = login;
    let mut out = viewer
        .replace(UA_EXPR, if ctx.ua_android { "ua-android" } else { "" })
        .replace(TM_EXPR, if ctx.test_mode { "true" } else { "false" })
        .replace(OWNER_EXPR, &jinja_tojson(&opt_json(&ctx.owner)))
        .replace(SNAP_EXPR, &jinja_tojson(&ctx.snapshot))
        .replace(PUKEY_EXPR, &jinja_tojson(&opt_json(&ctx.user_key)));

    // {% if thumbnail_perf %}…{% endif %} — flat blocks; inner substitutions.
    while let Some(start) = out.find(PERF_IF) {
        let body_start = start + PERF_IF.len();
        let rel_end = out[body_start..].find(ENDIF_TAG)?;
        let inner = out[body_start..body_start + rel_end].to_string();
        let rendered = match ctx.perf {
            // `| safe` inserts verbatim; the `{% include %}` renders its
            // sub-template — Jinja strips comments AND one trailing newline
            // (keep_trailing_newline=False) from the included source.
            Some((probe, perf)) => strip_comments(&inner)?
                .replace(PROBE_EXPR, probe)
                .replace(INCLUDE_EXPR, &strip_comments(strip_trailing_nl(perf))?),
            None => String::new(),
        };
        out.replace_range(start..body_start + rel_end + ENDIF_TAG.len(), &rendered);
    }
    guarded(&out)
}

/// Jinja `keep_trailing_newline=False`: drop exactly one final `\n`.
fn strip_trailing_nl(s: &str) -> &str {
    s.strip_suffix('\n').unwrap_or(s)
}

/// `None` when Jinja constructs remain — the caller must proxy.
fn guarded(out: &str) -> Option<String> {
    (!out.contains("{{") && !out.contains("{%") && !out.contains("{#")).then(|| out.to_string())
}

/// `json.dumps` (ensure_ascii, `', '`/`': '` separators) then the tojson
/// HTML-safety escapes (`<>&'` → `\uXXXX`), verbatim jinja2 semantics.
pub(crate) fn jinja_tojson(v: &Value) -> String {
    let mut s = String::new();
    py_dumps(v, &mut s);
    s.replace('<', "\\u003c")
        .replace('>', "\\u003e")
        .replace('&', "\\u0026")
        .replace('\'', "\\u0027")
}

fn opt_json(v: &Option<String>) -> Value {
    v.as_ref().map(|s| Value::String(s.clone())).unwrap_or(Value::Null)
}

fn py_dumps(v: &Value, out: &mut String) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                out.push_str(&i.to_string());
            } else if let Some(u) = n.as_u64() {
                out.push_str(&u.to_string());
            } else {
                out.push_str(&py_float_str(n.as_f64().unwrap_or(0.0)));
            }
        }
        Value::String(s) => py_str(s, out),
        Value::Array(a) => {
            out.push('[');
            for (i, x) in a.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                py_dumps(x, out);
            }
            out.push(']');
        }
        Value::Object(m) => {
            // jinja2's tojson passes sort_keys=True (policies default), so
            // object keys always serialize alphabetically — NOT insertion
            // order. Nested objects sort recursively the same way.
            let mut keys: Vec<&String> = m.keys().collect();
            keys.sort();
            out.push('{');
            for (i, k) in keys.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                py_str(k, out);
                out.push_str(": ");
                py_dumps(&m[*k], out);
            }
            out.push('}');
        }
    }
}

/// `json.dumps` string escaping with `ensure_ascii`: `"`/`\` and C0
/// escapes, everything outside 0x20–0x7e as `\uXXXX` UTF-16 units.
fn py_str(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => {
                out.push_str(&format!("\\u{:04x}", c as u32));
            }
            c if (c as u32) < 0x7f => out.push(c),
            c => {
                let mut buf = [0u16; 2];
                for u in c.encode_utf16(&mut buf) {
                    out.push_str(&format!("\\u{:04x}", u));
                }
            }
        }
    }
    out.push('"');
}

/// `str(float)` for the ranges we emit: shortest round-trip digits, always
/// with a decimal point (Rust prints `1` where Python prints `1.0`).
/// mtimes/revisions stay far below the `1e16` exponent cutoff.
fn py_float_str(f: f64) -> String {
    let s = format!("{f}");
    if s.contains(['.', 'e', 'E']) || s.contains("inf") || s.contains("NaN") {
        s
    } else {
        format!("{s}.0")
    }
}

/// Strip `{#…#}` comments (they may span lines). Unterminated comment is a
/// Jinja error upstream → `None` → proxy.
fn strip_comments(src: &str) -> Option<String> {
    if !src.contains("{#") {
        return Some(src.to_string());
    }
    let mut out = String::with_capacity(src.len());
    let mut rest = src;
    while let Some(s) = rest.find("{#") {
        out.push_str(&rest[..s]);
        rest = &rest[s + 2..];
        let e = rest.find("#}")?;
        rest = &rest[e + 2..];
    }
    out.push_str(rest);
    Some(out)
}

/// First `name` value in the query string (QueryParams.get → first wins),
/// percent/`+` decoded. `?thumb_perf=1&thumb_perf=0` still reads `1`.
pub(crate) fn first_query_param(uri: &Uri, name: &str) -> Option<String> {
    for pair in uri.query()?.split('&') {
        let (k, v) = pair.split_once('=').unwrap_or((pair, ""));
        if form_decode(k) == name {
            return Some(form_decode(v));
        }
    }
    None
}

fn form_decode(s: &str) -> String {
    let s = s.replace('+', " ");
    percent_encoding::percent_decode_str(&s)
        .decode_utf8()
        .map(|c| c.into_owned())
        .unwrap_or_else(|_| s.clone())
}

// ── static file serving (starlette StaticFiles/FileResponse) ────────────────

/// `GET /` — renders the shell for the resolved caller. File/template
/// failures proxy (upstream raises its own 500 surface).
pub(crate) fn index(
    app_dir: &Path,
    test_mode: bool,
    secret: &str,
    visibility: &state::VisibilityStore,
    user: &ShellUser,
    headers: &HeaderMap,
    thumb_perf: bool,
    head: bool,
) -> Serve {
    let (viewer, user_key) = match user {
        ShellUser::Login => (false, None),
        ShellUser::Viewer { user_key } => (true, user_key.clone()),
        ShellUser::Proxy => return Serve::Proxy,
    };
    let Ok(template) = std::fs::read_to_string(app_dir.join("templates").join("index.html")) else {
        return Serve::Proxy;
    };
    // `?thumb_perf=1` reads the probe + panel sources only when enabled.
    let (probe, perf_tpl) = if thumb_perf {
        let Ok(p) = std::fs::read_to_string(app_dir.join("perf").join("thumbnail-probe.js")) else {
            return Serve::Proxy;
        };
        let Ok(t) = std::fs::read_to_string(app_dir.join("templates").join("thumbnail_perf.html"))
        else {
            return Serve::Proxy;
        };
        (Some(p), Some(t))
    } else {
        (None, None)
    };

    let owner = user_key
        .as_deref()
        .map(|k| state::visibility_owner(k, secret));
    let snapshot = owner
        .as_ref()
        .and_then(|o| {
            visibility.read(state::GLOBAL_USER_KEY).ok().map(|s| {
                serde_json::json!({
                    "owner": o,
                    "revision": s.revision,
                    "hiddenRoots": s.hidden_roots,
                })
            })
        })
        .unwrap_or(Value::Null);
    let ua_android = headers
        .get(header::USER_AGENT)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_lowercase()
        .contains("android");

    let ctx = IndexCtx {
        user_key,
        owner,
        snapshot,
        test_mode,
        ua_android,
        perf: probe.as_deref().zip(perf_tpl.as_deref()),
    };
    let login = !viewer;
    let Some(body) = render_index_for(login, &template, &ctx) else {
        return Serve::Proxy;
    };
    let mut h: Vec<(HeaderName, HeaderValue)> = Vec::new();
    push(&mut h, "content-length", &body.len().to_string());
    push(&mut h, "content-type", "text/html; charset=utf-8");
    push(
        &mut h,
        "cache-control",
        if thumb_perf { "no-store" } else { "private, no-cache" },
    );
    // `session.get("user")` is touched on every non-test request upstream
    // (SessionMiddleware then emits `Vary: Cookie` — even with no cookie).
    if !test_mode {
        push(&mut h, "vary", "Cookie");
    }
    Serve::Done(finish(h, if head { Vec::new() } else { body.into_bytes() }, StatusCode::OK))
}

/// Dispatch on the login/viewer branch without complicating `IndexCtx`.
fn render_index_for(login: bool, src: &str, ctx: &IndexCtx) -> Option<String> {
    if login {
        // The login branch ignores every context value.
        let src = strip_comments(src)?;
        let rest = src.strip_prefix(IF_NOT_USER)?;
        let else_at = rest.find(ELSE_TAG)?;
        guarded(&rest[..else_at])
    } else {
        render_index(src, ctx)
    }
}

/// `/sw.js`, `/favicon.ico` — `FileResponse` with explicit media type and
/// headers dict; the `detail` body on a missing file matches HTTPException.
pub(crate) fn named_file(
    path: &Path,
    media_type: &str,
    extra: &[(&str, &str)],
    missing_detail: &str,
    req: &HeaderMap,
    head: bool,
) -> Serve {
    let Ok(md) = std::fs::metadata(path) else {
        return Serve::Done(state::detail(StatusCode::NOT_FOUND, missing_detail));
    };
    match file_response(path, &md, media_type, extra, &[], req, head) {
        Ok(res) => Serve::Done(res),
        Err(_) => Serve::Proxy,
    }
}

/// `/static/{*path}` — `NoCacheStaticFiles`: FileResponse + `Cache-Control:
/// no-cache` appended after the stat headers.
pub(crate) fn static_file(static_dir: &Path, rel: &str, req: &HeaderMap, head: bool) -> Serve {
    if rel.starts_with(['/', '\\']) {
        return Serve::Done(state::detail(StatusCode::NOT_FOUND, "Not Found"));
    }
    let base = match serve::realpath(static_dir) {
        Ok(b) => b,
        Err(_) => return Serve::Proxy,
    };
    let full = match serve::realpath(&base.join(rel)) {
        Ok(p) => p,
        Err(_) => return Serve::Proxy,
    };
    if !full.starts_with(&base) {
        return Serve::Done(state::detail(StatusCode::NOT_FOUND, "Not Found"));
    }
    let md = match std::fs::metadata(&full) {
        Ok(m) if m.is_file() => m,
        Ok(_) => return Serve::Done(state::detail(StatusCode::NOT_FOUND, "Not Found")),
        Err(e) => {
            return match e.kind() {
                std::io::ErrorKind::NotFound | std::io::ErrorKind::NotADirectory => {
                    Serve::Done(state::detail(StatusCode::NOT_FOUND, "Not Found"))
                }
                std::io::ErrorKind::PermissionDenied => {
                    Serve::Done(state::detail(StatusCode::UNAUTHORIZED, "Unauthorized"))
                }
                _ => Serve::Proxy,
            };
        }
    };
    match file_response(&full, &md, mime_for(&full), &[], NO_CACHE, req, head) {
        Ok(res) => Serve::Done(res),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            Serve::Done(state::detail(StatusCode::NOT_FOUND, "Not Found"))
        }
        Err(_) => Serve::Proxy,
    }
}

const NO_CACHE: &[(&str, &str)] = &[("cache-control", "no-cache")];
pub(crate) const SW_HEADERS: &[(&str, &str)] = &[
    ("service-worker-allowed", "/"),
    ("cache-control", "no-cache"),
];
pub(crate) const FAVICON_HEADERS: &[(&str, &str)] =
    &[("cache-control", "public, max-age=86400")];

/// `FileResponse`: init_headers order is [extra dict] → content-type →
/// accept-ranges → content-length → last-modified → etag; NoCacheStaticFiles
/// then appends its cache-control. Conditional 304 keeps only
/// NOT_MODIFIED_HEADERS, in original order.
fn file_response(
    path: &Path,
    md: &std::fs::Metadata,
    media_type: &str,
    pre: &[(&str, &str)],
    post: &[(&str, &str)],
    req: &HeaderMap,
    head: bool,
) -> std::io::Result<Response> {
    let bytes = std::fs::read(path)?;
    let size = md.len();
    let mtime = md
        .modified()
        .ok()
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    let last_modified = httpdate::fmt_http_date(
        std::time::UNIX_EPOCH + std::time::Duration::from_secs_f64(mtime),
    );
    let etag_base = format!("{}-{}", py_float_str(mtime), size);
    let etag = format!("\"{:x}\"", <md5::Md5 as md5::Digest>::digest(etag_base.as_bytes()));

    let content_type = if media_type.starts_with("text/") && !media_type.contains("charset=") {
        format!("{media_type}; charset=utf-8")
    } else {
        media_type.to_string()
    };

    let mut h: Vec<(HeaderName, HeaderValue)> = Vec::new();
    for (k, v) in pre {
        push(&mut h, k, v);
    }
    push(&mut h, "content-type", &content_type);
    push(&mut h, "accept-ranges", "bytes");
    push(&mut h, "content-length", &size.to_string());
    push(&mut h, "last-modified", &last_modified);
    push(&mut h, "etag", &etag);
    for (k, v) in post {
        push(&mut h, k, v);
    }

    if is_not_modified(req, &etag, &last_modified) {
        const KEEP: [&str; 6] =
            ["cache-control", "content-location", "date", "etag", "expires", "vary"];
        let mut res = Response::builder().status(StatusCode::NOT_MODIFIED).body(Body::empty()).unwrap();
        for (n, v) in &h {
            if KEEP.contains(&n.as_str()) {
                res.headers_mut().append(n.clone(), v.clone());
            }
        }
        return Ok(res);
    }

    // Range applies to GET/HEAD only; `if-range` must equal last-modified
    // or etag exactly (`W/` prefix suppresses range entirely).
    let use_range = match (
        req.get(header::RANGE).and_then(|v| v.to_str().ok()),
        req.get(header::IF_RANGE).and_then(|v| v.to_str().ok()),
    ) {
        (Some(_), Some(if_range)) => {
            !if_range.starts_with("W/") && (if_range == last_modified || if_range == etag)
        }
        (Some(_), None) => true,
        (None, _) => false,
    };

    if !use_range {
        return Ok(finish(h, if head { Vec::new() } else { bytes }, StatusCode::OK));
    }
    let range_hdr = req.get(header::RANGE).unwrap().to_str().unwrap_or("");
    match parse_range(range_hdr, size) {
        RangePlan::Full => Ok(finish(h, if head { Vec::new() } else { bytes }, StatusCode::OK)),
        RangePlan::Malformed(msg) => Ok(plaintext(StatusCode::BAD_REQUEST, msg, &[])),
        RangePlan::Unsatisfiable => Ok(plaintext(
            StatusCode::RANGE_NOT_SATISFIABLE,
            "",
            &[("content-range".into(), format!("bytes */{size}"))],
        )),
        RangePlan::Ranges(mut ranges) if ranges.len() == 1 => {
            let (s, e) = ranges.remove(0);
            set_header(&mut h, "content-length", &(e - s).to_string());
            push(&mut h, "content-range", &format!("bytes {s}-{}/{size}", e - 1));
            let body = if head { Vec::new() } else { bytes[s as usize..e as usize].to_vec() };
            Ok(finish(h, body, StatusCode::PARTIAL_CONTENT))
        }
        RangePlan::Ranges(ranges) => {
            let boundary = boundary();
            set_header(&mut h, "content-type", &format!("multipart/byteranges; boundary={boundary}"));
            let mut body: Vec<u8> = Vec::new();
            for (s, e) in &ranges {
                body.extend_from_slice(
                    format!("--{boundary}\r\nContent-Type: {content_type}\r\nContent-Range: bytes {s}-{}/{size}\r\n\r\n", e - 1)
                        .as_bytes(),
                );
                body.extend_from_slice(&bytes[*s as usize..*e as usize]);
                body.extend_from_slice(b"\r\n");
            }
            body.extend_from_slice(format!("--{boundary}--").as_bytes());
            set_header(&mut h, "content-length", &body.len().to_string());
            Ok(finish(h, if head { Vec::new() } else { body }, StatusCode::PARTIAL_CONTENT))
        }
    }
}

/// `is_not_modified`: `if-none-match` first (`*` or exact-etag list with
/// `W/` stripped per tag), else `if-modified-since >= last-modified`.
fn is_not_modified(req: &HeaderMap, etag: &str, last_modified: &str) -> bool {
    if let Some(inm) = req
        .get(header::IF_NONE_MATCH)
        .and_then(|v| v.to_str().ok())
        .filter(|s| !s.is_empty())
    {
        if inm.trim() == "*" {
            return true;
        }
        return inm
            .split(',')
            .any(|t| t.trim().strip_prefix("W/").unwrap_or_else(|| t.trim()) == etag);
    }
    req.get(header::IF_MODIFIED_SINCE)
        .and_then(|v| v.to_str().ok())
        .and_then(|s| httpdate::parse_http_date(s).ok())
        .zip(httpdate::parse_http_date(last_modified).ok())
        .is_some_and(|(ims, lm)| ims >= lm)
}

// ── Range parsing (starlette `_parse_range_header`) ─────────────────────────

enum RangePlan {
    Full,
    Ranges(Vec<(u64, u64)>),
    Malformed(&'static str),
    Unsatisfiable,
}

const MAX_RANGES: usize = 100;

fn parse_range(hdr: &str, size: u64) -> RangePlan {
    let Some((units, range_)) = hdr.split_once('=') else {
        return RangePlan::Malformed("Malformed range header.");
    };
    if units.trim().to_lowercase() != "bytes" {
        return RangePlan::Malformed("Only support bytes range");
    }
    if range_.matches(',').count() + 1 > MAX_RANGES {
        return RangePlan::Full;
    }
    let mut ranges: Vec<(i64, i64)> = Vec::new();
    for part in range_.split(',') {
        let part = part.trim();
        if part.is_empty() || part == "-" || !part.contains('-') {
            continue;
        }
        let (ss, es) = part.split_once('-').unwrap();
        let (ss, es) = (ss.trim(), es.trim());
        // One `try:` block upstream — any parse failure skips the part.
        let parsed = (|| {
            if ss.is_empty() {
                let e = py_int(es)?;
                Some((std::cmp::max(size as i64 - e, 0), size as i64))
            } else {
                let s = py_int(ss)?;
                let e = if es.is_empty() {
                    size as i64
                } else {
                    match py_int(es) {
                        Some(ei) if ei < size as i64 => ei + 1,
                        Some(_) => size as i64,
                        None => return None,
                    }
                };
                Some((s, e))
            }
        })();
        if let Some(r) = parsed {
            ranges.push(r);
        }
    }
    if ranges.is_empty() {
        return RangePlan::Malformed("Range header: range must be requested");
    }
    if ranges.iter().any(|&(s, _)| !(0..size as i64).contains(&s)) {
        return RangePlan::Unsatisfiable;
    }
    if ranges.iter().any(|&(s, e)| s >= e) {
        return RangePlan::Malformed("Range header: start must be less than end");
    }
    if ranges.len() == 1 {
        let (s, e) = ranges[0];
        return RangePlan::Ranges(vec![(s as u64, e as u64)]);
    }
    ranges.sort();
    let mut merged: Vec<(u64, u64)> = vec![(ranges[0].0 as u64, ranges[0].1 as u64)];
    for &(s, e) in &ranges[1..] {
        let last = merged.last_mut().unwrap();
        if s as u64 <= last.1 {
            last.1 = last.1.max(e as u64);
        } else {
            merged.push((s as u64, e as u64));
        }
    }
    RangePlan::Ranges(merged)
}

/// `int(str)`: optional sign, digits, single inner underscores; the whole
/// string must match (no trailing junk). Overflow saturates — an out-of-i64
/// bound still takes the unsatisfiable/clamped path like Python's bigint.
fn py_int(s: &str) -> Option<i64> {
    let t = s.trim();
    let (neg, rest) = match t.strip_prefix('-') {
        Some(r) => (true, r),
        None => (false, t.strip_prefix('+').unwrap_or(t)),
    };
    if rest.is_empty()
        || !rest.chars().all(|c| c.is_ascii_digit() || c == '_')
        || rest.starts_with('_')
        || rest.ends_with('_')
        || rest.contains("__")
    {
        return None;
    }
    let digits: String = rest.chars().filter(|&c| c != '_').collect();
    if neg {
        digits.parse::<i64>().ok().map(|v| -v).or(Some(i64::MIN))
    } else {
        digits.parse::<i64>().ok().or(Some(i64::MAX))
    }
}

/// `secrets.token_hex(13)` — 26 lowercase hex chars. Uniqueness per
/// response suffices; sha256 of (nanos, counter) is unpredictable enough.
fn boundary() -> String {
    static CTR: AtomicU64 = AtomicU64::new(0);
    use sha2::Digest;
    let mut h = sha2::Sha256::new();
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    h.update(nanos.to_be_bytes());
    h.update(CTR.fetch_add(1, Ordering::Relaxed).to_be_bytes());
    let d = h.finalize();
    d[..13].iter().map(|b| format!("{b:02x}")).collect()
}

// ── media types (python:3.14-slim mimetypes, no /etc/mime.types) ────────────

/// `mimetypes.guess_type` for the shell's files — the deployed image has no
/// system mime table, so this mirrors CPython's builtin map exactly for the
/// extensions that exist (verified: `.ico`→vnd.microsoft.icon, `.ts`→None).
fn mime_for(path: &Path) -> &'static str {
    let ext = path
        .extension()
        .and_then(|e| e.to_str())
        .map(|e| e.to_lowercase())
        .unwrap_or_default();
    match ext.as_str() {
        "js" | "mjs" => "text/javascript",
        "css" => "text/css",
        "html" | "htm" => "text/html",
        "svg" => "image/svg+xml",
        "ico" => "image/vnd.microsoft.icon",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "webp" => "image/webp",
        "avif" => "image/avif",
        "json" => "application/json",
        "woff" => "font/woff",
        "woff2" => "font/woff2",
        "webmanifest" => "application/manifest+json",
        "txt" => "text/plain",
        "wasm" => "application/wasm",
        "mp4" => "video/mp4",
        _ => "application/octet-stream",
    }
}

/// `GZipMiddleware.exclude_content_types` (starlette ≥1.6 defaults):
/// exact names plus the `audio/*`/`video/*` wildcards.
pub(crate) fn gzip_excluded(media_type: &str) -> bool {
    let base = media_type.split(';').next().unwrap_or("").trim();
    matches!(
        base,
        "application/grpc"
            | "application/gzip"
            | "application/x-gzip"
            | "application/zip"
            | "font/woff"
            | "font/woff2"
            | "image/avif"
            | "image/gif"
            | "image/jpeg"
            | "image/png"
            | "image/webp"
            | "text/event-stream"
    ) || base.starts_with("audio/")
        || base.starts_with("video/")
}

// ── response plumbing ───────────────────────────────────────────────────────

fn push(h: &mut Vec<(HeaderName, HeaderValue)>, name: &str, value: &str) {
    h.push((
        HeaderName::from_bytes(name.as_bytes()).expect("static header name"),
        HeaderValue::from_str(value).expect("header value"),
    ));
}

fn set_header(h: &mut Vec<(HeaderName, HeaderValue)>, name: &str, value: &str) {
    if let Some(e) = h.iter_mut().find(|(n, _)| n.as_str().eq_ignore_ascii_case(name)) {
        e.1 = HeaderValue::from_str(value).expect("header value");
    } else {
        push(h, name, value);
    }
}

fn finish(h: Vec<(HeaderName, HeaderValue)>, body: Vec<u8>, status: StatusCode) -> Response {
    let mut res = Response::builder().status(status).body(Body::from(body)).unwrap();
    for (n, v) in h {
        res.headers_mut().append(n, v);
    }
    res
}

/// `PlainTextResponse` — `text/plain; charset=utf-8`, content-length first
/// (init_headers appends it before content-type).
fn plaintext(status: StatusCode, body: &str, extra: &[(String, String)]) -> Response {
    let mut res = Response::builder().status(status).body(Body::from(body.to_string())).unwrap();
    for (k, v) in extra {
        res.headers_mut()
            .append(HeaderName::from_bytes(k.as_bytes()).unwrap(), HeaderValue::from_str(v).unwrap());
    }
    res.headers_mut().append(
        header::CONTENT_LENGTH,
        HeaderValue::from_str(&body.len().to_string()).unwrap(),
    );
    res.headers_mut().append(
        header::CONTENT_TYPE,
        HeaderValue::from_static("text/plain; charset=utf-8"),
    );
    res
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn hdrs(pairs: &[(&str, &str)]) -> HeaderMap {
        let mut m = HeaderMap::new();
        for (k, v) in pairs {
            m.insert(HeaderName::from_bytes(k.as_bytes()).unwrap(), HeaderValue::from_str(v).unwrap());
        }
        m
    }

    // ── tojson ──

    #[test]
    fn tojson_matches_jinja() {
        assert_eq!(jinja_tojson(&Value::Null), "null");
        assert_eq!(jinja_tojson(&Value::String("abc".into())), "\"abc\"");
        assert_eq!(jinja_tojson(&json_str("あ")), "\"\\u3042\"");
        assert_eq!(jinja_tojson(&json_str("<a&'>")), "\"\\u003ca\\u0026\\u0027\\u003e\"");
        // keys sort alphabetically — jinja2 tojson passes sort_keys=True
        let snap = serde_json::json!({"owner":"h","revision":3,"hiddenRoots":["r1","あ/折"]});
        assert_eq!(
            jinja_tojson(&snap),
            "{\"hiddenRoots\": [\"r1\", \"\\u3042/\\u6298\"], \"owner\": \"h\", \"revision\": 3}"
        );
        // astral chars → UTF-16 surrogate pairs, lowercase hex
        assert_eq!(jinja_tojson(&json_str("😀")), "\"\\ud83d\\ude00\"");
    }

    fn json_str(s: &str) -> Value {
        Value::String(s.to_string())
    }

    #[test]
    fn truthiness_matches_jinja_not() {
        for v in [
            Value::Null,
            Value::Bool(false),
            Value::Number(0.into()),
            json_str(""),
            serde_json::json!([]),
            serde_json::json!({}),
        ] {
            assert!(!json_truthy(&v), "{v:?} must be falsy");
        }
        for v in [
            Value::Bool(true),
            Value::Number(1.into()),
            Value::Number((-1).into()),
            json_str("x"),
            serde_json::json!([0]),
            serde_json::json!({"a": 0}),
        ] {
            assert!(json_truthy(&v), "{v:?} must be truthy");
        }
    }

    // ── template render ──

    const TEMPLATE: &str = include_str!("../../../app/templates/index.html");
    const PERF: &str = include_str!("../../../app/templates/thumbnail_perf.html");
    const PROBE: &str = include_str!("../../../app/perf/thumbnail-probe.js");

    fn ctx<'a>() -> IndexCtx<'a> {
        IndexCtx {
            user_key: Some("alice@example.com".into()),
            owner: Some("deadbeef".into()),
            snapshot: serde_json::json!({"owner":"deadbeef","revision":7,"hiddenRoots":["x"]}),
            test_mode: false,
            ua_android: false,
            perf: None,
        }
    }

    #[test]
    fn login_branch_is_static() {
        let src = strip_comments(TEMPLATE).unwrap();
        let rest = src.strip_prefix(IF_NOT_USER).unwrap();
        let expected = &rest[..rest.find(ELSE_TAG).unwrap()];
        let out = render_index_for(true, TEMPLATE, &ctx()).unwrap();
        assert_eq!(out, expected);
        assert!(out.starts_with("\n<!DOCTYPE html>"));
        assert!(out.ends_with("</html>\n"));
    }

    #[test]
    fn viewer_branch_substitutes_globals() {
        let out = render_index(TEMPLATE, &ctx()).unwrap();
        assert!(out.contains("<html lang=\"ja\" class=\"\">"), "ua class empty");
        assert!(out.contains("window.__BOOKQ_TEST_MODE__ = \"false\";"));
        assert!(out.contains("window.__BOOKQ_VISIBILITY_OWNER__ = \"deadbeef\";"));
        assert!(out.contains(
            "window.__BOOKQ_VISIBILITY_SNAPSHOT__ = {\"hiddenRoots\": [\"x\"], \"owner\": \"deadbeef\", \"revision\": 7};"
        ));
        assert!(out.contains("window.__BOOKQ_PROGRESS_USER__ = \"alice@example.com\";"));
        assert!(!out.contains("thumbnail-perf-panel"));
        assert!(out.ends_with("</html>\n"));
    }

    #[test]
    fn viewer_branch_null_and_android_variants() {
        let mut c = ctx();
        c.owner = None;
        c.snapshot = Value::Null;
        c.user_key = None;
        c.test_mode = true;
        c.ua_android = true;
        c.perf = Some((PROBE, PERF));
        let out = render_index(TEMPLATE, &c).unwrap();
        assert!(out.contains("<html lang=\"ja\" class=\"ua-android\">"));
        assert!(out.contains("window.__BOOKQ_TEST_MODE__ = \"true\";"));
        assert!(out.contains("window.__BOOKQ_VISIBILITY_OWNER__ = null;"));
        assert!(out.contains("window.__BOOKQ_VISIBILITY_SNAPSHOT__ = null;"));
        assert!(out.contains("window.__BOOKQ_PROGRESS_USER__ = null;"));
        assert!(out.contains("<script>(() => {\n  // Measurement-only observer."));
        assert!(out.contains("id=\"thumbnail-perf-panel\""));
        // the include file's {#…#} comment must not leak into the render
        assert!(!out.contains("Dev-only thumbnail timing panel"));
    }

    #[test]
    fn unknown_jinja_falls_back() {
        // A Jinja construct that survives substitution must fail the render
        // (the handler then proxies upstream rather than emit raw `{%`).
        let bad = TEMPLATE.replace("</head>", "{% unknown %}</head>");
        assert!(render_index(&bad, &ctx()).is_none());
        let bad2 = TEMPLATE.replace("{{ 'ua-android' if ua_android else '' }}", "{{ x.y }}");
        assert!(render_index(&bad2, &ctx()).is_none());
        // unterminated comment → Jinja error upstream → proxy
        let bad3 = TEMPLATE.replace("<title>", "{# open <title>");
        assert!(render_index(&bad3, &ctx()).is_none());
    }

    // ── float repr ──

    #[test]
    fn float_str_matches_python() {
        assert_eq!(py_float_str(1700000000.0), "1700000000.0");
        assert_eq!(py_float_str(1700000000.5), "1700000000.5");
    }

    // ── range parsing ──

    #[test]
    fn range_parse_matches_starlette() {
        let p = |v| match parse_range(v, 100) {
            RangePlan::Ranges(r) => format!("{r:?}"),
            RangePlan::Full => "full".into(),
            RangePlan::Malformed(m) => format!("400:{m}"),
            RangePlan::Unsatisfiable => "416".into(),
        };
        assert_eq!(p("bytes=0-99"), "[(0, 100)]");
        assert_eq!(p("bytes=50-"), "[(50, 100)]");
        assert_eq!(p("bytes=-20"), "[(80, 100)]");
        assert_eq!(p("bytes=0-500"), "[(0, 100)]");           // clamped
        assert_eq!(p("bytes=5-9"), "[(5, 10)]");
        assert_eq!(p("bytes=0-1,5-9"), "[(0, 2), (5, 10)]");
        assert_eq!(p("bytes=0-5,5-9"), "[(0, 10)]");           // merge
        assert_eq!(p("bytes=5-9,0-2"), "[(0, 3), (5, 10)]");   // sorted
        assert_eq!(p("garbage"), "400:Malformed range header.");
        assert_eq!(p("items=0-9"), "400:Only support bytes range");
        assert_eq!(p("bytes="), "400:Range header: range must be requested");
        assert_eq!(p("bytes=-"), "400:Range header: range must be requested");
        assert_eq!(p("bytes=abc"), "400:Range header: range must be requested");
        assert_eq!(p("bytes=50-40"), "400:Range header: start must be less than end");
        assert_eq!(p("bytes=100-"), "416");
        assert_eq!(p("bytes=200-300"), "416");
        assert_eq!(p("bytes=-0"), "416");                       // start==size
        assert_eq!(p("bytes=+5-9"), "[(5, 10)]");
        assert_eq!(p("bytes=1_0-2_0"), "[(10, 21)]");
        assert_eq!(p("bytes=9_9-"), "[(99, 100)]");
        // >100 comma parts → ignore the header entirely (full body)
        let many = (0..101).map(|i| format!("{i}-{i}")).collect::<Vec<_>>().join(",");
        assert_eq!(p(&format!("bytes={many}")), "full");
    }

    #[test]
    fn py_int_matches_python() {
        assert_eq!(py_int("42"), Some(42));
        assert_eq!(py_int("+42"), Some(42));
        assert_eq!(py_int("-42"), Some(-42));
        assert_eq!(py_int(" 42 "), Some(42));
        assert_eq!(py_int("4_2"), Some(42));
        assert_eq!(py_int(""), None);
        assert_eq!(py_int("_4"), None);
        assert_eq!(py_int("4_"), None);
        assert_eq!(py_int("4__2"), None);
        assert_eq!(py_int("x"), None);
        assert_eq!(py_int("99999999999999999999"), Some(i64::MAX));
    }

    // ── file response ──

    fn tmpfile(name: &str, body: &[u8]) -> (PathBuf, std::fs::Metadata) {
        let dir = std::env::temp_dir().join(format!("bookq-shell-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join(name);
        std::fs::write(&p, body).unwrap();
        let md = std::fs::metadata(&p).unwrap();
        (p, md)
    }

    fn hdr_list(res: &Response) -> Vec<(String, String)> {
        res.headers()
            .iter()
            .map(|(k, v)| (k.as_str().to_string(), v.to_str().unwrap().to_string()))
            .collect()
    }

    #[tokio::test]
    async fn file_response_full_and_304() {
        let (p, md) = tmpfile("full.txt", b"hello world");
        let req = hdrs(&[]);
        let res = file_response(&p, &md, "text/plain", &[], NO_CACHE, &req, false).unwrap();
        assert_eq!(res.status(), StatusCode::OK);
        let hs = hdr_list(&res);
        assert_eq!(hs[0], ("content-type".to_string(), "text/plain; charset=utf-8".to_string()));
        assert_eq!(hs[1].0, "accept-ranges");
        assert_eq!(hs[2].0, "content-length");
        assert_eq!(hs[3].0, "last-modified");
        assert_eq!(hs[4].0, "etag");
        assert_eq!(hs[5], ("cache-control".to_string(), "no-cache".to_string()));
        let etag = hs[4].1.clone();
        assert!(etag.starts_with('"') && etag.ends_with('"') && etag.len() == 34);

        // if-none-match → 304 keeping only etag + cache-control, in order
        let res2 = file_response(&p, &md, "text/plain", &[], NO_CACHE, &hdrs(&[("if-none-match", &etag)]), false).unwrap();
        assert_eq!(res2.status(), StatusCode::NOT_MODIFIED);
        let h2 = hdr_list(&res2);
        assert_eq!(h2, vec![
            ("etag".to_string(), etag.clone()),
            ("cache-control".to_string(), "no-cache".to_string()),
        ]);
        // weak etag compare
        let res3 = file_response(&p, &md, "text/plain", &[], NO_CACHE, &hdrs(&[("if-none-match", &format!("W/{etag}"))]), false).unwrap();
        assert_eq!(res3.status(), StatusCode::NOT_MODIFIED);
        // if-modified-since
        let lm = hs[3].1.clone();
        let res4 = file_response(&p, &md, "text/plain", &[], NO_CACHE, &hdrs(&[("if-modified-since", &lm)]), false).unwrap();
        assert_eq!(res4.status(), StatusCode::NOT_MODIFIED);
    }

    #[tokio::test]
    async fn file_response_ranges() {
        let (p, md) = tmpfile("ranges.txt", b"0123456789");
        let req = hdrs(&[("range", "bytes=2-4")]);
        let res = file_response(&p, &md, "text/plain", &[], NO_CACHE, &req, false).unwrap();
        assert_eq!(res.status(), StatusCode::PARTIAL_CONTENT);
        assert_eq!(res.headers()["content-range"], "bytes 2-4/10");
        assert_eq!(res.headers()["content-length"], "3");
        let body = http_body_util::BodyExt::collect(res.into_body()).await.unwrap().to_bytes();
        assert_eq!(&body[..], b"234");

        // HEAD: same headers, no body
        let res = file_response(&p, &md, "text/plain", &[], NO_CACHE, &req, true).unwrap();
        assert_eq!(res.headers()["content-range"], "bytes 2-4/10");
        assert_eq!(res.headers()["content-length"], "3");
        let body = http_body_util::BodyExt::collect(res.into_body()).await.unwrap().to_bytes();
        assert!(body.is_empty());

        // multipart
        let res = file_response(&p, &md, "text/plain", &[], NO_CACHE, &hdrs(&[("range", "bytes=0-1,4-5")]), false).unwrap();
        assert_eq!(res.status(), StatusCode::PARTIAL_CONTENT);
        let ct = res.headers()["content-type"].to_str().unwrap().to_string();
        let boundary = ct.strip_prefix("multipart/byteranges; boundary=").unwrap();
        assert_eq!(boundary.len(), 26);
        let cl = res.headers()["content-length"].to_str().unwrap().to_string();
        let body = http_body_util::BodyExt::collect(res.into_body()).await.unwrap().to_bytes();
        let expect = format!(
            "--{b}\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Range: bytes 0-1/10\r\n\r\n01\r\n--{b}\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Range: bytes 4-5/10\r\n\r\n45\r\n--{b}--",
            b = boundary
        );
        assert_eq!(body.as_ref(), expect.as_bytes());
        assert_eq!(cl, expect.len().to_string());

        // malformed / unsatisfiable
        let res = file_response(&p, &md, "text/plain", &[], NO_CACHE, &hdrs(&[("range", "items=0-1")]), false).unwrap();
        assert_eq!(res.status(), StatusCode::BAD_REQUEST);
        assert_eq!(res.headers()["content-type"], "text/plain; charset=utf-8");
        let res = file_response(&p, &md, "text/plain", &[], NO_CACHE, &hdrs(&[("range", "bytes=100-")]), false).unwrap();
        assert_eq!(res.status(), StatusCode::RANGE_NOT_SATISFIABLE);
        assert_eq!(res.headers()["content-range"], "bytes */10");
    }

    #[test]
    fn mime_map_matches_slim_python() {
        let c = |n: &str| mime_for(Path::new(n));
        assert_eq!(c("a.js"), "text/javascript");
        assert_eq!(c("api-types.d.ts"), "application/octet-stream");
        assert_eq!(c("favicon.ico"), "image/vnd.microsoft.icon");
        assert_eq!(c("a.svg"), "image/svg+xml");
        assert_eq!(c("a.unknown"), "application/octet-stream");
    }
}
