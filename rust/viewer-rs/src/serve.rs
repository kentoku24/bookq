//! Native read-path endpoints for viewer-rs — issue #163, parallel-backend R1.
//!
//! Ports of `app/routes.py`'s `/api/images` and `/image/{path}` with
//! byte-level response parity against the Python backend. Anything this
//! module cannot serve with *proven* identical semantics returns
//! `Serve::Proxy` and falls through to the Python upstream:
//!
//!   - PDFs (pypdfium2 rendering has no Rust equivalent here)
//!   - RAR/CBR members of *multipart* sets (the bundled unrar can't chain
//!     volumes; single-volume entries extract in-process)
//!   - multi-range requests (hypercorn emits multipart/byteranges with a
//!     random boundary — byte parity is impossible; single ranges are native)
//!
//! Auth: native serving is gated on `BOOKQ_TEST_MODE=true`. The Python side
//! gates these routes behind `require_auth`; that session-cookie check is
//! not ported yet, so outside test mode everything proxies upstream.

use std::{
    collections::{HashMap, HashSet, VecDeque},
    ffi::OsString,
    io,
    io::Write,
    path::{Component, Path, PathBuf},
    sync::{LazyLock, Mutex},
};

use axum::{
    body::{Body, HttpBody},
    http::{header, HeaderMap, HeaderValue, StatusCode},
    response::{IntoResponse, Response},
};
use sha1::{Digest, Sha1};

/// The two verdicts a native handler can reach: a finished response, or
/// "fall through to the Python upstream".
pub enum Serve {
    Done(Response),
    Proxy,
}

// ── Suffix/extension tables (mirror app/scanner.py + app/archive.py) ──────────

pub(crate) const IMAGE_EXTENSIONS: &[&str] = &[
    ".avif", ".webp", ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg",
];
pub(crate) const ARCHIVE_EXTENSIONS: &[&str] = &[".zip", ".cbz", ".rar", ".cbr", ".epub"];
pub(crate) const PDF_EXTENSIONS: &[&str] = &[".pdf"];
const MAX_ARCHIVE_ENTRIES: usize = 2000;

pub(crate) fn suffix_lower(p: &Path) -> String {
    p.extension()
        .and_then(|e| e.to_str())
        .map(|e| format!(".{}", e.to_lowercase()))
        .unwrap_or_default()
}

pub(crate) fn is_archive_file(p: &Path) -> bool {
    p.is_file() && ARCHIVE_EXTENSIONS.contains(&suffix_lower(p).as_str())
}

pub(crate) fn is_pdf_file(p: &Path) -> bool {
    p.is_file() && PDF_EXTENSIONS.contains(&suffix_lower(p).as_str())
}

pub(crate) fn is_rar_path(p: &Path) -> bool {
    matches!(suffix_lower(p).as_str(), ".rar" | ".cbr")
}

// ── Path resolution (port of pathlib resolve/realpath, non-strict) ────────────

/// Lexical + symlink resolution matching Python's `Path.resolve(strict=False)`:
/// symlinks are followed (including dangling targets, lexically), `..` pops the
/// resolved prefix, missing components pass through. Errors only on symlink
/// loops / cwd failure — which `_is_within` maps to "outside" like Python's
/// OSError/RuntimeError catch.
pub(crate) fn realpath(path: &Path) -> io::Result<PathBuf> {
    let mut out = if path.is_absolute() {
        PathBuf::from("/")
    } else {
        std::env::current_dir()?
    };
    let mut pending: Vec<OsString> = path
        .components()
        .filter_map(|c| match c {
            Component::Normal(s) => Some(s.to_os_string()),
            Component::ParentDir => Some(OsString::from("..")),
            _ => None,
        })
        .collect();
    pending.reverse(); // pop() pulls from the front logically
    let mut depth = 0;
    while let Some(comp) = pending.pop() {
        if comp == ".." {
            out.pop();
            continue;
        }
        out.push(&comp);
        if let Ok(target) = std::fs::read_link(&out) {
            out.pop();
            let mut splice: Vec<OsString> = target
                .components()
                .filter_map(|c| match c {
                    Component::Normal(s) => Some(s.to_os_string()),
                    Component::ParentDir => Some(OsString::from("..")),
                    _ => None,
                })
                .collect();
            if target.is_absolute() {
                out = PathBuf::from("/");
            }
            splice.extend(pending.iter().rev().cloned());
            splice.reverse();
            pending = splice;
            depth += 1;
            if depth > 40 {
                return Err(io::Error::from_raw_os_error(40));
            }
        }
    }
    Ok(out)
}

