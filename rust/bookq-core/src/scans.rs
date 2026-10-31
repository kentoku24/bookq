//! W3 (issue #163): filesystem walk ports — `scan_fs` (scan_tree's
//! classification+sort+visited work), `tree_signature` (compute_signature),
//! and `list_images`.
//!
//! Same contract as W1/W2: every function returns `None` when it cannot
//! prove byte-identical output — the Python wrappers then run the reference
//! implementation. Non-UTF-8 names are the main bail-out (a Rust `String`
//! cannot hold surrogateescape chars); the live corpus has none, and the
//! fallback keeps behaviour identical when they appear.
//!
//! Sorting mirrors Python tuple comparisons: `(natural_key(n), n)` for
//! dirs/archives/pdfs and `(_sort_key(n), n)` for files, where the final
//! `n` tiebreak is Python str order == UTF-8 byte order.

use std::cmp::Ordering;
use std::collections::HashSet;
use std::fs::{self, Metadata};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};

use crate::{normalize_kanji_impl, split_numeric, Part};

const IMAGE_EXT: [&str; 8] = [
    ".avif", ".webp", ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg",
];
const ARCHIVE_EXT: [&str; 5] = [".zip", ".cbz", ".rar", ".cbr", ".epub"];
const PDF_EXT: &str = ".pdf";
const TOML_NAME: &str = "metadata.toml";
const SIG_WORKERS: usize = 8;

// ── name helpers ────────────────────────────────────────────────────────────

/// `Path.suffix.lower()` for a non-dot name: last `.` onward, lowercased.
/// Python's `Path("a.").suffix` is `"."` — we keep the dot in the result.
fn suffix_lower(name: &str) -> Option<String> {
    name.rfind('.').map(|i| name[i..].to_lowercase())
}

fn utf8_name(e: &fs::DirEntry) -> Option<String> {
    std::str::from_utf8(e.file_name().as_bytes())
        .ok()
        .map(|s| s.to_string())
}

/// `entry.is_dir()` — follows symlinks like os.DirEntry.is_dir().
/// NOTE: `DirEntry::metadata()` is lstat (does NOT follow); Python's
/// `entry.stat()` follows — that is `fs::metadata(path)`.
fn is_dir_e(e: &fs::DirEntry) -> bool {
    match e.file_type() {
        Ok(t) if t.is_symlink() => {
            fs::metadata(e.path()).map(|m| m.is_dir()).unwrap_or(false)
        }
        Ok(t) => t.is_dir(),
        Err(_) => false,
    }
}

/// `entry.is_file()` — follows symlinks.
fn is_file_e(e: &fs::DirEntry) -> bool {
    match e.file_type() {
        Ok(t) if t.is_symlink() => {
            fs::metadata(e.path()).map(|m| m.is_file()).unwrap_or(false)
        }
        Ok(t) => t.is_file(),
        Err(_) => false,
    }
}

// ── Python-identical ordering ───────────────────────────────────────────────

/// Compare two digit runs as Python `int()`s without bignum: strip leading
/// zeros, then (len, lex) — equal value ⇔ equal int.
fn num_cmp(a: &str, b: &str) -> Ordering {
    let a = a.trim_start_matches('0');
    let b = b.trim_start_matches('0');
    a.len().cmp(&b.len()).then_with(|| a.cmp(b))
}

/// `sorted(key=lambda n: (natural_key(n), n))` — natural_key tuple compare,
/// then the raw-name tiebreak.
pub fn natural_cmp(a: &str, b: &str) -> Ordering {
    let ka = split_numeric(&normalize_kanji_impl(a));
    let kb = split_numeric(&normalize_kanji_impl(b));
    for i in 0..ka.len().min(kb.len()) {
        // Even positions are Text and odd are Num for every split — the two
        // sequences are structurally aligned, so a Text/Num pair cannot occur.
        let c = match (&ka[i], &kb[i]) {
            (Part::Text(x), Part::Text(y)) => x.cmp(y),
            (Part::Num(x), Part::Num(y)) => num_cmp(x, y),
            _ => Ordering::Equal,
        };
        if c != Ordering::Equal {
            return c;
        }
    }
    ka.len().cmp(&kb.len()).then_with(|| a.cmp(b))
}

