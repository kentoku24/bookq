//! bookq_rs — native (Rust) implementation of the sort-key core.
//!
//! Phase 1 W1 pilot (issue #163): byte-for-byte port of `app/scanner.py`'s
//! `_sort_key` / `_normalize_kanji` / `_kanji_run_to_int` / `natural_key`.
//! The Python originals stay the reference implementation — these functions
//! must produce *identical* key structures so all downstream `sorted(key=…)`
//! comparisons behave exactly as the Python versions did (including the
//! `int` vs `str` TypeError behaviour on mixed shapes, which the callers'
//! tuple comparisons still own).
//!
//! Parity notes:
//! * `natural_key` keeps `re.split("([0-9]+)")` semantics: leading/trailing
//!   empty-string parts are preserved, so `natural_key("1a")` is `('', 1, 'a')`.
//! * Numeric parts are created through Python's `int()` so arbitrarily long
//!   digit runs keep bignum parity (`natural_key("x"+"9"*30)` == Python).
//! * Input `str` containing lone surrogates (surrogateescape filenames) cannot
//!   be represented as Rust `&str`; `to_str()` then fails and the Python-side
//!   wrapper falls back to the reference implementation — parity preserved.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyString, PyTuple};
use pyo3::IntoPyObjectExt;

// ── Kanji numeral tables (mirrors app/scanner.py) ────────────────────────────

fn kanji_digit(c: char) -> Option<i64> {
    match c {
        '〇' => Some(0),
        '一' => Some(1),
        '二' => Some(2),
        '三' => Some(3),
        '四' => Some(4),
        '五' => Some(5),
        '六' => Some(6),
        '七' => Some(7),
        '八' => Some(8),
        '九' => Some(9),
        _ => None,
    }
}

fn kanji_unit(c: char) -> Option<i64> {
    match c {
        '十' => Some(10),
        '百' => Some(100),
        '千' => Some(1000),
        _ => None,
    }
}

fn is_kanji_big(c: char) -> bool {
    matches!(c, '万' | '億' | '兆')
}

/// `[〇一二三四五六七八九十百千万億兆]` — the `_KANJI_RUN_RE` class.
fn is_kanji_run_char(c: char) -> bool {
    kanji_digit(c).is_some() || kanji_unit(c).is_some() || is_kanji_big(c)
}

/// `_KANJI_PRE` = set("第【之の・ー- ")  (trailing space included)
fn is_kanji_pre(c: char) -> bool {
    matches!(c, '第' | '【' | '之' | 'の' | '・' | 'ー' | '-' | ' ')
}

/// `_KANJI_POST` = set("部話章巻回集編幕期頁号節月】")
fn is_kanji_post(c: char) -> bool {
    matches!(
        c,
        '部' | '話' | '章' | '巻' | '回' | '集' | '編' | '幕' | '期' | '頁' | '号' | '節' | '月'
            | '】'
    )
}

/// `_kanji_run_to_int`: '二十三'->23, '十'->10, '二百五'->205; None on 万/億/兆.
/// Accumulation mirrors Python exactly: digits set `cur`, units add
/// `(cur or 1) * unit` to `total` and reset `cur`. Run length is bounded by
/// the input string, so i64 cannot overflow on any realistic name
/// (max ~255 chars × 1110 per unit ≪ i64::MAX).
fn kanji_run_to_int_impl(run: &[char]) -> Option<i64> {
    if run.iter().any(|&c| is_kanji_big(c)) {
        return None;
    }
    let (mut total, mut cur): (i64, i64) = (0, 0);
    for &ch in run {
        if let Some(d) = kanji_digit(ch) {
            cur = d;
        } else if let Some(u) = kanji_unit(ch) {
            total += (if cur == 0 { 1 } else { cur }) * u;
            cur = 0;
        }
    }
    Some(total + cur)
}

