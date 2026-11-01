//! `/thumb`, `/view`, `/api/v1/thumb-sprite` — M7 of #163.
//!
//! Hit-path native over the SHARED thumb-cache volume: every endpoint's
//! stat→key→sha1→ETag→`_not_modified`→dest-lookup preamble is ported, so a
//! cache hit is served byte-identically to Python (`thumbs.py`). A miss
//! falls through to the upstream Pillow pipeline, which renders into the
//! same cache dir — the next identical request is a native hit.
//!
//! Never native: rendering/transcoding (Pillow), PDF pages (pypdfium2),
//! RAR extraction (external tool) — those requests proxy whole.

use std::collections::HashMap;
use std::path::Path;

use axum::body::Body;
use axum::http::{header, HeaderMap, HeaderValue, StatusCode};
use axum::response::Response;
use sha1::{Digest, Sha1};

use crate::serve::{
    archive_not_modified, fmt_mtime, http_date, is_image_entry, is_pdf_file, is_within, realpath,
    resolve_archive, suffix_lower, Serve,
};

const CACHE_MAX_AGE: i64 = 86400;
const SPRITE_MAX_ENTRIES: usize = 9;
const SPRITE_CELL_WIDTH: i64 = 200;
const GRID_DIMS: [(i64, (i64, i64)); 3] = [(200, (240, 360)), (400, (480, 720)), (800, (960, 1440))];

fn detail(status: StatusCode, msg: &str) -> Response {
    let mut res = Response::builder()
        .status(status)
        .body(Body::from(
            serde_json::to_string(&serde_json::json!({"detail": msg})).unwrap(),
        ))
        .unwrap();
    res.headers_mut().insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("application/json"),
    );
    res
}

fn sha1hex(s: &str) -> String {
    format!("{:x}", Sha1::digest(s.as_bytes()))
}

/// Python `str(float)`: integral values keep a trailing `.0`.
fn py_float(v: f64) -> String {
    if v.is_finite() && v.fract() == 0.0 && v.abs() < 1e15 {
        format!("{v:.1}")
    } else {
        format!("{v}")
    }
}

/// `normalize_width` — 200|400|800, anything else → 400.
fn normalize_width(raw: Option<&String>) -> i64 {
    let Some(raw) = raw else { return 400 };
    match raw.trim().parse::<i64>() {
        Ok(w) if [200, 400, 800].contains(&w) => w,
        _ => 400,
    }
}

/// `int(mtime)` semantics: truncation toward zero (mtimes are positive).
fn fstat(path: &Path) -> Option<std::fs::Metadata> {
    std::fs::metadata(path).ok()
}

fn stat_mtime_size(path: &Path) -> Option<(f64, f64)> {
    let m = fstat(path)?;
    let mtime = m
        .modified()
        .ok()?
        .duration_since(std::time::UNIX_EPOCH)
        .ok()?
        .as_secs_f64();
    Some((mtime, m.len() as f64))
}

/// `_stat_archive_source` — inner guards → stat → pdf/image shape → key.
fn stat_archive_source(archive: &Path, inner: &str) -> Result<(f64, f64, String), Response> {
    if inner.is_empty() || inner.starts_with('/') || inner.split('/').any(|p| p == "..") {
        return Err(detail(StatusCode::BAD_REQUEST, "Invalid image path"));
    }
    let Some((mtime, size)) = stat_mtime_size(archive) else {
        return Err(detail(StatusCode::NOT_FOUND, "File not found"));
    };
    if is_pdf_file(archive) {
        let ok = inner.len() >= 6
            && inner.starts_with('p')
            && inner.ends_with(".png")
            && inner[1..inner.len() - 4].chars().all(|c| c.is_ascii_digit());
        if !ok {
            return Err(detail(StatusCode::NOT_FOUND, "File not found"));
        }
    } else if !is_image_entry(inner) {
        return Err(detail(StatusCode::NOT_FOUND, "File not found"));
    }
    Ok((
        mtime,
        size,
        format!("{}|{inner}|{}|{}", archive.display(), py_float(mtime), size as u64),
    ))
}

