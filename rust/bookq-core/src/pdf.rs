//! PDF helpers — issue #402 (native PDF pipeline via pdfium-render).
//!
//! Ports of `app/archive_pdf.py`:
//! * `pdf_page_name` / `parse_pdf_page_name` — the `p001.png` synthetic names.
//! * `pdf_page_count` — pages-tree count with the same memo + disk-cache
//!   contract as Python: a process-internal `(path, mtime_ns, size)` memo
//!   (TTL 300s, mirroring `_PDF_MEMO_TTL`) consulted first, then the
//!   restart-spanning `warmup-cache-v1.json` disk cache (`BOOKQ_CACHE_DIR`,
//!   `kind="pdf"`, 7-day TTL, 5000-entry cap), then the live read. Any cache
//!   error is fail-open (falls through to the live read).
//! * `render_pdf_page` — pypdfium2's `render(scale=2.0)` equivalent via the
//!   same PDFium engine (`pdfium-render` crate). Returns PNG bytes, or `None`
//!   when unavailable — the caller falls through to the Python upstream
//!   (which owns the 501 path), so a missing `libpdfium` never breaks
//!   serving, it just proxies like before.
//!
//! The live count still rides `lopdf` (pure Rust, no native lib needed):
//! page counts are structural (pages-tree length), identical between lopdf
//! and pypdfium2 for well-formed files, and `0` on any open/parse failure
//! (the Python never-raise contract).

use std::collections::{HashMap, VecDeque};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::Instant;

// ── synthetic page names (app/archive_pdf.py) ─────────────────────────────

/// `f"p{index + 1:03d}.png"` — 1-based synthetic page filename.
pub fn pdf_page_name(index: usize) -> String {
    format!("p{:03}.png", index + 1)
}

/// `r"p(\d+)\.png"` fullmatch mapped back to a 0-based index, range-checked
/// against `page_count` (`None` when the shape or range is wrong).
pub fn parse_pdf_page_name(name: &str, page_count: usize) -> Option<usize> {
    let inner = name.strip_prefix('p')?.strip_suffix(".png")?;
    if inner.is_empty() || !inner.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    // Leading zeros are fine (`p001.png`); an all-digit run that overflows
    // usize parses to Err → None, matching Python's int() → range-fail path
    // (a huge number is never `< page_count` anyway).
    let idx = inner.parse::<usize>().ok()?.checked_sub(1)?;
    if idx < page_count { Some(idx) } else { None }
}

// ── process-internal count memo (mirrors _PDF_COUNT_MEMO) ─────────────────

const PDF_MEMO_TTL_SECS: f64 = 300.0;
const PDF_MEMO_CAP: usize = 256;

struct PdfMemo {
    map: HashMap<(String, u64, u64), (usize, Instant)>,
    lru: VecDeque<(String, u64, u64)>,
    hits: u64,
    misses: u64,
}

static PDF_MEMO: std::sync::LazyLock<Mutex<PdfMemo>> =
    std::sync::LazyLock::new(|| {
        Mutex::new(PdfMemo {
            map: HashMap::new(),
            lru: VecDeque::new(),
            hits: 0,
            misses: 0,
        })
    });

fn memo_key(path: &Path) -> Option<(String, u64, u64)> {
    let st = path.metadata().ok()?;
    let mtime_ns = st
        .modified()
        .ok()?
        .duration_since(std::time::UNIX_EPOCH)
        .ok()?
        .as_nanos() as u64;
    Some((path.to_string_lossy().into_owned(), mtime_ns, st.len()))
}

fn memo_get(key: &(String, u64, u64)) -> Option<usize> {
    let mut g = PDF_MEMO.lock().ok()?;
    let (count, fresh) = match g.map.get(key) {
        Some((count, at)) => (*count, at.elapsed().as_secs_f64() <= PDF_MEMO_TTL_SECS),
        None => {
            g.misses += 1;
            return None;
        }
    };
    if !fresh {
        g.map.remove(key);
        g.misses += 1;
        return None;
    }
    g.hits += 1;
    Some(count)
}

