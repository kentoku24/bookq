//! dual-reader shared work manifest — port of `app/reader_sync/catalog.py`.
//!
//! Enumeration is the SAME code path the solo viewer uses: the cached
//! scan_tree behind IndexState and the shared /api/images enumeration
//! (`_list_folder_images`). PageRef = {folder, name} — folder is the
//! downloads-relative serving path, name the entry name verbatim.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

use super::{model, protocol};
use crate::serve;
use crate::tree;

// ── error codes (typed, distinguishable) ──────────────────────────────────

pub const ERR_MALFORMED: &str = "ERR_MALFORMED";
pub const ERR_RANGE: &str = "ERR_RANGE";
pub const ERR_SIZE: &str = "ERR_SIZE";
pub const ERR_NO_INITIAL_PAGE: &str = "ERR_NO_INITIAL_PAGE";
pub const ERR_PAGE_NOT_FOUND: &str = "ERR_PAGE_NOT_FOUND";
pub const ERR_EMPTY_WORK: &str = "ERR_EMPTY_WORK";
pub const ERR_FORBIDDEN: &str = "ERR_FORBIDDEN";
pub const ERR_NOT_FOUND: &str = "ERR_NOT_FOUND";
pub const ERR_BROKEN_ARCHIVE: &str = "ERR_BROKEN_ARCHIVE";
pub const ERR_TIMEOUT: &str = "ERR_TIMEOUT";

#[derive(Debug)]
pub struct ManifestError {
    pub code: &'static str,
    pub message: String,
}
impl std::fmt::Display for ManifestError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code, self.message)
    }
}
impl std::error::Error for ManifestError {}

fn merr<T>(code: &'static str, msg: impl Into<String>) -> Result<T, ManifestError> {
    Err(ManifestError {
        code,
        message: msg.into(),
    })
}

/// Same constant as app.js SIBLING_AUTOLOAD_MIN_DEPTH.
const SIBLING_MIN_DEPTH: usize = 2;

pub const MANIFEST_CHUNK_BYTES: usize = 30 * 1024;

const MANIFEST_KIND: &str = "manifest-chunk";

/// Digest covers work content only — initial/digest itself excluded.
const CONTENT_KEYS: &[&str] = &[
    "v",
    "folder",
    "offset",
    "chapters",
    "pageRefs",
    "spreadTable",
];

// ── tree predicates ────────────────────────────────────────────────────────

pub(crate) fn has_only_images(node: &tree::FolderNode) -> bool {
    !node.children.is_empty()
        && node
            .children
            .iter()
            .all(|c| matches!(c, tree::Node::Image { .. }))
}

pub(crate) fn has_images(node: &tree::FolderNode) -> bool {
    node.children
        .iter()
        .any(|c| matches!(c, tree::Node::Image { .. }))
}

fn compose(parent: &str, name: &str) -> String {
    if parent.is_empty() {
        name.to_string()
    } else {
        format!("{parent}/{name}")
    }
}

/// Collapse empty and "." segments — one canonical spelling per folder.
fn norm_path(path: &str) -> String {
    path.split('/')
        .filter(|p| !p.is_empty() && *p != ".")
        .collect::<Vec<_>>()
        .join("/")
}

/// `(siblings_list, parent_path)` whose list holds `target` — structural
/// parent lookup mirroring `findParentOf` (names are never split on "/").
pub(crate) fn find_container<'a>(
    nodes: &'a [tree::Node],
    target: &str,
    path: &str,
) -> Option<(&'a [tree::Node], String)> {
    for node in nodes {
        let full = compose(path, node.name());
        if full == target {
            return Some((nodes, path.to_string()));
        }
        if let tree::Node::Folder(f) = node {
            if target.starts_with(&format!("{full}/")) {
                if let Some(hit) = find_container(&f.children, target, &full) {
                    return Some(hit);
                }
            }
        }
    }
    None
}

