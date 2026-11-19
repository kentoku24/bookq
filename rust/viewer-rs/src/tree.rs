//! `/api/folders` tree + index assembly — R1b of issue #163.
//!
//! Ports `app/scanner.py::scan_tree` (node shapes, single-child flattening,
//! cover fields, archive/PDF virtual folders) plus the metadata attach and
//! subtree facets from `app/routes.py`/`app/metadata`.
//!
//! #322: nodes are a typed model (`Node`/`FolderNode`), NOT `serde_json::Value`.
//! The JSON contract is emitted through borrowed `Serialize` views
//! (`TreeSer`/`EnvSer`), so a full-tree response serializes straight off the
//! cached structure — no per-request DOM deep-clone — and `subtree_facets`
//! lives in a side `FacetMap` looked up by path during serialization instead
//! of a `facets.clone()` embedded into every ancestor folder node.
//! Repeated strings (page names like `001.jpg`, artist/tag values) are
//! interned once per build (`Interner`) and shared via `Arc<str>`.

use std::collections::{HashMap, HashSet};
use std::path::Path;
use std::sync::Arc;

use serde::ser::{SerializeMap, SerializeSeq};
use serde::{Deserialize, Serialize, Serializer};
use serde_json::{Map, Value};

use bookq_core::{self, ComicRec, FsDir};

use crate::serve::{list_archive_entries, suffix_is_epub};

const ARCHIVE_TREE_MAX_DEPTH: usize = 32;

/// Per-build string interner — identical names/values share one `Arc<str>`
/// allocation for the lifetime of the built tree + facet map. Dropped with
/// the build; the `Arc`s it handed out live on inside `CacheData`.
#[derive(Default)]
pub struct Interner {
    map: HashMap<Box<str>, Arc<str>>,
}

impl Interner {
    pub fn intern(&mut self, s: &str) -> Arc<str> {
        if let Some(a) = self.map.get(s) {
            return a.clone();
        }
        let a: Arc<str> = Arc::from(s);
        self.map.insert(Box::from(&*a), a.clone());
        a
    }
}

// ── typed node model ────────────────────────────────────────────────────────

/// `metadata` object for a folder whose ComicRec has `has_metadata`.
/// `folder_path`/`folder_name` are emitted from serialization context
/// (folder_name is stored — a flattened `a/b` node name differs from the
/// record's basename).
pub struct NodeMeta {
    pub folder_name: Box<str>,
    pub title_jp: Box<str>,
    pub title_en: Box<str>,
    pub artist: Vec<Arc<str>>,
    pub tags: Vec<Arc<str>>,
    pub source: Box<str>,
    pub pages: i64,
}

pub struct FolderNode {
    pub name: Arc<str>,
    pub children: Vec<Node>,
    /// `thumb` — emitted as string or null (always present).
    pub thumb: Option<Arc<str>>,
    /// `thumbs` — emitted only when len >= 2 (contract).
    pub thumbs: Vec<Arc<str>>,
    /// `archive: true` — virtual folder backed by zip/cbz/rar/epub (and pdf).
    pub archive: bool,
    /// `pdf: true` — emitted only on pdf nodes.
    pub pdf: bool,
    /// `has_metadata` — always emitted on folders once the index is attached.
    pub has_metadata: bool,
    /// `metadata` — emitted only when the comic record carries metadata.
    pub metadata: Option<Box<NodeMeta>>,
}

pub enum Node {
    Image { name: Arc<str> },
    Folder(Box<FolderNode>),
}

impl Node {
    pub fn name(&self) -> &str {
        match self {
            Node::Image { name } => name,
            Node::Folder(f) => &f.name,
        }
    }
    pub fn as_folder(&self) -> Option<&FolderNode> {
        match self {
            Node::Folder(f) => Some(f),
            _ => None,
        }
    }
    pub fn is_folder(&self) -> bool {
        matches!(self, Node::Folder(_))
    }
    fn as_folder_mut(&mut self) -> Option<&mut FolderNode> {
        match self {
            Node::Folder(f) => Some(f),
            _ => None,
        }
    }
    fn set_name(&mut self, name: Arc<str>) {
        match self {
            Node::Image { name: n } => *n = name,
            Node::Folder(f) => f.name = name,
        }
    }
}

