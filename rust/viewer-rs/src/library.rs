//! library.rs — ライブラリ編集の書き込み面 (追加 + 削除)。
//!
//! `api/openapi.v1.yaml` の `/api/v1/library/*` と `v1-semantics.md` §8 の実装:
//! multipart 単発アップロード・分割アップロードセッション・削除。すべての
//! 書き込みは対象 dir 内の `.bq-tmp-*` か library 直下の `.bookq-uploads/`
//! (scanner は dot-prefixed entry を拾わない) 経由で、fsync → atomic rename
//! で確定する。途中切断で未完成の本体がライブラリ名で残ることはない。

use axum::{
    body::Body,
    extract::Multipart,
    http::{header, HeaderMap, StatusCode},
    response::Response,
};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use tokio::io::AsyncWriteExt;

use crate::serve;
use crate::state;
use crate::AppState;

/// Staging dir for chunked sessions. Dot-prefixed — the scanner
/// (bookq-core scans.rs/meta.rs) never surfaces dot entries in the tree.
pub(crate) const UPLOAD_DIR_NAME: &str = ".bookq-uploads";
/// Per-file tmp prefix inside the upload target dir.
const TMP_PREFIX: &str = ".bq-tmp-";
/// `BOOKQ_UPLOAD_SESSION_TTL_SECS` default: 7 days.
const DEFAULT_SESSION_TTL_SECS: u64 = 7 * 24 * 3600;
/// Auto-rename tries `name (n).ext` for n in 1..=999 → 409 when exhausted.
const MAX_RENAME_ATTEMPTS: u32 = 999;
/// Final filename byte cap (POSIX NAME_MAX).
const MAX_NAME_BYTES: usize = 255;

// ── small helpers ────────────────────────────────────────────────────────────

fn json(status: StatusCode, v: serde_json::Value) -> Response {
    let mut r = Response::builder()
        .status(status)
        .body(Body::from(v.to_string()))
        .unwrap();
    r.headers_mut().insert(
        header::CONTENT_TYPE,
        axum::http::HeaderValue::from_static("application/json"),
    );
    r
}

fn uploads_disabled() -> Response {
    state::detail(StatusCode::SERVICE_UNAVAILABLE, "uploads disabled")
}

fn now_secs() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// `BOOKQ_UPLOAD_SESSION_TTL_SECS` (default 7d).
fn session_ttl() -> u64 {
    std::env::var("BOOKQ_UPLOAD_SESSION_TTL_SECS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(DEFAULT_SESSION_TTL_SECS)
}

/// `BOOKQ_UPLOAD_MAX_BYTES` — `None` = unlimited.
fn max_upload_bytes() -> Option<u64> {
    std::env::var("BOOKQ_UPLOAD_MAX_BYTES")
        .ok()
        .and_then(|v| v.parse().ok())
        .filter(|v| *v > 0)
}

fn new_id() -> String {
    format!("{:032x}", rand::random::<u128>())
}

/// The reader's archive+pdf set is also the upload allowlist.
fn upload_suffix(name: &str) -> bool {
    matches!(
        serve::suffix_lower(Path::new(name)).as_str(),
        ".zip" | ".cbz" | ".rar" | ".cbr" | ".epub" | ".pdf"
    )
}

// ── name / dir validation ────────────────────────────────────────────────────

/// Final filename rules (v1-semantics §8): basename only — no separators,
/// controls, edge spaces/dots, or leading dot (hidden + staging).
pub(crate) fn sanitize_upload_name(name: &str) -> Result<(), &'static str> {
    if name.is_empty() {
        return Err("empty name");
    }
    if name.len() > MAX_NAME_BYTES {
        return Err("name too long");
    }
    if name == "." || name == ".." {
        return Err("reserved name");
    }
    if name.starts_with(['.', ' ']) || name.ends_with([' ', '.']) {
        return Err("invalid edge characters");
    }
    if name
        .chars()
        .any(|c| c.is_control() || c == '/' || c == '\\')
    {
        return Err("invalid character in name");
    }
    Ok(())
}

/// `dir` (ルート相対、空 = ルート) → canonical dir inside library.
/// Err maps to `state::detail`: 400 unsafe shape / staging / not-a-dir,
/// 403 outside, 404 missing.
pub(crate) fn resolve_upload_dir(
    library: &Path,
    dir: &str,
) -> Result<PathBuf, (StatusCode, &'static str)> {
    if !dir.is_empty() && !serve::is_safe_entry(dir) {
        return Err((StatusCode::BAD_REQUEST, "invalid dir"));
    }
    // Dot components are never listable — refuse them outright. Besides
    // hiding user dot-dirs uniformly, this blocks `.bookq-uploads` under
    // every case spelling (the post-realpath name compare below preserves
    // the caller's case on case-insensitive filesystems).
    if serve::has_dot_component(dir) {
        return Err((StatusCode::BAD_REQUEST, "invalid dir"));
    }
    let cand = if dir.is_empty() {
        library.to_path_buf()
    } else {
        library.join(dir)
    };
    if !serve::is_within(library, &cand) {
        return Err((StatusCode::FORBIDDEN, "dir escapes library root"));
    }
    let rp = match serve::realpath(&cand) {
        Ok(p) => p,
        Err(_) => return Err((StatusCode::NOT_FOUND, "dir not found")),
    };
    match std::fs::metadata(&rp) {
        Err(_) => Err((StatusCode::NOT_FOUND, "dir not found")),
        Ok(m) if !m.is_dir() => Err((StatusCode::BAD_REQUEST, "dir is not a directory")),
        Ok(_) => {
            if let Ok(rel) = rp.strip_prefix(library) {
                if rel
                    .components()
                    .next()
                    .and_then(|c| c.as_os_str().to_str())
                    == Some(UPLOAD_DIR_NAME)
                {
                    return Err((
                        StatusCode::BAD_REQUEST,
                        "dir is the upload staging area",
                    ));
                }
            }
            Ok(rp)
        }
    }
}