/// `_normalize_kanji`: convert kanji numerals in numeric slots to ASCII digits.
/// A run converts only when the char before it (if any) is in PRE and the char
/// after it (if any) is in POST; string edges pass both guards.
pub(crate) fn normalize_kanji_impl(name: &str) -> String {
    let chars: Vec<char> = name.chars().collect();
    let n = chars.len();
    let mut out = String::with_capacity(name.len());
    let mut i = 0usize;
    while i < n {
        if !is_kanji_run_char(chars[i]) {
            out.push(chars[i]);
            i += 1;
            continue;
        }
        let start = i;
        while i < n && is_kanji_run_char(chars[i]) {
            i += 1;
        }
        let run = &chars[start..i];
        let keep_run = |out: &mut String, run: &[char]| out.extend(run.iter());
        if start > 0 && !is_kanji_pre(chars[start - 1]) {
            keep_run(&mut out, run);
            continue;
        }
        if i < n && !is_kanji_post(chars[i]) {
            keep_run(&mut out, run);
            continue;
        }
        match kanji_run_to_int_impl(run) {
            Some(v) => out.push_str(&v.to_string()),
            None => keep_run(&mut out, run),
        }
    }
    out
}

/// One part of a `re.split("([0-9]+)")` result.
pub(crate) enum Part {
    Text(String),
    Num(String), // ASCII digits only; int() applied on the Python side
}

/// Faithful `re.split(r"([0-9]+)", s)`: alternating text/num segments,
/// always beginning and ending with a text segment (possibly empty).
pub(crate) fn split_numeric(s: &str) -> Vec<Part> {
    let mut parts = Vec::new();
    let mut text = String::new();
    let mut run: Option<String> = None;
    for c in s.chars() {
        if c.is_ascii_digit() {
            match run.as_mut() {
                Some(r) => r.push(c),
                None => {
                    parts.push(Part::Text(std::mem::take(&mut text)));
                    run = Some(c.to_string());
                }
            }
        } else if let Some(r) = run.take() {
            parts.push(Part::Num(r));
            text.push(c);
        } else {
            text.push(c);
        }
    }
    if let Some(r) = run {
        parts.push(Part::Num(r));
    }
    parts.push(Part::Text(text));
    parts
}

// ── Python-facing helpers ────────────────────────────────────────────────────

/// Downcast to PyString and extract `&str`. Any failure (non-str, or str with
/// lone surrogates) maps to PyValueError so the wrapper can fall back to the
/// reference implementation.
fn extract_name<'py>(obj: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyString>> {
    obj.cast::<PyString>()
        .map(|s| s.clone())
        .map_err(|_| PyValueError::new_err("expected str"))
}

/// builtins.int — hoisted so batch calls do one import per list, not per part.
fn int_fn<'py>(py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
    Ok(py.import("builtins")?.getattr("int")?)
}

/// Build the Python `int` for a digit run via builtins — keeps bignum parity.
fn py_int(int_f: &Bound<'_, PyAny>, digits: &str) -> PyResult<Py<PyAny>> {
    Ok(int_f.call1((digits,))?.unbind())
}

/// Convert split parts into the Python tuple `(text | int, ...)`.
fn parts_to_tuple(
    py: Python<'_>,
    int_f: &Bound<'_, PyAny>,
    parts: Vec<Part>,
) -> PyResult<Py<PyTuple>> {
    let mut items: Vec<Py<PyAny>> = Vec::with_capacity(parts.len());
    for p in parts {
        let obj = match p {
            Part::Num(d) => py_int(int_f, &d)?,
            Part::Text(t) => PyString::new(py, &t).into_any().unbind(),
        };
        items.push(obj);
    }
    Ok(PyTuple::new(py, items)?.unbind())
}

// ── Exported functions (names mirror app/scanner.py) ─────────────────────────