/// `_is_within`: target inside base after resolving both.
/// `base` must already be canonical — `AppState` canonicalizes `downloads`
/// once at startup. Only `target` pays a per-call realpath (required for
/// symlink-escape safety); canonicalizing `base` per request was a
/// stat-walk on every hot-path call.
pub(crate) fn is_within(base: &Path, target: &Path) -> bool {
    match realpath(target) {
        Ok(t) => t.starts_with(base),
        _ => false,
    }
}

/// `_resolve_archive`: longest prefix of `rel` that resolves to an
/// archive/PDF file → `(archive_path, inner_path)`. `.` and empty components
/// are dropped; any `..` rejects the whole path.
pub(crate) fn resolve_archive(downloads: &Path, rel: &str) -> Option<(PathBuf, String)> {
    let parts: Vec<&str> = rel.split('/').filter(|p| !p.is_empty() && *p != ".").collect();
    if parts.iter().any(|p| *p == "..") {
        return None;
    }
    // `range(len(parts) - 1, 0, -1)` — the inner entry is mandatory: a bare
    // archive filename is an ordinary file (plain-file branch downstream).
    for i in (1..parts.len()).rev() {
        let cand = downloads.join(parts[..i].join("/"));
        if is_archive_file(&cand) || is_pdf_file(&cand) {
            return Some((cand, parts[i..].join("/")));
        }
    }
    None
}

// ── Archive listing (port of app/archive.py) ──────────────────────────────────

/// `_is_safe_entry`: no drive-letter / absolute / unsafe components.
fn is_safe_entry(name: &str) -> bool {
    if name.len() >= 2 && name.as_bytes()[1] == b':' {
        return false;
    }
    !name
        .replace('\\', "/")
        .split('/')
        .any(|p| p.is_empty() || p == "." || p == "..")
}

/// `_is_image_entry`: exposable image inside an archive.
pub(crate) fn is_image_entry(name: &str) -> bool {
    if !is_safe_entry(name) || name.ends_with('/') {
        return false;
    }
    let base = name.rsplit('/').next().unwrap_or("");
    if base.is_empty() || base.starts_with('.') {
        return false;
    }
    if name.starts_with("__MACOSX/") || name.contains("/__MACOSX/") {
        return false;
    }
    IMAGE_EXTENSIONS.contains(&suffix_lower(Path::new(base)).as_str())
}

/// `_finalize`: image filter + first-wins dedupe + natural sort + 2000 cap.
fn finalize(names: impl IntoIterator<Item = String>, keep_order: bool) -> Vec<String> {
    let mut seen = HashSet::new();
    let mut unique = Vec::new();
    for n in names {
        if !seen.contains(&n) && is_image_entry(&n) {
            seen.insert(n.clone());
            unique.push(n);
        }
    }
    if !keep_order {
        unique.sort_by(|a, b| bookq_core::natural_cmp(a, b));
    }
    unique.truncate(MAX_ARCHIVE_ENTRIES);
    unique
}

/// `list_archive_entries` without the disk cache (read-path parity only;
/// the warm cache is a performance layer whose output is identical).
/// Unreadable archives yield `[]`, matching Python's never-raise contract.
///
/// Header parsing rides `bookq_core::open_archive` (#217 port): a memoized
/// parsed archive answers repeat listings/reads without re-walking the
/// headers — same object the entry-read path shares.
pub(crate) fn list_archive_entries(path: &Path) -> Vec<String> {
    let memo = bookq_core::open_archive(path);
    match suffix_lower(path).as_str() {
        ".rar" | ".cbr" => finalize(
            memo.and_then(|a| a.rar_names())
                .or_else(|| bookq_core::rar_names(path))
                .unwrap_or_default()
                .into_iter()
                .filter(|(_, is_dir)| !is_dir)
                .map(|(n, _)| n),
            false,
        ),
        ".epub" => {
            let (raw, spine) = memo
                .and_then(|a| a.epub_raw())
                .or_else(|| bookq_core::epub_raw(path))
                .unwrap_or_default();
            let present: HashSet<&String> = raw.iter().collect();
            let ordered = finalize(spine.into_iter().filter(|n| present.contains(n)), true);
            if !ordered.is_empty() {
                return ordered;
            }
            finalize(raw, false)
        }
        _ => finalize(
            memo.and_then(|a| a.zip_names())
                .or_else(|| bookq_core::zip_names(path))
                .unwrap_or_default(),
            false,
        ),
    }
}

/// unrar allocates the decompress window per extraction (RAR5 dict sizes
/// run 32MB–4GB); spawn_blocking's ≤512 threads could otherwise stack
/// enough concurrent windows to OOM the gateway. Cap concurrent
/// in-process rar extraction at the CPU count — zip reads stay unbounded.
static RAR_EXTRACT: std::sync::LazyLock<tokio::sync::Semaphore> =
    std::sync::LazyLock::new(|| {
        tokio::sync::Semaphore::new(
            std::thread::available_parallelism().map(|n| n.get()).unwrap_or(4))
    });