// ── placement ────────────────────────────────────────────────────────────────

/// Any filesystem entry at `p` (incl. dangling symlinks — a dangling link
/// must still block the slot).
fn exists_any(p: &Path) -> bool {
    p.symlink_metadata().is_ok()
}

/// `name` or `stem (n).ext` — first free slot in `dir` (files AND dirs
/// collide). `None` after MAX_RENAME_ATTEMPTS → caller answers 409.
pub(crate) fn dedupe_name(dir: &Path, name: &str) -> Option<PathBuf> {
    let cand = dir.join(name);
    if !exists_any(&cand) {
        return Some(cand);
    }
    let (stem, ext) = match name.rfind('.') {
        Some(i) if i > 0 => (&name[..i], &name[i..]),
        _ => (name, ""),
    };
    for n in 1..=MAX_RENAME_ATTEMPTS {
        let c = dir.join(format!("{stem} ({n}){ext}"));
        if !exists_any(&c) {
            return Some(c);
        }
    }
    None
}

/// fsync the tmp file, rename into place, fsync the containing dir
/// (best-effort — some filesystems disallow dir fsync).
fn atomic_place(tmp: &Path, dest: &Path) -> std::io::Result<()> {
    {
        let f = std::fs::File::options().write(true).open(tmp)?;
        f.sync_all()?;
    }
    std::fs::rename(tmp, dest)?;
    if let Some(parent) = dest.parent() {
        if let Ok(d) = std::fs::File::open(parent) {
            let _ = d.sync_all();
        }
    }
    Ok(())
}

/// Result entry `{name, path}`: `dir_rel` is the client-supplied root-relative
/// dir ("" = root) so the response path mirrors what the client browses.
fn placed_entry(dir_rel: &str, dest: &Path) -> serde_json::Value {
    let name = dest
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    let path = if dir_rel.is_empty() {
        name.clone()
    } else {
        format!("{dir_rel}/{name}")
    };
    serde_json::json!({"name": name, "path": path})
}

// ── chunked sessions ─────────────────────────────────────────────────────────

#[derive(Serialize, Deserialize, Clone)]
pub(crate) struct UploadSession {
    pub dir: String,
    pub name: String,
    pub size: u64,
    pub offset: u64,
    pub created: u64,
    /// Creator's email — a session id alone must not let another member
    /// probe, append to, cancel, or finalize someone else's upload.
    /// `default` keeps pre-owner meta.json loadable; an empty owner then
    /// fails every `session_owned` check (fail closed) until the TTL
    /// sweep drops the session.
    #[serde(default)]
    pub owner: String,
}

/// Opaque ids are server-generated hex — anything else is simply "not found".
fn valid_id(id: &str) -> bool {
    (16..=64).contains(&id.len())
        && id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

fn session_dir(upload_root: &Path, id: &str) -> PathBuf {
    upload_root.join(id)
}

fn load_session(
    upload_root: &Path,
    id: &str,
) -> Option<(UploadSession, PathBuf, PathBuf)> {
    if !valid_id(id) {
        return None;
    }
    let dir = session_dir(upload_root, id);
    let text = std::fs::read_to_string(dir.join("meta.json")).ok()?;
    let sess: UploadSession = serde_json::from_str(&text).ok()?;
    let blob = dir.join("blob");
    // Reconcile the acked offset against the blob: meta is written after the
    // data fsync, so `offset` never leads the file — if it does the pair is
    // corrupt. A longer blob is an unconfirmed tail (the append landed but
    // the offset persist failed or the request raced) — truncate it so a
    // resent chunk can't double-append.
    let blob_len = std::fs::metadata(&blob).map(|m| m.len()).unwrap_or(0);
    if blob_len < sess.offset {
        return None;
    }
    if blob_len > sess.offset {
        let f = std::fs::File::options().write(true).open(&blob).ok()?;
        f.set_len(sess.offset).ok()?;
    }
    Some((sess, dir, blob))
}

/// Owner binding for every session op — the 128-bit id is bearer material
/// only to its creator; anyone else gets the same 404 as a missing
/// session so the endpoint isn't an existence oracle either.
fn session_owned(sess: &UploadSession, owner: &str) -> bool {
    !sess.owner.is_empty() && sess.owner.eq_ignore_ascii_case(owner)
}

fn save_session(dir: &Path, sess: &UploadSession) -> std::io::Result<()> {
    let meta = dir.join("meta.json");
    let tmp = dir.join("meta.tmp");
    std::fs::write(&tmp, serde_json::to_vec(sess)?)?;
    std::fs::rename(&tmp, &meta)
}

/// Drop sessions older than `ttl_secs`. A dir whose meta is unreadable is
/// only stale once it is also older than `SWEEP_GRACE_SECS` — an init races
/// this sweep between `create_dir_all` and the first `save_session`, and a
/// bare "no meta" verdict would delete a session still being created.
const SWEEP_GRACE_SECS: u64 = 60;

pub(crate) fn sweep_stale_sessions(upload_root: &Path, ttl_secs: u64) {
    let Ok(rd) = std::fs::read_dir(upload_root) else {
        return;
    };
    let now = now_secs();
    for e in rd.flatten() {
        let name = e.file_name();
        let Some(id) = name.to_str() else { continue };
        if !valid_id(id) {
            continue;
        }
        let meta = e.path().join("meta.json");
        let stale = std::fs::read_to_string(&meta)
            .ok()
            .and_then(|s| serde_json::from_str::<UploadSession>(&s).ok())
            .map(|s| now.saturating_sub(s.created) > ttl_secs)
            .unwrap_or_else(|| {
                // Meta missing/corrupt — stale only past the grace window,
                // measured on the dir's own mtime.
                e.metadata()
                    .and_then(|m| m.modified())
                    .ok()
                    .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                    .map(|d| now.saturating_sub(d.as_secs()) > SWEEP_GRACE_SECS)
                    .unwrap_or(false)
            });
        if stale {
            let _ = std::fs::remove_dir_all(e.path());
        }
    }
}

/// Startup probe: `<library>/.bookq-uploads/` creatable → uploads enabled.
pub(crate) fn probe_upload_root(library: &Path) -> Option<PathBuf> {
    let root = library.join(UPLOAD_DIR_NAME);
    match std::fs::create_dir_all(&root) {
        Ok(()) => {
            sweep_stale_sessions(&root, session_ttl());
            Some(root)
        }
        Err(e) => {
            tracing::warn!(
                path = %root.display(),
                error = %e,
                "library not writable — library upload/delete answer 503"
            );
            None
        }
    }
}

// ── POST /api/v1/library/files (multipart) ───────────────────────────────────

/// Per-file validation before any byte is written.
fn precheck_file(name: &str) -> Option<&'static str> {
    if sanitize_upload_name(name).is_err() {
        return Some("invalid_name");
    }
    if !upload_suffix(name) {
        return Some("unsupported_type");
    }
    None
}

