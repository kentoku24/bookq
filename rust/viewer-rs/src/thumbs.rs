//! `/thumb`, `/view`, `/api/v1/thumb-sprite` — M7 of #163.
//!
//! Hit-path native over the SHARED thumb-cache volume: every endpoint's
//! stat→key→sha1→ETag→`_not_modified`→dest-lookup preamble is ported, so a
//! cache hit is served byte-identically to Python (`thumbs.py`). A miss is
//! rendered in-process (`image` crate — decode, EXIF orientation, Lanczos
//! thumbnail, JPEG encode) and stored into the same cache dir with the
//! same atomic tmp+rename + oldest-mtime prune as `_store_thumb`.
//!
//! Still proxied: sources only upstream can reach (PDF pages — pypdfium2;
//! multipart RAR sets — `rar_needs_upstream`, since bundled unrar cannot
//! chain volumes), formats the `image` crate can't decode that Pillow
//! might (`.avif`, `.tiff`, archive files used as sources — gated by
//! `NATIVE_DECODE` before the payload read), archive entries we fail to
//! extract (unsupported compression/CRC — upstream decides), and the
//! sprite compose path (misses proxy). Single-volume RAR members read
//! in-process via `bookq_core` (bundled unrar), so `/view` on a rar/cbr
//! entry is native end to end.
//!
//! Known output deviation: the `image` JpegEncoder emits 4:4:4 (no chroma
//! subsampling), so generated files run somewhat larger than Pillow's
//! libjpeg-turbo 4:2:0 output at equal quality — same decoder-visible
//! contract (JPEG at the same dimensions/quality), bigger bytes.

use std::collections::HashMap;
use std::path::Path;

use axum::body::Body;
use axum::http::{header, HeaderMap, HeaderValue, StatusCode};
use axum::response::Response;
use image::ImageDecoder;
use sha1::{Digest, Sha1};

use crate::serve::{
    archive_not_modified, fmt_mtime, http_date, is_image_entry, is_pdf_file, is_rar_path,
    is_within, not_modified, realpath, resolve_archive, suffix_lower, Serve,
};

const CACHE_MAX_AGE: i64 = 86400;
const SPRITE_MAX_ENTRIES: usize = 9;
const SPRITE_CELL_WIDTH: i64 = 200;
const GRID_DIMS: [(i64, (i64, i64)); 3] = [(200, (240, 360)), (400, (480, 720)), (800, (960, 1440))];
const JPEG_QUALITY: u8 = 70;
const VIEW_JPEG_QUALITY: u8 = 85;
const VIEW_MAX_WIDTH: u32 = 2560;
const THUMB_CACHE_MAX_FILES: usize = 5000;

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