/// Port of `getSiblingLeafFolders`: ordered chapter-folder full paths
/// when the sibling run applies, else None (single chapter).
pub(crate) fn sibling_leaf_folders(tree: &[tree::Node], folder: &str) -> Option<Vec<String>> {
    if folder.matches('/').count() < SIBLING_MIN_DEPTH {
        return None;
    }
    let (siblings, parent_path) = find_container(tree, folder, "")?;
    if parent_path.is_empty() {
        return None; // root-level node — no parent run
    }
    let leaves: Vec<String> = siblings
        .iter()
        .filter_map(|n| n.as_folder())
        .filter(|f| has_only_images(f))
        .map(|f| compose(&parent_path, &f.name))
        .collect();
    if leaves.len() <= 1 {
        return None;
    }
    Some(leaves)
}

// ── shared enumeration bridge ──────────────────────────────────────────────

/// The archive/PDF file behind `folder` — the file itself, or the longest
/// ancestor that is one (inner-prefix paths).
fn archive_source(downloads: &Path, folder: &str) -> Option<PathBuf> {
    let file_path = downloads.join(folder);
    if serve::is_archive_file(&file_path) || serve::is_pdf_file(&file_path) {
        return Some(file_path);
    }
    serve::resolve_archive(downloads, folder).map(|(arc, _)| arc)
}

/// Cheap "is this really its claimed container type" check — distinguishes
/// BROKEN (container unrecognized) from EMPTY (valid container, no images).
fn container_intact(path: &Path) -> bool {
    match serve::suffix_lower(path).as_str() {
        ".rar" | ".cbr" => {
            // rarfile.is_rarfile — _RAR_ID is the 6-byte "Rar!\x1a\x07";
            // RAR4 (\x07\x00) and RAR5 (\x07\x01\x00) both start with it.
            std::fs::File::open(path)
                .and_then(|mut f| {
                    use std::io::Read;
                    let mut b = [0u8; 6];
                    f.read_exact(&mut b).map(|_| b)
                })
                .map(|b| &b[..] == b"Rar!\x1a\x07")
                .unwrap_or(false)
        }
        ".pdf" => std::fs::File::open(path)
            .and_then(|mut f| {
                use std::io::Read;
                let mut b = [0u8; 5];
                f.read_exact(&mut b).map(|_| b)
            })
            .map(|b| &b[..] == b"%PDF-")
            .unwrap_or(false),
        _ => is_zipfile(path),
    }
}

/// `zipfile.is_zipfile` — EndRecData search for the EOCD signature within
/// the last 64KiB+22 of the file.
fn is_zipfile(path: &Path) -> bool {
    use std::io::{Read, Seek, SeekFrom};
    let mut f = match std::fs::File::open(path) {
        Ok(f) => f,
        Err(_) => return false,
    };
    let size = f.metadata().map(|m| m.len()).unwrap_or(0);
    let window = size.min(65536 + 22);
    if f.seek(SeekFrom::End(-(window as i64))).is_err() {
        return false;
    }
    let mut buf = Vec::with_capacity(window as usize);
    if f.take(window).read_to_end(&mut buf).is_err() {
        return false;
    }
    // concat=0 may appear — search all occurrences like EndRecData does.
    let sig = b"PK\x05\x06";
    buf.windows(sig.len()).rev().any(|w| w == sig)
}

/// Result of the enumeration core — Ok names, or the HTTPException
/// (status, detail) Python would raise.
enum ListErr {
    Http(u16, String),
    Io,
}

/// `_list_folder_images` — the shared /api/images enumeration core.
fn list_folder_images(downloads: &Path, folder: &str) -> Result<Vec<String>, ListErr> {
    let folder_path = downloads.join(folder);
    if !serve::is_within(downloads, &folder_path) {
        return Err(ListErr::Http(403, "Forbidden".into()));
    }
    // Non-strict resolve — missing components pass through.
    let resolved = serve::realpath(&folder_path).unwrap_or_else(|_| folder_path.clone());
    if serve::is_archive_file(&resolved) {
        return Ok(serve::list_archive_entries(&resolved));
    }
    if serve::is_pdf_file(&resolved) {
        let count = bookq_core::pdf_page_count(&resolved);
        return Ok((0..count).map(|i| format!("p{:03}.png", i + 1)).collect());
    }
    if let Some((archive, inner)) = serve::resolve_archive(downloads, folder) {
        if !serve::is_within(downloads, &archive) {
            return Err(ListErr::Http(403, "Forbidden".into()));
        }
        let archive_res = serve::realpath(&archive).unwrap_or(archive);
        let scoped = serve::list_archive_prefix(&archive_res, &inner);
        if scoped.is_empty() {
            return Err(ListErr::Http(404, format!("Folder not found: {folder}")));
        }
        return Ok(scoped);
    }
    if !resolved.is_dir() {
        return Err(ListErr::Http(404, format!("Folder not found: {folder}")));
    }
    match bookq_core::list_images(&resolved) {
        Some(v) => Ok(v),
        None => Err(ListErr::Io),
    }
}