/// `_stat_source` — mtime/size/cache-key WITHOUT opening archives or
/// extracting bytes (same validation as `load_source_bytes`).
fn stat_source(downloads: &Path, path: &str) -> Result<(f64, f64, String), Response> {
    if let Some((archive, inner)) = resolve_archive(downloads, path) {
        if !is_within(downloads, &archive) {
            return Err(detail(StatusCode::FORBIDDEN, "Forbidden"));
        }
        let archive = realpath(&archive)
            .map_err(|_| detail(StatusCode::NOT_FOUND, "File not found"))?;
        return stat_archive_source(&archive, &inner);
    }
    if path.starts_with('/') || path.ends_with('/') {
        return Err(detail(StatusCode::BAD_REQUEST, "Invalid image path"));
    }
    if path.contains('\x00') {
        return Err(detail(StatusCode::NOT_FOUND, "File not found"));
    }
    let file_path = downloads.join(path);
    if !is_within(downloads, &file_path) {
        return Err(detail(StatusCode::FORBIDDEN, "Forbidden"));
    }
    let file_path =
        realpath(&file_path).map_err(|_| detail(StatusCode::NOT_FOUND, "File not found"))?;
    if !file_path.is_file() {
        return Err(detail(StatusCode::NOT_FOUND, "File not found"));
    }
    let Some((mtime, size)) = stat_mtime_size(&file_path) else {
        return Err(detail(StatusCode::NOT_FOUND, "File not found"));
    };
    Ok((
        mtime,
        size,
        format!("{}|{}|{}", file_path.display(), py_float(mtime), size as u64),
    ))
}

/// `load_source_bytes` for the passthrough `/view` branch. Returns `Err`
/// for the guard failures (400/403/404) and `Ok(None)` for "needs upstream
/// tooling" (rar/pdf) so the caller proxies instead of reproducing 501s.
fn load_source_bytes(
    downloads: &Path,
    path: &str,
) -> Result<Option<(Vec<u8>, f64, f64, String)>, Response> {
    if let Some((archive, inner)) = resolve_archive(downloads, path) {
        if !is_within(downloads, &archive) {
            return Err(detail(StatusCode::FORBIDDEN, "Forbidden"));
        }
        let archive = realpath(&archive)
            .map_err(|_| detail(StatusCode::NOT_FOUND, "File not found"))?;
        if inner.is_empty() || inner.starts_with('/') || inner.split('/').any(|p| p == "..") {
            return Err(detail(StatusCode::BAD_REQUEST, "Invalid image path"));
        }
        let Some((mtime, size)) = stat_mtime_size(&archive) else {
            return Err(detail(StatusCode::NOT_FOUND, "File not found"));
        };
        if is_pdf_file(&archive) || crate::serve::is_rar_path(&archive) {
            return Ok(None);
        }
        if !is_image_entry(&inner) {
            return Err(detail(StatusCode::NOT_FOUND, "File not found"));
        }
        let key = format!(
            "{}|{inner}|{}|{}",
            archive.display(),
            py_float(mtime),
            size as u64
        );
        return match bookq_core::zip_read_entry(&archive, &inner) {
            Some(data) => Ok(Some((data, mtime, size, key))),
            None => Err(detail(StatusCode::NOT_FOUND, "File not found")),
        };
    }
    if path.starts_with('/') || path.ends_with('/') {
        return Err(detail(StatusCode::BAD_REQUEST, "Invalid image path"));
    }
    if path.contains('\x00') {
        return Err(detail(StatusCode::NOT_FOUND, "File not found"));
    }
    let file_path = downloads.join(path);
    if !is_within(downloads, &file_path) {
        return Err(detail(StatusCode::FORBIDDEN, "Forbidden"));
    }
    let file_path =
        realpath(&file_path).map_err(|_| detail(StatusCode::NOT_FOUND, "File not found"))?;
    if !file_path.is_file() {
        return Err(detail(StatusCode::NOT_FOUND, "File not found"));
    }
    let Some((mtime, size)) = stat_mtime_size(&file_path) else {
        return Err(detail(StatusCode::NOT_FOUND, "File not found"));
    };
    let data = std::fs::read(&file_path)
        .map_err(|_| detail(StatusCode::NOT_FOUND, "File not found"))?;
    Ok(Some((
        data,
        mtime,
        size,
        format!("{}|{}|{}", file_path.display(), py_float(mtime), size as u64),
    )))
}