/// `sorted(key=lambda n: (_sort_key(n), n))` — ints only, then tiebreak.
pub fn sort_file_cmp(a: &str, b: &str) -> Ordering {
    let nums = |s: &str| -> Vec<String> {
        split_numeric(s)
            .into_iter()
            .filter_map(|p| match p {
                Part::Num(d) => Some(d),
                _ => None,
            })
            .collect()
    };
    let (ka, kb) = (nums(a), nums(b));
    for i in 0..ka.len().min(kb.len()) {
        let c = num_cmp(&ka[i], &kb[i]);
        if c != Ordering::Equal {
            return c;
        }
    }
    ka.len().cmp(&kb.len()).then_with(|| a.cmp(b))
}

// ── scan_fs: scan_tree's FS half ────────────────────────────────────────────

/// One directory's classified+sorted entry names; subdirs recurse.
/// `(files, archives, pdfs, [(name, subdir)...])` mirrors the iterdir
/// classification — the Python side applies its own node assembly
/// (flatten/cover rules stay in Python so archive/pdf expansion keeps
/// using the Python cache and extractor paths).
pub struct FsDir {
    pub files: Vec<String>,
    pub archives: Vec<String>,
    pub pdfs: Vec<String>,
    pub subdirs: Vec<(String, FsDir)>,
}

fn scan_dir(base: &Path, visited: &mut HashSet<(u64, u64)>) -> Option<FsDir> {
    let (mut files, mut archives, mut pdfs, mut dirnames) =
        (Vec::new(), Vec::new(), Vec::new(), Vec::new());
    for entry in fs::read_dir(base).ok()? {
        let entry = entry.ok()?; // iterdir OSError → Python raises → fallback
        let name_bytes = entry.file_name();
        if name_bytes.as_bytes().first() == Some(&b'.') {
            continue;
        }
        let name = utf8_name(&entry)?; // non-UTF-8 → whole-scan fallback
        if is_dir_e(&entry) {
            let st = match fs::metadata(entry.path()) {
                Ok(m) => m,
                Err(_) => continue, // broken symlink / unreadable entry
            };
            if !visited.insert((st.dev(), st.ino())) {
                continue;
            }
            dirnames.push(name);
        } else if is_file_e(&entry) {
            match suffix_lower(&name).as_deref() {
                Some(s) if IMAGE_EXT.contains(&s) => files.push(name),
                Some(s) if ARCHIVE_EXT.contains(&s) => archives.push(name),
                Some(PDF_EXT) => pdfs.push(name),
                _ => {}
            }
        }
    }
    files.sort_by(|a, b| sort_file_cmp(a, b));
    dirnames.sort_by(|a, b| natural_cmp(a, b));
    archives.sort_by(|a, b| natural_cmp(a, b));
    pdfs.sort_by(|a, b| natural_cmp(a, b));
    let mut subdirs = Vec::with_capacity(dirnames.len());
    for d in dirnames {
        subdirs.push((d.clone(), scan_dir(&base.join(&d), visited)?));
    }
    Some(FsDir {
        files,
        archives,
        pdfs,
        subdirs,
    })
}

/// `scan_tree(base)` FS walk; `[]` when *base* does not exist.
pub fn scan_fs(base: &Path) -> Option<FsDir> {
    if !base.exists() {
        return Some(FsDir {
            files: vec![],
            archives: vec![],
            pdfs: vec![],
            subdirs: vec![],
        });
    }
    let mut visited = HashSet::new();
    if let Ok(st) = fs::metadata(base) {
        visited.insert((st.dev(), st.ino()));
    }
    scan_dir(base, &mut visited)
}

