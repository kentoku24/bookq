//! `/api/folders` tree + index assembly — R1b of issue #163.
//!
//! Ports `app/scanner.py::scan_tree` (node shapes, single-child flattening,
//! cover fields, archive/PDF virtual folders) plus the metadata attach and
//! subtree facets from `app/routes.py`/`app/metadata`. JSON objects are built
//! with serde_json's preserve_order so serialized bytes match Python's
//! insertion-ordered dicts exactly.

use std::collections::{HashMap, HashSet};
use std::path::Path;

use serde_json::{Map, Value};

use bookq_core::{self, ComicRec, FsDir};

use crate::serve::{list_archive_entries, suffix_is_epub};

const ARCHIVE_TREE_MAX_DEPTH: usize = 32;

fn obj(pairs: Vec<(&str, Value)>) -> Value {
    let mut m = Map::new();
    for (k, v) in pairs {
        m.insert(k.to_string(), v);
    }
    Value::Object(m)
}

fn image_node(name: &str) -> Value {
    obj(vec![
        ("name", Value::String(name.to_string())),
        ("type", Value::String("image".into())),
        ("children", Value::Null),
    ])
}

/// `_first_image_of` — first image child, or `"<child>/<child.thumb>"`.
fn first_image_of(children: &[Value]) -> Option<String> {
    for child in children {
        if child["type"] == "image" {
            return child["name"].as_str().map(|s| s.to_string());
        }
        if let Some(t) = child["thumb"].as_str() {
            return Some(format!("{}/{}", child["name"].as_str().unwrap_or(""), t));
        }
    }
    None
}

/// `folder_cover_fields` — (thumb, thumbs|None). thumbs only when 2+ exist.
fn folder_cover_fields(children: &[Value]) -> (Value, Value) {
    let thumb = first_image_of(children);
    let mut sub: Vec<String> = Vec::new();
    for child in children {
        if child["type"] == "folder" && child["thumb"].is_string() {
            sub.push(format!(
                "{}/{}",
                child["name"].as_str().unwrap_or(""),
                child["thumb"].as_str().unwrap_or("")
            ));
        }
        if sub.len() >= 9 {
            break;
        }
    }
    (
        thumb.map(Value::String).unwrap_or(Value::Null),
        if sub.len() >= 2 {
            Value::Array(sub.into_iter().map(Value::String).collect())
        } else {
            Value::Null
        },
    )
}

fn folder_node(name: &str, children: Vec<Value>) -> Value {
    let (thumb, thumbs) = folder_cover_fields(&children);
    let mut pairs = vec![
        ("name", Value::String(name.to_string())),
        ("type", Value::String("folder".into())),
        ("children", Value::Array(children)),
        ("thumb", thumb),
    ];
    if thumbs.is_array() {
        pairs.push(("thumbs", thumbs));
    }
    obj(pairs)
}

/// `build_archive_tree` — nested folder tree from finalized entry names.
fn build_archive_tree(entries: &[String]) -> Vec<Value> {
    #[derive(Default)]
    struct Dir {
        files: Vec<String>,
        file_set: HashSet<String>,
        dirs: HashMap<String, Dir>,
    }
    let mut root = Dir::default();
    for name in entries {
        if name.is_empty() || name.ends_with('/') {
            continue;
        }
        let parts: Vec<&str> = name.split('/').collect();
        if parts.iter().any(|p| p.is_empty() || *p == "." || *p == "..") {
            continue;
        }
        if parts.len() - 1 > ARCHIVE_TREE_MAX_DEPTH {
            let tail = parts[ARCHIVE_TREE_MAX_DEPTH..].join("/");
            let mut p: Vec<String> = parts[..ARCHIVE_TREE_MAX_DEPTH]
                .iter()
                .map(|s| s.to_string())
                .collect();
            p.push(tail);
            let refs: Vec<&str> = p.iter().map(|s| s.as_str()).collect();
            insert(&mut root, &refs);
            continue;
        }
        insert(&mut root, &parts);
    }
    fn insert(root: &mut Dir, parts: &[&str]) {
        let mut node = root;
        for part in &parts[..parts.len() - 1] {
            node = node.dirs.entry(part.to_string()).or_default();
        }
        let leaf = parts[parts.len() - 1];
        if !leaf.is_empty() && node.file_set.insert(leaf.to_string()) {
            node.files.push(leaf.to_string());
        }
    }
    fn build(node: &Dir) -> Vec<Value> {
        let mut files = node.files.clone();
        files.sort_by(|a, b| bookq_core::natural_cmp(a, b));
        let mut children: Vec<Value> = files.iter().map(|n| image_node(n)).collect();
        let mut dirnames: Vec<&String> = node.dirs.keys().collect();
        dirnames.sort_by(|a, b| bookq_core::natural_cmp(a, b));
        for d in dirnames {
            children.push(folder_node(d, build(&node.dirs[d])));
        }
        children
    }
    if entries.is_empty() {
        return Vec::new();
    }
    build(&root)
}