// ── construction (scan_fs → nodes) ───────────────────────────────────────────

fn image_node(name: &str, interner: &mut Interner) -> Node {
    Node::Image {
        name: interner.intern(name),
    }
}

/// `_first_image_of` — first image child, or `"<child>/<child.thumb>"`.
fn first_image_of(children: &[Node]) -> Option<String> {
    for child in children {
        match child {
            Node::Image { name } => return Some(name.to_string()),
            Node::Folder(f) => {
                if let Some(t) = &f.thumb {
                    return Some(format!("{}/{}", f.name, t));
                }
            }
        }
    }
    None
}

/// `folder_cover_fields` — (thumb, thumbs). thumbs kept only when 2+ exist.
fn folder_cover_fields(children: &[Node], interner: &mut Interner) -> (Option<Arc<str>>, Vec<Arc<str>>) {
    let thumb = first_image_of(children).map(|s| interner.intern(&s));
    let mut sub: Vec<Arc<str>> = Vec::new();
    for child in children {
        if let Node::Folder(f) = child {
            if f.thumb.is_some() {
                let joined = format!("{}/{}", f.name, f.thumb.as_ref().unwrap());
                sub.push(interner.intern(&joined));
            }
        }
        if sub.len() >= 9 {
            break;
        }
    }
    (
        thumb,
        if sub.len() >= 2 { sub } else { Vec::new() },
    )
}

fn folder_node(name: &str, children: Vec<Node>, interner: &mut Interner) -> Node {
    let (thumb, thumbs) = folder_cover_fields(&children, interner);
    Node::Folder(Box::new(FolderNode {
        name: interner.intern(name),
        children,
        thumb,
        thumbs,
        archive: false,
        pdf: false,
        has_metadata: false,
        metadata: None,
    }))
}

/// `build_archive_tree` — nested folder tree from finalized entry names.
fn build_archive_tree(entries: &[String], interner: &mut Interner) -> Vec<Node> {
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
    fn build(node: &Dir, interner: &mut Interner) -> Vec<Node> {
        let mut files = node.files.clone();
        files.sort_by(|a, b| bookq_core::natural_cmp(a, b));
        let mut children: Vec<Node> = files.iter().map(|n| image_node(n, interner)).collect();
        let mut dirnames: Vec<&String> = node.dirs.keys().collect();
        dirnames.sort_by(|a, b| bookq_core::natural_cmp(a, b));
        for d in dirnames {
            children.push(folder_node(d, build(&node.dirs[d], interner), interner));
        }
        children
    }
    if entries.is_empty() {
        return Vec::new();
    }
    build(&root, interner)
}

/// `_scan_archive_node` — virtual folder for ZIP/CBZ/RAR/EPUB.
fn archive_node(path: &Path, aname: &str, interner: &mut Interner) -> Node {
    let inner = list_archive_entries(path);
    if suffix_is_epub(aname) {
        let children: Vec<Node> = inner.iter().map(|n| image_node(n, interner)).collect();
        let thumb = inner.first().map(|s| interner.intern(s));
        return Node::Folder(Box::new(FolderNode {
            name: interner.intern(aname),
            children,
            thumb,
            thumbs: Vec::new(),
            archive: true,
            pdf: false,
            has_metadata: false,
            metadata: None,
        }));
    }
    let children = build_archive_tree(&inner, interner);
    let (thumb, thumbs) = folder_cover_fields(&children, interner);
    Node::Folder(Box::new(FolderNode {
        name: interner.intern(aname),
        children,
        thumb,
        thumbs,
        archive: true,
        pdf: false,
        has_metadata: false,
        metadata: None,
    }))
}

/// `_scan_pdf_node` — virtual folder with `p%03d.png` synthetic page names.
fn pdf_node(path: &Path, pname: &str, interner: &mut Interner) -> Node {
    let count = bookq_core::pdf_page_count(path);
    let children: Vec<Node> = (0..count)
        .map(|i| image_node(&format!("p{:03}.png", i + 1), interner))
        .collect();
    let thumb = if count > 0 {
        Some(interner.intern("p001.png"))
    } else {
        None
    };
    Node::Folder(Box::new(FolderNode {
        name: interner.intern(pname),
        children,
        thumb,
        thumbs: Vec::new(),
        archive: true,
        pdf: true,
        has_metadata: false,
        metadata: None,
    }))
}

