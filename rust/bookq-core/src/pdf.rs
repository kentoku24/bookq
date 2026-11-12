//! PDF page counting + page rendering — port of `app/archive_pdf.py` (#402).
//!
//! Counting and rendering both ride PDFium — the same engine `pypdfium2`
//! wraps — via the `pdfium-render` crate, so page indexes and rendered
//! dimensions match the Python backend pixel-for-pixel (up to the PNG/JPEG
//! encoder, which is allowed to differ: decoded pixels are the contract).
//!
//! Failure posture mirrors `archive_pdf.py` exactly: a count/render that
//! cannot be proven returns `0` / `None` and the caller falls through to
//! the Python upstream (which 501s when `pypdfium2` is missing, 404s for a
//! bad page index). Nothing here raises.
//!
//! Two cache layers sit in front of the raw count, both ported verbatim:
//!
//! * process-internal memo keyed `(path, mtime_ns, size)` with the same
//!   300s horizon as `_PDF_MEMO_TTL` (an mtime/size mismatch re-reads
//!   immediately), plus hit/miss counters for observability,
//! * the restart-spanning disk cache (`app/warmup_cache.py`,
//!   `warmup-cache-v1.json`, `{"kind": "pdf", ...}` entries keyed
//!   `r:<rel>\0<mtime_ns>\0<size>`), consulted only on memo miss and
//!   fail-open throughout.
//!
//! When no PDFium shared library is present (dev shells, `cargo test`
//! without the `.so`), counting falls back to `lopdf` and then the
//! `/Type /Page` regex heuristic — the same ladder Python climbs when
//! `pypdfium2` is not installed — and rendering returns `None` so callers
//! proxy upstream instead of inventing bytes.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{LazyLock, Mutex};
use std::time::{Duration, Instant};

// ── pdf_page_name / parse_pdf_page_name (pure ports) ─────────────────────────

/// Synthetic 1-based page filename exposed to the viewer (`p001.png` …).
pub fn pdf_page_name(index: usize) -> String {
    format!("p{:03}.png", index + 1)
}

/// Map `p001.png`-style names back to a 0-based index, or `None` — port of
/// `parse_pdf_page_name` (`re.fullmatch(r"p(\d+)\.png", name)`, range-checked
/// against `page_count`). `p\d+` with leading zeros (or none) all parse via
/// the integer conversion, exactly like Python's `int(m.group(1)) - 1`.
pub fn parse_pdf_page_name(name: &str, page_count: usize) -> Option<usize> {
    let digits = name.strip_prefix('p')?.strip_suffix(".png")?;
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    // `usize::from_str` rejects `+`/whitespace; overflow maps to None.
    // Python's `int()` accepts arbitrarily long digit runs, but any run
    // that overflows usize is far beyond `page_count` anyway.
    let idx_1based: usize = digits.parse().ok()?;
    let idx = idx_1based.checked_sub(1)?;
    if idx < page_count { Some(idx) } else { None }
}

// ── PDFium binding (dynamic, fail-open) ──────────────────────────────────────

/// Render scale matching `render_pdf_page(path, index, scale=2.0)`.
pub const RENDER_SCALE: f32 = 2.0;

/// Process-global PDFium handle. Binding is attempted once; when no usable
/// `libpdfium` is found the cell holds `None` and every PDFium-backed entry
/// point degrades to its fallback (lopdf/regex count, `None` render).
static PDFIUM: LazyLock<Mutex<Option<pdfium_render::prelude::Pdfium>>> =
    LazyLock::new(|| Mutex::new(bind_pdfium()));

fn bind_pdfium() -> Option<pdfium_render::prelude::Pdfium> {
    use pdfium_render::prelude::Pdfium;
    // `PDFIUM_DYNAMIC_LIB_PATH` names the directory holding `libpdfium.so`
    // (Dockerfile.viewer-rs sets it); otherwise fall back to the default
    // chain (executable dir, then system library).
    let bindings = match std::env::var("PDFIUM_DYNAMIC_LIB_PATH").ok().filter(|s| !s.is_empty()) {
        Some(dir) => Pdfium::bind_to_library(Pdfium::pdfium_platform_library_name_at_path(&dir)),
        None => Pdfium::bind_to_library(
            Pdfium::pdfium_platform_library_name_at_path("./"),
        )
        .or_else(|_| Pdfium::bind_to_system_library()),
    };
    bindings.map(Pdfium::new).ok()
}