/// `list_images(folder_path)`: image names, `_sort_file` order.
pub fn list_images(path: &Path) -> Option<Vec<String>> {
    let mut files = Vec::new();
    for entry in fs::read_dir(path).ok()? {
        let entry = entry.ok()?;
        if !is_file_e(&entry) {
            continue;
        }
        if entry.file_name().as_bytes().first() == Some(&b'.') {
            continue;
        }
        let name = utf8_name(&entry)?;
        if suffix_lower(&name)
            .map(|s| IMAGE_EXT.contains(&s.as_str()))
            .unwrap_or(false)
        {
            files.push(name);
        }
    }
    files.sort_by(|a, b| sort_file_cmp(a, b));
    Some(files)
}

// ── compute_signature ───────────────────────────────────────────────────────

type Local = (Vec<u8>, Vec<(String, Metadata)>);

/// Python ``st_mtime_ns`` — full timestamp in ns (sec + nsec), NOT
/// ``MetadataExt::mtime_nsec`` which is only the fractional part.
fn mtime_ns(st: &Metadata) -> i64 {
    st.mtime() * 1_000_000_000 + st.mtime_nsec()
}

/// `_scan_dir_local`: local digest terms + subdir list for ONE directory.
/// Returns None on scandir/iteration error (Python propagates → caller
/// rescans); per-entry stat failures hash the name only.
fn scan_dir_local(path: &Path, dir_st: &Metadata) -> Option<Local> {
    let mut local: Vec<u8> = Vec::new();
    local.extend_from_slice(b"\x00d");
    local.extend_from_slice(&mtime_ns(dir_st).to_le_bytes());
    let mut ents = Vec::new();
    for entry in fs::read_dir(path).ok()? {
        let entry = entry.ok()?; // iteration OSError → whole-walk None
        if entry.file_name().as_bytes().first() == Some(&b'.') {
            continue;
        }
        ents.push(entry);
    }
    // Python sorts by the (UTF-8) str name — same as byte order here.
    ents.sort_by(|a, b| a.file_name().as_bytes().cmp(b.file_name().as_bytes()));
    let mut children: Vec<(String, Metadata)> = Vec::new();
    for entry in ents {
        let raw = entry.file_name();
        let nb = raw.as_bytes();
        local.extend_from_slice(b"\x00n");
        local.extend_from_slice(nb);
        let name = std::str::from_utf8(nb).ok()?; // non-UTF-8 → fallback
        let ft = match entry.file_type() {
            Ok(t) => t,
            Err(_) => continue, // d_type probe failed — name only
        };
        let emit_terms = |local: &mut Vec<u8>, name: &str, st: &Metadata| {
            let tag: &[u8] = if name == TOML_NAME {
                b"\x00t"
            } else if suffix_lower(name)
                .map(|s| ARCHIVE_EXT.contains(&s.as_str()) || s == PDF_EXT)
                .unwrap_or(false)
            {
                b"\x00f"
            } else {
                return;
            };
            local.extend_from_slice(tag);
            local.extend_from_slice(&mtime_ns(&st).to_le_bytes());
            local.extend_from_slice(&st.size().to_le_bytes());
        };
        if ft.is_symlink() {
            let st = match fs::metadata(entry.path()) {
                Ok(s) => s,
                Err(_) => continue, // dangling symlink — name hashed only
            };
            if st.is_dir() {
                children.push((name.to_string(), st));
            } else if st.is_file() {
                emit_terms(&mut local, name, &st);
            }
        } else if ft.is_dir() {
            let st = match fs::metadata(entry.path()) {
                Ok(s) => s,
                Err(_) => continue,
            };
            children.push((name.to_string(), st));
        } else if ft.is_file() {
            if name != TOML_NAME
                && !suffix_lower(name)
                    .map(|s| ARCHIVE_EXT.contains(&s.as_str()) || s == PDF_EXT)
                    .unwrap_or(false)
            {
                continue; // plain image etc: name only, zero stat calls
            }
            let st = match fs::metadata(entry.path()) {
                Ok(s) => s,
                Err(_) => continue,
            };
            emit_terms(&mut local, name, &st);
        }
    }
    Some((local, children))
}