/// `read_archive_entry` read path (#217 port): the memoized open archive
/// answers repeat page reads without re-parsing the central directory.
/// `None` covers every non-proven read exactly like `zip_read_entry` —
/// unstat'able paths fall through to the uncached one-shot open, and a
/// failed parse yields `None` just as Python propagates the open failure.
pub(crate) fn archive_read_entry(archive: &Path, inner: &str) -> Option<Vec<u8>> {
    let _permit = if is_rar_path(archive) {
        Some(RAR_EXTRACT.blocking_acquire().ok()?)
    } else {
        None
    };
    match bookq_core::open_archive(archive) {
        Some(oa) => oa.read_entry(archive, inner),
        // Uncached one-shot fallback — `rar_read_entry` resolves the
        // member by ordinal inside `rar_names`, so it reads only what the
        // ported listing can name (None → 404 when the listing fails,
        // matching Python's open-failure path).
        None => {
            if is_rar_path(archive) {
                bookq_core::rar_read_entry(archive, inner)
            } else {
                bookq_core::zip_read_entry(archive, inner)
            }
        }
    }
}

/// `_list_archive_prefix`: entries under an inner prefix, relative to it.
pub(crate) fn list_archive_prefix(archive: &Path, inner: &str) -> Vec<String> {
    let prefix = inner.trim_matches('/');
    if prefix.is_empty()
        || prefix == "."
        || prefix == ".."
        || prefix.split('/').any(|p| p == "..")
    {
        return Vec::new();
    }
    let head = format!("{prefix}/");
    list_archive_entries(archive)
        .into_iter()
        .filter(|e| e.starts_with(&head))
        .map(|e| e[head.len()..].to_string())
        .collect()
}

// ── HTTP primitives (dates, etags, conditional, ranges, mime) ─────────────────

pub(crate) fn fmt_mtime(mtime: f64) -> String {
    format!("{mtime:.0}")
}

pub(crate) fn http_date(secs_f64: f64) -> String {
    httpdate::fmt_http_date(std::time::UNIX_EPOCH + std::time::Duration::from_secs_f64(secs_f64.max(0.0)))
}

/// `_not_modified` for archive entries (`int(mtime)` truncation).
pub(crate) fn archive_not_modified(headers: &HeaderMap, etag: &str, mtime: f64) -> bool {
    if let Some(v) = headers.get(header::IF_NONE_MATCH).and_then(|v| v.to_str().ok()) {
        if v == etag {
            return true;
        }
    }
    if let Some(v) = headers.get(header::IF_MODIFIED_SINCE).and_then(|v| v.to_str().ok()) {
        if let Ok(t) = httpdate::parse_http_date(v) {
            let ts = t
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_secs_f64())
                .unwrap_or(0.0);
            if (mtime as i64) as f64 <= ts {
                return true;
            }
        }
    }
    false
}

/// The `/image` plain-file conditional (`st_mtime <= ts` — float, no truncation).
fn file_not_modified(headers: &HeaderMap, etag: &str, mtime: f64) -> bool {
    if let Some(v) = headers.get(header::IF_NONE_MATCH).and_then(|v| v.to_str().ok()) {
        if v == etag {
            return true;
        }
    }
    if let Some(v) = headers.get(header::IF_MODIFIED_SINCE).and_then(|v| v.to_str().ok()) {
        if let Ok(t) = httpdate::parse_http_date(v) {
            let ts = t
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_secs_f64())
                .unwrap_or(0.0);
            if mtime <= ts {
                return true;
            }
        }
    }
    false
}

/// `_parse_range` — archive-entry semantics: any invalid/unusable range is
/// ignored and the caller serves 200 in full.
fn parse_archive_range(header: &str, size: u64) -> Option<(u64, u64)> {
    let m = header.trim().strip_prefix("bytes=")?;
    let (s, e) = m.split_once('-')?;
    if s.is_empty() && e.is_empty() {
        return None;
    }
    if s.is_empty() {
        let n: u64 = e.parse().ok()?;
        if n == 0 {
            return None;
        }
        return Some((size.saturating_sub(n), size - 1));
    }
    let start: u64 = s.parse().ok()?;
    let end: u64 = if e.is_empty() { size - 1 } else { e.parse().ok()? };
    if start >= size || end < start {
        return None;
    }
    Some((start, end.min(size - 1)))
}