/// Whether a PDFium shared library was successfully bound — render parity
/// tests skip when this is false (same ladder as Python-without-pypdfium2).
pub fn pdfium_available() -> bool {
    PDFIUM.lock().map(|g| g.is_some()).unwrap_or(false)
}

// ── Raw counting (no memo, no disk) ──────────────────────────────────────────

/// `/Type\s*/Page[^s]` fallback — byte-for-byte the `_PAGE_RE` heuristic
/// Python applies when `pypdfium2` is unavailable.
fn regex_page_count(data: &[u8]) -> usize {
    const NEEDLE: &[u8] = b"/Type";
    let mut count = 0;
    let mut i = 0;
    while i + NEEDLE.len() <= data.len() {
        if &data[i..i + NEEDLE.len()] == NEEDLE {
            let mut j = i + NEEDLE.len();
            while j < data.len() && (data[j] == b' ' || data[j] == b'\t' || data[j] == b'\n' || data[j] == b'\r' || data[j] == b'\x0c' || data[j] == b'\x00') {
                j += 1;
            }
            if data[j..].starts_with(b"/Page")
                && !matches!(data.get(j + 5), Some(b's') | Some(b'S'))
            {
                // `/Type\s*/Page[^s]`: the char after `/Page` must not be
                // `s` (excludes `/Pages`). Python's `[^s]` also requires a
                // char to exist — a trailing `/Page` at EOF does NOT match.
                count += 1;
            }
        }
        i += 1;
    }
    count
}

/// Raw page count without memo or disk cache.
///
/// With a bound PDFium this is `len(PdfDocument)` exactly as `pypdfium2`
/// reports it (open failure → 0). Without the shared library it climbs the
/// same ladder Python uses when `pypdfium2` is missing (`lopdf`, then the
/// regex heuristic), so `cargo test` and slim environments keep working.
pub fn pdf_page_count_uncached(path: &Path) -> usize {
    if let Some(count) = pdfium_page_count(path) {
        return count;
    }
    // No PDFium library — lopdf, then regex (never raises).
    if let Ok(doc) = lopdf::Document::load(path) {
        return doc.get_pages().len();
    }
    std::fs::read(path).map(|d| regex_page_count(&d)).unwrap_or(0)
}

/// `Some(count)` when a PDFium library is bound (`None` = no library, so
/// the caller climbs the fallback ladder). An unloadable document yields
/// `Some(0)`, matching `pypdfium2`'s open-failure contract.
fn pdfium_page_count(path: &Path) -> Option<usize> {
    let guard = PDFIUM.lock().ok()?;
    let pdfium = guard.as_ref()?;
    let count = match pdfium.load_pdf_from_file(path, None) {
        Ok(doc) => doc.pages().len() as usize,
        Err(_) => 0,
    };
    Some(count)
}

// ── In-process memo (port of _PDF_COUNT_MEMO) ─────────────────────────────────

/// Same horizon as `_PDF_MEMO_TTL` (300s): older entries force a re-read.
const PDF_MEMO_TTL: Duration = Duration::from_secs(300);

struct PdfMemo {
    map: HashMap<(String, u128, u64), (usize, Instant)>,
    hits: u64,
    misses: u64,
}

static PDF_MEMO: LazyLock<Mutex<PdfMemo>> = LazyLock::new(|| {
    Mutex::new(PdfMemo {
        map: HashMap::new(),
        hits: 0,
        misses: 0,
    })
});

/// Copy of the memo hit/miss counters (observability for tests).
pub fn pdf_memo_stats() -> (u64, u64) {
    PDF_MEMO
        .lock()
        .map(|m| (m.hits, m.misses))
        .unwrap_or((0, 0))
}