#[pyfunction]
fn _kanji_run_to_int(py: Python<'_>, run: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    let s = extract_name(run)?;
    let chars: Vec<char> = s.to_str()?.chars().collect();
    match kanji_run_to_int_impl(&chars) {
        Some(v) => v.into_py_any(py),
        None => Ok(py.None()),
    }
}

#[pyfunction]
fn _normalize_kanji(_py: Python<'_>, name: &Bound<'_, PyAny>) -> PyResult<String> {
    let s = extract_name(name)?;
    Ok(normalize_kanji_impl(s.to_str()?))
}

#[pyfunction]
fn _sort_key(py: Python<'_>, name: &Bound<'_, PyAny>) -> PyResult<Py<PyTuple>> {
    let s = extract_name(name)?;
    let int_f = int_fn(py)?;
    let mut items: Vec<Py<PyAny>> = Vec::new();
    for p in split_numeric(s.to_str()?) {
        if let Part::Num(d) = p {
            items.push(py_int(&int_f, &d)?);
        }
    }
    Ok(PyTuple::new(py, items)?.unbind())
}

#[pyfunction]
fn natural_key(py: Python<'_>, name: &Bound<'_, PyAny>) -> PyResult<Py<PyTuple>> {
    let s = extract_name(name)?;
    let normalized = normalize_kanji_impl(s.to_str()?);
    parts_to_tuple(py, &int_fn(py)?, split_numeric(&normalized))
}

// ── Batch variants ───────────────────────────────────────────────────────────
// One FFI crossing per list instead of per name; callers sort
// `zip(keys, names)` in Python, which is element-for-element identical to
// `sorted(names, key=lambda n: (natural_key(n), n))` — the same (key, name)
// tuple pairs feed the same comparison on the Python side. (Positions in the
// key tuple are structurally aligned — even indices text, odd indices ints —
// so key comparison never mixes types.)
// `Vec<String>` extraction rejects lone surrogates for the whole batch; the
// Python wrapper then falls back to the per-name path, which falls back per
// name to the reference impl — same net result as Python alone.

#[pyfunction]
fn natural_keys(py: Python<'_>, names: Vec<String>) -> PyResult<Vec<Py<PyTuple>>> {
    let int_f = int_fn(py)?;
    names
        .iter()
        .map(|n| {
            parts_to_tuple(
                py,
                &int_f,
                split_numeric(&normalize_kanji_impl(n)),
            )
        })
        .collect()
}

#[pyfunction]
fn sort_keys(py: Python<'_>, names: Vec<String>) -> PyResult<Vec<Py<PyTuple>>> {
    let int_f = int_fn(py)?;
    names
        .iter()
        .map(|n| {
            let mut items: Vec<Py<PyAny>> = Vec::new();
            for p in split_numeric(n) {
                if let Part::Num(d) = p {
                    items.push(py_int(&int_f, &d)?);
                }
            }
            PyTuple::new(py, items).map(|t| t.unbind())
        })
        .collect()
}

mod archive;

// ── W2: archive readers (parity contract documented in archive.rs) ─────────

/// `zip_names(path)` → CD-ordered entry names; None → Python fallback.
#[pyfunction]
fn zip_names(path: &str) -> Option<Vec<String>> {
    archive::zip_names(std::path::Path::new(path))
}

/// `zip_read_entry(path, inner)` → entry bytes; None → Python fallback.
#[pyfunction]
fn zip_read_entry(path: &str, inner: &str) -> Option<Vec<u8>> {
    archive::zip_read_entry(std::path::Path::new(path), inner)
}

/// `epub_raw(path)` → (raw zip names, spine-resolved names); None → fallback.
#[pyfunction]
fn epub_raw(path: &str) -> Option<(Vec<String>, Vec<String>)> {
    archive::epub_raw(std::path::Path::new(path))
}

/// `rar_names(path)` → (filename, is_dir) in header order; None → fallback.
#[pyfunction]
fn rar_names(path: &str) -> Option<Vec<(String, bool)>> {
    archive::rar_names(std::path::Path::new(path))
}