/// Page names for one chapter — identical precedence to loadImages:
/// tree leaf children, then mixed-folder direct images, then enumeration.
fn chapter_pages(
    downloads: &Path,
    tree: &[tree::Node],
    folder: &str,
) -> Result<Vec<String>, ManifestError> {
    let node = if folder.is_empty() {
        None
    } else {
        tree::find_tree_node(tree, folder, "").and_then(|n| n.as_folder())
    };
    if let Some(f) = node {
        if has_only_images(f) {
            return Ok(f
                .children
                .iter()
                .map(|c| c.name().to_string())
                .collect());
        }
        if has_images(f) {
            return Ok(f
                .children
                .iter()
                .filter(|c| matches!(c, tree::Node::Image { .. }))
                .map(|c| c.name().to_string())
                .collect());
        }
    }
    match list_folder_images(downloads, folder) {
        Ok(v) => Ok(v),
        Err(ListErr::Http(403, _)) => merr(ERR_FORBIDDEN, "Forbidden"),
        Err(ListErr::Http(404, detail)) => {
            // A path under (or pointing at) an unreadable archive reports
            // broken, not "not found".
            match archive_source(downloads, folder) {
                Some(arc) if !container_intact(&arc) => merr(
                    ERR_BROKEN_ARCHIVE,
                    format!("unreadable archive for {folder:?}"),
                ),
                _ => merr(ERR_NOT_FOUND, detail),
            }
        }
        Err(ListErr::Http(_, detail)) => merr(ERR_MALFORMED, detail),
        // (OSError, ValueError) during enumeration → FORBIDDEN in py.
        Err(ListErr::Io) => merr(ERR_FORBIDDEN, format!("enumeration failed: {folder:?}")),
    }
}

// ── manifest build (sync — runs off the executor) ──────────────────────────

/// Resolve `folder` into the shared manifest (content fields + digest).
pub fn build_manifest_core(
    downloads: &Path,
    tree: &[tree::Node],
    folder: &str,
    offset: i64,
) -> Result<Value, ManifestError> {
    if !serve::is_within(downloads, &downloads.join(folder)) {
        return merr(
            ERR_FORBIDDEN,
            format!("path outside downloads dir: {folder:?}"),
        );
    }
    let node = if folder.is_empty() {
        None
    } else {
        tree::find_tree_node(tree, folder, "").and_then(|n| n.as_folder())
    };
    let is_mixed = node.is_some_and(|f| has_images(f) && !has_only_images(f));
    let siblings = sibling_leaf_folders(tree, folder);

    let (chapter_folders, in_tree_names) = match &siblings {
        Some(s) if s.len() > 1 && !is_mixed => (s.clone(), true),
        _ => (vec![folder.to_string()], false),
    };

    let mut chapters = Vec::new();
    let mut page_refs = Vec::new();
    for cf in &chapter_folders {
        let names = chapter_pages(downloads, tree, cf)?;
        let cnode = tree::find_tree_node(tree, cf, "");
        let cname = if in_tree_names && cnode.is_some() {
            cnode.map(|n| n.name().to_string()).unwrap_or_default()
        } else {
            cf.rsplit('/').next().unwrap_or("").to_string()
        };
        let pages: Vec<Value> = names
            .into_iter()
            .map(|n| {
                let mut m = Map::new();
                m.insert("folder".into(), Value::String(cf.clone()));
                m.insert("name".into(), Value::String(n));
                Value::Object(m)
            })
            .collect();
        let mut ch = Map::new();
        ch.insert("name".into(), Value::String(cname));
        ch.insert("folder".into(), Value::String(cf.clone()));
        ch.insert("pages".into(), Value::Array(pages.clone()));
        chapters.push(Value::Object(ch));
        page_refs.extend(pages);
    }

    if page_refs.is_empty() {
        match archive_source(downloads, folder) {
            Some(arc) if !container_intact(&arc) => {
                return merr(
                    ERR_BROKEN_ARCHIVE,
                    format!("unreadable archive for {folder:?}"),
                );
            }
            _ => return merr(ERR_EMPTY_WORK, format!("no pages in work: {folder:?}")),
        }
    }

    // S01 builds the shared spread table — chapters never share a spread.
    let plan = model::build_spread_plan(&chapters, offset).map_err(|e| ManifestError {
        code: e.code,
        message: e.message,
    })?;
    let spread_table: Vec<Value> = plan.iter().map(model::serialize_spread).collect();

    let mut core = Map::new();
    core.insert("v".into(), Value::from(protocol::PROTOCOL_VERSION));
    core.insert("folder".into(), Value::String(folder.to_string()));
    core.insert("offset".into(), Value::from(offset));
    core.insert("chapters".into(), Value::Array(chapters));
    core.insert("pageRefs".into(), Value::Array(page_refs));
    core.insert("spreadTable".into(), Value::Array(spread_table));
    let mut core = Value::Object(core);
    let digest = manifest_digest(&core)?;
    core.as_object_mut()
        .unwrap()
        .insert("digest".into(), Value::String(digest));
    Ok(core)
}