/// Drop all memoized page counts and reset hit/miss counters.
pub fn clear_pdf_page_count_memo() {
    if let Ok(mut m) = PDF_MEMO.lock() {
        m.map.clear();
        m.hits = 0;
        m.misses = 0;
    }
}

fn memo_key(path: &Path) -> Option<(String, u128, u64)> {
    let st = std::fs::metadata(path).ok()?;
    let mtime_ns = st
        .modified()
        .ok()?
        .duration_since(std::time::UNIX_EPOCH)
        .ok()?
        .as_nanos();
    Some((path.to_string_lossy().into_owned(), mtime_ns, st.len()))
}

// ── Disk cache (port of warmup_cache.py's pdf kind) ──────────────────────────

/// 7 days: corruption safety valve only — the stat tuple in the key is the
/// real invalidator. Read at call time like Python (never a default arg).
fn persistent_cache_ttl() -> f64 {
    7.0 * 24.0 * 3600.0
}

const MAX_DISK_ENTRIES: usize = 5000;
const CACHE_FILENAME: &str = "warmup-cache-v1.json";

fn cache_file_path() -> PathBuf {
    let base = std::env::var("BOOKQ_CACHE_DIR").unwrap_or_else(|_| {
        format!("{}/bookq-cache", std::env::temp_dir().to_string_lossy())
    });
    PathBuf::from(base).join(CACHE_FILENAME)
}