/// The shared response tail for cached/loaded bytes.
fn media_response(
    body: Vec<u8>,
    media: &'static str,
    etag: &str,
    mtime: f64,
    is_svg: bool,
) -> Response {
    let mut res = Response::builder()
        .status(StatusCode::OK)
        .body(Body::from(body))
        .unwrap();
    let h = res.headers_mut();
    h.insert(header::CONTENT_TYPE, HeaderValue::from_static(media));
    h.insert(
        header::CACHE_CONTROL,
        HeaderValue::from_str(&format!("public, max-age={CACHE_MAX_AGE}")).unwrap(),
    );
    h.insert(header::ETAG, HeaderValue::from_str(etag).unwrap());
    h.insert(
        header::LAST_MODIFIED,
        HeaderValue::from_str(&http_date(mtime)).unwrap(),
    );
    h.insert(
        header::HeaderName::from_static("x-content-type-options"),
        HeaderValue::from_static("nosniff"),
    );
    if is_svg {
        h.insert(
            header::HeaderName::from_static("content-security-policy"),
            HeaderValue::from_static("default-src 'none'; sandbox"),
        );
    }
    res
}

// ── /thumb/{path}?w= ──────────────────────────────────────────────────────────

/// `handle_thumb` — stat fast path + shared-cache hit; miss proxies.
pub fn thumb(downloads: &Path, cache: &Path, path: &str, w: Option<&String>, headers: &HeaderMap) -> Serve {
    let width = normalize_width(w);
    let (mtime, size, key) = match stat_source(downloads, path) {
        Ok(x) => x,
        Err(res) => return Serve::Done(res),
    };
    let is_svg = suffix_lower(Path::new(path)) == ".svg";
    let digest = sha1hex(&key);
    let etag = format!("\"{}-{}-{width}-{}\"", fmt_mtime(mtime), fmt_mtime(size), &digest[..16]);
    if archive_not_modified(headers, &etag, mtime) {
        return Serve::Done(
            Response::builder()
                .status(StatusCode::NOT_MODIFIED)
                .body(Body::empty())
                .unwrap(),
        );
    }
    let mut dest = cache
        .join("thumbs")
        .join(width.to_string())
        .join(format!("{digest}.jpg"));
    if is_svg {
        dest.set_extension("svg");
    }
    if !dest.is_file() {
        return Serve::Proxy;
    }
    let body = match std::fs::read(&dest) {
        Ok(b) => b,
        Err(_) => return Serve::Proxy,
    };
    Serve::Done(media_response(
        body,
        if is_svg { "image/svg+xml" } else { "image/jpeg" },
        &etag,
        mtime,
        is_svg,
    ))
}

// ── /view/{path} ──────────────────────────────────────────────────────────────

const VIEW_PASSTHROUGH: [(&str, &str); 4] = [
    (".jpg", "image/jpeg"),
    (".jpeg", "image/jpeg"),
    (".png", "image/png"),
    (".gif", "image/gif"),
];