/// `scan_tree` — assemble nodes from the `FsDir` skeleton.
/// `None` means the FS walk failed (scan_fs error → caller proxies upstream).
pub fn scan_tree(base: &Path, interner: &mut Interner) -> Option<Vec<Node>> {
    let fs = bookq_core::scan_fs(base)?;
    Some(assemble(&fs, base, interner))
}

fn assemble(dir: &FsDir, base: &Path, interner: &mut Interner) -> Vec<Node> {
    let mut entries: Vec<Node> = Vec::new();
    for f in &dir.files {
        entries.push(image_node(f, interner));
    }
    for a in &dir.archives {
        entries.push(archive_node(&base.join(a), a, interner));
    }
    for p in &dir.pdfs {
        entries.push(pdf_node(&base.join(p), p, interner));
    }
    for (dname, sub) in &dir.subdirs {
        let mut child = assemble(sub, &base.join(dname), interner);
        if dir.files.is_empty()
            && dir.archives.is_empty()
            && dir.pdfs.is_empty()
            && dir.subdirs.len() == 1
        {
            for c in child.iter_mut() {
                let flattened = format!("{dname}/{}", c.name());
                c.set_name(interner.intern(&flattened));
            }
            entries.extend(child);
        } else {
            entries.push(folder_node(dname, child, interner));
        }
    }
    entries
}

// ── metadata attach (routes.py _attach_metadata) ────────────────────────────

/// `ComicMetadata.to_dict()` — `asdict` order: folder_path, folder_name,
/// title_jp, title_en, artist, tags, source, has_metadata, total_pages.
/// Search rows still serialize through this `Value` form (per-request, small).
pub fn comic_to_json(c: &ComicRec) -> Value {
    let (path, name, tj, te, artist, tags, source, has, pages) = c;
    let mut m = Map::new();
    for (k, v) in [
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
    ] {
        m.insert(k.to_string(), v);
    }
    Value::Object(m)
}

/// `_attach_metadata` — set has_metadata on every folder node and embed the
/// comic record (typed `NodeMeta`) where the record carries metadata.
pub fn attach_metadata(
    nodes: &mut [Node],
    lookup: &HashMap<String, ComicRec>,
    parent: &str,
    interner: &mut Interner,
) {
    for node in nodes.iter_mut() {
        let Some(f) = node.as_folder_mut() else {
            continue;
        };
        let path = if parent.is_empty() {
            f.name.to_string()
        } else {
            format!("{parent}/{}", f.name)
        };
        match lookup.get(&path) {
            Some(comic) if comic.7 => {
                f.has_metadata = true;
                f.metadata = Some(Box::new(NodeMeta {
                    folder_name: Box::from(comic.1.as_str()),
                    title_jp: Box::from(comic.2.as_str()),
                    title_en: Box::from(comic.3.as_str()),
                    artist: comic.4.iter().map(|s| interner.intern(s)).collect(),
                    tags: comic.5.iter().map(|s| interner.intern(s)).collect(),
                    source: Box::from(comic.6.as_str()),
                    pages: comic.8,
                }));
            }
            _ => f.has_metadata = false,
        }
        attach_metadata(&mut f.children, lookup, &path, interner);
    }
}

// ── subtree facets — side map, looked up by path at serialize time (#322) ───

/// Sorted facet sets for one folder path — `Arc<str>` strings shared with
/// the interned metadata so each distinct artist/tag is allocated once.
pub struct FacetSet {
    pub artists: Box<[Arc<str>]>,
    pub tags: Box<[Arc<str>]>,
}
pub type FacetMap = HashMap<Box<str>, FacetSet>;

impl FacetSet {
    fn is_empty(&self) -> bool {
        self.artists.is_empty() && self.tags.is_empty()
    }
}