// ── digest / canonical bytes / chunking ────────────────────────────────────

/// `json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",",":"))`.
/// ASCII-only so byte offsets equal char offsets — chunk slices can never
/// split a multibyte character.
pub fn canonical(obj: &Value) -> String {
    let mut out = String::new();
    write_canonical(obj, &mut out);
    out
}

fn write_canonical(obj: &Value, out: &mut String) {
    match obj {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => out.push_str(&n.to_string()),
        Value::String(s) => write_ascii_str(s, out),
        Value::Array(a) => {
            out.push('[');
            for (i, v) in a.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_canonical(v, out);
            }
            out.push(']');
        }
        Value::Object(o) => {
            let mut keys: Vec<&String> = o.keys().collect();
            keys.sort();
            out.push('{');
            for (i, k) in keys.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_ascii_str(k, out);
                out.push(':');
                write_canonical(&o[*k], out);
            }
            out.push('}');
        }
    }
}

/// Python `ensure_ascii` string escaping: shorthand controls, \u00xx C0,
/// \uXXXX BMP, surrogate pairs above U+FFFF.
fn write_ascii_str(s: &str, out: &mut String) {
    out.push('"');
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0C}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => {
                out.push_str(&format!("\\u{:04x}", c as u32));
            }
            c if (c as u32) < 0x7f => out.push(c),
            c if (c as u32) <= 0xffff => {
                out.push_str(&format!("\\u{:04x}", c as u32));
            }
            c => {
                let u = c as u32 - 0x10000;
                out.push_str(&format!(
                    "\\u{:04x}\\u{:04x}",
                    0xd800 + (u >> 10),
                    0xdc00 + (u & 0x3ff)
                ));
            }
        }
    }
    out.push('"');
}

/// sha256 over the canonical content fields.
pub fn manifest_digest(manifest: &Value) -> Result<String, ManifestError> {
    let mut content = Map::new();
    for k in CONTENT_KEYS {
        match manifest.get(*k) {
            Some(v) => {
                content.insert((*k).to_string(), v.clone());
            }
            None => return merr(ERR_MALFORMED, format!("manifest missing content key: {k}")),
        }
    }
    let data = canonical(&Value::Object(content));
    Ok(format!("sha256:{:x}", Sha256::digest(data.as_bytes())))
}

/// Canonical wire bytes of the full manifest dict.
#[allow(dead_code)]
pub fn encode_manifest(manifest: &Value) -> Vec<u8> {
    canonical(manifest).into_bytes()
}

