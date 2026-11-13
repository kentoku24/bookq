//! `api/vectors/*.json` conformance (#280, #459) — the same cases the iOS
//! (`ConformanceVectorsTests.swift`) and Python suites run, executed
//! against the Rust ports so the implementations cannot drift.
//!
//! `positions-cases.json` is exercised separately by
//! `state.rs::position_book_conformance_cases`; `solo-spreads.json` is
//! client-layout logic (js/swift only) and has no Rust port.

#[cfg(test)]
mod tests {
    use crate::reader_sync::catalog;
    use crate::{thumbs, tree};
    use serde_json::{json, Value};

    const WORK_CHAPTERS: &str = include_str!("../../../api/vectors/work-chapters.json");
    const PATH_LOOKUP: &str = include_str!("../../../api/vectors/path-lookup.json");
    const ENCODE_PATH: &str = include_str!("../../../api/vectors/encode-path.json");

    fn build_tree(case: &Value) -> Vec<tree::Node> {
        let nodes_de: Vec<tree::NodeDe> = serde_json::from_value(case["tree"].clone())
            .unwrap_or_else(|e| panic!("{}: bad tree: {e}", case["name"]));
        let mut interner = tree::Interner::default();
        nodes_de
            .into_iter()
            .map(|n| n.into_node(&mut interner))
            .collect()
    }

    // ── work-chapters ───────────────────────────────────────────────────
    // Mirror of the Python harness `_resolve_chapters`: the filesystem
    // fallback behind archives/PDFs is out of vector scope — such cases pin
    // the selection only and expect `pages: []`.

    fn chapter_pages(tree: &[tree::Node], cf: &str) -> Vec<String> {
        let node = if cf.is_empty() {
            None
        } else {
            tree::find_tree_node(tree, cf, "").and_then(|n| n.as_folder())
        };
        if let Some(f) = node {
            if catalog::has_only_images(f) {
                return f.children.iter().map(|c| c.name().to_string()).collect();
            }
            if catalog::has_images(f) {
                return f
                    .children
                    .iter()
                    .filter(|c| matches!(c, tree::Node::Image { .. }))
                    .map(|c| c.name().to_string())
                    .collect();
            }
        }
        Vec::new()
    }

    fn resolve_chapters(tree: &[tree::Node], folder: &str) -> (Vec<String>, Vec<Value>) {
        let node = tree::find_tree_node(tree, folder, "").and_then(|n| n.as_folder());
        let is_mixed =
            node.is_some_and(|f| catalog::has_images(f) && !catalog::has_only_images(f));
        let siblings = catalog::sibling_leaf_folders(tree, folder);
        let (chapter_folders, in_tree_names) = match &siblings {
            Some(s) if s.len() > 1 && !is_mixed => (s.clone(), true),
            _ => (vec![folder.to_string()], false),
        };
        let chapters = chapter_folders
            .iter()
            .map(|cf| {
                let names = chapter_pages(tree, cf);
                let cnode = tree::find_tree_node(tree, cf, "").and_then(|n| n.as_folder());
                let cname = match (in_tree_names, cnode) {
                    (true, Some(f)) => f.name.to_string(),
                    _ => cf.rsplit('/').next().unwrap_or("").to_string(),
                };
                serde_json::json!({"name": cname, "folder": cf, "pages": names})
            })
            .collect();
        (chapter_folders, chapters)
    }

    #[test]
    fn work_chapters_vectors() {
        let data: Value = serde_json::from_str(WORK_CHAPTERS).unwrap();
        assert_eq!(data["format"], 1);
        assert!(data["languages"].as_array().unwrap().contains(&json!("rust")));
        for case in data["cases"].as_array().unwrap() {
            let name = case["name"].as_str().unwrap();
            let tree = build_tree(case);
            let (folders, chapters) = resolve_chapters(&tree, case["folder"].as_str().unwrap());
            assert_eq!(
                folders.len() > 1,
                case["expectCross"].as_bool().unwrap(),
                "{name} expectCross"
            );
            assert_eq!(
                serde_json::json!(folders),
                case["expectChapterFolders"],
                "{name} folders"
            );
            assert_eq!(
                serde_json::json!(chapters),
                case["expectChapters"],
                "{name} chapters"
            );
        }
    }

    // ── path-lookup ─────────────────────────────────────────────────────

    #[test]
    fn path_lookup_vectors() {
        let data: Value = serde_json::from_str(PATH_LOOKUP).unwrap();
        assert_eq!(data["format"], 1);
        for case in data["cases"].as_array().unwrap() {
            let name = case["name"].as_str().unwrap();
            let tree = build_tree(case);
            let target = case["target"].as_str().unwrap_or("");
            let node = if target.is_empty() {
                None
            } else {
                tree::find_tree_node(&tree, target, "")
            };
            let got_node = node.map(|n| {
                serde_json::json!({
                    "name": n.name().to_string(),
                    "type": match n {
                        tree::Node::Folder(_) => "folder",
                        _ => "image",
                    },
                })
            });
            assert_eq!(
                got_node.unwrap_or(Value::Null),
                case["expectNode"],
                "{name} expectNode"
            );
            let found = if target.is_empty() {
                None
            } else {
                catalog::find_container(&tree, target, "")
            };
            let got_parent = found.and_then(|(_, pp)| {
                if pp.is_empty() {
                    None
                } else {
                    Some(Value::String(pp))
                }
            });
            assert_eq!(
                got_parent.unwrap_or(Value::Null),
                case["expectParent"],
                "{name} expectParent"
            );
        }
    }

    // ── encode-path ─────────────────────────────────────────────────────
    // Clients encode per-segment; the server must decode back to the
    // original path. `unquote` is the decode port — a mismatch here is the
    // whole-path `%2F` class of bug.

    #[test]
    fn encode_path_vectors() {
        let data: Value = serde_json::from_str(ENCODE_PATH).unwrap();
        assert_eq!(data["format"], 1);
        for case in data["cases"].as_array().unwrap() {
            let name = case["name"].as_str().unwrap();
            let expect = case["expect"].as_str().unwrap();
            assert_eq!(
                thumbs::unquote(expect),
                case["path"].as_str().unwrap(),
                "{name} path decode"
            );
            // image cases additionally pin the full /image/ URL
            let Some(url) = case["expectImageUrl"].as_str() else {
                continue;
            };
            let stripped = url.strip_prefix("/image/").unwrap_or(url);
            // empty-folder rule: root file -> '<name>', never '//'
            let folder = case["folder"].as_str().unwrap();
            let file = case["file"].as_str().unwrap();
            let want = if folder.is_empty() {
                file.to_string()
            } else {
                format!("{folder}/{file}")
            };
            assert_eq!(thumbs::unquote(stripped), want, "{name} imageUrl decode");
        }
    }
}