/// Drain a field we're skipping — `next_field` only advances once the
/// previous field is consumed.
async fn drain_field(field: &mut axum::extract::multipart::Field<'_>) {
    while let Ok(Some(_)) = field.chunk().await {}
}

/// Does the part's Content-Disposition actually carry a `filename` token
/// (`filename=` or RFC 5987 `filename*=`)? Checked on the raw bytes —
/// the whole point is catching names the UTF-8 decode couldn't show us.
/// Mirrors multer's parameter boundary: the token must start at a `;`
/// or ASCII whitespace boundary so `myfilename=` can't false-positive.
/// (Not quote-aware, like multer itself — harmless because this check
/// is only consulted when multer already failed to decode the name.)
fn disposition_has_filename(headers: &axum::http::HeaderMap) -> bool {
    let Some(v) = headers.get(axum::http::header::CONTENT_DISPOSITION) else {
        return false;
    };
    let b = v.as_bytes();
    (0..b.len()).any(|i| {
        (i == 0 || matches!(b[i - 1], b';' | b' ' | b'\t'))
            && (b[i..].starts_with(b"filename=") || b[i..].starts_with(b"filename*="))
    })
}

/// `dir` is resolved + validated by the caller. Streams each `file` field to
/// a `.bq-tmp-*` sibling then renames; per-file failures land in `errors[]`.
pub(crate) async fn post_files(
    st: &AppState,
    dir: &str,
    mut mp: Multipart,
) -> Response {
    let Some(_root) = st.upload_root.clone() else {
        return uploads_disabled();
    };
    let target = match resolve_upload_dir(&st.library, dir) {
        Ok(p) => p,
        Err((c, m)) => return state::detail(c, m),
    };
    let cap = max_upload_bytes();
    let mut files: Vec<serde_json::Value> = Vec::new();
    let mut errors: Vec<serde_json::Value> = Vec::new();
    let mut saw_malformed = false;
    loop {
        let mut field = match mp.next_field().await {
            Ok(Some(f)) => f,
            Ok(None) => break,
            Err(_) => {
                // Mid-stream parse failure must not hide the files that
                // already placed — report them (and refresh the index)
                // instead of returning a bare 400.
                saw_malformed = true;
                errors.push(serde_json::json!({"name": "*", "code": "malformed"}));
                break;
            }
        };
        if field.name() != Some("file") {
            drain_field(&mut field).await;
            continue;
        }
        // A `file` part without a filename is a form field, not a file
        // upload — skip it like unknown fields so an all-nonfile request
        // hits the "no file fields" 400 below instead of a 200. But
        // `file_name()` is also None when the name exists and just isn't
        // UTF-8 (or is RFC 5987 `filename*=` only) — that IS an upload
        // attempt, so report it as a per-file error instead of skipping.
        let name = match field.file_name() {
            Some(n) => n.to_string(),
            None if disposition_has_filename(field.headers()) => {
                drain_field(&mut field).await;
                errors.push(serde_json::json!({"name": "?", "code": "invalid_name"}));
                continue;
            }
            None => {
                drain_field(&mut field).await;
                continue;
            }
        };
        if let Some(code) = precheck_file(&name) {
            drain_field(&mut field).await;
            errors.push(serde_json::json!({"name": name, "code": code}));
            continue;
        }
        let tmp = target.join(format!("{TMP_PREFIX}{}", new_id()));
        let mut out = match tokio::fs::File::create(&tmp).await {
            Ok(f) => f,
            Err(_) => {
                drain_field(&mut field).await;
                errors.push(serde_json::json!({"name": name, "code": "io_error"}));
                continue;
            }
        };
        let mut written: u64 = 0;
        let mut fail: Option<&str> = None;
        loop {
            match field.chunk().await {
                Ok(Some(chunk)) => {
                    if let Some(cap) = cap {
                        if written + chunk.len() as u64 > cap {
                            fail = Some("too_large");
                            break;
                        }
                    }
                    if out.write_all(&chunk).await.is_err() {
                        fail = Some("io_error");
                        break;
                    }
                    written += chunk.len() as u64;
                }
                Ok(None) => break,
                Err(_) => {
                    fail = Some("io_error");
                    break;
                }
            }
        }
        drop(out);
        if let Some(code) = fail {
            let _ = tokio::fs::remove_file(&tmp).await;
            drain_field(&mut field).await;
            errors.push(serde_json::json!({"name": name, "code": code}));
            continue;
        }
        // Dedupe + rename under the placement lock — a check-then-rename
        // across tasks lets two same-name uploads pick the same free slot,
        // and Unix `rename` silently replaces the destination.
        let placed = {
            let lock = st.place_lock.clone();
            let target_c = target.clone();
            let name_c = name.clone();
            let tmp_c = tmp.clone();
            tokio::task::spawn_blocking(move || {
                let _g = lock.lock().unwrap_or_else(|e| e.into_inner());
                let Some(dest) = dedupe_name(&target_c, &name_c) else {
                    return Err("conflict");
                };
                atomic_place(&tmp_c, &dest)
                    .map(|()| dest)
                    .map_err(|_| "io_error")
            })
            .await
        };
        match placed {
            Ok(Ok(dest)) => {
                let mut e = placed_entry(dir, &dest);
                e["size"] = serde_json::json!(written);
                files.push(e);
            }
            Ok(Err(code)) => {
                let _ = tokio::fs::remove_file(&tmp).await;
                errors.push(serde_json::json!({"name": name, "code": code}));
            }
            Err(_) => {
                let _ = tokio::fs::remove_file(&tmp).await;
                errors.push(serde_json::json!({"name": name, "code": "io_error"}));
            }
        }
    }
    // Nothing placed: an empty request is 400, and so is a body that
    // failed to parse — a malformed multipart is a bad request, not a
    // per-file rejection (unlike unsupported extensions, which are
    // schema-valid input reported under `errors` at 200).
    if files.is_empty() && (errors.is_empty() || saw_malformed) {
        let msg = if saw_malformed { "malformed multipart body" } else { "no file fields" };
        return state::detail(StatusCode::BAD_REQUEST, msg);
    }
    if !files.is_empty() {
        st.index.schedule_refresh(&st.library);
    }
    json(
        StatusCode::OK,
        serde_json::json!({"files": files, "errors": errors}),
    )
}