/// `_scan_archive_node` — virtual folder for ZIP/CBZ/RAR/EPUB.
fn archive_node(path: &Path, aname: &str) -> Value {
    let inner = list_archive_entries(path);
    if suffix_is_epub(aname) {
        let children: Vec<Value> = inner.iter().map(|n| image_node(n)).collect();
        let thumb = inner
            .first()
            .map(|s| Value::String(s.clone()))
            .unwrap_or(Value::Null);
        return obj(vec![
            ("name", Value::String(aname.to_string())),
            ("type", Value::String("folder".into())),
            ("children", Value::Array(children)),
            ("thumb", thumb),
            ("archive", Value::Bool(true)),
        ]);
    }
    let children = build_archive_tree(&inner);
    let (thumb, thumbs) = folder_cover_fields(&children);
    let mut pairs = vec![
        ("name", Value::String(aname.to_string())),
        ("type", Value::String("folder".into())),
        ("children", Value::Array(children)),
        ("thumb", thumb),
        ("archive", Value::Bool(true)),
    ];
    if thumbs.is_array() {
        pairs.push(("thumbs", thumbs));
    }
    obj(pairs)
}

/// `_scan_pdf_node` — virtual folder with `p%03d.png` synthetic page names.
fn pdf_node(path: &Path, pname: &str) -> Value {
    let count = bookq_core::pdf_page_count(path);
    let children: Vec<Value> = (0..count)
        .map(|i| image_node(&format!("p{:03}.png", i + 1)))
        .collect();
    let thumb = if count > 0 {
        Value::String("p001.png".into())
    } else {
        Value::Null
    };
    obj(vec![
        ("name", Value::String(pname.to_string())),
        ("type", Value::String("folder".into())),
        ("children", Value::Array(children)),
        ("thumb", thumb),
        ("archive", Value::Bool(true)),
        ("pdf", Value::Bool(true)),
    ])
}

/// `scan_tree` — assemble nodes from the `FsDir` skeleton.
/// `None` means the FS walk failed (scan_fs error → caller proxies upstream).
pub fn scan_tree(base: &Path) -> Option<Vec<Value>> {
    let fs = bookq_core::scan_fs(base)?;
    Some(assemble(&fs, base))
}

fn assemble(dir: &FsDir, base: &Path) -> Vec<Value> {
    let mut entries: Vec<Value> = Vec::new();
    for f in &dir.files {
        entries.push(image_node(f));
    }
    for a in &dir.archives {
        entries.push(archive_node(&base.join(a), a));
    }
    for p in &dir.pdfs {
        entries.push(pdf_node(&base.join(p), p));
    }
    for (dname, sub) in &dir.subdirs {
        let mut child = assemble(sub, &base.join(dname));
        if dir.files.is_empty()
            && dir.archives.is_empty()
            && dir.pdfs.is_empty()
            && dir.subdirs.len() == 1
        {
            for c in child.iter_mut() {
                if let Some(n) = c["name"].as_str() {
                    c["name"] = Value::String(format!("{dname}/{n}"));
                }
            }
            entries.extend(child);
        } else {
            entries.push(folder_node(dname, child));
        }
    }
    entries
}

// ── metadata attach (routes.py _attach_metadata / _attach_subtree_facets) ────