/// `handle_view` — passthrough types serve original bytes natively;
/// normalized types hit the shared `view/` cache; misses proxy.
pub fn view(downloads: &Path, cache: &Path, path: &str, headers: &HeaderMap) -> Serve {
    let suffix = suffix_lower(Path::new(path));
    let passthrough = VIEW_PASSTHROUGH
        .iter()
        .find(|(s, _)| *s == suffix)
        .map(|(_, m)| *m);
    let is_svg = suffix == ".svg";
    if passthrough.is_some() || is_svg {
        let Some((data, mtime, size, key)) = (match load_source_bytes(downloads, path) {
            Ok(x) => x,
            Err(res) => return Serve::Done(res),
        }) else {
            return Serve::Proxy;
        };
        let digest = sha1hex(&key);
        let etag = format!(
            "\"{}-{}-view-{}\"",
            fmt_mtime(mtime),
            fmt_mtime(size),
            &digest[..16]
        );
        if archive_not_modified(headers, &etag, mtime) {
            return Serve::Done(
                Response::builder()
                    .status(StatusCode::NOT_MODIFIED)
                    .body(Body::empty())
                    .unwrap(),
            );
        }
        return Serve::Done(media_response(
            data,
            if is_svg {
                "image/svg+xml"
            } else {
                passthrough.unwrap_or("image/jpeg")
            },
            &etag,
            mtime,
            is_svg,
        ));
    }
    let (mtime, size, key) = match stat_source(downloads, path) {
        Ok(x) => x,
        Err(res) => return Serve::Done(res),
    };
    let digest = sha1hex(&key);
    let etag = format!(
        "\"{}-{}-view-{}\"",
        fmt_mtime(mtime),
        fmt_mtime(size),
        &digest[..16]
    );
    if archive_not_modified(headers, &etag, mtime) {
        return Serve::Done(
            Response::builder()
                .status(StatusCode::NOT_MODIFIED)
                .body(Body::empty())
                .unwrap(),
        );
    }
    let dest = cache
        .join("view")
        .join(format!("{digest}.jpg"));
    if !dest.is_file() {
        return Serve::Proxy;
    }
    let body = match std::fs::read(&dest) {
        Ok(b) => b,
        Err(_) => return Serve::Proxy,
    };
    Serve::Done(media_response(body, "image/jpeg", &etag, mtime, false))
}

// ── /api/v1/thumb-sprite ──────────────────────────────────────────────────────

/// `_raw_query_value` — first still-percent-encoded value of `key` in a
/// raw query string (latin-1 view; comma-containing names survive because
/// the CSV split happens before decoding).
fn raw_query_value(query_string: &str, key: &str) -> Option<String> {
    let mut value = None;
    for part in query_string.split('&') {
        let Some((k, v)) = part.split_once('=') else {
            continue;
        };
        if k == key && value.is_none() {
            value = Some(v.to_string());
        }
    }
    value
}

/// Python `urllib.parse.unquote(..., errors="replace")`.
fn unquote(s: &str) -> String {
    percent_encoding::percent_decode_str(s)
        .decode_utf8()
        .map(|c| c.into_owned())
        .unwrap_or_else(|_| {
            percent_encoding::percent_decode_str(s)
                .decode_utf8_lossy()
                .into_owned()
        })
}

/// `_parse_sprite_rows` — "3,3,3" → per-row counts (≤3 rows, 1..9 each).
fn parse_sprite_rows(raw: Option<&String>) -> Option<Vec<i64>> {
    let raw = raw?;
    if raw.is_empty() {
        return None;
    }
    let rows: Vec<i64> = raw
        .split(',')
        .map(|x| x.parse::<i64>())
        .collect::<Result<_, _>>()
        .ok()?;
    if rows.is_empty() || rows.len() > 3 || rows.iter().any(|r| !(1..=9).contains(r)) {
        return None;
    }
    Some(rows)
}

/// `_dir_sprite_name_bad` — traversal/absolute/null → bad cell.
fn dir_sprite_name_bad(name: &str) -> bool {
    name.is_empty()
        || name.starts_with('/')
        || name.split('/').any(|p| p == "..")
        || name.contains('\x00')
}