/// Minimal `mimetypes.guess_type` for the extensions this app serves.
pub(crate) fn guess_type(name: &str) -> &'static str {
    match suffix_lower(Path::new(name)).as_str() {
        ".jpg" | ".jpeg" => "image/jpeg",
        ".png" => "image/png",
        ".gif" => "image/gif",
        ".webp" => "image/webp",
        ".avif" => "image/avif",
        ".bmp" => "image/bmp",
        ".svg" | ".svgz" => "image/svg+xml",
        ".tif" | ".tiff" => "image/tiff",
        ".ico" => "image/vnd.microsoft.icon",
        ".pdf" => "application/pdf",
        ".zip" => "application/zip",
        ".epub" => "application/epub+zip",
        ".rar" => "application/vnd.rar",
        ".json" => "application/json",
        ".txt" => "text/plain",
        ".html" | ".htm" => "text/html",
        ".css" => "text/css",
        ".js" => "application/javascript",
        _ => "application/octet-stream",
    }
}

// ── Response builders ─────────────────────────────────────────────────────────

fn response(status: StatusCode, body: Body) -> Response {
    let mut r = Response::new(body);
    *r.status_mut() = status;
    r
}

fn set_header(r: &mut Response, name: header::HeaderName, value: &str) {
    if let Ok(v) = HeaderValue::from_str(value) {
        r.headers_mut().insert(name, v);
    }
}

fn detail(status: StatusCode, msg: &str) -> Response {
    let body = serde_json::json!({"detail": msg}).to_string();
    let mut r = response(status, Body::from(body));
    set_header(&mut r, header::CONTENT_TYPE, "application/json");
    r
}

pub(crate) fn not_modified() -> Response {
    response(StatusCode::NOT_MODIFIED, Body::empty())
}

/// `_archive_bytes_response`: in-memory bytes with conditional/Range support.
fn archive_bytes_response(
    headers: &HeaderMap,
    data: Vec<u8>,
    media: &str,
    etag: &str,
    last_modified: &str,
    mtime: f64,
) -> Response {
    if archive_not_modified(headers, etag, mtime) {
        return not_modified();
    }
    let mut r = response(StatusCode::OK, Body::empty());
    set_header(&mut r, header::CACHE_CONTROL, "private, max-age=3600");
    set_header(&mut r, header::ETAG, etag);
    set_header(&mut r, header::LAST_MODIFIED, last_modified);
    set_header(&mut r, header::ACCEPT_RANGES, "bytes");
    set_header(&mut r, header::CONTENT_TYPE, media);
    if let Some(range_header) = headers.get(header::RANGE).and_then(|v| v.to_str().ok()) {
        if let Some((start, end)) = parse_archive_range(range_header, data.len() as u64) {
            *r.status_mut() = StatusCode::PARTIAL_CONTENT;
            set_header(
                &mut r,
                header::CONTENT_RANGE,
                &format!("bytes {start}-{end}/{}", data.len()),
            );
            *r.body_mut() = Body::from(data[start as usize..=end as usize].to_vec());
            return r;
        }
    }
    *r.body_mut() = Body::from(data);
    r
}

// ── /api/images ───────────────────────────────────────────────────────────────

/// `_list_folder_images` + the v1 route's response shaping.
/// Success = `{"folder": <raw query>, "images": [...]}`; errors keep the
/// shared `{detail}` shape unwrapped. Returns `Serve::Proxy` whenever a PDF
/// is involved — pypdfium2 semantics cannot be reproduced natively yet.
pub fn api_images(downloads: &Path, folder: &str) -> Serve {
    let folder_path = downloads.join(folder);
    if !is_within(downloads, &folder_path) {
        return Serve::Done(detail(StatusCode::FORBIDDEN, "Forbidden"));
    }
    let resolved = match realpath(&folder_path) {
        Ok(p) => p,
        Err(_) => return Serve::Done(detail(StatusCode::FORBIDDEN, "Forbidden")),
    };
    if is_archive_file(&resolved) {
        let names = list_archive_entries(&resolved);
        return Serve::Done(json_list(folder, names));
    }
    if is_pdf_file(&resolved) {
        return Serve::Proxy; // pdf_page_count needs pypdfium2
    }
    if let Some((archive, inner)) = resolve_archive(downloads, folder) {
        if !is_within(downloads, &archive) {
            return Serve::Done(detail(StatusCode::FORBIDDEN, "Forbidden"));
        }
        if is_pdf_file(&archive) {
            return Serve::Proxy;
        }
        let archive_resolved = match realpath(&archive) {
            Ok(p) => p,
            Err(_) => return Serve::Done(detail(StatusCode::FORBIDDEN, "Forbidden")),
        };
        let scoped = list_archive_prefix(&archive_resolved, &inner);
        if scoped.is_empty() {
            return Serve::Done(detail(
                StatusCode::NOT_FOUND,
                &format!("Folder not found: {folder}"),
            ));
        }
        return Serve::Done(json_list(folder, scoped));
    }
    if !resolved.is_dir() {
        return Serve::Done(detail(
            StatusCode::NOT_FOUND,
            &format!("Folder not found: {folder}"),
        ));
    }
    Serve::Done(json_list(folder, dir_images_cached(&resolved)))
}