/// `ComicMetadata.to_dict()` — `asdict` order: folder_path, folder_name,
/// title_jp, title_en, artist, tags, source, has_metadata, total_pages.
fn comic_to_json(c: &ComicRec) -> Value {
    let (path, name, tj, te, artist, tags, source, has, pages) = c;
    obj(vec![
        ("folder_path", Value::String(path.clone())),
        ("folder_name", Value::String(name.clone())),
        ("title_jp", Value::String(tj.clone())),
        ("title_en", Value::String(te.clone())),
        (
            "artist",
            Value::Array(artist.iter().map(|s| Value::String(s.clone())).collect()),
        ),
        (
            "tags",
            Value::Array(tags.iter().map(|s| Value::String(s.clone())).collect()),
        ),
        ("source", Value::String(source.clone())),
        ("has_metadata", Value::Bool(*has)),
        ("total_pages", Value::from(*pages)),
    ])
}

pub fn attach_metadata(nodes: &mut [Value], lookup: &HashMap<String, ComicRec>, parent: &str) {
    for node in nodes.iter_mut() {
        if node["type"] != "folder" {
            continue;
        }
        let name = node["name"].as_str().unwrap_or("");
        let path = if parent.is_empty() {
            name.to_string()
        } else {
            format!("{parent}/{name}")
        };
        if let Some(comic) = lookup.get(&path) {
            if comic.7 {
                node["has_metadata"] = Value::Bool(true);
                node["metadata"] = comic_to_json(comic);
            } else {
                node["has_metadata"] = Value::Bool(false);
            }
        } else {
            node["has_metadata"] = Value::Bool(false);
        }
        if let Some(children) = node.get_mut("children").and_then(|c| c.as_array_mut()) {
            attach_metadata(children, lookup, &path);
        }
    }
}

pub fn attach_subtree_facets(
    nodes: &mut [Value],
    subtree: &HashMap<String, Value>,
    parent: &str,
) {
    for node in nodes.iter_mut() {
        if node["type"] != "folder" {
            continue;
        }
        let name = node["name"].as_str().unwrap_or("");
        let path = if parent.is_empty() {
            name.to_string()
        } else {
            format!("{parent}/{name}")
        };
        if let Some(facets) = subtree.get(&path) {
            // Python attaches only when any(facets.values()) is truthy.
            if facets
                .as_object()
                .is_some_and(|m| m.values().any(|v| v.as_array().is_some_and(|a| !a.is_empty())))
            {
                node["subtree_facets"] = facets.clone();
            }
        }
        if let Some(children) = node.get_mut("children").and_then(|c| c.as_array_mut()) {
            attach_subtree_facets(children, subtree, &path);
        }
    }
}

/// `_subtree_facets` — path → {artists,tags} aggregated over descendant comics.
pub fn subtree_facets(comics: &[ComicRec]) -> HashMap<String, Value> {
    let mut agg: HashMap<String, (HashSet<String>, HashSet<String>)> = HashMap::new();
    for c in comics {
        let mut prefix = String::new();
        for part in c.0.split('/') {
            if !prefix.is_empty() {
                prefix.push('/');
            }
            prefix.push_str(part);
            let a = agg.entry(prefix.clone()).or_default();
            for s in &c.4 {
                a.0.insert(s.clone());
            }
            for s in &c.5 {
                a.1.insert(s.clone());
            }
        }
    }
    agg.into_iter()
        .map(|(path, (artists, tags))| {
            let mut a: Vec<String> = artists.into_iter().collect();
            a.sort_by(|x, y| x.to_lowercase().cmp(&y.to_lowercase()).then(x.cmp(y)));
            let mut t: Vec<String> = tags.into_iter().collect();
            t.sort_by(|x, y| x.to_lowercase().cmp(&y.to_lowercase()).then(x.cmp(y)));
            (
                path,
                obj(vec![
                    (
                        "artists",
                        Value::Array(a.into_iter().map(Value::String).collect()),
                    ),
                    ("tags", Value::Array(t.into_iter().map(Value::String).collect())),
                ]),
            )
        })
        .collect()
}

