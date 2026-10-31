//! W4 (issue #163): `MetadataIndex` read-path ports.
//!
//! `meta_walk` is `_walk`'s whole job — natural-sorted DFS collecting
//! `ComicMetadata` records (dirs parsed via metadata.toml, archives/PDFs
//! indexed by name). `parse_metadata_batch` covers `rebuild_from_tree`'s
//! per-prefix `ComicMetadata.from_folder` calls in one FFI crossing.
//!
//! Per-folder results: `None` = folder skipped (mirrors `_parse_folder`'s
//! None — an error Python swallows). A `Poison` condition (data whose Python
//! semantics Rust cannot reproduce cheaply — e.g. non-str scalar fields, or
//! non-ASCII digits inside an `int()` string) makes the WHOLE call return
//! None so the wrapper runs the reference code — never a partial divergence.

use std::collections::HashSet;
use std::fs;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::MetadataExt;
use std::path::Path;

use crate::scans::natural_cmp;

/// Tuple layout == `ComicMetadata` field order.
pub type ComicRec = (
    String,      // folder_path
    String,      // folder_name
    String,      // title_jp
    String,      // title_en
    Vec<String>, // artist
    Vec<String>, // tags
    String,      // source
    bool,        // has_metadata
    i64,         // total_pages
);

const WALK_SUFFIXES: [&str; 6] = [".zip", ".cbz", ".rar", ".cbr", ".epub", ".pdf"];
const TAG_KEYS: [&str; 6] = ["male", "female", "mixed", "other", "tag", "misc"];

enum Folder {
    Rec(ComicRec),
    Skip,
    Poison,
}

enum IntRes {
    V(i64),
    Fail,
    Poison,
}

fn suffix_lower(name: &str) -> Option<String> {
    name.rfind('.').map(|i| name[i..].to_lowercase())
}

/// `int(x)` on a toml value. `Fail` = Python's int() also raises (folder
/// skipped); `Poison` = can't prove (non-ASCII digit strings, which Python
/// accepts via Unicode decimal digits, or i64 overflow where Python's
/// bignum int() still succeeds).
fn toml_int(v: &toml::Value) -> IntRes {
    match v {
        toml::Value::Integer(i) => IntRes::V(*i),
        toml::Value::Float(f) => IntRes::V(*f as i64),
        toml::Value::Boolean(b) => IntRes::V(*b as i64),
        toml::Value::String(s) => {
            let t = s.trim();
            let (sign, body) = match t.strip_prefix('-') {
                Some(b) => (-1i64, b),
                None => (1, t.strip_prefix('+').unwrap_or(t)),
            };
            if body.is_empty() {
                return IntRes::Fail;
            }
            if body.bytes().any(|b| !b.is_ascii()) {
                return IntRes::Poison;
            }
            // PEP 515: single underscores between digits only.
            let bytes = body.as_bytes();
            let mut digits = String::with_capacity(bytes.len());
            for (i, &c) in bytes.iter().enumerate() {
                if c == b'_' {
                    if i == 0
                        || i + 1 == bytes.len()
                        || !bytes[i - 1].is_ascii_digit()
                        || !bytes[i + 1].is_ascii_digit()
                    {
                        return IntRes::Fail;
                    }
                } else if c.is_ascii_digit() {
                    digits.push(c as char);
                } else {
                    return IntRes::Fail;
                }
            }
            match digits.parse::<i64>() {
                Ok(v) => IntRes::V(sign * v),
                Err(_) => IntRes::Poison, // bignum — Python int() succeeds
            }
        }
        _ => IntRes::Fail, // table/array/datetime → int() raises
    }
}

/// `_split_aliases`: ' | '-separated entries → trimmed non-empty parts.
fn split_aliases(raw: &[String]) -> Vec<String> {
    let mut out = Vec::new();
    for entry in raw {
        for part in entry.split(" | ") {
            let p = part.trim();
            if !p.is_empty() {
                out.push(p.to_string());
            }
        }
    }
    out
}