/// Split the canonical manifest bytes into bounded chunk dicts.
pub fn split_manifest(manifest: &Value) -> Result<Vec<Value>, ManifestError> {
    let digest = match manifest.get("digest").and_then(|v| v.as_str()) {
        Some(d) => d.to_string(),
        None => return merr(ERR_MALFORMED, "manifest digest missing or stale"),
    };
    if manifest_digest(manifest)? != digest {
        return merr(ERR_MALFORMED, "manifest digest missing or stale");
    }
    let data = canonical(manifest);
    if data.len() > protocol::MAX_MANIFEST_BYTES {
        return merr(
            ERR_SIZE,
            format!("manifest exceeds {} bytes", protocol::MAX_MANIFEST_BYTES),
        );
    }
    let max_bytes = MANIFEST_CHUNK_BYTES;
    let bytes = data.as_bytes();
    let slices: Vec<&str> = if bytes.is_empty() {
        vec![""]
    } else {
        (0..bytes.len())
            .step_by(max_bytes)
            .map(|i| &data[i..(i + max_bytes).min(bytes.len())])
            .collect()
    };
    let count = slices.len();
    let mut chunks = Vec::with_capacity(count);
    for (i, piece) in slices.into_iter().enumerate() {
        let chunk = protocol::frame(&[
            ("v", Value::from(protocol::PROTOCOL_VERSION)),
            ("kind", Value::String(MANIFEST_KIND.into())),
            ("digest", Value::String(digest.clone())),
            ("index", Value::from(i as i64)),
            ("count", Value::from(count as i64)),
            ("data", Value::String(piece.to_string())),
        ]);
        protocol::encode_frame(&chunk).map_err(|_| ManifestError {
            code: ERR_SIZE,
            message: "chunk exceeds the frame cap".into(),
        })?;
        chunks.push(chunk);
    }
    Ok(chunks)
}

// ── per-tree-generation memo (reader_manifest_cache) ───────────────────────

/// `app.state.reader_manifest_cache` — resolved manifests memoized per
/// tree generation. Holds the CacheData Arc itself (py `cache["tree"] is
/// tree` keeps the tree object referenced): a bare ptr could collide with
/// a reallocated cache (ABA) and serve a stale manifest.
pub struct ManifestCache {
    tree: Option<std::sync::Arc<crate::folders::CacheData>>,
    pub(crate) items: HashMap<(String, i64), Value>,
}

impl ManifestCache {
    pub fn new() -> Self {
        Self {
            tree: None,
            items: HashMap::new(),
        }
    }

    /// Reset the items when `cache` is a different CacheData identity.
    pub fn reset_for(&mut self, cache: &std::sync::Arc<crate::folders::CacheData>) {
        let same = self
            .tree
            .as_ref()
            .is_some_and(|t| std::sync::Arc::ptr_eq(t, cache));
        if !same {
            self.tree = Some(cache.clone());
            self.items.clear();
        }
    }

    /// Returns the cached manifest core for (folder, offset) or builds it.
    #[allow(dead_code)]
    pub fn get_or_build<F>(
        &mut self,
        cache: &std::sync::Arc<crate::folders::CacheData>,
        key: (String, i64),
        build: F,
    ) -> Result<Value, ManifestError>
    where
        F: FnOnce() -> Result<Value, ManifestError>,
    {
        self.reset_for(cache);
        match self.items.get(&key) {
            Some(v) => Ok(v.clone()),
            None => {
                let v = build()?;
                self.items.insert(key, v.clone());
                Ok(v)
            }
        }
    }
}

// ── public entry point ─────────────────────────────────────────────────────

/// Validate the resolve args — mirror `resolve_work`'s preamble. Returns
/// the normalized `(folder, PageRef)`.
pub fn validate_args(
    opened_folder: &Value,
    initial_page_ref: &Value,
    offset: &Value,
) -> Result<(String, model::PageRef), ManifestError> {
    let folder_raw = match opened_folder.as_str() {
        Some(s) => s,
        None => return merr(ERR_MALFORMED, "openedFolder must be str"),
    };
    let folder = norm_path(folder_raw);
    if folder.split('/').any(|p| p == "..") {
        return merr(ERR_FORBIDDEN, format!("path segment '..': {folder_raw:?}"));
    }
    // isinstance(offset,bool) → ERR_RANGE: bools passed the sequenced gate
    // under Python's True==1 semantics and are rejected here instead.
    // `offset not in (0,1)` is Python `==`: 0.0/1.0 pass like ints.
    if offset.is_boolean() || !offset.as_f64().is_some_and(|f| f == 0.0 || f == 1.0) {
        return merr(ERR_RANGE, format!("offset must be 0 or 1, got {offset}"));
    }
    if initial_page_ref.is_null() {
        return merr(ERR_NO_INITIAL_PAGE, "initialPageRef is required");
    }
    let f = initial_page_ref.get("folder").and_then(|v| v.as_str());
    let n = initial_page_ref.get("name").and_then(|v| v.as_str());
    match (initial_page_ref.is_object(), f, n) {
        (true, Some(f), Some(n)) => Ok((
            folder,
            model::PageRef {
                folder: norm_path(f),
                name: n.to_string(),
            },
        )),
        (true, _, _) => merr(ERR_MALFORMED, "initialPageRef needs {folder:str,name:str}"),
        _ => merr(ERR_MALFORMED, "initialPageRef must be a PageRef dict"),
    }
}