fn memo_put(key: (String, u64, u64), count: usize) {
    if let Ok(mut g) = PDF_MEMO.lock() {
        g.map.insert(key.clone(), (count, Instant::now()));
        g.lru.push_back(key);
        while g.map.len() > PDF_MEMO_CAP {
            if let Some(old) = g.lru.pop_front() {
                g.map.remove(&old);
            } else {
                break;
            }
        }
    }
}

/// Drop all memoized counts and reset hit/miss counters (tests).
pub fn clear_pdf_page_count_memo() {
    if let Ok(mut g) = PDF_MEMO.lock() {
        g.map.clear();
        g.lru.clear();
        g.hits = 0;
        g.misses = 0;
    }
}

/// Copy of the memo hit/miss counters (observability for tests).
pub fn pdf_memo_stats() -> (u64, u64) {
    PDF_MEMO
        .lock()
        .map(|g| (g.hits, g.misses))
        .unwrap_or((0, 0))
}

// ── restart-spanning disk cache (mirrors app/warmup_cache.py pdf entries) ──

const DISK_CACHE_FILENAME: &str = "warmup-cache-v1.json";
const DISK_CACHE_TTL_SECS: f64 = 7.0 * 24.0 * 3600.0;
const DISK_CACHE_MAX_ENTRIES: usize = 5000;

fn cache_file_path() -> PathBuf {
    let base = std::env::var("BOOKQ_CACHE_DIR").unwrap_or_else(|_| {
        std::env::temp_dir().join("bookq-cache").to_string_lossy().into_owned()
    });
    PathBuf::from(base).join(DISK_CACHE_FILENAME)
}

/// `cache_key(downloads_root, path)` — DOWNLOADS-relative `r:` key when the
/// target sits under the root, abspath `a:` fallback otherwise (same repr as
/// Python: `rel\\x00mtime_ns\\x00size`). `None` when the path cannot be stated.
fn disk_cache_key(downloads_root: &str, path: &Path) -> Option<String> {
    let st = path.metadata().ok()?;
    let mtime_ns = st
        .modified()
        .ok()?
        .duration_since(std::time::UNIX_EPOCH)
        .ok()?
        .as_nanos() as u64;
    let size = st.len();
        if !downloads_root.is_empty() {
            let root = Path::new(downloads_root);
            if let Ok(rel) = path.strip_prefix(root) {
                return Some(format!(
                    "r:{}\x00{}\x00{}",
                    rel.to_string_lossy().replace('\\', "/"),
                    mtime_ns,
                    size
                ));
            }
            // Same comparison on resolved paths (covers symlinked tmpdirs).
            if let (Ok(p), Ok(r)) = (path.canonicalize(), root.canonicalize()) {
                if let Ok(rel) = p.strip_prefix(&r) {
                    return Some(format!(
                        "r:{}\x00{}\x00{}",
                        rel.to_string_lossy().replace('\\', "/"),
                        mtime_ns,
                        size
                    ));
                }
            }
        }
        Some(format!("a:{}\x00{}\x00{}", path.display(), mtime_ns, size))
}

fn disk_key_for_path(path: &Path) -> Option<String> {
    let root = std::env::var("DOWNLOADS_DIR").unwrap_or_default();
    disk_cache_key(&root, path)
}

fn disk_get(key: &str) -> Option<usize> {
    let data: serde_json::Value = (|| {
        let raw = std::fs::read_to_string(cache_file_path()).ok()?;
        serde_json::from_str(&raw).ok()
    })()?;
    let entry = data.get(key)?;
    if entry.get("kind")?.as_str()? != "pdf" {
        return None;
    }
    let saved = entry.get("saved_at")?.as_f64()?;
    if std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
        - saved
        > DISK_CACHE_TTL_SECS
    {
        return None;
    }
    let count = entry.get("count")?.as_u64()? as usize;
    Some(count)
}