mod meta;
mod scans;

// ── W3: filesystem walks (parity contract documented in scans.rs) ─────────

/// `scan_fs(path)` → nested (files, archives, pdfs, [(name, subdir)...]);
/// None → Python fallback.
#[pyfunction]
fn scan_fs(py: Python<'_>, path: &str) -> Option<Py<PyAny>> {
    let p = std::path::PathBuf::from(path);
    let got = py.detach(move || scans::scan_fs(&p));
    got.map(|d| fsdir_to_py(py, d))
}

fn fsdir_to_py(py: Python<'_>, d: scans::FsDir) -> Py<PyAny> {
    let subs: Vec<(String, Py<PyAny>)> = d
        .subdirs
        .into_iter()
        .map(|(n, s)| (n, fsdir_to_py(py, s)))
        .collect();
    PyTuple::new(py, [
        d.files.into_py_any(py).unwrap_or_else(|_| py.None()),
        d.archives.into_py_any(py).unwrap_or_else(|_| py.None()),
        d.pdfs.into_py_any(py).unwrap_or_else(|_| py.None()),
        subs.into_py_any(py).unwrap_or_else(|_| py.None()),
    ])
    .map(|t| t.into_any().unbind())
    .unwrap_or_else(|_| py.None())
}

/// `list_images(path)` → sorted image names; None → Python fallback.
#[pyfunction]
fn list_images_rs(path: &str) -> Option<Vec<String>> {
    scans::list_images(std::path::Path::new(path))
}

/// `tree_signature(path)` → sha256 hexdigest; None → Python fallback.
#[pyfunction]
fn tree_signature_rs(py: Python<'_>, path: &str) -> Option<String> {
    let p = std::path::PathBuf::from(path);
    py.detach(move || scans::tree_signature(&p))
}

// ── W4: metadata read path (parity contract documented in meta.rs) ────────

/// `meta_walk(root)` → ComicRec list in `_walk` order; None → fallback.
#[pyfunction]
fn meta_walk(py: Python<'_>, root: &str) -> Option<Vec<meta::ComicRec>> {
    let p = std::path::PathBuf::from(root);
    py.detach(move || meta::meta_walk(&p))
}

/// `parse_metadata_batch(base, folders)` → Option<ComicRec> per folder;
/// None → Python fallback for the whole batch.
#[pyfunction]
fn parse_metadata_batch(
    py: Python<'_>,
    base: &str,
    folders: Vec<String>,
) -> Option<Vec<Option<meta::ComicRec>>> {
    let p = std::path::PathBuf::from(base);
    py.detach(move || meta::parse_metadata_batch(&p, &folders))
}

#[pymodule]
fn bookq_rs(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(_kanji_run_to_int, m)?)?;
    m.add_function(wrap_pyfunction!(_normalize_kanji, m)?)?;
    m.add_function(wrap_pyfunction!(_sort_key, m)?)?;
    m.add_function(wrap_pyfunction!(natural_key, m)?)?;
    m.add_function(wrap_pyfunction!(natural_keys, m)?)?;
    m.add_function(wrap_pyfunction!(sort_keys, m)?)?;
    m.add_function(wrap_pyfunction!(zip_names, m)?)?;
    m.add_function(wrap_pyfunction!(zip_read_entry, m)?)?;
    m.add_function(wrap_pyfunction!(epub_raw, m)?)?;
    m.add_function(wrap_pyfunction!(rar_names, m)?)?;
    m.add_function(wrap_pyfunction!(scan_fs, m)?)?;
    m.add_function(wrap_pyfunction!(list_images_rs, m)?)?;
    m.add_function(wrap_pyfunction!(tree_signature_rs, m)?)?;
    m.add_function(wrap_pyfunction!(meta_walk, m)?)?;
    m.add_function(wrap_pyfunction!(parse_metadata_batch, m)?)?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