/// `handle_thumb_sprite` — validation + key → hit serve; miss proxies.
pub fn thumb_sprite(
    downloads: &Path,
    cache: &Path,
    q: &HashMap<String, String>,
    query_string: &str,
    headers: &HeaderMap,
) -> Serve {
    let folder = q.get("folder").map(|s| s.as_str()).unwrap_or("");
    let cell = SPRITE_CELL_WIDTH;
    let rows = parse_sprite_rows(q.get("rows"));
    let grid_size = GRID_DIMS
        .iter()
        .find(|(w, _)| *w == normalize_width(q.get("w")))
        .map(|(_, s)| *s)
        .unwrap_or((480, 720));

    if folder.is_empty()
        || folder.starts_with('/')
        || folder.ends_with('/')
        || folder.contains('\x00')
    {
        return Serve::Done(detail(StatusCode::BAD_REQUEST, "Invalid sprite folder"));
    }
    if folder.split('/').any(|p| p == "..") {
        return Serve::Done(detail(StatusCode::BAD_REQUEST, "Invalid sprite folder"));
    }
    let folder_path = downloads.join(folder);
    if !is_within(downloads, &folder_path) {
        return Serve::Done(detail(StatusCode::FORBIDDEN, "Forbidden"));
    }
    let Ok(folder_path) = realpath(&folder_path) else {
        return Serve::Done(detail(StatusCode::NOT_FOUND, "File not found"));
    };
    let is_archive = folder_path.is_file() && crate::serve::is_archive_file(&folder_path);
    let is_dir = folder_path.is_dir();
    if !folder_path.exists() {
        return Serve::Done(detail(StatusCode::NOT_FOUND, "File not found"));
    }
    if !is_archive && !is_dir {
        return Serve::Done(detail(StatusCode::BAD_REQUEST, "Not an archive or directory"));
    }

    let Some(raw_names) = raw_query_value(query_string, "names") else {
        return Serve::Done(detail(StatusCode::BAD_REQUEST, "Missing names"));
    };
    let names: Vec<String> = raw_names
        .split(',')
        .map(unquote)
        .filter(|n| !n.is_empty())
        .take(SPRITE_MAX_ENTRIES)
        .collect();
    if names.is_empty() {
        return Serve::Done(detail(StatusCode::BAD_REQUEST, "Missing names"));
    }

    let names_part = names.join("\x00");
    let rows_part = rows.as_ref().map(|r| {
        format!(
            "{}@{}x{}",
            r.iter().map(|v| v.to_string()).collect::<Vec<_>>().join("-"),
            grid_size.0,
            grid_size.1
        )
    });
    let rows_part = rows_part.as_deref().unwrap_or("");

    let (key, etag_mtime, etag_size) = if is_archive {
        let Some((mtime, size)) = stat_mtime_size(&folder_path) else {
            return Serve::Done(detail(StatusCode::NOT_FOUND, "File not found"));
        };
        (
            format!(
                "{}|{}|{}|{names_part}|{cell}|{rows_part}",
                folder_path.display(),
                py_float(mtime),
                size as u64
            ),
            mtime,
            size,
        )
    } else {
        // `_stat_dir_sprite_entries`: per-name stat keys, `bad:`/`miss:`
        // placeholders, max mtime.
        let mut keys: Vec<String> = Vec::new();
        let mut max_mtime = 0.0f64;
        for name in &names {
            if dir_sprite_name_bad(name) {
                keys.push(format!("bad:{name}"));
                continue;
            }
            match stat_source(downloads, &format!("{folder}/{name}")) {
                Ok((mtime, _size, skey)) => {
                    max_mtime = max_mtime.max(mtime);
                    keys.push(skey);
                }
                Err(_) => keys.push(format!("miss:{name}")),
            }
        }
        let entries_part = keys.join("\x00");
        (
            format!(
                "dir|{}|{names_part}|{entries_part}|{cell}|{rows_part}",
                folder_path.display()
            ),
            max_mtime.max(0.0),
            keys.len() as f64,
        )
    };

    let digest = sha1hex(&key);
    let etag = format!(
        "\"{}-{}-{cell}-s{}\"",
        fmt_mtime(etag_mtime),
        fmt_mtime(etag_size),
        &digest[..16]
    );
    if archive_not_modified(headers, &etag, etag_mtime) {
        return Serve::Done(
            Response::builder()
                .status(StatusCode::NOT_MODIFIED)
                .body(Body::empty())
                .unwrap(),
        );
    }
    let dest = cache
        .join("sprites")
        .join(cell.to_string())
        .join(format!("{digest}.jpg"));
    if !dest.is_file() {
        return Serve::Proxy;
    }
    let body = match std::fs::read(&dest) {
        Ok(b) => b,
        Err(_) => return Serve::Proxy,
    };
    Serve::Done(media_response(body, "image/jpeg", &etag, etag_mtime, false))
}