// ── DELETE /api/v1/library/files ─────────────────────────────────────────────

/// `path` (ルート相対) — file or recursive dir. Root/staging refuse 400,
/// outside 403, missing 404.
pub(crate) async fn delete_file(st: &AppState, path: &str) -> Response {
    let Some(_root) = st.upload_root.clone() else {
        return uploads_disabled();
    };
    if path.is_empty() || !serve::is_safe_entry(path) {
        return state::detail(StatusCode::BAD_REQUEST, "invalid path");
    }
    // Dot-prefixed components are never listable — covers `.bookq-uploads`
    // in any caller-supplied case on case-insensitive filesystems.
    if serve::has_dot_component(path) {
        return state::detail(StatusCode::BAD_REQUEST, "invalid path");
    }
    if path.split('/').next() == Some(UPLOAD_DIR_NAME) {
        return state::detail(
            StatusCode::BAD_REQUEST,
            "cannot delete the upload staging area",
        );
    }
    let cand = st.library.join(path);
    if !serve::is_within(&st.library, &cand) {
        return state::detail(
            StatusCode::FORBIDDEN,
            "path escapes library root",
        );
    }
    let library = st.library.clone();
    let path_owned = path.to_string();
    let out = tokio::task::spawn_blocking(move || {
        let rp = serve::realpath(&cand)
            .map_err(|_| StatusCode::NOT_FOUND)?;
        if rp == library {
            return Err(StatusCode::BAD_REQUEST);
        }
        if let Ok(rel) = rp.strip_prefix(&library) {
            if rel
                .components()
                .next()
                .and_then(|c| c.as_os_str().to_str())
                == Some(UPLOAD_DIR_NAME)
            {
                return Err(StatusCode::BAD_REQUEST);
            }
        }
        let md = std::fs::metadata(&rp).map_err(|_| StatusCode::NOT_FOUND)?;
        let kind = if md.is_dir() { "dir" } else { "file" };
        let res = if md.is_dir() {
            std::fs::remove_dir_all(&rp)
        } else {
            std::fs::remove_file(&rp)
        };
        match res {
            Ok(()) => Ok(kind),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                Err(StatusCode::NOT_FOUND)
            }
            Err(_) => Err(StatusCode::INTERNAL_SERVER_ERROR),
        }
    })
    .await;
    match out {
        Ok(Ok(kind)) => {
            st.index.schedule_refresh(&st.library);
            json(
                StatusCode::OK,
                serde_json::json!({"deleted": path_owned, "kind": kind}),
            )
        }
        Ok(Err(StatusCode::NOT_FOUND)) => {
            state::detail(StatusCode::NOT_FOUND, "not found")
        }
        Ok(Err(StatusCode::BAD_REQUEST)) => {
            state::detail(StatusCode::BAD_REQUEST, "cannot delete this path")
        }
        _ => state::detail(StatusCode::INTERNAL_SERVER_ERROR, "delete failed"),
    }
}

