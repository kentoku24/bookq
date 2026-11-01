//! bookq-core — pure-Rust port of bookq's CPU-heavy server-side logic.
//!
//! Issue #163, parallel-backend track: the code below was originally written
//! for the in-process PyO3 experiment (PRs #172/#176/#177, now closed) and is
//! verbatim behaviour-wise — `app/*.py` stays the reference implementation.
//! Consumed by the viewer-rs binary; `app/` itself is never touched.
//!
//! Parity notes (from the PyO3 phase, still the contract):
//! * `natural_key` keeps `re.split("([0-9]+)")` semantics: leading/trailing
//!   empty-string parts are preserved, so `natural_key("1a")` is `('', 1, 'a')`.
//! * Filename sort order mirrors Python's `(natural_key(n), n)` tuple ordering
//!   via [`natural_cmp`]; image order mirrors `_sort_key` via [`sort_file_cmp`]
//!   with `cover.*` first.
//! * Symlink stat calls follow targets like Python `entry.stat()`; directory
//!   cycles terminate on `(st_dev, st_ino)` visited tracking.

mod archive;
mod meta;
mod scans;

pub use archive::{epub_raw, rar_names, zip_names, zip_read_entry, ZipEntry};
pub use meta::{meta_walk, parse_metadata_batch, ComicRec};
pub use scans::{
    list_images, natural_cmp, scan_fs, sort_file_cmp, tree_signature, FsDir,
};

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
    Num(String),
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::cmp::Ordering;

    #[test]
    fn kanji_run_to_int() {
        let chars = |s: &str| s.chars().collect::<Vec<_>>();
        assert_eq!(kanji_run_to_int_impl(&chars("二十三")), Some(23));
        assert_eq!(kanji_run_to_int_impl(&chars("十")), Some(10));
        assert_eq!(kanji_run_to_int_impl(&chars("二百五")), Some(205));
        assert_eq!(kanji_run_to_int_impl(&chars("万")), None);
    }

    #[test]
    fn normalize_kanji() {
        assert_eq!(normalize_kanji_impl("【第十話】"), "【第10話】");
        assert_eq!(normalize_kanji_impl("第一部"), "第1部");
        // Proper nouns are guarded: no PRE/POST context, no conversion.
        assert_eq!(normalize_kanji_impl("千尋"), "千尋");
        assert_eq!(normalize_kanji_impl("六本木"), "六本木");
    }

    #[test]
    fn natural_cmp_orders_like_python_tuples() {
        assert_eq!(natural_cmp("ch-2", "ch-10"), Ordering::Less);
        assert_eq!(natural_cmp("【第一部】", "【第十話】"), Ordering::Less);
        // Tie-break on raw name like Python's (key, name) comparison.
        assert_eq!(natural_cmp("a2", "a02"), Ordering::Greater);
    }

    #[test]
    fn sort_file_cmp_cover_first() {
        assert_eq!(sort_file_cmp("cover.png", "001.png"), Ordering::Less);
        assert_eq!(sort_file_cmp("01.png", "2.png"), Ordering::Less);
        assert_eq!(sort_file_cmp("10.jpg", "9.jpg"), Ordering::Greater);
    }
}

/// `pdf_page_count(path)` — pypdfium2's `len(PdfDocument)` equivalent:
/// parsed pages-tree count, `0` on any open/parse failure (matching the
/// Python contract of never raising for a bad PDF).
pub fn pdf_page_count(path: &std::path::Path) -> usize {
    match lopdf::Document::load(path) {
        Ok(doc) => doc.get_pages().len(),
        Err(_) => 0,
    }
}