/// `load_source_bytes` — source payload + (mtime, size, cache-key) for the
/// `/view` passthrough branch and the thumb/view generate paths. Returns
/// `Err` for the guard failures (400/403/404) and `Ok(None)` for "needs
/// upstream tooling" (pdf pages — pypdfium2; multipart RAR — bundled
/// unrar can't chain volumes; archive entries we can't extract —
/// unsupported compression or CRC failure included) so callers proxy
/// instead of reproducing 501/404 semantics Python owns. Single-volume
/// RAR/CBR members read natively via `archive_read_entry`.
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
        // PDF pages stay upstream (pypdfium2); rar/cbr entries read
        // natively via `archive_read_entry` → bundled unrar — except
        // multipart sets, which 7z chains volumes for upstream.
        if is_pdf_file(&archive) {
            return Ok(None);
        }
        if is_rar_path(&archive) && bookq_core::rar_needs_upstream(&archive) {
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
        return match crate::serve::archive_read_entry(&archive, &inner) {
            Some(data) => Ok(Some((data, mtime, size, key))),
            // `None` means "unsure" — unsupported compression (bzip2/LZMA),
            // CRC failure, or an absent member. Python decides: zipfile
            // decodes what we can't, and a truly absent entry 404s there.
            None => Ok(None),
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

// ── cache-miss generation (the Pillow pipeline, in-process) ──────────────────

/// Extensions the enabled `image` features can decode — anything else
/// proxies BEFORE the source load: pulling a whole `.avif`/`.tiff`/`.cbz`
/// payload just to fail `into_decoder` pays the read twice (the broken-
/// image case repeats it on EVERY request, uncached).
const NATIVE_DECODE: &[&str] = &[".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"];

fn native_decodable(path: &str) -> bool {
    NATIVE_DECODE.contains(&suffix_lower(Path::new(path)).as_str())
}

/// Concurrent-generation cap. `spawn_blocking` defaults to 512 threads and
/// each in-flight decode holds a full-resolution buffer (an RGBA webp at
/// the /view ceiling is ~100MB), so a cold-cache 100-tile burst would OOM
/// the container — Python was implicitly capped by anyio's 40-thread pool.
static GEN_PERMITS: std::sync::LazyLock<tokio::sync::Semaphore> =
    std::sync::LazyLock::new(|| {
        tokio::sync::Semaphore::new(
            std::thread::available_parallelism()
                .map(|n| n.get())
                .unwrap_or(4),
        )
    });

/// `Image.open` + `ImageOps.exif_transpose`: format-sniffed decode with
/// the EXIF orientation applied. Animated formats decode to their first
/// frame, like Pillow's thumbnail path.
fn load_oriented(data: &[u8]) -> Option<image::DynamicImage> {
    let mut dec = image::ImageReader::new(std::io::Cursor::new(data))
        .with_guessed_format()
        .ok()?
        .into_decoder()
        .ok()?;
    let orientation = dec
        .orientation()
        .unwrap_or(image::metadata::Orientation::NoTransforms);
    let mut im = image::DynamicImage::from_decoder(dec).ok()?;
    im.apply_orientation(orientation);
    Some(im)
}

/// JPEG encode matching Pillow's mode rule: `L` stays grayscale, anything
/// else becomes RGB (alpha simply drops, like `convert("RGB")`). Borrows
/// the buffer when the image is already in the target mode — `to_rgb8`/
/// `to_luma8` would clone ~78MB at the /view ceiling for no reason.
/// NOTE: the `image` JpegEncoder emits 4:4:4 (no chroma subsampling —
/// its Component h/v are fixed 1,1), so output runs somewhat larger than
/// Pillow's libjpeg-turbo 4:2:0 at equal quality.
fn jpeg_encode(im: &image::DynamicImage, quality: u8) -> Option<Vec<u8>> {
    use image::{DynamicImage as D, ExtendedColorType as C};
    let mut out = Vec::new();
    let mut enc = image::codecs::jpeg::JpegEncoder::new_with_quality(&mut out, quality);
    let (w, h) = (im.width(), im.height());
    let r = match im {
        D::ImageLuma8(b) => enc.encode(b.as_raw(), w, h, C::L8),
        D::ImageRgb8(b) => enc.encode(b.as_raw(), w, h, C::Rgb8),
        D::ImageLuma16(_) => enc.encode(&im.to_luma8(), w, h, C::L8),
        _ => enc.encode(&im.to_rgb8(), w, h, C::Rgb8),
    };
    r.ok()?;
    Some(out)
}

/// Alpha composite onto white — `make_view_bytes`'s paste(rgba, mask).
fn flatten_on_white(im: &image::DynamicImage) -> image::RgbImage {
    fn flat(rgba: &image::RgbaImage) -> image::RgbImage {
        let mut out = image::RgbImage::new(rgba.width(), rgba.height());
        for (o, p) in out.pixels_mut().zip(rgba.pixels()) {
            let a = p[3] as u32;
            let inv = 255 - a;
            for c in 0..3 {
                o[c] = ((p[c] as u32 * a + 255 * inv) / 255) as u8;
            }
        }
        out
    }
    match im {
        image::DynamicImage::ImageRgba8(b) => flat(b),
        _ => flat(&im.to_rgba8()),
    }
}

/// `im.thumbnail((bw, bh), LANCZOS)` — fit inside the box keeping aspect,
/// never upscale. `resize` shares `thumbnail`'s fit-in-box dimensions but
/// takes a filter: `thumbnail` itself is nearest-neighbour (one source
/// pixel per target), which aliases visibly on line art — Pillow uses
/// LANCZOS, so we do too.
fn thumbnail_box(im: image::DynamicImage, bw: u32, bh: u32) -> image::DynamicImage {
    if im.width() > bw || im.height() > bh {
        im.resize(bw, bh, image::imageops::FilterType::Lanczos3)
    } else {
        im
    }
}

/// `make_thumbnail`: shrink inside (w, w*4) keeping aspect, never upscale,
/// JPEG q70.
fn make_thumb(data: &[u8], width: i64) -> Option<Vec<u8>> {
    let w = (width as u32).max(1);
    let im = load_oriented(data)?;
    let im = thumbnail_box(im, w, w.saturating_mul(4));
    jpeg_encode(&im, JPEG_QUALITY)
}

/// `make_view_bytes`: ≤2560-wide JPEG q85; alpha flatten onto white, `L`
/// kept grayscale (same mode rules as Pillow).
fn make_view(data: &[u8]) -> Option<Vec<u8>> {
    let im = load_oriented(data)?;
    // Pillow resizes RGBA premultiplied — compositing onto white FIRST is
    // the same linear operation (flat = premult + (1-a)·white), avoids the
    // dark halo an unmultiplied resize leaves on transparent edges, and
    // resizes a 3-channel buffer instead of 4.
    let im = if im.color().has_alpha() {
        image::DynamicImage::ImageRgb8(flatten_on_white(&im))
    } else {
        im
    };
    let im = thumbnail_box(im, VIEW_MAX_WIDTH, VIEW_MAX_WIDTH.saturating_mul(4));
    jpeg_encode(&im, VIEW_JPEG_QUALITY)
}

/// `_store_thumb`: uniquely-named tmp sibling + atomic rename, then the
/// bounded namespace prune. Fail-open — a store error just means the next
/// request regenerates.
fn store_cached(dest: &Path, bytes: &[u8]) {
    let Some(dir) = dest.parent() else { return };
    if std::fs::create_dir_all(dir).is_err() {
        return;
    }
    // pid+nanos+seq — within one process the counter guarantees uniqueness
    // even when the clock can't (coarse resolution, NTP step-back). This
    // is what Python's os.urandom(6) suffix exists for: two same-dest
    // writers must never share a tmp name or one truncates under the
    // other and publishes a torn JPEG.
    static SEQ: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    let seq = SEQ.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let tmp = dir.join(format!(
        ".{}.{}.{}.{}.tmp",
        dest.file_name().and_then(|n| n.to_str()).unwrap_or("x"),
        std::process::id(),
        nanos,
        seq
    ));
    if std::fs::write(&tmp, bytes).is_err() || std::fs::rename(&tmp, dest).is_err() {
        let _ = std::fs::remove_file(&tmp);
        return;
    }
    prune_dir(dir);
}

/// `prune_cache_dir`: evict oldest-mtime files back under the cap; every
/// error swallowed (eviction must never break serving). The cap is a
/// parameter like Python's so tests can exercise eviction cheaply.
fn prune_dir(dir: &Path) {
    prune_dir_cap(dir, THUMB_CACHE_MAX_FILES);
}

fn prune_dir_cap(dir: &Path, cap: usize) {
    let Ok(rd) = std::fs::read_dir(dir) else { return };
    // Count via d_type first — a full-stat walk on every miss costs
    // cap×requests metadata calls (5000×100 tiles = 500k stats) when the
    // namespace is under the cap, which is almost always.
    let mut files = Vec::new();
    for e in rd.flatten() {
        if e.file_type().map(|t| t.is_file()).unwrap_or(false) {
            files.push(e);
        }
    }
    if files.len() <= cap {
        return;
    }
    let mut entries: Vec<(std::time::SystemTime, std::path::PathBuf)> = Vec::new();
    for e in files {
        let Ok(t) = e.metadata().and_then(|m| m.modified()) else {
            continue;
        };
        entries.push((t, e.path()));
    }
    entries.sort_by_key(|(t, _)| *t);
    let excess = entries.len().saturating_sub(cap);
    for (_, p) in entries.into_iter().take(excess) {
        let _ = std::fs::remove_file(p);
    }
}

// ── /thumb/{path}?w= ──────────────────────────────────────────────────────────

/// (etag, dest) derivation shared by the stat and generate paths — a
/// format change that touched only one side would have hits and misses
/// reading different cache files (permanent double-miss).
fn thumb_etag(mtime: f64, size: f64, width: i64, digest: &str) -> String {
    format!(
        "\"{}-{}-{width}-{}\"",
        fmt_mtime(mtime),
        fmt_mtime(size),
        &digest[..16]
    )
}

fn thumb_dest(cache: &Path, digest: &str, width: i64, is_svg: bool) -> std::path::PathBuf {
    let mut dest = cache
        .join("thumbs")
        .join(width.to_string())
        .join(format!("{digest}.jpg"));
    if is_svg {
        dest.set_extension("svg");
    }
    dest
}

fn view_etag(mtime: f64, size: f64, digest: &str) -> String {
    format!(
        "\"{}-{}-view-{}\"",
        fmt_mtime(mtime),
        fmt_mtime(size),
        &digest[..16]
    )
}

/// `handle_thumb` — stat fast path + shared-cache hit; a miss renders
/// in-process (SVG sources pass their bytes through like Pillow's
/// `_make_thumb_bytes`), falling back to proxy only when the source can't
/// be reached natively (rar/pdf) or the `image` crate can't decode it.
pub fn thumb(downloads: &Path, cache: &Path, path: &str, w: Option<&String>, headers: &HeaderMap) -> Serve {
    let width = normalize_width(w);
    let (mtime, size, key) = match stat_source(downloads, path) {
        Ok(x) => x,
        Err(res) => return Serve::Done(res),
    };
    let is_svg = suffix_lower(Path::new(path)) == ".svg";
    let digest = sha1hex(&key);
    let etag = thumb_etag(mtime, size, width, &digest);
    if archive_not_modified(headers, &etag, mtime) {
        return Serve::Done(not_modified());
    }
    let dest = thumb_dest(cache, &digest, width, is_svg);
    if !dest.is_file() {
        return generate_thumb(downloads, cache, path, width, is_svg, headers);
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

/// `handle_thumb`'s miss path: `load_source_bytes` → re-derive etag/dest →
/// recheck the cache (a concurrent writer may have landed it) → render →
/// `_store_thumb`. SVG sources store/served as their original bytes.
fn generate_thumb(
    downloads: &Path,
    cache: &Path,
    path: &str,
    width: i64,
    is_svg: bool,
    headers: &HeaderMap,
) -> Serve {
    if !is_svg && !native_decodable(path) {
        return Serve::Proxy; // e.g. .avif — decode would fail after the read
    }
    let Some((data, mtime, size, key)) = (match load_source_bytes(downloads, path) {
        Ok(x) => x,
        Err(res) => return Serve::Done(res),
    }) else {
        return Serve::Proxy;
    };
    let digest = sha1hex(&key);
    let etag = thumb_etag(mtime, size, width, &digest);
    if archive_not_modified(headers, &etag, mtime) {
        return Serve::Done(not_modified());
    }
    let dest = thumb_dest(cache, &digest, width, is_svg);
    let body = if dest.is_file() {
        match std::fs::read(&dest) {
            Ok(b) => b,
            Err(_) => return Serve::Proxy,
        }
    } else {
        let made = if is_svg {
            Some(data)
        } else {
            let Ok(_permit) = GEN_PERMITS.blocking_acquire() else {
                return Serve::Proxy;
            };
            make_thumb(&data, width)
        };
        let Some(body) = made else { return Serve::Proxy };
        store_cached(&dest, &body);
        body
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
/// normalized types hit the shared `view/` cache; misses render
/// in-process with upstream fallback.
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
        let etag = view_etag(mtime, size, &digest);
        if archive_not_modified(headers, &etag, mtime) {
            return Serve::Done(not_modified());
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
    let etag = view_etag(mtime, size, &digest);
    if archive_not_modified(headers, &etag, mtime) {
        return Serve::Done(not_modified());
    }
    let dest = cache.join("view").join(format!("{digest}.jpg"));
    if !dest.is_file() {
        return generate_view(downloads, cache, path, headers);
    }
    let body = match std::fs::read(&dest) {
        Ok(b) => b,
        Err(_) => return Serve::Proxy,
    };
    Serve::Done(media_response(body, "image/jpeg", &etag, mtime, false))
}

/// `handle_view`'s normalized-format miss path: `load_source_bytes` →
/// re-derive etag/dest → recheck the cache → `make_view_bytes` →
/// `_store_thumb`. Sources only upstream can reach (rar/pdf) and decodes
/// the `image` crate can't handle still proxy.
fn generate_view(downloads: &Path, cache: &Path, path: &str, headers: &HeaderMap) -> Serve {
    if !native_decodable(path) {
        return Serve::Proxy; // e.g. .avif — decode would fail after the read
    }
    let Some((data, mtime, size, key)) = (match load_source_bytes(downloads, path) {
        Ok(x) => x,
        Err(res) => return Serve::Done(res),
    }) else {
        return Serve::Proxy;
    };
    let digest = sha1hex(&key);
    let etag = view_etag(mtime, size, &digest);
    if archive_not_modified(headers, &etag, mtime) {
        return Serve::Done(not_modified());
    }
    let dest = cache.join("view").join(format!("{digest}.jpg"));
    let body = if dest.is_file() {
        match std::fs::read(&dest) {
            Ok(b) => b,
            Err(_) => return Serve::Proxy,
        }
    } else {
        let Ok(_permit) = GEN_PERMITS.blocking_acquire() else {
            return Serve::Proxy;
        };
        let Some(body) = make_view(&data) else {
            return Serve::Proxy;
        };
        store_cached(&dest, &body);
        body
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
        return Serve::Done(not_modified());
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    fn png_rgb(w: u32, h: u32, px: [u8; 3]) -> Vec<u8> {
        let mut im = image::RgbImage::new(w, h);
        for p in im.pixels_mut() {
            *p = image::Rgb(px);
        }
        let mut out = Vec::new();
        image::DynamicImage::ImageRgb8(im)
            .write_to(&mut Cursor::new(&mut out), image::ImageFormat::Png)
            .unwrap();
        out
    }

    fn jpeg_dims(data: &[u8]) -> (u32, u32) {
        let d = image::load_from_memory(data).unwrap();
        (d.width(), d.height())
    }

    /// `make_thumb` shrinks inside (w, w*4) keeping aspect and emits JPEG.
    #[test]
    fn thumb_shrinks_to_width() {
        let src = png_rgb(1200, 900, [10, 200, 30]);
        let jpg = make_thumb(&src, 200).unwrap();
        assert_eq!(&jpg[..2], b"\xff\xd8");
        assert_eq!(jpeg_dims(&jpg), (200, 150));
    }

    /// Small sources never upscale (Pillow `thumbnail` semantics).
    #[test]
    fn thumb_never_upscales() {
        let src = png_rgb(50, 40, [0, 0, 0]);
        let jpg = make_thumb(&src, 800).unwrap();
        assert_eq!(jpeg_dims(&jpg), (50, 40));
    }

    /// Undecodable bytes → None (caller proxies so Pillow can still 404).
    #[test]
    fn thumb_garbage_is_none() {
        assert!(make_thumb(b"not an image", 400).is_none());
    }

    /// `make_view` caps at 2560 wide and flattens alpha onto white.
    #[test]
    fn view_caps_and_flattens() {
        // 4000x3000 half-transparent red → ≤2560 wide, white-blended pixels.
        let mut im = image::RgbaImage::new(4000, 3000);
        for p in im.pixels_mut() {
            *p = image::Rgba([255, 0, 0, 128]);
        }
        let mut src = Vec::new();
        image::DynamicImage::ImageRgba8(im)
            .write_to(&mut Cursor::new(&mut src), image::ImageFormat::Png)
            .unwrap();
        let jpg = make_view(&src).unwrap();
        let (w, h) = jpeg_dims(&jpg);
        assert_eq!((w, h), (2560, 1920));
        let decoded = image::load_from_memory(&jpg).unwrap().to_rgb8();
        let px = decoded.get_pixel(10, 10);
        // 255*0.5 red + 255*0.5 white → ~(255, 127, 127); jpeg chroma
        // subsampling can move G/B by a couple of LSBs.
        assert!((251..=255).contains(&px[0]));
        assert!((123..=131).contains(&px[1]));
        assert!((px[1] as i16 - px[2] as i16).abs() <= 2);
    }

    /// Lanczos, not nearest: a 1px checkerboard downscaled 2x must average
    /// to mid-grey — nearest sampling would alias to pure black/white.
    #[test]
    fn thumb_downscale_averages_not_samples() {
        let mut im = image::RgbImage::new(400, 400);
        for (x, y, p) in im.enumerate_pixels_mut() {
            *p = if (x + y) % 2 == 0 {
                image::Rgb([0, 0, 0])
            } else {
                image::Rgb([255, 255, 255])
            };
        }
        let mut src = Vec::new();
        image::DynamicImage::ImageRgb8(im)
            .write_to(&mut Cursor::new(&mut src), image::ImageFormat::Png)
            .unwrap();
        let jpg = make_thumb(&src, 200).unwrap();
        let decoded = image::load_from_memory(&jpg).unwrap().to_rgb8();
        let mean: u32 = decoded.pixels().map(|p| p[0] as u32).sum::<u32>()
            / (decoded.width() * decoded.height());
        assert!((100..=155).contains(&mean), "mean {mean} — not averaged");
    }

    /// `_store_thumb` + `prune_cache_dir`: atomic land, oldest-mtime evict.
    #[test]
    fn store_and_prune() {
        let dir = std::env::temp_dir().join(format!("bq-thumb-{}", std::process::id()));
        let ns = dir.join("thumbs/400");
        std::fs::create_dir_all(&ns).unwrap();
        let dest = ns.join("abc.jpg");
        store_cached(&dest, b"jpeg-bytes");
        assert_eq!(std::fs::read(&dest).unwrap(), b"jpeg-bytes");
        // Seed 6 old-mtime files (the fresh abc.jpg is newest) → prune to
        // 4 evicts the three oldest seeded names.
        let base = std::time::SystemTime::now() - std::time::Duration::from_secs(100_000);
        for i in 0..6 {
            let p = ns.join(format!("f{i}.jpg"));
            std::fs::write(&p, b"x").unwrap();
            set_mtime(&p, base + std::time::Duration::from_secs(i));
        }
        prune_dir_cap(&ns, 4);
        assert!(!ns.join("f0.jpg").exists());
        assert!(!ns.join("f1.jpg").exists());
        assert!(!ns.join("f2.jpg").exists());
        assert!(ns.join("f3.jpg").is_file());
        std::fs::remove_dir_all(&dir).ok();
    }

    fn set_mtime(p: &Path, t: std::time::SystemTime) {
        std::fs::File::options()
            .write(true)
            .open(p)
            .unwrap()
            .set_times(std::fs::FileTimes::new().set_modified(t))
            .unwrap();
    }

    // ── generate-path regressions (load → 304 → render → store) ─────────────

    fn tmp_dirs(tag: &str) -> (std::path::PathBuf, std::path::PathBuf) {
        let base = std::env::temp_dir().join(format!(
            "bq-gen-{tag}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let dl = base.join("dl");
        let cache = base.join("cache");
        std::fs::create_dir_all(&dl).unwrap();
        std::fs::create_dir_all(&cache).unwrap();
        // canonicalize: /var → /private/var on macOS, or is_within 403s.
        (dl.canonicalize().unwrap(), cache.canonicalize().unwrap())
    }

    fn status(s: &Serve) -> StatusCode {
        match s {
            Serve::Done(r) => r.status(),
            Serve::Proxy => StatusCode::IM_A_TEAPOT,
        }
    }

    /// `/thumb` miss → render → land under thumbs/<w>/<digest>.jpg → a
    /// repeat request with If-None-Match answers 304.
    #[test]
    fn thumb_generate_caches_and_304s() {
        let (dl, cache) = tmp_dirs("t1");
        std::fs::write(dl.join("a.png"), png_rgb(800, 600, [9, 9, 9])).unwrap();
        let h = HeaderMap::new();
        let res = thumb(&dl, &cache, "a.png", None, &h);
        let Serve::Done(r) = res else { panic!("expected Done") };
        assert_eq!(r.status(), StatusCode::OK);
        let etag = r
            .headers()
            .get(header::ETAG)
            .unwrap()
            .to_str()
            .unwrap()
            .to_string();
        let stored: Vec<_> = std::fs::read_dir(cache.join("thumbs/400"))
            .unwrap()
            .flatten()
            .collect();
        assert_eq!(stored.len(), 1);
        assert_eq!(stored[0].file_name().to_str().unwrap().len(), 44); // 40-hex + .jpg
        let mut h2 = HeaderMap::new();
        h2.insert(header::IF_NONE_MATCH, etag.parse().unwrap());
        assert_eq!(status(&thumb(&dl, &cache, "a.png", None, &h2)), StatusCode::NOT_MODIFIED);
        std::fs::remove_dir_all(dl.parent().unwrap()).ok();
    }

    /// SVG misses store the ORIGINAL bytes under a `.svg` dest (Pillow
    /// passthrough), not a re-rendered jpeg.
    #[test]
    fn thumb_svg_passthrough_stores_svg() {
        let (dl, cache) = tmp_dirs("t2");
        std::fs::write(dl.join("i.svg"), b"<svg xmlns=\"x\"/>").unwrap();
        let h = HeaderMap::new();
        let res = thumb(&dl, &cache, "i.svg", None, &h);
        let Serve::Done(r) = res else { panic!("expected Done") };
        assert_eq!(r.status(), StatusCode::OK);
        assert_eq!(r.headers()[header::CONTENT_TYPE], "image/svg+xml");
        let stored: Vec<_> = std::fs::read_dir(cache.join("thumbs/400"))
            .unwrap()
            .flatten()
            .collect();
        assert!(stored[0]
            .file_name()
            .to_str()
            .unwrap()
            .ends_with(".svg"));
        std::fs::remove_dir_all(dl.parent().unwrap()).ok();
    }

    /// Extensions `image` can't decode proxy BEFORE the payload read —
    /// a garbage `.avif` must not even try (same for broken-but-decodable
    /// names, which reach the decoder and still proxy).
    #[test]
    fn thumb_undecodable_proxies() {
        let (dl, cache) = tmp_dirs("t3");
        std::fs::write(dl.join("v.avif"), b"garbage").unwrap();
        std::fs::write(dl.join("x.png"), b"garbage").unwrap();
        let h = HeaderMap::new();
        assert!(matches!(
            thumb(&dl, &cache, "v.avif", None, &h),
            Serve::Proxy
        ));
        assert!(matches!(
            thumb(&dl, &cache, "x.png", None, &h),
            Serve::Proxy
        ));
        std::fs::remove_dir_all(dl.parent().unwrap()).ok();
    }

    /// An archive member we can't extract (unsupported compression, CRC
    /// failure, absent) proxies — never a native 404, because Python's
    /// zipfile/rarfile may still read it.
    #[test]
    fn thumb_missing_archive_member_proxies() {
        let (dl, cache) = tmp_dirs("t4");
        std::fs::write(dl.join("book.cbz"), b"not a zip").unwrap();
        let h = HeaderMap::new();
        assert!(matches!(
            thumb(&dl, &cache, "book.cbz/x.png", None, &h),
            Serve::Proxy
        ));
        std::fs::remove_dir_all(dl.parent().unwrap()).ok();
    }

    /// `/view` normalized miss → jpeg under `view/` with the shared etag.
    #[test]
    fn view_generate_caches_jpeg() {
        let (dl, cache) = tmp_dirs("v1");
        let mut src = Vec::new();
        image::DynamicImage::ImageRgb8(image::RgbImage::from_pixel(64, 64, image::Rgb([1, 2, 3])))
            .write_to(&mut Cursor::new(&mut src), image::ImageFormat::Bmp)
            .unwrap();
        std::fs::write(dl.join("c.bmp"), src).unwrap();
        let h = HeaderMap::new();
        let res = view(&dl, &cache, "c.bmp", &h);
        let Serve::Done(r) = res else { panic!("expected Done") };
        assert_eq!(r.status(), StatusCode::OK);
        assert_eq!(r.headers()[header::CONTENT_TYPE], "image/jpeg");
        assert_eq!(
            std::fs::read_dir(cache.join("view")).unwrap().count(),
            1
        );
        std::fs::remove_dir_all(dl.parent().unwrap()).ok();
    }

    /// Premultiplied resize: a transparent→opaque boundary must composite
    /// to Pillow's (255,127,127), not the (191,127,127) dark halo an
    /// unmultiplied resize produces.
    #[test]
    fn view_alpha_premultiplies_before_resize() {
        let mut im = image::RgbaImage::new(4000, 20);
        for (x, _, p) in im.enumerate_pixels_mut() {
            *p = if x < 2000 {
                image::Rgba([0, 0, 0, 0])
            } else {
                image::Rgba([255, 0, 0, 255])
            };
        }
        let mut src = Vec::new();
        image::DynamicImage::ImageRgba8(im)
            .write_to(&mut Cursor::new(&mut src), image::ImageFormat::Png)
            .unwrap();
        let jpg = make_view(&src).unwrap();
        let dec = image::load_from_memory(&jpg).unwrap().to_rgb8();
        // Lanczos ringing makes any single edge pixel unreliable — compare
        // the MEAN over the boundary band: flatten-first keeps red ~255,
        // unmultiplied-then-flatten drops it to ~191.
        let (w, h) = (dec.width(), dec.height());
        let (x0, x1) = (w * 19 / 40, w * 21 / 40);
        let mut sum = 0u64;
        for y in 0..h {
            for x in x0..x1 {
                sum += dec.get_pixel(x, y)[0] as u64;
            }
        }
        let mean_r = sum / ((x1 - x0) as u64 * h as u64);
        assert!(mean_r > 215, "boundary mean R {mean_r} — not premultiplied");
    }


}