/// Directory image-listing memo keyed on dir mtime — a readdir of a large
/// folder on a slow volume costs tens of ms, while a single metadata call
/// costs ~µs. Dir mtime changes on add/remove/rename (file-content edits
/// do not affect the name list, so they need no invalidation).
static DIR_IMAGES_MEMO: LazyLock<Mutex<DirImagesMemo>> = LazyLock::new(|| {
    Mutex::new(DirImagesMemo {
        map: HashMap::new(),
        lru: VecDeque::new(),
    })
});
const DIR_IMAGES_MEMO_CAP: usize = 128;

struct DirImagesMemo {
    map: HashMap<PathBuf, (u128, Vec<String>)>,
    lru: VecDeque<PathBuf>,
}

fn dir_mtime_key(dir: &Path) -> Option<u128> {
    dir.metadata()
        .ok()
        .and_then(|m| m.modified().ok())
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|d| d.as_nanos())
}

fn dir_images_cached(dir: &Path) -> Vec<String> {
    let mt = dir_mtime_key(dir);
    if let Some(mt) = mt {
        if let Ok(g) = DIR_IMAGES_MEMO.lock() {
            if let Some((k, names)) = g.map.get(dir) {
                if *k == mt {
                    return names.clone();
                }
            }
        }
    }
    let names = bookq_core::list_images(dir).unwrap_or_default();
    if let Some(mt) = mt {
        if let Ok(mut g) = DIR_IMAGES_MEMO.lock() {
            g.map.insert(dir.to_path_buf(), (mt, names.clone()));
            g.lru.push_back(dir.to_path_buf());
            while g.map.len() > DIR_IMAGES_MEMO_CAP {
                if let Some(old) = g.lru.pop_front() {
                    g.map.remove(&old);
                }
            }
        }
    }
    names
}

/// Compact JSON list matching Starlette's `json.dumps(..., separators=(",",":"))`.
/// v1 images envelope — `{"folder": folder, "images": [...]}` compact JSON.
fn json_list(folder: &str, names: Vec<String>) -> Response {
    let body = serde_json::json!({"folder": folder, "images": names});
    let mut r = response(
        StatusCode::OK,
        Body::from(serde_json::to_string(&body).unwrap()),
    );
    set_header(&mut r, header::CONTENT_TYPE, "application/json");
    r
}

// ── /image/{path} ─────────────────────────────────────────────────────────────

/// `serve_image` — archive-entry reads and plain-file serving.
/// RAR/CBR entries extract in-process via bundled unrar; only PDF pages
/// (pypdfium2) and multi-range requests still proxy upstream.
pub fn image(downloads: &Path, path: &str, headers: &HeaderMap) -> Serve {
    if let Some((archive, inner)) = resolve_archive(downloads, path) {
        if !is_within(downloads, &archive) {
            return Serve::Done(detail(StatusCode::FORBIDDEN, "Forbidden"));
        }
        if is_pdf_file(&archive) {
            return Serve::Proxy; // pdf render → upstream pypdfium2
        }
        let archive_resolved = match realpath(&archive) {
            Ok(p) => p,
            Err(_) => return Serve::Done(detail(StatusCode::NOT_FOUND, "File not found")),
        };
        return serve_archive_entry(&archive_resolved, &inner, headers);
    }

    // Lexical guards come after archive resolution, exactly like Python.
    if path.starts_with('/') || path.ends_with('/') {
        return Serve::Done(detail(StatusCode::BAD_REQUEST, "Invalid image path"));
    }
    let file_path = downloads.join(path);
    if !is_within(downloads, &file_path) {
        return Serve::Done(detail(StatusCode::FORBIDDEN, "Forbidden"));
    }
    let file_path = match realpath(&file_path) {
        Ok(p) => p,
        Err(_) => return Serve::Done(detail(StatusCode::NOT_FOUND, "File not found")),
    };
    let stat = match file_path.metadata().ok().filter(|m| m.is_file()) {
        Some(m) => m,
        None => return Serve::Done(detail(StatusCode::NOT_FOUND, "File not found")),
    };
    let mtime = stat
        .modified()
        .ok()
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    let size = stat.len();
    let etag = format!("\"{}-{size}\"", fmt_mtime(mtime));
    let last_modified = http_date(mtime);
    if file_not_modified(headers, &etag, mtime) {
        return Serve::Done(not_modified());
    }
    serve_plain_file(&file_path, size, &etag, &last_modified, headers)
}