/// `r:<rel-posix>\0<mtime_ns>\0<size>` when under `downloads_root`, else
/// `a:<abspath>\0<mtime_ns>\0<size>` — never embeds either mount root beyond
/// the relative form, so re-mounted trees keep hitting.
fn cache_key(downloads_root: Option<&Path>, path: &Path) -> Option<String> {
    let st = std::fs::metadata(path).ok()?;
    let mtime_ns = st
        .modified()
        .ok()?
        .duration_since(std::time::UNIX_EPOCH)
        .ok()?
        .as_nanos();
    let size = st.len();
    if let Some(root) = downloads_root {
        if let Ok(rel) = path.strip_prefix(root) {
            return Some(format!(
                "r:{}\x00{}\x00{}",
                rel.to_string_lossy().replace('\\', "/"),
                mtime_ns,
                size
            ));
        }
        // Same comparison on resolved paths (covers symlinked tmpdirs).
        if let (Ok(p), Ok(r)) = (std::fs::canonicalize(path), std::fs::canonicalize(root)) {
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

fn cache_key_for_path(path: &Path) -> Option<String> {
    let root = std::env::var("DOWNLOADS_DIR").unwrap_or_default();
    let root = if root.is_empty() { None } else { Some(PathBuf::from(root)) };
    cache_key(root.as_deref(), path)
}

/// Minimal JSON-dict read: parse only well-formed `{"key": {"kind",
/// "count", "saved_at"}}` mappings, ignore everything else (fail-open).
/// Whole-file locking is out of scope — Python serialises only its own
/// threads too; concurrent writers last-win, readers never break.
fn disk_get_pdf_count(key: &str) -> Option<usize> {
    let text = std::fs::read_to_string(cache_file_path()).ok()?;
    let data: serde_json::Value = serde_json::from_str(&text).ok()?;
    let entry = data.get(key)?;
    if entry.get("kind")?.as_str()? != "pdf" {
        return None;
    }
    let saved_at = entry.get("saved_at")?.as_f64()?;
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .ok()?
        .as_secs_f64();
    if now - saved_at > persistent_cache_ttl() {
        return None;
    }
    let count = entry.get("count")?.as_u64()?;
    usize::try_from(count).ok()
}

fn disk_put_pdf_count(key: &str, count: usize) {
    let path = cache_file_path();
    // Load-modify-save; any failure at any step is swallowed (fail-open).
    let mut data: serde_json::Value = std::fs::read_to_string(&path)
        .ok()
        .and_then(|t| serde_json::from_str(&t).ok())
        .filter(|v: &serde_json::Value| v.is_object())
        .unwrap_or_else(|| serde_json::Value::Object(Default::default()));
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    if let Some(map) = data.as_object_mut() {
        map.insert(
            key.to_string(),
            serde_json::json!({"kind": "pdf", "count": count, "saved_at": now}),
        );
        // `_evict_oldest`: drop oldest-`saved_at` entries past the cap.
        while map.len() > MAX_DISK_ENTRIES {
            let oldest = map
                .iter()
                .min_by(|a, b| {
                    saved_at_of(a.1)
                        .partial_cmp(&saved_at_of(b.1))
                        .unwrap_or(std::cmp::Ordering::Equal)
                })
                .map(|(k, _)| k.clone());
            match oldest {
                Some(k) => {
                    map.remove(&k);
                }
                None => break,
            }
        }
    }
    // Atomic tmp+rename; uniquely-named tmp so concurrent writers never
    // share a path (same rule as `_store_thumb`'s `.pid.rand.tmp`).
    if let Some(dir) = path.parent() {
        if std::fs::create_dir_all(dir).is_err() {
            return;
        }
    }
    let tmp = path.with_extension(format!(
        "tmp-{}.{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0)
    ));
    if serde_json::to_string(&data)
        .ok()
        .and_then(|body| std::fs::write(&tmp, body).ok())
        .is_some()
    {
        let _ = std::fs::rename(&tmp, &path);
        let _ = std::fs::remove_file(&tmp);
    }
}

fn saved_at_of(entry: &serde_json::Value) -> f64 {
    entry.get("saved_at").and_then(|v| v.as_f64()).unwrap_or(0.0)
}

/// `pdf_page_count` with both cache layers — the `app.archive_pdf`
/// entry point. `downloads_root=None` keeps the env-lookup behavior via
/// `DOWNLOADS_DIR` (all existing callers work unchanged); `scan_tree`
/// passes its top-level base so keys stay `r:`-relative.
pub fn pdf_page_count(path: &Path) -> usize {
    pdf_page_count_with_root(path, None)
}

pub fn pdf_page_count_with_root(path: &Path, downloads_root: Option<&Path>) -> usize {
    let key = match memo_key(path) {
        Some(k) => k,
        None => return 0,
    };
    if let Ok(m) = PDF_MEMO.lock() {
        if let Some((count, at)) = m.map.get(&key) {
            if at.elapsed() <= PDF_MEMO_TTL {
                let count = *count;
                drop(m);
                if let Ok(mut m) = PDF_MEMO.lock() {
                    m.hits += 1;
                }
                return count;
            }
        }
    }
    if let Ok(mut m) = PDF_MEMO.lock() {
        m.misses += 1;
    }
    // Restart-spanning disk cache, consulted ONLY on memo miss (fail-open).
    let disk_key = downloads_root
        .map(|r| cache_key(Some(r), path))
        .unwrap_or_else(|| cache_key_for_path(path));
    if let Some(ref dk) = disk_key {
        if let Some(hit) = disk_get_pdf_count(dk) {
            if let Ok(mut m) = PDF_MEMO.lock() {
                m.map.insert(key, (hit, Instant::now()));
            }
            return hit;
        }
    }
    let count = pdf_page_count_uncached(path);
    if let Ok(mut m) = PDF_MEMO.lock() {
        m.map.insert(key, (count, Instant::now()));
    }
    if let Some(dk) = disk_key {
        disk_put_pdf_count(&dk, count);
    }
    count
}

// ── Rendering ─────────────────────────────────────────────────────────────────

/// Render 0-based page `index` to PNG bytes at `RENDER_SCALE`, or `None`
/// when unavailable — port of `render_pdf_page(path, index, scale=2.0)`.
///
/// Pixel contract (== `pypdfium2` `page.render(scale=2.0)` + `to_pil` +
/// `PIL-save-PNG`): white opaque fill, annotations drawn, dimensions
/// `ceil(pt * 2)` per side. The PNG *encoding* differs (Pillow vs `image`
/// crate) — only decoded pixels are contractual.
pub fn render_pdf_page(path: &Path, index: usize) -> Option<Vec<u8>> {
    let guard = PDFIUM.lock().ok()?;
    let pdfium = guard.as_ref()?;
    let doc = pdfium.load_pdf_from_file(path, None).ok()?;
    let pages = doc.pages();
    if index >= pages.len() as usize {
        return None;
    }
    let page = pages.get(index as i32).ok()?;
    // pypdfium2 dims: `ceil(pt * scale)` per side (rotation swaps only
    // for an explicit extra rotation, which the app never passes).
    let w = (page.width().value * RENDER_SCALE).ceil().max(1.0) as i32;
    let h = (page.height().value * RENDER_SCALE).ceil().max(1.0) as i32;
    let bitmap = page
        .render_with_config(&pdfium_render::prelude::PdfRenderConfig::new().set_fixed_size(w, h))
        .ok()?;
    let image = bitmap.as_image().ok()?;
    // Opaque RGB like pypdfium2's BGR→PIL-RGB: composite onto white so
    // a stray alpha (engine-version dependent) can never leak through.
    let rgb = flatten_on_white(&image.to_rgba8());
    let mut out = Vec::new();
    {
        let enc = image::codecs::png::PngEncoder::new(&mut out);
        use image::ImageEncoder;
        enc.write_image(&rgb, w as u32, h as u32, image::ExtendedColorType::Rgb8)
            .ok()?;
    }
    Some(out)
}

/// Alpha composite onto white — same linear op as Pillow's
/// `bg.paste(rgba, mask)` in `make_view_bytes`.
fn flatten_on_white(rgba: &image::RgbaImage) -> Vec<u8> {
    let mut out = Vec::with_capacity((rgba.width() * rgba.height() * 3) as usize);
    for p in rgba.pixels() {
        let a = p[3] as u32;
        let inv = 255 - a;
        out.push(((p[0] as u32 * a + 255 * inv) / 255) as u8);
        out.push(((p[1] as u32 * a + 255 * inv) / 255) as u8);
        out.push(((p[2] as u32 * a + 255 * inv) / 255) as u8);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn page_name_roundtrip() {
        assert_eq!(pdf_page_name(0), "p001.png");
        assert_eq!(pdf_page_name(9), "p010.png");
        assert_eq!(parse_pdf_page_name("p001.png", 3), Some(0));
        assert_eq!(parse_pdf_page_name("p03.png", 3), Some(2));
        assert_eq!(parse_pdf_page_name("p1.png", 3), Some(0));
        // Range-checked like Python: 0-based idx must be < page_count.
        assert_eq!(parse_pdf_page_name("p000.png", 3), None);
        assert_eq!(parse_pdf_page_name("p004.png", 3), None);
        assert_eq!(parse_pdf_page_name("cover.png", 3), None);
        assert_eq!(parse_pdf_page_name("p01.jpg", 3), None);
        assert_eq!(parse_pdf_page_name("p.png", 3), None);
        assert_eq!(parse_pdf_page_name("xp001.png", 3), None);
    }

    #[test]
    fn regex_heuristic_counts_pages() {
        // Same shape as tests/test_archive.py::_make_pdf (no xref table):
        // two `/Type /Page` objects plus the `/Type /Pages` root.
        let data = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n\
            2 0 obj\n<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>\nendobj\n\
            3 0 obj\n<< /Type /Page /Parent 2 0 R >>\nendobj\n\
            4 0 obj\n<< /Type/Page/Parent 2 0 R >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF";
        assert_eq!(regex_page_count(data), 2);
        assert_eq!(regex_page_count(b"garbage"), 0);
    }

    /// Minimal valid PDF (with xref) written by hand — lopdf and PDFium
    /// must agree with pypdfium2's count of 2.
    fn write_valid_pdf(path: &Path) {
        let mut objects: Vec<(u32, Vec<u8>)> = Vec::new();
        objects.push((1, b"<< /Type /Catalog /Pages 2 0 R >>".to_vec()));
        objects.push((2, b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>".to_vec()));
        for n in [3u32, 4] {
            objects.push((
                n,
                format!(
                    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 300] /Contents {} 0 R >>",
                    n + 2
                )
                .into_bytes(),
            ));
        }
        for n in [5u32, 6] {
            let stream = format!("BT /F1 12 Tf 10 280 Td (Page {}) Tj ET", n - 4);
            objects.push((
                n,
                format!("<< /Length {} >>\nstream\n{}\nendstream", stream.len(), stream).into_bytes(),
            ));
        }
        let mut out = b"%PDF-1.4\n".to_vec();
        let mut offsets = Vec::new();
        for (num, body) in &objects {
            offsets.push((num, out.len()));
            out.extend_from_slice(format!("{} 0 obj\n", num).as_bytes());
            out.extend_from_slice(body);
            out.extend_from_slice(b"\nendobj\n");
        }
        let xref_pos = out.len();
        out.extend_from_slice(format!("xref\n0 {}\n", objects.len() + 1).as_bytes());
        out.extend_from_slice(b"0000000000 65535 f \n");
        for (_, off) in &offsets {
            out.extend_from_slice(format!("{:010} 00000 n \n", off).as_bytes());
        }
        out.extend_from_slice(
            format!(
                "trailer\n<< /Size {} /Root 1 0 R >>\nstartxref\n{}\n%%EOF",
                objects.len() + 1,
                xref_pos
            )
            .as_bytes(),
        );
        std::fs::write(path, out).unwrap();
    }

    fn tmp_pdf(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "bq-pdf-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join(name);
        write_valid_pdf(&p);
        p
    }

    #[test]
    fn uncached_count_agrees_on_valid_pdf() {
        let p = tmp_pdf("doc.pdf");
        // lopdf leg (always available): exact count on xref-valid files.
        assert_eq!(pdf_page_count_uncached(&p), 2);
        // ... and the PDFium leg agrees when the library is present.
        if pdfium_available() {
            assert_eq!(pdfium_page_count(&p), Some(2));
        }
        std::fs::remove_dir_all(p.parent().unwrap()).ok();
    }

    #[test]
    fn memo_and_disk_cache_roundtrip() {
        let p = tmp_pdf("memo.pdf");
        clear_pdf_page_count_memo();
        let before = pdf_memo_stats();
        // Point the disk cache at a scratch dir (call-time env, like Python).
        let cache_dir = p.parent().unwrap().join("cache");
        std::env::set_var("BOOKQ_CACHE_DIR", &cache_dir);
        // First call: miss → live read → fills memo + disk.
        assert_eq!(pdf_page_count(&p), 2);
        let (hits, misses) = pdf_memo_stats();
        assert!(misses > before.1);
        // Second call: memo hit, no re-read.
        assert_eq!(pdf_page_count(&p), 2);
        let (hits2, _) = pdf_memo_stats();
        assert!(hits2 > hits);
        // Cold memo + warm disk: disk hit serves without a live read.
        clear_pdf_page_count_memo();
        assert_eq!(pdf_page_count(&p), 2);
        // Corrupt the disk entry → falls back to the live read (fail-open).
        std::fs::write(cache_dir.join(CACHE_FILENAME), b"{broken").unwrap();
        clear_pdf_page_count_memo();
        assert_eq!(pdf_page_count(&p), 2);
        std::env::remove_var("BOOKQ_CACHE_DIR");
        std::fs::remove_dir_all(p.parent().unwrap()).ok();
    }

    #[test]
    fn render_matches_pypdfium2_contract_when_lib_present() {
        if !pdfium_available() {
            // Same ladder as Python-without-pypdfium2: nothing to assert.
            return;
        }
        let p = tmp_pdf("render.pdf");
        // Page size is 200x300pt → 400x600px at scale 2.0 (ceil, like pypdfium2).
        let png = render_pdf_page(&p, 0).expect("render page 0");
        assert_eq!(&png[..8], b"\x89PNG\r\n\x1a\n");
        let img = image::load_from_memory(&png).unwrap();
        assert_eq!((img.width(), img.height()), (400, 600));
        // Out-of-range index → None (Python 404s; the gateway proxies).
        assert!(render_pdf_page(&p, 2).is_none());
        std::fs::remove_dir_all(p.parent().unwrap()).ok();
    }
}