/// `_subtree_facets` — path → {artists,tags} aggregated over descendant comics.
/// Interned: the same artist/tag string is one allocation no matter how many
/// ancestor folders list it.
pub fn subtree_facets(comics: &[ComicRec], interner: &mut Interner) -> FacetMap {
    let mut agg: HashMap<Box<str>, (HashSet<Arc<str>>, HashSet<Arc<str>>)> = HashMap::new();
    for c in comics {
        let mut prefix = String::new();
        for part in c.0.split('/') {
            if !prefix.is_empty() {
                prefix.push('/');
            }
            prefix.push_str(part);
            let a = agg.entry(Box::from(prefix.as_str())).or_default();
            for s in &c.4 {
                a.0.insert(interner.intern(s));
            }
            for s in &c.5 {
                a.1.insert(interner.intern(s));
            }
        }
    }
    agg.into_iter()
        .map(|(path, (artists, tags))| {
            let mut a: Vec<Arc<str>> = artists.into_iter().collect();
            a.sort_by(|x, y| x.to_lowercase().cmp(&y.to_lowercase()).then(x.cmp(y)));
            let mut t: Vec<Arc<str>> = tags.into_iter().collect();
            t.sort_by(|x, y| x.to_lowercase().cmp(&y.to_lowercase()).then(x.cmp(y)));
            (
                path,
                FacetSet {
                    artists: a.into_boxed_slice(),
                    tags: t.into_boxed_slice(),
                },
            )
        })
        .collect()
}

// ── borrowed serialization views (zero-clone emit) ──────────────────────────

/// Emits a node list as a JSON array. `shallow` = the `shallow_from_tree`
/// contract: folder children only, each with `children: []` and all other
/// fields intact. `parent` is the path prefix for facet lookup ("" at root).
pub struct TreeSer<'a> {
    pub nodes: &'a [Node],
    pub facets: &'a FacetMap,
    pub parent: &'a str,
    pub shallow: bool,
}

impl Serialize for TreeSer<'_> {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        let mut seq = s.serialize_seq(None)?;
        for n in self.nodes {
            if self.shallow && !n.is_folder() {
                continue;
            }
            seq.serialize_element(&NodeSer {
                node: n,
                facets: self.facets,
                parent: self.parent,
                shallow: self.shallow,
            })?;
        }
        seq.end()
    }
}

struct NodeSer<'a> {
    node: &'a Node,
    facets: &'a FacetMap,
    parent: &'a str,
    shallow: bool,
}

impl Serialize for NodeSer<'_> {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        match self.node {
            Node::Image { name } => {
                let mut m = s.serialize_map(Some(3))?;
                m.serialize_entry("name", name)?;
                m.serialize_entry("type", "image")?;
                m.serialize_entry("children", &Value::Null)?;
                m.end()
            }
            Node::Folder(f) => {
                let path = if self.parent.is_empty() {
                    f.name.to_string()
                } else {
                    format!("{}/{}", self.parent, f.name)
                };
                let facets = self
                    .facets
                    .get(path.as_str())
                    .filter(|x| !x.is_empty());
                let mut m = s.serialize_map(None)?;
                m.serialize_entry("name", &f.name)?;
                m.serialize_entry("type", "folder")?;
                if self.shallow {
                    m.serialize_entry("children", &EmptySeq)?;
                } else {
                    m.serialize_entry(
                        "children",
                        &TreeSer {
                            nodes: &f.children,
                            facets: self.facets,
                            parent: &path,
                            shallow: false,
                        },
                    )?;
                }
                m.serialize_entry("thumb", &f.thumb)?;
                if f.thumbs.len() >= 2 {
                    m.serialize_entry("thumbs", &f.thumbs)?;
                }
                if f.archive {
                    m.serialize_entry("archive", &true)?;
                }
                if f.pdf {
                    m.serialize_entry("pdf", &true)?;
                }
                m.serialize_entry("has_metadata", &f.has_metadata)?;
                if let Some(meta) = &f.metadata {
                    m.serialize_entry(
                        "metadata",
                        &MetaSer {
                            meta,
                            path: &path,
                        },
                    )?;
                }
                if let Some(fac) = facets {
                    m.serialize_entry("subtree_facets", &FacetSer(fac))?;
                }
                m.end()
            }
        }
    }
}

struct EmptySeq;
impl Serialize for EmptySeq {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_seq(Some(0))?.end()
    }
}