/// `_serve_archive_entry` for ZIP/CBZ/EPUB and RAR/CBR (pdf proxies upstream).
fn serve_archive_entry(archive: &Path, inner: &str, headers: &HeaderMap) -> Serve {
    if inner.is_empty() || inner.starts_with('/') || inner.split('/').any(|p| p == "..") {
        return Serve::Done(detail(StatusCode::BAD_REQUEST, "Invalid image path"));
    }
    let stat = match archive.metadata() {
        Ok(m) => m,
        Err(_) => return Serve::Done(detail(StatusCode::NOT_FOUND, "File not found")),
    };
    let mtime = stat
        .modified()
        .ok()
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    let last_modified = http_date(mtime);
    if !is_image_entry(inner) {
        return Serve::Done(detail(StatusCode::NOT_FOUND, "File not found"));
    }
    let digest = format!("{:x}", Sha1::digest(inner.as_bytes()));
    let etag = format!("\"{}-{}-{}\"", fmt_mtime(mtime), stat.len(), &digest[..16]);
    if archive_not_modified(headers, &etag, mtime) {
        return Serve::Done(not_modified());
    }
    if is_rar_path(archive) && bookq_core::rar_needs_upstream(archive) {
        // Multipart set — the bundled unrar can't chain volumes; 7z can.
        return Serve::Proxy;
    }
    let data = match archive_read_entry(archive, inner) {
        Some(d) => d,
        None => return Serve::Done(detail(StatusCode::NOT_FOUND, "File not found")),
    };
    Serve::Done(archive_bytes_response(
        headers,
        data,
        guess_type(inner),
        &etag,
        &last_modified,
        mtime,
    ))
}

// ── Plain-file response with hypercorn `pathsend` Range semantics ─────────────