fn sig_level(
    level: &[(PathBuf, Metadata)],
) -> Vec<Option<(Vec<u8>, Vec<(String, Metadata)>)>> {
    let n = level.len();
    if n <= 2 {
        return level
            .iter()
            .map(|(p, st)| scan_dir_local(p, st))
            .collect();
    }
    let slots: Vec<std::sync::Mutex<Option<(Vec<u8>, Vec<(String, Metadata)>)>>> =
        (0..n).map(|_| std::sync::Mutex::new(None)).collect();
    let counter = std::sync::atomic::AtomicUsize::new(0);
    std::thread::scope(|s| {
        for _ in 0..SIG_WORKERS.min(n) {
            s.spawn(|| loop {
                let i = counter.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                if i >= n {
                    break;
                }
                *slots[i].lock().unwrap() = scan_dir_local(&level[i].0, &level[i].1);
            });
        }
    });
    slots.into_iter().map(|m| m.into_inner().unwrap()).collect()
}

/// `compute_signature(root)` → sha256 hexdigest; None on any scandir or
/// root-stat `OSError`, or when *root* is missing/not a directory.
pub fn tree_signature(root: &Path) -> Option<String> {
    use sha2::Digest;
    let root_st = fs::metadata(root).ok()?;
    if !root_st.is_dir() {
        return None;
    }
    let mut visited: HashSet<(u64, u64)> = HashSet::new();
    visited.insert((root_st.dev(), root_st.ino()));
    let mut levels: Vec<Vec<(PathBuf, Metadata)>> = vec![vec![(root.to_path_buf(), root_st.clone())]];
    let mut locals: std::collections::HashMap<(u64, u64), Vec<u8>> = Default::default();
    let mut kids: std::collections::HashMap<(u64, u64), Vec<(String, (u64, u64))>> =
        Default::default();
    let mut level_idx = 0;
    while level_idx < levels.len() {
        let level = std::mem::take(&mut levels[level_idx]);
        let results = sig_level(&level);
        let mut next_level: Vec<(PathBuf, Metadata)> = Vec::new();
        for ((path, st), res) in level.iter().zip(results.into_iter()) {
            let (local_terms, children) = res?;
            let ident = (st.dev(), st.ino());
            locals.insert(ident, local_terms);
            let mut ordered: Vec<(String, (u64, u64))> = Vec::new();
            for (child_name, child_st) in children {
                let cident = (child_st.dev(), child_st.ino());
                if visited.contains(&cident) {
                    continue;
                }
                visited.insert(cident);
                ordered.push((child_name.clone(), cident));
                next_level.push((path.join(&child_name), child_st));
            }
            kids.insert(ident, ordered);
        }
        levels[level_idx] = level;
        if !next_level.is_empty() {
            levels.push(next_level);
        }
        level_idx += 1;
    }
    // Bottom-up in reverse BFS order (children combine before parents).
    let mut digests: std::collections::HashMap<(u64, u64), [u8; 32]> = Default::default();
    for level in levels.iter().rev() {
        for (_path, st) in level {
            let ident = (st.dev(), st.ino());
            let mut h = sha2::Sha256::new();
            h.update(&locals[&ident]);
            let mut k = std::mem::take(kids.get_mut(&ident).unwrap());
            k.sort_by(|a, b| a.0.cmp(&b.0));
            for (child_name, cident) in &k {
                h.update(child_name.as_bytes());
                h.update(digests[cident]);
            }
            kids.insert(ident, k);
            digests.insert(ident, h.finalize().into());
        }
    }
    let rid = (root_st.dev(), root_st.ino());
    let d = digests[&rid];
    let mut out = String::with_capacity(64);
    for b in d {
        out.push_str(&format!("{:02x}", b));
    }
    Some(out)
}