/// `_find_tree_node` — locate a folder node by its full path (never splits
/// flattened names on "/").
pub fn find_tree_node<'a>(nodes: &'a [Value], target: &str, parent_path: &str) -> Option<&'a Value> {
    for node in nodes {
        let name = node["name"].as_str().unwrap_or("");
        let full = if parent_path.is_empty() {
            name.to_string()
        } else {
            format!("{parent_path}/{name}")
        };
        if full == target {
            return Some(node);
        }
        if let Some(children) = node["children"].as_array() {
            if target.starts_with(&format!("{full}/")) {
                if let Some(found) = find_tree_node(children, target, &full) {
                    return Some(found);
                }
            }
        }
    }
    None
}

// ── Live-FS shallow listing (no index required) ───────────────────────────────

/// The live-FS fallback branch of `api_folders` (depth mode, cold cache):
/// one iterdir level, suffix-classified, archives → pdfs → dirs ordering.
/// `None` → target is not a readable dir.
pub fn shallow_live(target: &Path) -> Option<Vec<Value>> {
    let rd = std::fs::read_dir(target).ok()?;
    let (mut archives, mut pdfs, mut dirs) = (Vec::new(), Vec::new(), Vec::new());
    for entry in rd.flatten() {
        let name = match entry.file_name().to_str() {
            Some(s) => s.to_string(),
            None => continue, // non-UTF-8: Python includes it; keep skip → proxy risk noted
        };
        if name.starts_with('.') {
            continue;
        }
        // entry.metadata() follows symlinks — matches Path.is_dir()/is_file().
        let md = match entry.metadata() {
            Ok(m) => m,
            Err(_) => continue,
        };
        if md.is_dir() {
            dirs.push(name);
        } else if md.is_file() {
            match suffix_lower(&name).as_str() {
                s if crate::serve::ARCHIVE_EXTENSIONS.contains(&s) => archives.push(name),
                s if crate::serve::PDF_EXTENSIONS.contains(&s) => pdfs.push(name),
                _ => {}
            }
        }
    }
    archives.sort_by(|a, b| bookq_core::natural_cmp(a, b));
    pdfs.sort_by(|a, b| bookq_core::natural_cmp(a, b));
    dirs.sort_by(|a, b| bookq_core::natural_cmp(a, b));
    let mut out = Vec::new();
    for n in archives {
        out.push(obj(vec![
            ("name", Value::String(n)),
            ("type", Value::String("folder".into())),
            ("children", Value::Array(vec![])),
            ("thumb", Value::Null),
            ("archive", Value::Bool(true)),
        ]));
    }
    for n in pdfs {
        out.push(obj(vec![
            ("name", Value::String(n)),
            ("type", Value::String("folder".into())),
            ("children", Value::Array(vec![])),
            ("thumb", Value::Null),
            ("archive", Value::Bool(true)),
            ("pdf", Value::Bool(true)),
        ]));
    }
    for n in dirs {
        out.push(obj(vec![
            ("name", Value::String(n)),
            ("type", Value::String("folder".into())),
            ("children", Value::Array(vec![])),
        ]));
    }
    Some(out)
}

fn suffix_lower(name: &str) -> String {
    Path::new(name)
        .extension()
        .and_then(|e| e.to_str())
        .map(|e| format!(".{}", e.to_lowercase()))
        .unwrap_or_default()
}

/// Shallow copies of the cached tree's folder children (`{**c, "children": []}`).
pub fn shallow_from_tree(children: &[Value]) -> Vec<Value> {
    children
        .iter()
        .filter(|c| c["type"] == "folder")
        .map(|c| {
            let mut m = c.as_object().cloned().unwrap_or_default();
            m.insert("children".to_string(), Value::Array(vec![]));
            Value::Object(m)
        })
        .collect()
}

/// Count archive+pdf nodes — the steady-state fill generation (1 + count).
pub fn archive_node_count(nodes: &[Value]) -> i64 {
    let mut n = 0i64;
    let mut stack: Vec<&Value> = nodes.iter().collect();
    while let Some(node) = stack.pop() {
        if node["type"] == "folder" {
            if node["archive"].as_bool() == Some(true) {
                n += 1;
            }
            if let Some(ch) = node["children"].as_array() {
                stack.extend(ch.iter());
            }
        }
    }
    n
}