// ── /api/v1/library/uploads (chunked sessions) ───────────────────────────────

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct InitBody {
    #[serde(default)]
    dir: String,
    name: String,
    size: u64,
}

/// POST /api/v1/library/uploads — `{dir,name,size}` → 201 `{upload_id,offset}`.
pub(crate) async fn uploads_init(st: &AppState, body: &[u8], owner: &str) -> Response {
    let Some(root) = st.upload_root.clone() else {
        return uploads_disabled();
    };
    let init: InitBody = match serde_json::from_slice(body) {
        Ok(v) => v,
        Err(_) => return state::detail(StatusCode::BAD_REQUEST, "invalid body"),
    };
    if let Err(msg) = sanitize_upload_name(&init.name) {
        return state::detail(StatusCode::BAD_REQUEST, msg);
    }
    if !upload_suffix(&init.name) {
        return state::detail(
            StatusCode::UNSUPPORTED_MEDIA_TYPE,
            "unsupported file type",
        );
    }
    if init.size == 0 {
        return state::detail(StatusCode::BAD_REQUEST, "size must be >= 1");
    }
    if let Some(cap) = max_upload_bytes() {
        if init.size > cap {
            return state::detail(
                StatusCode::PAYLOAD_TOO_LARGE,
                "size exceeds upload limit",
            );
        }
    }
    if let Err((c, m)) = resolve_upload_dir(&st.library, &init.dir) {
        return state::detail(c, m);
    }
    // Sweep under the session lock: it deletes session dirs, and an
    // append/finalize in flight on a stale-but-active session must not
    // have its dir removed mid-write. Held through the create so a
    // concurrent init's sweep can't see this dir pre-meta (belt-and-braces
    // with the sweep grace — the lock makes the gap unreachable anyway).
    let _sess_guard = st.session_lock.lock().await;
    sweep_stale_sessions(&root, session_ttl());
    let id = new_id();
    let dir = session_dir(&root, &id);
    let sess = UploadSession {
        dir: init.dir,
        name: init.name,
        size: init.size,
        offset: 0,
        created: now_secs(),
        owner: owner.to_string(),
    };
    let created = tokio::task::spawn_blocking(move || {
        std::fs::create_dir_all(&dir)?;
        std::fs::File::create(dir.join("blob"))?;
        save_session(&dir, &sess)
    })
    .await;
    match created {
        Ok(Ok(())) => json(
            StatusCode::CREATED,
            serde_json::json!({"upload_id": id, "offset": 0}),
        ),
        _ => state::detail(
            StatusCode::INTERNAL_SERVER_ERROR,
            "could not create upload session",
        ),
    }
}

/// GET /api/v1/library/uploads/{id} — resume probe.
pub(crate) async fn upload_get(st: &AppState, id: &str, owner: &str) -> Response {
    let Some(root) = st.upload_root.clone() else {
        return uploads_disabled();
    };
    // Read-only to the client, but load_session's reconcile WRITES (it
    // truncates an unconfirmed tail) — a probe that lands mid-append would
    // otherwise cut the blob under the in-flight PUT and strand the
    // session. Same lock as every other session mutation.
    let _sess_guard = st.session_lock.lock().await;
    let Some((sess, _, _)) = load_session(&root, id) else {
        return state::detail(StatusCode::NOT_FOUND, "not found");
    };
    if !session_owned(&sess, owner) {
        return state::detail(StatusCode::NOT_FOUND, "not found");
    }
    json(
        StatusCode::OK,
        serde_json::json!({
            "upload_id": id,
            "dir": sess.dir,
            "name": sess.name,
            "size": sess.size,
            "offset": sess.offset,
        }),
    )
}

/// DELETE /api/v1/library/uploads/{id} — discard the session.
pub(crate) async fn upload_cancel(st: &AppState, id: &str, owner: &str) -> Response {
    let Some(root) = st.upload_root.clone() else {
        return uploads_disabled();
    };
    // Serialize against an in-flight append — deleting the dir mid-append
    // would orphan a half-written blob.
    let _sess_guard = st.session_lock.lock().await;
    let Some((sess, dir, _)) = load_session(&root, id) else {
        return state::detail(StatusCode::NOT_FOUND, "not found");
    };
    if !session_owned(&sess, owner) {
        return state::detail(StatusCode::NOT_FOUND, "not found");
    }
    let gone = tokio::task::spawn_blocking(move || std::fs::remove_dir_all(&dir)).await;
    match gone {
        Ok(Ok(())) => Response::builder()
            .status(StatusCode::NO_CONTENT)
            .body(Body::empty())
            .unwrap(),
        _ => state::detail(StatusCode::INTERNAL_SERVER_ERROR, "delete failed"),
    }
}