/// Finish a resolved core: pin `initial.page` + locate its spreadId.
/// A ref absent from the work is ERR_PAGE_NOT_FOUND — never a fallback.
pub fn finish_resolve(mut core: Value, refer: &model::PageRef) -> Result<Value, ManifestError> {
    let mut refd = Map::new();
    refd.insert("folder".into(), Value::String(refer.folder.clone()));
    refd.insert("name".into(), Value::String(refer.name.clone()));
    let refd = Value::Object(refd);
    let page_refs = core
        .get("pageRefs")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    if !page_refs.contains(&refd) {
        return merr(
            ERR_PAGE_NOT_FOUND,
            format!("initial page not in work: {refd:?}"),
        );
    }
    let mut spread_id = Value::Null;
    if let Some(table) = core.get("spreadTable").and_then(|v| v.as_array()) {
        for s in table {
            if s.get("right") == Some(&refd) || s.get("left") == Some(&refd) {
                spread_id = s.get("id").cloned().unwrap_or(Value::Null);
                break;
            }
        }
    }
    let mut initial = Map::new();
    initial.insert("page".into(), refd);
    initial.insert("spreadId".into(), spread_id);
    core.as_object_mut()
        .unwrap()
        .insert("initial".into(), Value::Object(initial));
    Ok(core)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn canonical_sorts_keys_and_escapes_ascii() {
        let mut m = Map::new();
        m.insert("b".into(), Value::from(1));
        m.insert("a".into(), Value::String("x\u{4e2d}\n".into()));
        m.insert(
            "z".into(),
            Value::Array(vec![Value::Null, Value::Bool(true)]),
        );
        // sorted keys, ensure_ascii escapes, separators=",",":"
        assert_eq!(
            canonical(&Value::Object(m)),
            "{\"a\":\"x\\u4e2d\\n\",\"b\":1,\"z\":[null,true]}"
        );
        // surrogate pair for non-BMP
        let mut m = Map::new();
        m.insert("k".into(), Value::String("\u{1F600}".into()));
        assert_eq!(canonical(&Value::Object(m)), "{\"k\":\"\\ud83d\\ude00\"}");
    }

    #[test]
    fn digest_and_chunking() {
        let mut m = Map::new();
        m.insert("v".into(), Value::from(1));
        m.insert("folder".into(), Value::String("f".into()));
        m.insert("offset".into(), Value::from(0));
        m.insert("chapters".into(), Value::Array(vec![]));
        m.insert(
            "pageRefs".into(),
            Value::Array(vec![Value::String("p".into())]),
        );
        m.insert("spreadTable".into(), Value::Array(vec![]));
        let mut manifest = Value::Object(m);
        let digest = manifest_digest(&manifest).unwrap();
        assert!(digest.starts_with("sha256:") && digest.len() == 7 + 64);
        manifest
            .as_object_mut()
            .unwrap()
            .insert("digest".into(), Value::String(digest.clone()));
        let chunks = split_manifest(&manifest).unwrap();
        assert_eq!(chunks.len(), 1);
        assert_eq!(chunks[0]["kind"], "manifest-chunk");
        assert_eq!(chunks[0]["digest"], digest);
        assert_eq!(chunks[0]["index"], 0);
        assert_eq!(chunks[0]["count"], 1);
        // tampered digest is rejected
        manifest
            .as_object_mut()
            .unwrap()
            .insert("digest".into(), Value::String("sha256:00".into()));
        assert_eq!(split_manifest(&manifest).unwrap_err().code, ERR_MALFORMED);
    }
}