/// `_flatten_tags`: ordered category concat, per-entry comma split,
/// dedupe preserving first-seen order. Non-list category values are
/// skipped by Python's isinstance guard; non-str members likewise.
fn flatten_tags(tags: &toml::Table) -> Vec<String> {
    let mut result: Vec<String> = Vec::new();
    for key in TAG_KEYS {
        if let Some(toml::Value::Array(entries)) = tags.get(key) {
            for e in entries {
                if let toml::Value::String(s) = e {
                    for part in s.split(',') {
                        let p = part.trim();
                        if !p.is_empty() {
                            result.push(p.to_string());
                        }
                    }
                }
            }
        }
    }
    let mut seen: HashSet<&str> = HashSet::new();
    result
        .iter()
        .filter(|s| seen.insert(s.as_str()))
        .cloned()
        .collect()
}

fn minimal(rel: String, name: String) -> ComicRec {
    (rel, name, String::new(), String::new(), vec![], vec![], String::new(), false, 0)
}

/// `ComicMetadata.from_folder` → Rec; `relative_to` failure or an
/// int()-raising `total_pages` → Skip (Python `_parse_folder` returns
/// None); unprovable → Poison.
fn parse_one(folder: &Path, base: &Path) -> Folder {
    let rel_p = match folder.strip_prefix(base) {
        Ok(r) => r,
        Err(_) => return Folder::Skip,
    };
    let rel_b = rel_p.as_os_str().as_bytes();
    let rel = if rel_b.is_empty() {
        ".".to_string() // str(Path(x).relative_to(x)) == '.'
    } else {
        match std::str::from_utf8(rel_b) {
            Ok(s) => s.to_string(),
            Err(_) => return Folder::Poison,
        }
    };
    let name = match folder.file_name() {
        Some(n) => match std::str::from_utf8(n.as_bytes()) {
            Ok(s) => s.to_string(),
            Err(_) => return Folder::Poison,
        },
        None => String::new(), // Path('/').name == ''
    };
    let mf = folder.join("metadata.toml");
    if fs::metadata(&mf).is_err() {
        return Folder::Rec(minimal(rel, name));
    }
    let data: toml::Table = match fs::read(&mf)
        .ok()
        .and_then(|b| String::from_utf8(b).ok())
        .and_then(|s| s.parse().ok())
    {
        Some(t) => t,
        None => return Folder::Rec(minimal(rel, name)), // parse error → minimal
    };
    let str_field = |tbl: Option<&toml::Value>, key: &str| -> Result<String, ()> {
        match tbl.and_then(|v| v.as_table()).and_then(|t| t.get(key)) {
            None => Ok(String::new()),
            Some(toml::Value::String(s)) => Ok(s.clone()),
            Some(_) => Err(()), // non-str scalar → Python keeps raw value → Poison
        }
    };
    let comic_v = data.get("comic");
    let tags_v = data.get("tags");
    let title_jp = match str_field(comic_v, "title_jp") {
        Ok(s) => s,
        Err(_) => return Folder::Poison,
    };
    let title_en = match str_field(comic_v, "title_en") {
        Ok(s) => s,
        Err(_) => return Folder::Poison,
    };
    let source = match str_field(data.get("source"), "name") {
        Ok(s) => s,
        Err(_) => return Folder::Poison,
    };
    // artist: list → _split_aliases; dict → Python iterates its KEYS;
    // other non-iterable → TypeError → folder skipped.
    let artist: Vec<String> = match tags_v
        .and_then(|v| v.as_table())
        .and_then(|t| t.get("artist"))
    {
        None => vec![],
        Some(toml::Value::Array(items)) => {
            if items.iter().any(|i| !matches!(i, toml::Value::String(_))) {
                return Folder::Skip; // entry.split() on non-str raises
            }
            split_aliases(
                &items
                    .iter()
                    .map(|i| i.as_str().unwrap().to_string())
                    .collect::<Vec<_>>(),
            )
        }
        Some(toml::Value::Table(t)) => {
            split_aliases(&t.keys().cloned().collect::<Vec<_>>())
        }
        Some(_) => return Folder::Skip,
    };
    let tags: Vec<String> = match tags_v {
        Some(toml::Value::Table(t)) => flatten_tags(t),
        _ => vec![],
    };
    let total_pages = match comic_v
        .and_then(|v| v.as_table())
        .and_then(|t| t.get("total_pages"))
    {
        None => 0,
        Some(v) => match toml_int(v) {
            IntRes::V(i) => i,
            IntRes::Fail => return Folder::Skip,
            IntRes::Poison => return Folder::Poison,
        },
    };
    Folder::Rec((
        rel,
        name,
        title_jp,
        title_en,
        artist,
        tags,
        source,
        true,
        total_pages,
    ))
}