/// `{folder_path, folder_name, title_jp, title_en, artist, tags, source,
/// has_metadata, total_pages}` — `comic_to_json`'s key order.
struct MetaSer<'a> {
    meta: &'a NodeMeta,
    path: &'a str,
}
impl Serialize for MetaSer<'_> {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        let mut m = s.serialize_map(Some(9))?;
        m.serialize_entry("folder_path", self.path)?;
        m.serialize_entry("folder_name", &self.meta.folder_name)?;
        m.serialize_entry("title_jp", &self.meta.title_jp)?;
        m.serialize_entry("title_en", &self.meta.title_en)?;
        m.serialize_entry("artist", &self.meta.artist)?;
        m.serialize_entry("tags", &self.meta.tags)?;
        m.serialize_entry("source", &self.meta.source)?;
        m.serialize_entry("has_metadata", &true)?;
        m.serialize_entry("total_pages", &self.meta.pages)?;
        m.end()
    }
}

struct FacetSer<'a>(&'a FacetSet);
impl Serialize for FacetSer<'_> {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        let mut m = s.serialize_map(Some(2))?;
        m.serialize_entry("artists", &self.0.artists)?;
        m.serialize_entry("tags", &self.0.tags)?;
        m.end()
    }
}

/// `{kind, nodes, generation}` — the v1 envelope (order preserved).
pub struct EnvSer<'a, T: Serialize + ?Sized> {
    pub kind: &'a str,
    pub nodes: &'a T,
    pub generation: i64,
}
impl<T: Serialize + ?Sized> Serialize for EnvSer<'_, T> {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        let mut m = s.serialize_map(Some(3))?;
        m.serialize_entry("kind", self.kind)?;
        m.serialize_entry("nodes", self.nodes)?;
        m.serialize_entry("generation", &self.generation)?;
        m.end()
    }
}

// ── lookup helpers ──────────────────────────────────────────────────────────

/// `_find_tree_node` — locate a node by its full path (never splits
/// flattened names on "/").
pub fn find_tree_node<'a>(nodes: &'a [Node], target: &str, parent_path: &str) -> Option<&'a Node> {
    for node in nodes {
        let full = if parent_path.is_empty() {
            node.name().to_string()
        } else {
            format!("{parent_path}/{}", node.name())
        };
        if full == target {
            return Some(node);
        }
        if let Node::Folder(f) = node {
            if target.starts_with(&format!("{full}/")) {
                if let Some(found) = find_tree_node(&f.children, target, &full) {
                    return Some(found);
                }
            }
        }
    }
    None
}

/// `MetadataIndex.rebuild_from_tree` — parse every folder node's own path
/// plus each ancestor prefix of a flattened name (``"a/b/c"`` → ``"a"``,
/// ``"a/b"``, ``"a/b/c"``), on folder nodes AND on image-node dir prefixes
/// (the image filename itself is never parsed). Deduped by first-seen
/// prefix in DFS order — each folder is parsed at most once even when
/// nested nodes repeat an ancestor. Paths under archive nodes are phantom
/// FS paths by design (``x.rar/InnerDir`` parses to a minimal record).
pub fn rebuild_index_from_tree(nodes: &[Node], base: &Path) -> Vec<ComicRec> {
    fn visit(nodes: &[Node], parent: &str, seen: &mut HashSet<String>, folders: &mut Vec<String>) {
        for node in nodes {
            let rel = if parent.is_empty() {
                node.name().to_string()
            } else {
                format!("{parent}/{}", node.name())
            };
            let parts: Vec<&str> = rel.split('/').collect();
            match node {
                Node::Folder(f) => {
                    for d in 1..=parts.len() {
                        let p = parts[..d].join("/");
                        if seen.insert(p.clone()) {
                            folders.push(p);
                        }
                    }
                    visit(&f.children, &rel, seen, folders);
                }
                Node::Image { .. } => {
                    for d in 1..parts.len() {
                        let p = parts[..d].join("/");
                        if seen.insert(p.clone()) {
                            folders.push(p);
                        }
                    }
                }
            }
        }
    }
    let mut seen = HashSet::new();
    let mut folders = Vec::new();
    visit(nodes, "", &mut seen, &mut folders);
    // `_parse_folder` swallows per-folder errors → None; a batch-level
    // Poison (unprovable) falls back to the empty index, matching the
    // caller's `unwrap_or_default` posture.
    bookq_core::parse_metadata_batch(base, &folders)
        .map(|v| v.into_iter().flatten().collect())
        .unwrap_or_default()
}