/// PUT /api/v1/library/uploads/{id} — append the next chunk at
/// `Upload-Offset`. Mismatch answers 409 + `{offset}` so the client resumes
/// from the authoritative position. `Content-Length` is required so an
/// over-declared total can be rejected before a single byte lands.
pub(crate) async fn upload_append(
    st: &AppState,
    id: &str,
    owner: &str,
    headers: &HeaderMap,
    body: Body,
) -> Response {
    let Some(root) = st.upload_root.clone() else {
        return uploads_disabled();
    };
    // Serialize the load→append→persist cycle — two PUTs at the same
    // offset both pass the check otherwise and the blob/meta pair
    // diverges. One lock covers every session: correctness over fairness
    // (a stalled client delays other sessions' ops; appends are short).
    let _sess_guard = st.session_lock.lock().await;
    let Some((mut sess, dir, blob)) = load_session(&root, id) else {
        return state::detail(StatusCode::NOT_FOUND, "not found");
    };
    if !session_owned(&sess, owner) {
        return state::detail(StatusCode::NOT_FOUND, "not found");
    }
    let off = headers
        .get("upload-offset")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.parse::<u64>().ok());
    let Some(off) = off else {
        return state::detail(StatusCode::BAD_REQUEST, "invalid Upload-Offset");
    };
    if off != sess.offset {
        return json(
            StatusCode::CONFLICT,
            serde_json::json!({
                "detail": "offset mismatch",
                "code": "offset_mismatch",
                "offset": sess.offset,
            }),
        );
    }
    let clen = headers
        .get(header::CONTENT_LENGTH)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.parse::<u64>().ok());
    let Some(clen) = clen else {
        return state::detail(
            StatusCode::BAD_REQUEST,
            "Content-Length required",
        );
    };
    if sess.offset.saturating_add(clen) > sess.size {
        return state::detail(
            StatusCode::BAD_REQUEST,
            "chunk exceeds declared size",
        );
    }
    let mut f = match tokio::fs::OpenOptions::new()
        .append(true)
        .open(&blob)
        .await
    {
        Ok(f) => f,
        Err(_) => {
            return state::detail(
                StatusCode::INTERNAL_SERVER_ERROR,
                "could not open upload blob",
            );
        }
    };
    let mut stream = http_body_util::BodyExt::into_data_stream(body);
    let mut received: u64 = 0;
    let mut io_fail = false;
    while let Some(item) = futures_util::StreamExt::next(&mut stream).await {
        match item {
            Ok(chunk) => {
                if received + chunk.len() as u64 > clen {
                    io_fail = true;
                    break;
                }
                if f.write_all(&chunk).await.is_err() {
                    io_fail = true;
                    break;
                }
                received += chunk.len() as u64;
            }
            Err(_) => {
                io_fail = true;
                break;
            }
        }
    }
    if f.sync_all().await.is_err() {
        io_fail = true;
    }
    drop(f);
    // Bytes already landed are a valid prefix — persist the advanced offset
    // even on a short/aborted body so the client resumes from the truth. If
    // the persist fails the acked offset would be a lie (load reconciles the
    // blob down to the last saved offset), so fail the request instead.
    if received > 0 {
        sess.offset += received;
        if save_session(&dir, &sess).is_err() {
            return state::detail(
                StatusCode::INTERNAL_SERVER_ERROR,
                "could not persist upload state",
            );
        }
    }
    if io_fail || received != clen {
        return state::detail(
            StatusCode::BAD_REQUEST,
            "incomplete chunk body",
        );
    }
    json(
        StatusCode::OK,
        serde_json::json!({"offset": sess.offset}),
    )
}