enum RangePlan {
    Full,
    Single(u64, u64),
    Multi,
    BadRequest(&'static str),
    Unsatisfiable,
}

/// hypercorn's `Range` grammar: `bytes=` + comma list of `start-end|start-|-N`.
fn plan_range(headers: &HeaderMap, size: u64) -> RangePlan {
    let Some(h) = headers.get(header::RANGE).and_then(|v| v.to_str().ok()) else {
        return RangePlan::Full;
    };
    let Some(spec) = h.trim().strip_prefix("bytes=") else {
        return RangePlan::BadRequest("Only support bytes range");
    };
    let mut ranges = Vec::new();
    for part in spec.split(',') {
        let Some((s, e)) = part.trim().split_once('-') else {
            return RangePlan::BadRequest("Range header: range must be requested");
        };
        if s.is_empty() && e.is_empty() {
            return RangePlan::BadRequest("Range header: range must be requested");
        }
        if s.is_empty() {
            let Ok(n) = e.parse::<u64>() else {
                return RangePlan::BadRequest("Range header: range must be requested");
            };
            if n == 0 {
                return RangePlan::Unsatisfiable;
            }
            ranges.push((size.saturating_sub(n), size - 1));
            continue;
        }
        let Ok(start) = s.parse::<u64>() else {
            return RangePlan::BadRequest("Range header: range must be requested");
        };
        // Explicit end: "start must be less than end" rejects before the
        // satisfiability check (hypercorn order). Open-ended (`start-`) skips
        // that check — a beyond-EOF start answers 416, not 400.
        if !e.is_empty() {
            let Ok(raw_end) = e.parse::<u64>() else {
                return RangePlan::BadRequest("Range header: range must be requested");
            };
            if start > raw_end {
                return RangePlan::BadRequest("Range header: start must be less than end");
            }
            if start >= size {
                return RangePlan::Unsatisfiable;
            }
            ranges.push((start, raw_end.min(size - 1)));
            continue;
        }
        if start >= size {
            return RangePlan::Unsatisfiable;
        }
        ranges.push((start, size - 1));
    }
    match ranges.len() {
        0 => RangePlan::BadRequest("Range header: range must be requested"),
        1 => {
            let (s, e) = ranges[0];
            RangePlan::Single(s, e)
        }
        _ => RangePlan::Multi,
    }
}

fn serve_plain_file(path: &Path, size: u64, etag: &str, last_modified: &str, headers: &HeaderMap) -> Serve {
    let mut r = response(StatusCode::OK, Body::empty());
    set_header(&mut r, header::CACHE_CONTROL, "private, max-age=3600");
    set_header(&mut r, header::ETAG, etag);
    set_header(&mut r, header::LAST_MODIFIED, last_modified);
    set_header(&mut r, header::CONTENT_TYPE, guess_type(&path.to_string_lossy()));
    set_header(&mut r, header::ACCEPT_RANGES, "bytes");
    match plan_range(headers, size) {
        RangePlan::Full => match std::fs::read(path) {
            // Whole-buffer read: ReaderStream's 8KB chunks pay a per-chunk
            // wake through the spawn_blocking → Body stream bridge — ~1ms
            // per chunk vs hypercorn's sendfile — so a single memcpy is
            // cheaper at typical page sizes (100KB–a few MB).
            Ok(bytes) => {
                set_header(&mut r, header::CONTENT_LENGTH, &size.to_string());
                *r.body_mut() = Body::from(bytes);
                Serve::Done(r)
            }
            Err(_) => Serve::Done(detail(StatusCode::NOT_FOUND, "File not found")),
        },
        RangePlan::Single(start, end) => {
            *r.status_mut() = StatusCode::PARTIAL_CONTENT;
            set_header(&mut r, header::CONTENT_RANGE, &format!("bytes {start}-{end}/{size}"));
            set_header(&mut r, header::CONTENT_LENGTH, &(end - start + 1).to_string());
            match std::fs::File::open(path) {
                Ok(mut f) => {
                    use std::io::{Read, Seek, SeekFrom};
                    let n = (end - start + 1) as usize;
                    let mut buf = vec![0u8; n];
                    let ok = f
                        .seek(SeekFrom::Start(start))
                        .and_then(|_| f.read_exact(&mut buf));
                    match ok {
                        Ok(()) => {
                            *r.body_mut() = Body::from(buf);
                            Serve::Done(r)
                        }
                        Err(_) => Serve::Done(detail(StatusCode::NOT_FOUND, "File not found")),
                    }
                }
                Err(_) => Serve::Done(detail(StatusCode::NOT_FOUND, "File not found")),
            }
        }
        // hypercorn answers multi-range with multipart/byteranges under a
        // random boundary — unmatchable, so defer the request to upstream.
        RangePlan::Multi => Serve::Proxy,
        RangePlan::BadRequest(msg) => {
            let mut r = response(StatusCode::BAD_REQUEST, Body::from(msg.to_string()));
            set_header(&mut r, header::CONTENT_TYPE, "text/plain; charset=utf-8");
            Serve::Done(r)
        }
        RangePlan::Unsatisfiable => {
            let mut r = response(StatusCode::RANGE_NOT_SATISFIABLE, Body::empty());
            set_header(&mut r, header::CONTENT_RANGE, &format!("bytes */{size}"));
            set_header(&mut r, header::CONTENT_LENGTH, "0");
            set_header(&mut r, header::CONTENT_TYPE, "text/plain; charset=utf-8");
            Serve::Done(r)
        }
    }
}

// ── gzip (ApiGZipMiddleware parity for native /api/* responses) ───────────────

/// Apply the path-scoped gzip policy observed from Python: bodies ≥1024B on
/// compressed prefixes carry `vary: Accept-Encoding`; they are gzipped (level
/// 6 = starlette's default) only when the client advertises gzip.
pub async fn maybe_gzip(req_headers: &HeaderMap, res: Response, compressible: bool) -> Response {
    if !compressible {
        return res;
    }
    let wants_gzip = req_headers
        .get(header::ACCEPT_ENCODING)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.split(',').any(|t| t.trim().starts_with("gzip")));
    if res.body().size_hint().exact().is_none_or(|l| l < 1024) {
        return res;
    }
    let (mut parts, body) = res.into_parts();
    parts.headers.insert(header::VARY, HeaderValue::from_static("Accept-Encoding"));
    if !wants_gzip {
        return Response::from_parts(parts, body);
    }
    use http_body_util::BodyExt;
    let Ok(collected) = body.collect().await else {
        return Response::from_parts(parts, Body::empty());
    };
    let bytes = collected.to_bytes();
    // OS byte 3 (Unix) — zlib's gzip.compress sets it from the platform;
    // flate2's default header writes 255 which differs on the wire.
    let mut enc = flate2::GzBuilder::new()
        .operating_system(3)
        .write(Vec::new(), flate2::Compression::new(6));
    if enc.write_all(&bytes).is_err() {
        return Response::from_parts(parts, Body::from(bytes));
    }
    match enc.finish() {
        Ok(gz) => {
            parts.headers.insert(header::CONTENT_ENCODING, HeaderValue::from_static("gzip"));
            if let Ok(v) = HeaderValue::from_str(&gz.len().to_string()) {
                parts.headers.insert(header::CONTENT_LENGTH, v);
            }
            Response::from_parts(parts, Body::from(gz))
        }
        Err(_) => Response::from_parts(parts, Body::from(bytes)),
    }
}