// ── Live-FS shallow listing (no index required) ───────────────────────────────

/// The live-FS fallback branch of `api_folders` (depth mode, cold cache):
/// one iterdir level, suffix-classified, archives → pdfs → dirs ordering.
/// `None` → target is not a readable dir. Per-request small — stays `Value`.
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
    let shallow = |pairs: Vec<(&str, Value)>| {
        let mut m = Map::new();
        for (k, v) in pairs {
            m.insert(k.to_string(), v);
        }
        Value::Object(m)
    };
    for n in archives {
        out.push(shallow(vec![
            ("name", Value::String(n)),
            ("type", Value::String("folder".into())),
            ("children", Value::Array(vec![])),
            ("thumb", Value::Null),
            ("archive", Value::Bool(true)),
        ]));
    }
    for n in pdfs {
        out.push(shallow(vec![
            ("name", Value::String(n)),
            ("type", Value::String("folder".into())),
            ("children", Value::Array(vec![])),
            ("thumb", Value::Null),
            ("archive", Value::Bool(true)),
            ("pdf", Value::Bool(true)),
        ]));
    }
    for n in dirs {
        out.push(shallow(vec![
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

/// Count archive+pdf nodes — the steady-state fill generation (1 + count).
pub fn archive_node_count(nodes: &[Node]) -> i64 {
    let mut n = 0i64;
    let mut stack: Vec<&Node> = nodes.iter().collect();
    while let Some(node) = stack.pop() {
        if let Node::Folder(f) = node {
            if f.archive {
                n += 1;
            }
            stack.extend(f.children.iter());
        }
    }
    n
}

// ── snapshot load (folders.rs index-snapshot.json) ──────────────────────────

/// Deserialize mirror of the serialized node shape. `subtree_facets` (older
/// snapshots) is ignored — the side map is rebuilt from `comics` on load.
#[derive(Deserialize)]
pub struct NodeDe {
    pub name: String,
    #[serde(rename = "type")]
    pub ty: String,
    pub children: Option<Vec<NodeDe>>,
    pub thumb: Option<String>,
    pub thumbs: Option<Vec<String>>,
    pub archive: Option<bool>,
    pub pdf: Option<bool>,
    pub has_metadata: Option<bool>,
    pub metadata: Option<MetaDe>,
}

#[derive(Deserialize)]
pub struct MetaDe {
    pub folder_name: String,
    pub title_jp: String,
    pub title_en: String,
    pub artist: Vec<String>,
    pub tags: Vec<String>,
    pub source: String,
    pub total_pages: i64,
}

impl NodeDe {
    pub fn into_node(self, interner: &mut Interner) -> Node {
        let name = interner.intern(&self.name);
        if self.ty == "folder" {
            let children: Vec<Node> = self
                .children
                .unwrap_or_default()
                .into_iter()
                .map(|c| c.into_node(interner))
                .collect();
            Node::Folder(Box::new(FolderNode {
                name,
                children,
                thumb: self.thumb.map(|t| interner.intern(&t)),
                thumbs: self
                    .thumbs
                    .unwrap_or_default()
                    .iter()
                    .map(|t| interner.intern(t))
                    .collect(),
                archive: self.archive.unwrap_or(false),
                pdf: self.pdf.unwrap_or(false),
                has_metadata: self.has_metadata.unwrap_or(false),
                metadata: self.metadata.map(|m| {
                    Box::new(NodeMeta {
                        folder_name: Box::from(m.folder_name.as_str()),
                        title_jp: Box::from(m.title_jp.as_str()),
                        title_en: Box::from(m.title_en.as_str()),
                        artist: m.artist.iter().map(|s| interner.intern(s)).collect(),
                        tags: m.tags.iter().map(|s| interner.intern(s)).collect(),
                        source: Box::from(m.source.as_str()),
                        pages: m.total_pages,
                    })
                }),
            }))
        } else {
            Node::Image { name }
        }
    }
}