/// POST /api/v1/library/uploads/{id}/finalize — `offset == size` required;
/// fsync → auto-rename → atomic rename → session cleanup.
pub(crate) async fn upload_finalize(st: &AppState, id: &str, owner: &str) -> Response {
    let Some(root) = st.upload_root.clone() else {
        return uploads_disabled();
    };
    // Same serialization as append — a racing PUT must not land more bytes
    // while the blob is being renamed out from under it.
    let _sess_guard = st.session_lock.lock().await;
    let Some((sess, dir, blob)) = load_session(&root, id) else {
        return state::detail(StatusCode::NOT_FOUND, "not found");
    };
    if !session_owned(&sess, owner) {
        return state::detail(StatusCode::NOT_FOUND, "not found");
    }
    // The blob is the authority, not the meta counter — load_session already
    // reconciles them (shorter = corrupt → None, longer = truncate), so
    // agreeing here means the file really holds `size` bytes.
    let blob_len = std::fs::metadata(&blob).map(|m| m.len()).unwrap_or(0);
    if sess.offset != sess.size || blob_len != sess.size {
        return json(
            StatusCode::CONFLICT,
            serde_json::json!({
                "detail": "upload incomplete",
                "code": "incomplete",
                "offset": sess.offset,
            }),
        );
    }
    // Re-validate at finalize: the session predates any later constraint
    // change, and `dir` may have been deleted since init.
    if let Err(msg) = sanitize_upload_name(&sess.name) {
        return state::detail(StatusCode::BAD_REQUEST, msg);
    }
    if !upload_suffix(&sess.name) {
        return state::detail(
            StatusCode::UNSUPPORTED_MEDIA_TYPE,
            "unsupported file type",
        );
    }
    let target = match resolve_upload_dir(&st.library, &sess.dir) {
        Ok(p) => p,
        Err((c, m)) => return state::detail(c, m),
    };
    // Dedupe + place under the same placement lock as post_files.
    let placed = {
        let lock = st.place_lock.clone();
        let name_c = sess.name.clone();
        let dir2 = dir.clone();
        tokio::task::spawn_blocking(move || {
            let _g = lock.lock().unwrap_or_else(|e| e.into_inner());
            let Some(dest) = dedupe_name(&target, &name_c) else {
                return Err("conflict");
            };
            atomic_place(&blob, &dest)
                .map(|()| {
                    let _ = std::fs::remove_dir_all(&dir2);
                    dest
                })
                .map_err(|_| "io_error")
        })
        .await
    };
    match placed {
        Ok(Ok(dest)) => {
            st.index.schedule_refresh(&st.library);
            let mut e = placed_entry(&sess.dir, &dest);
            e["size"] = serde_json::json!(sess.size);
            json(StatusCode::OK, e)
        }
        Ok(Err("conflict")) => {
            state::detail(StatusCode::CONFLICT, "no free filename slot")
        }
        _ => state::detail(StatusCode::INTERNAL_SERVER_ERROR, "finalize failed"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmpdir(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!(
            "bookq-lib-{}-{}-{}",
            tag,
            std::process::id(),
            now_secs_nanos()
        ));
        std::fs::create_dir_all(&d).unwrap();
        // `is_within` expects a canonical base (AppState canonicalizes
        // `library` at startup) — temp_dir is a symlink on macOS.
        d.canonicalize().unwrap()
    }

    fn now_secs_nanos() -> u128 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0)
    }

    #[test]
    fn sanitize_accepts_plain_and_unicode_names() {
        assert!(sanitize_upload_name("work.cbz").is_ok());
        assert!(sanitize_upload_name("vol 01.part01.rar").is_ok());
        assert!(sanitize_upload_name("第01巻.zip").is_ok());
        assert!(sanitize_upload_name("a-b_c (2).epub").is_ok());
    }

    #[test]
    fn sanitize_rejects_hostile_names() {
        for n in [
            "",
            ".",
            "..",
            ".hidden.zip",
            " a.zip",
            "a.zip ",
            "a.zip.",
            "a/b.zip",
            "a\\b.zip",
            "a\0b.zip",
            "a\nb.zip",
            "CON ", // trailing space already covered, keep explicit
        ] {
            assert!(sanitize_upload_name(n).is_err(), "expected reject: {n:?}");
        }
        let long = "a".repeat(256) + ".zip";
        assert!(sanitize_upload_name(&long).is_err());
    }

    #[test]
    fn upload_suffix_matches_reader_set() {
        for n in [
            "w.zip", "w.cbz", "w.rar", "w.cbr", "w.epub", "w.pdf", "W.RAR",
        ] {
            assert!(upload_suffix(n), "expected accept: {n}");
        }
        for n in ["w.txt", "w.png", "w.7z", "w", ".rar"] {
            assert!(!upload_suffix(n), "expected reject: {n}");
        }
    }

    #[test]
    fn dedupe_picks_free_slot() {
        let d = tmpdir("dedupe");
        assert_eq!(dedupe_name(&d, "a.rar").unwrap(), d.join("a.rar"));
        std::fs::write(d.join("a.rar"), b"x").unwrap();
        assert_eq!(dedupe_name(&d, "a.rar").unwrap(), d.join("a (1).rar"));
        std::fs::write(d.join("a (1).rar"), b"x").unwrap();
        assert_eq!(dedupe_name(&d, "a.rar").unwrap(), d.join("a (2).rar"));
        // a dir collides too
        std::fs::create_dir(d.join("d")).unwrap();
        assert_eq!(dedupe_name(&d, "d").unwrap(), d.join("d (1)"));
        std::fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn resolve_dir_root_and_nested() {
        let d = tmpdir("resolve");
        std::fs::create_dir(d.join("sub")).unwrap();
        assert_eq!(resolve_upload_dir(&d, "").unwrap(), d);
        assert_eq!(
            resolve_upload_dir(&d, "sub").unwrap(),
            serve::realpath(&d.join("sub")).unwrap()
        );
        std::fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn resolve_dir_rejects_escape_missing_nondir() {
        let d = tmpdir("resolve-bad");
        std::fs::write(d.join("f.txt"), b"x").unwrap();
        assert_eq!(
            resolve_upload_dir(&d, "..").unwrap_err().0,
            StatusCode::BAD_REQUEST
        );
        assert_eq!(
            resolve_upload_dir(&d, "a/../b").unwrap_err().0,
            StatusCode::BAD_REQUEST
        );
        assert_eq!(
            resolve_upload_dir(&d, "missing").unwrap_err().0,
            StatusCode::NOT_FOUND
        );
        assert_eq!(
            resolve_upload_dir(&d, "f.txt").unwrap_err().0,
            StatusCode::BAD_REQUEST
        );
        std::fs::remove_dir_all(&d).ok();
    }

    #[cfg(unix)]
    #[test]
    fn resolve_dir_rejects_symlink_escape_and_staging() {
        let d = tmpdir("resolve-sym");
        let outside = tmpdir("resolve-out");
        std::os::unix::fs::symlink(&outside, d.join("link")).unwrap();
        assert_eq!(
            resolve_upload_dir(&d, "link").unwrap_err().0,
            StatusCode::FORBIDDEN
        );
        std::fs::create_dir(d.join(UPLOAD_DIR_NAME)).unwrap();
        assert_eq!(
            resolve_upload_dir(&d, UPLOAD_DIR_NAME).unwrap_err().0,
            StatusCode::BAD_REQUEST
        );
        std::fs::remove_dir_all(&d).ok();
        std::fs::remove_dir_all(&outside).ok();
    }

    #[test]
    fn session_roundtrip_and_validation() {
        let d = tmpdir("session");
        let id = new_id();
        let sdir = session_dir(&d, &id);
        std::fs::create_dir_all(&sdir).unwrap();
        let sess = UploadSession {
            dir: "sub".into(),
            name: "a.rar".into(),
            size: 10,
            offset: 4,
            created: now_secs(),
            owner: "a@example.com".into(),
        };
        save_session(&sdir, &sess).unwrap();
        // reconcile: blob must reach at least the acked offset.
        std::fs::write(sdir.join("blob"), b"1234").unwrap();
        let (loaded, _, blob) = load_session(&d, &id).unwrap();
        assert_eq!(loaded.offset, 4);
        assert_eq!(loaded.name, "a.rar");
        assert_eq!(blob, sdir.join("blob"));
        assert!(load_session(&d, "../etc").is_none());
        assert!(load_session(&d, "short").is_none());
        assert!(load_session(&d, &"x".repeat(32)).is_none()); // no meta.json
        std::fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn session_owned_binds_creator() {
        let sess = UploadSession {
            dir: String::new(),
            name: "a.rar".into(),
            size: 1,
            offset: 0,
            created: now_secs(),
            owner: "Alice@Example.com".into(),
        };
        assert!(session_owned(&sess, "alice@example.com"));
        assert!(!session_owned(&sess, "bob@example.com"));
        // Legacy meta without an owner fails closed.
        let mut legacy = sess.clone();
        legacy.owner = String::new();
        assert!(!session_owned(&legacy, "alice@example.com"));
    }

    #[test]
    fn sweep_drops_stale_sessions() {
        let d = tmpdir("sweep");
        for (id, created) in [
            ("old", 0u64),
            ("new", now_secs()),
        ] {
            let id = if id == "old" {
                "a".repeat(32)
            } else {
                "b".repeat(32)
            };
            let sdir = session_dir(&d, &id);
            std::fs::create_dir_all(&sdir).unwrap();
            save_session(
                &sdir,
                &UploadSession {
                    dir: String::new(),
                    name: "a.rar".into(),
                    size: 1,
                    offset: 0,
                    created,
                    owner: "a@example.com".into(),
                },
            )
            .unwrap();
        }
        sweep_stale_sessions(&d, 60);
        assert!(!session_dir(&d, &"a".repeat(32)).exists());
        assert!(session_dir(&d, &"b".repeat(32)).exists());
        // garbage entries are left alone (not ours to delete)
        std::fs::write(d.join("stray"), b"x").unwrap();
        sweep_stale_sessions(&d, 60);
        assert!(d.join("stray").exists());
        std::fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn atomic_place_moves_and_dedupes() {
        let d = tmpdir("place");
        let tmp = d.join(".bq-tmp-x");
        std::fs::write(&tmp, b"data").unwrap();
        atomic_place(&tmp, &d.join("a.rar")).unwrap();
        assert_eq!(std::fs::read(d.join("a.rar")).unwrap(), b"data");
        assert!(!tmp.exists());
        std::fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn load_session_reconciles_blob_against_meta() {
        let d = tmpdir("reconcile");
        let id = new_id();
        let sdir = session_dir(&d, &id);
        std::fs::create_dir_all(&sdir).unwrap();
        let sess = UploadSession {
            dir: String::new(),
            name: "a.rar".into(),
            size: 10,
            offset: 4,
            created: now_secs(),
            owner: "a@example.com".into(),
        };
        save_session(&sdir, &sess).unwrap();
        // Unconfirmed tail: the append landed but the offset persist died —
        // the blob is truncated back to the acked offset so a resent chunk
        // can't double-append.
        std::fs::write(sdir.join("blob"), b"1234567").unwrap();
        let (loaded, _, _) = load_session(&d, &id).unwrap();
        assert_eq!(loaded.offset, 4);
        assert_eq!(std::fs::metadata(sdir.join("blob")).unwrap().len(), 4);
        // Meta ahead of the blob is corrupt, not recoverable.
        let mut ahead = sess.clone();
        ahead.offset = 9;
        save_session(&sdir, &ahead).unwrap();
        assert!(load_session(&d, &id).is_none());
        // A fresh session (offset 0, no blob yet) still loads.
        let mut fresh = sess;
        fresh.offset = 0;
        save_session(&sdir, &fresh).unwrap();
        std::fs::remove_file(sdir.join("blob")).unwrap();
        assert!(load_session(&d, &id).is_some());
        std::fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn sweep_keeps_fresh_metaless_dirs() {
        let d = tmpdir("sweep-grace");
        // An init in flight — dir exists, meta.json not yet written. A
        // concurrent sweep must not reap it inside the grace window.
        let id = "c".repeat(32);
        let sdir = session_dir(&d, &id);
        std::fs::create_dir_all(&sdir).unwrap();
        sweep_stale_sessions(&d, 60);
        assert!(sdir.exists());
        // Corrupt meta on a fresh dir lands on the same grace branch.
        std::fs::write(sdir.join("meta.json"), b"{not json").unwrap();
        sweep_stale_sessions(&d, 60);
        assert!(sdir.exists());
        std::fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn dedupe_under_lock_is_race_free() {
        // The handler's critical section, at unit level: N racers each do
        // dedupe + publish inside the one lock — every name must be unique.
        let d = tmpdir("dedupe-lock");
        let lock = std::sync::Mutex::new(());
        let barrier = std::sync::Barrier::new(8);
        // collect handles BEFORE joining — a lazy map would join each
        // thread before spawning the next and deadlock on the barrier.
        let names: Vec<String> = std::thread::scope(|s| {
            let handles: Vec<_> = (0..8)
                .map(|_| {
                    s.spawn(|| {
                        barrier.wait();
                        let _g = lock.lock().unwrap();
                        let dest = dedupe_name(&d, "a.rar").unwrap();
                        std::fs::write(&dest, b"x").unwrap();
                        dest.file_name()
                            .unwrap()
                            .to_string_lossy()
                            .into_owned()
                    })
                })
                .collect();
            handles.into_iter().map(|h| h.join().unwrap()).collect()
        });
        let mut uniq = names.clone();
        uniq.sort();
        uniq.dedup();
        assert_eq!(uniq.len(), names.len(), "racing dedupes shared a slot");
        std::fs::remove_dir_all(&d).ok();
    }
}