impl IntoResponse for Serve {
    fn into_response(self) -> Response {
        match self {
            Serve::Done(r) => r,
            Serve::Proxy => response(StatusCode::BAD_GATEWAY, Body::from("unreachable\n")),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::http::HeaderValue;

    fn hdrs(pairs: &[(&str, &str)]) -> HeaderMap {
        let mut m = HeaderMap::new();
        for (k, v) in pairs {
            m.insert(
                header::HeaderName::from_bytes(k.as_bytes()).unwrap(),
                HeaderValue::from_str(v).unwrap(),
            );
        }
        m
    }

    #[test]
    fn range_parsing_matches_hypercorn() {
        let h = |v| hdrs(&[("range", v)]);
        assert!(matches!(plan_range(&hdrs(&[]), 100), RangePlan::Full));
        assert!(matches!(plan_range(&h("bytes=0-99"), 100), RangePlan::Single(0, 99)));
        assert!(matches!(plan_range(&h("bytes=50-"), 100), RangePlan::Single(50, 99)));
        assert!(matches!(plan_range(&h("bytes=-20"), 100), RangePlan::Single(80, 99)));
        assert!(matches!(plan_range(&h("bytes=0-500"), 100), RangePlan::Single(0, 99)));
        assert!(matches!(plan_range(&h("bytes=abc"), 100), RangePlan::BadRequest(_)));
        assert!(matches!(plan_range(&h("items=0-10"), 100), RangePlan::BadRequest(_)));
        assert!(matches!(plan_range(&h("bytes=50-40"), 100), RangePlan::BadRequest(_)));
        assert!(matches!(plan_range(&h("bytes=-"), 100), RangePlan::BadRequest(_)));
        // open-ended past EOF → 416, not 400 (the fix verified against hypercorn)
        assert!(matches!(plan_range(&h("bytes=100-"), 100), RangePlan::Unsatisfiable));
        assert!(matches!(plan_range(&h("bytes=999999999999-"), 100), RangePlan::Unsatisfiable));
        assert!(matches!(plan_range(&h("bytes=-0"), 100), RangePlan::Unsatisfiable));
        assert!(matches!(plan_range(&h("bytes=0-1,5-9"), 100), RangePlan::Multi));
    }

    #[test]
    fn archive_range_ignores_invalid() {
        assert_eq!(parse_archive_range("bytes=0-9", 100), Some((0, 9)));
        assert_eq!(parse_archive_range("bytes=-10", 100), Some((90, 99)));
        assert_eq!(parse_archive_range("bytes=10-", 100), Some((10, 99)));
        assert_eq!(parse_archive_range("bytes=0-9999", 100), Some((0, 99)));
        assert_eq!(parse_archive_range("bytes=abc", 100), None);
        assert_eq!(parse_archive_range("bytes=0-1,5-9", 100), None);
        assert_eq!(parse_archive_range("bytes=200-", 100), None);
        assert_eq!(parse_archive_range("bytes=50-40", 100), None);
        assert_eq!(parse_archive_range("items=0-9", 100), None);
    }

    #[test]
    fn image_entry_filter() {
        assert!(is_image_entry("a/b/01.jpg"));
        assert!(is_image_entry("01.PNG"));
        assert!(!is_image_entry(".hidden.jpg"));
        assert!(!is_image_entry("__MACOSX/x.jpg"));
        assert!(!is_image_entry("a/../b.jpg"));
        assert!(!is_image_entry("C:/x.jpg"));
        assert!(!is_image_entry("dir/"));
        assert!(!is_image_entry("a//b.jpg"));
        assert!(!is_image_entry("x.txt"));
    }

    #[test]
    fn etag_and_304() {
        let h = hdrs(&[("if-none-match", "\"1-2-abc\"")]);
        assert!(archive_not_modified(&h, "\"1-2-abc\"", 1000.0));
        assert!(!archive_not_modified(&h, "\"1-2-abd\"", 1000.0));
        let h2 = hdrs(&[("if-modified-since", "Sat, 31 Oct 2026 02:00:00 GMT")]);
        assert!(archive_not_modified(&h2, "\"x\"", 1_700_000_000.9)); // int(mtime) <= ts
        assert!(!archive_not_modified(&h2, "\"x\"", 1_800_000_000.9)); // mtime after header
    }
}

pub(crate) fn suffix_is_epub(name: &str) -> bool {
    name.rsplit('.').next().is_some_and(|e| e.eq_ignore_ascii_case("epub"))
}