/// `MetadataIndex._walk` whole-call: DFS in `(natural_key, name)` order;
/// dirs parsed via metadata.toml, archive/PDF files indexed as minimal
/// records. None → fallback.
pub fn meta_walk(base: &Path) -> Option<Vec<ComicRec>> {
    if !base.is_dir() {
        return Some(vec![]); // Python: warning + empty index
    }
    let mut visited = HashSet::new();
    if let Ok(st) = fs::metadata(base) {
        visited.insert((st.dev(), st.ino()));
    }
    let mut out = Vec::new();
    match walk_dir(base, base, &mut visited, &mut out) {
        Some(Folder::Poison) => None,
        _ => Some(out),
    }
}

fn walk_dir(
    dir: &Path,
    base: &Path,
    visited: &mut HashSet<(u64, u64)>,
    out: &mut Vec<ComicRec>,
) -> Option<Folder> {
    let mut ents = Vec::new();
    for entry in fs::read_dir(dir).ok()? {
        let entry = entry.ok()?; // iterdir OSError → Python raises → fallback
        if entry.file_name().as_bytes().first() == Some(&b'.') {
            continue;
        }
        let name = match std::str::from_utf8(entry.file_name().as_bytes()) {
            Ok(s) => s.to_string(),
            Err(_) => return Some(Folder::Poison),
        };
        ents.push((name, entry));
    }
    ents.sort_by(|a, b| natural_cmp(&a.0, &b.0));
    for (name, entry) in ents {
        let ft = entry.file_type().ok();
        // NOTE: `DirEntry::metadata()` is lstat; Python's stat follows —
        // use `fs::metadata(path)` wherever Python calls `entry.stat()`.
        let is_dir = match ft {
            Some(t) if t.is_symlink() => {
                fs::metadata(entry.path()).map(|m| m.is_dir()).unwrap_or(false)
            }
            Some(t) => t.is_dir(),
            None => false,
        };
        let is_file = match ft {
            Some(t) if t.is_symlink() => {
                fs::metadata(entry.path()).map(|m| m.is_file()).unwrap_or(false)
            }
            Some(t) => t.is_file(),
            None => false,
        };
        if !is_dir {
            // archive/PDF indexed as minimal comic by name
            if is_file
                && suffix_lower(&name)
                    .map(|s| WALK_SUFFIXES.contains(&s.as_str()))
                    .unwrap_or(false)
            {
                let path = entry.path();
                if let Some(rel) = path
                    .strip_prefix(base)
                    .ok()
                    .and_then(|r| std::str::from_utf8(r.as_os_str().as_bytes()).ok())
                {
                    out.push(minimal(rel.to_string(), name));
                } else {
                    return Some(Folder::Poison);
                }
            }
            continue;
        }
        let st = match fs::metadata(entry.path()) {
            Ok(m) => m,
            Err(_) => continue, // broken symlink / unreadable — skip
        };
        if !visited.insert((st.dev(), st.ino())) {
            continue;
        }
        match parse_one(&entry.path(), base) {
            Folder::Rec(r) => out.push(r),
            Folder::Skip => {}
            Folder::Poison => return Some(Folder::Poison),
        }
        // recursion proceeds even when the folder's own record was skipped
        walk_dir(&entry.path(), base, visited, out)?;
    }
    Some(Folder::Skip)
}

/// `rebuild_from_tree`'s parse step: one FFI for all candidate folders.
/// `folders` are RELATIVE paths under `base`; outer None → Python does
/// the whole batch (Poison anywhere).
pub fn parse_metadata_batch(base: &Path, folders: &[String]) -> Option<Vec<Option<ComicRec>>> {
    let mut out = Vec::with_capacity(folders.len());
    for rel in folders {
        match parse_one(&base.join(rel), base) {
            Folder::Rec(r) => out.push(Some(r)),
            Folder::Skip => out.push(None),
            Folder::Poison => return None,
        }
    }
    Some(out)
}