fn disk_put(key: &str, count: usize) {
    // Fail-open: every error is swallowed — a broken cache only costs I/O.
    let target = cache_file_path();
    let mut data: serde_json::Map<String, serde_json::Value> = (|| {
        let raw = std::fs::read_to_string(&target).ok()?;
        serde_json::from_str::<serde_json::Value>(&raw)
            .ok()?
            .as_object()
            .cloned()
    })()
    .unwrap_or_default();
    let saved = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    data.insert(
        key.to_string(),
        serde_json::json!({"kind": "pdf", "count": count, "saved_at": saved}),
    );
    while data.len() > DISK_CACHE_MAX_ENTRIES {
        let oldest = data
            .iter()
            .min_by(|a, b| {
                a.1.get("saved_at")
                    .and_then(|v| v.as_f64())
                    .unwrap_or(0.0)
                    .partial_cmp(
                        &b.1.get("saved_at").and_then(|v| v.as_f64()).unwrap_or(0.0),
                    )
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
            .map(|(k, _)| k.clone());
        match oldest {
            Some(k) => {
                data.remove(&k);
            }
            None => break,
        }
    }
    let tmp = target.with_extension(format!("tmp-{}", std::process::id()));
    if std::fs::create_dir_all(target.parent().unwrap_or(Path::new("."))).is_err() {
        return;
    }
    if serde_json::to_string(&data)
        .ok()
        .and_then(|s| std::fs::write(&tmp, s).ok())
        .is_some()
    {
        let _ = std::fs::rename(&tmp, &target);
    }
    let _ = std::fs::remove_file(&tmp);
}

// ── live count + public entry point ─────────────────────────────────────────

fn uncached_pdf_page_count(path: &Path) -> usize {
    if let Ok(doc) = lopdf::Document::load(path) {
        return doc.get_pages().len();
    }
    // Regex fallback mirroring Python's no-pypdfium2 path (`_PAGE_RE`):
    // xref-less or mildly corrupt files still yield a structural count
    // instead of 0, matching `len(PdfDocument)` for well-formed inputs.
    match std::fs::read(path) {
        Ok(data) => count_page_markers(&data),
        Err(_) => 0,
    }
}

/// `rb"/Type\s*/Page[^s]"` — `/Pages` excluded via the trailing `[^s]`.
fn count_page_markers(data: &[u8]) -> usize {
    const MARK: &[u8] = b"/Type";
    const PAGE: &[u8] = b"/Page";
    let mut count = 0;
    let mut i = 0;
    while i + MARK.len() <= data.len() {
        if &data[i..i + MARK.len()] == MARK {
            let mut j = i + MARK.len();
            while j < data.len() && matches!(data[j], b' ' | b'\t' | b'\n' | b'\r' | 0x0C) {
                j += 1;
            }
            if data[j..].starts_with(PAGE) {
                let after = j + PAGE.len();
                if data.get(after) != Some(&b's') {
                    count += 1;
                }
            }
        }
        i += 1;
    }
    count
}

/// Page count with the Python memo + disk-cache contract (never raises;
/// `0` on error or when the path cannot be stated).
pub fn pdf_page_count(path: &Path) -> usize {
    let Some(key) = memo_key(path) else { return 0 };
    if let Some(hit) = memo_get(&key) {
        return hit;
    }
    // Restart-spanning disk cache, consulted ONLY on memo miss (fail-open).
    let disk_key = disk_key_for_path(path);
    if let Some(ref dk) = disk_key {
        if let Some(hit) = disk_get(dk) {
            memo_put(key, hit);
            return hit;
        }
    }
    let count = uncached_pdf_page_count(path);
    memo_put(key, count);
    if let Some(dk) = disk_key {
        disk_put(&dk, count);
    }
    count
}

// ── PDFium rendering (pypdfium2's render(scale=2.0) equivalent) ──────────────

/// Render 0-based page `index` at `scale` (Python: `scale=2.0`) to PNG bytes,
/// or `None` when unavailable (missing libpdfium, bad document, bad index).
///
/// Fail-open by design: the caller falls through to the Python upstream,
/// which owns the 501 path — a host without `libpdfium` keeps proxying
/// exactly like before this change.
pub fn render_pdf_page(path: &Path, index: usize, scale: f32) -> Option<Vec<u8>> {
    let pdfium = pdfium()?;
    let doc = pdfium.load_pdf_from_file(path, None).ok()?;
    let pages = doc.pages();
    if index >= pages.len() as usize {
        return None;
    }
    let page = pages.get(index as i32).ok()?;
    let config = pdfium_render::prelude::PdfRenderConfig::new()
        .scale_page_by_factor(scale);
    let bitmap = page.render_with_config(&config).ok()?;
    let image = bitmap.as_image().ok()?;
    let mut buf = Vec::new();
    {
        use std::io::Cursor;
        image
            .write_to(&mut Cursor::new(&mut buf), image::ImageFormat::Png)
            .ok()?;
    }
    Some(buf)
}

fn pdfium() -> Option<&'static pdfium_render::prelude::Pdfium> {
    static CELL: std::sync::OnceLock<Option<pdfium_render::prelude::Pdfium>> =
        std::sync::OnceLock::new();
    CELL.get_or_init(|| {
        use pdfium_render::prelude::Pdfium;
        // Explicit override for tests / custom layouts.
        if let Ok(p) = std::env::var("PDFIUM_LIB_PATH") {
            if !p.is_empty() {
                if let Ok(lib) = Pdfium::bind_to_library(&p) {
                    return Some(Pdfium::new(lib));
                }
            }
        }
        let bound = Pdfium::bind_to_library(Pdfium::pdfium_platform_library_name_at_path("./"))
            .or_else(|_| Pdfium::bind_to_system_library());
        bound.ok().map(Pdfium::new)
    })
    .as_ref()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Cargo runs tests in one process: the memo counters and env vars below
    /// are process-global, so these tests serialize on this lock.
    static TEST_LOCK: std::sync::LazyLock<Mutex<()>> =
        std::sync::LazyLock::new(|| Mutex::new(()));

    fn make_pdf(path: &Path, pages: usize) {
        // Minimal multi-page PDF with a valid xref table (same objects as
        // tests/test_scanner_perf_todo6.py::_make_pdf, plus byte offsets).
        let mut objs = vec!["1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n".to_string()];
        let kids: Vec<String> = (0..pages).map(|i| format!("{} 0 R", 3 + i)).collect();
        objs.push(format!(
            "2 0 obj\n<< /Type /Pages /Kids [{}] /Count {} >>\nendobj\n",
            kids.join(" "),
            pages
        ));
        for i in 0..pages {
            objs.push(format!(
                "{} 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents {} 0 R >>\nendobj\n",
                3 + i,
                3 + pages + i
            ));
        }
        for i in 0..pages {
            let content = format!("BT 10 90 Td (Page {}) Tj ET", i + 1);
            objs.push(format!(
                "{} 0 obj\n<< /Length {} >>\nstream\n{}\nendstream\nendobj\n",
                3 + pages + i,
                content.len(),
                content
            ));
        }
        let total = objs.len();
        let mut out = b"%PDF-1.4\n".to_vec();
        let mut offsets = Vec::with_capacity(total);
        for obj in &objs {
            offsets.push(out.len());
            out.extend(obj.as_bytes());
        }
        let xref_at = out.len();
        out.extend(format!("xref\n0 {}\n0000000000 65535 f \n", total + 1).as_bytes());
        for off in &offsets {
            out.extend(format!("{:010} 00000 n \n", off).as_bytes());
        }
        out.extend(
            format!(
                "trailer\n<< /Size {} /Root 1 0 R >>\nstartxref\n{}\n%%EOF",
                total + 1,
                xref_at
            )
            .as_bytes(),
        );
        std::fs::write(path, out).unwrap();
    }

    #[test]
    fn page_names_round_trip() {
        assert_eq!(pdf_page_name(0), "p001.png");
        assert_eq!(pdf_page_name(41), "p042.png");
        assert_eq!(parse_pdf_page_name("p001.png", 3), Some(0));
        assert_eq!(parse_pdf_page_name("p003.png", 3), Some(2));
        assert_eq!(parse_pdf_page_name("p004.png", 3), None);
        assert_eq!(parse_pdf_page_name("p000.png", 3), None);
        assert_eq!(parse_pdf_page_name("01.jpg", 3), None);
        assert_eq!(parse_pdf_page_name("p01.png", 3), Some(0));
    }

    #[test]
    fn count_xrefless_falls_back_to_markers() {
        let _guard = TEST_LOCK.lock().unwrap();
        // The tests/test_scanner_perf_todo6.py::_make_pdf shape (objects +
        // trailer, no xref): lopdf cannot parse it, but pypdfium2 counts the
        // pages — the marker fallback keeps parity instead of answering 0.
        let dir = std::env::temp_dir().join(format!("bq-pdfx-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let pdf = dir.join("x.pdf");
        std::fs::write(
            &pdf,
            b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n\
              2 0 obj\n<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>\nendobj\n\
              3 0 obj\n<< /Type /Page /Parent 2 0 R >>\nendobj\n\
              4 0 obj\n<< /Type /Page /Parent 2 0 R >>\nendobj\n\
              trailer\n<< /Root 1 0 R >>\n%%EOF",
        )
        .unwrap();
        clear_pdf_page_count_memo();
        assert_eq!(uncached_pdf_page_count(&pdf), 2);
        // `/Type/Pagex` counts (`[^s]` only excludes `/Pages`); the
        // double-space variant exercises the `\s*` gap.
        assert_eq!(count_page_markers(b"a /Type  /Page b /Type/Pagex /Type /Pages"), 2);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn count_memo_and_disk_parity() {
        let _guard = TEST_LOCK.lock().unwrap();
        let dir = std::env::temp_dir().join(format!("bq-pdf-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let pdf = dir.join("doc.pdf");
        make_pdf(&pdf, 2);
        clear_pdf_page_count_memo();
        // Point the disk cache at a scratch dir so the test never touches
        // the developer's real cache.
        let cache_dir = dir.join("cache");
        std::env::set_var("BOOKQ_CACHE_DIR", &cache_dir);
        std::env::set_var("DOWNLOADS_DIR", &dir);
        assert_eq!(pdf_page_count(&pdf), 2);
        let (hits, misses) = pdf_memo_stats();
        assert_eq!((hits, misses), (0, 1));
        assert_eq!(pdf_page_count(&pdf), 2);
        let (hits, _) = pdf_memo_stats();
        assert!(hits >= 1);
        // Restart simulation: cleared memo must still hit the disk cache
        // without re-reading the document.
        clear_pdf_page_count_memo();
        assert_eq!(pdf_page_count(&pdf), 2);
        assert!(cache_dir.join(DISK_CACHE_FILENAME).is_file());
        std::env::remove_var("BOOKQ_CACHE_DIR");
        std::env::remove_var("DOWNLOADS_DIR");
        std::fs::remove_dir_all(&dir).ok();
    }
    /// Rendering without libpdfium must fall back to None (proxy), never panic.
    #[test]
    fn render_without_lib_is_none_not_panic() {
        let _guard = TEST_LOCK.lock().unwrap();
        let dir = std::env::temp_dir().join(format!("bq-pdfr-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let pdf = dir.join("r.pdf");
        make_pdf(&pdf, 1);
        // Point at a nonexistent library only when no override is set, so a
        // developer machine WITH libpdfium still exercises the real path.
        let had_override = std::env::var("PDFIUM_LIB_PATH").is_ok();
        if !had_override {
            std::env::set_var("PDFIUM_LIB_PATH", "/nonexistent-libpdfium.so");
        }
        let out = render_pdf_page(&pdf, 0, 2.0);
        if had_override {
            // A configured lib may or may not render this synthetic file;
            // either outcome is fail-open and must not panic.
            let _ = out;
        } else {
            assert!(out.is_none());
            std::env::remove_var("PDFIUM_LIB_PATH");
        }
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn render_with_lib_produces_scaled_png() {
        // Exercises the real PDFium path when a library is configured
        // (PDFIUM_LIB_PATH or system lib); passes trivially without one.
        // CI has no libpdfium, so this is a no-op there — the Docker image
        // (Dockerfile.viewer-rs) is where the native path is guaranteed.
        let _guard = TEST_LOCK.lock().unwrap();
        if std::env::var("PDFIUM_LIB_PATH").is_err() {
            return;
        }
        let dir = std::env::temp_dir().join(format!("bq-pdfr2-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let pdf = dir.join("r.pdf");
        make_pdf(&pdf, 1);
        let out = render_pdf_page(&pdf, 0, 2.0).expect("lib configured but render failed");
        assert_eq!(&out[..8], b"\x89PNG\r\n\x1a\n");
        let dims = image::load_from_memory(&out).unwrap();
        // MediaBox 200x200pt at scale 2.0 → 400x400px, like pypdfium2.
        assert_eq!((dims.width(), dims.height()), (400, 400));
        assert!(render_pdf_page(&pdf, 7, 2.0).is_none()); // out of range
        std::fs::remove_dir_all(&dir).ok();
    }
}
