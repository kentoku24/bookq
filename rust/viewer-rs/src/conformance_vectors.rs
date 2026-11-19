//! `api/vectors/*.json` conformance (#280, #459) — the same cases the iOS
//! (`ConformanceVectorsTests.swift`) and Python suites run, executed
//! against the Rust ports so the implementations cannot drift.
//!
//! `positions-cases.json` is exercised separately by
//! `state.rs::position_book_conformance_cases`; `solo-spreads.json` /
//! `chapter-padding.json` have no Rust layout port — the runners below pin
//! the solo spread rule (`pad=(startIdx-spreadOff+2)%2` + `rebuildSpreads`)
//! against the paired-reader `reader_sync::model::build_spread_plan` (T01).

#[cfg(test)]
mod tests {
    use crate::reader_sync::{catalog, model};
    use crate::{thumbs, tree};
    use serde_json::{json, Value};
    use std::collections::{HashMap, VecDeque};

    const WORK_CHAPTERS: &str = include_str!("../../../api/vectors/work-chapters.json");
    const PATH_LOOKUP: &str = include_str!("../../../api/vectors/path-lookup.json");
    const ENCODE_PATH: &str = include_str!("../../../api/vectors/encode-path.json");
    const SOLO_SPREADS: &str = include_str!("../../../api/vectors/solo-spreads.json");
    const CHAPTER_PADDING: &str = include_str!("../../../api/vectors/chapter-padding.json");

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

    // ── solo-spreads / chapter-padding vs build_spread_plan (work-model/T01)
    // Solo rule: `pad=(startIdx-spreadOff+2)%2` per chapter boundary on the
    // post-shift index, then global `rebuildSpreads` (offset leading
    // singletons, pairs, spacers dropped, chapterStart when a spread's first
    // page is boundaries[c>0]). Paired rule: `build_spread_plan` pairs
    // per-chapter with the first page as cover solo when offset==1 and has
    // no single-page mode. The two agree for offset==0 spread cases and
    // disagree for single-mode multi-page chapters and offset==1
    // multi-chapter works (global grouping vs per-chapter cover).

    /// Known divergences: case name -> classification. `cargo test` stays
    /// green while proving the classification matches reality: the runner
    /// asserts the actual mismatch set equals this table.
    fn known_spread_divergence(name: &str) -> Option<&'static str> {
        match name {
            // build_spread_plan has no single-page mode (always pairs).
            "single-mode-5" => Some("single-mode-pairing"),
            // build_spread_plan treats each chapter's first page as a cover
            // solo; the solo rule groups globally from index 0 so the
            // leading spacer consumes the offset singleton.
            "cross-offset1-leading-spacer" => Some("offset1-per-chapter-cover"),
            "three-chapters-offset1-cumulative" => Some("offset1-per-chapter-cover"),
            "cross-two-chapter-offset1" => Some("offset1-per-chapter-cover"),
            "cross-three-chapter-offset1" => Some("offset1-per-chapter-cover"),
            _ => None,
        }
    }

    fn is_spacer(im: &Value) -> bool {
        im.get("isSpacer").and_then(|v| v.as_bool()).unwrap_or(false)
    }

    /// Rebuild unpadded `build_spread_plan` chapters from padded solo
    /// images + boundaries: slice `[boundaries[c]..boundaries[c+1])`,
    /// drop spacers. Folder is the first real page's folder ("" when the
    /// slice carries no real page, e.g. spacer-only / empty cases).
    fn solo_chapters(images: &[Value], boundaries: &[i64]) -> Vec<Value> {
        let mut chapters = Vec::new();
        for (ci, &b) in boundaries.iter().enumerate() {
            let start = b as usize;
            let end = if ci + 1 < boundaries.len() {
                boundaries[ci + 1] as usize
            } else {
                images.len()
            };
            let mut pages = Vec::new();
            let mut folder = String::new();
            for im in images.iter().take(end).skip(start) {
                if is_spacer(im) {
                    continue;
                }
                if folder.is_empty() {
                    folder = im
                        .get("folder")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .to_string();
                }
                pages.push(Value::String(
                    im.get("name").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                ));
            }
            // Same-chapter duplicate names cannot happen (filenames are
            // unique per folder); folder disambiguates cross-chapter "1"s.
            chapters.push(json!({"folder": folder, "pages": pages}));
        }
        // Empty images with boundaries [0]: keep the single empty chapter so
        // build_spread_plan yields zero spreads (matches expectSpreads []).
        if chapters.is_empty() {
            chapters.push(json!({"folder": "", "pages": []}));
        }
        chapters
    }

    /// Map a `build_spread_plan` result back to global padded indices:
    /// `(spreads_global, idx_to_spread, chapter_start)`. Consumes the
    /// `(folder, name)` occurrence queue in plan order (plan order follows
    /// chapter/page order, same as the global order).
    fn plan_to_global(
        name: &str,
        plan: &[model::Spread],
        images: &[Value],
        boundaries: &[i64],
    ) -> (Vec<Vec<i64>>, Vec<i64>, Vec<bool>) {
        let mut queue: HashMap<(String, String), VecDeque<usize>> = HashMap::new();
        for (idx, im) in images.iter().enumerate() {
            if is_spacer(im) {
                continue;
            }
            let key = (
                im.get("folder").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                im.get("name").and_then(|v| v.as_str()).unwrap_or("").to_string(),
            );
            queue.entry(key).or_default().push_back(idx);
        }
        // spread_id contract: c{ci}:s{si} with per-chapter sequential si.
        let mut seen: HashMap<String, usize> = HashMap::new();
        let mut spreads_global: Vec<Vec<i64>> = Vec::new();
        let mut idx_to_spread = vec![-1i64; images.len()];
        for (plan_idx, s) in plan.iter().enumerate() {
            let si = seen.entry(s.chapter_folder.clone()).or_insert(0);
            let want_id = format!("c{}:s{}", {
                // chapter index = position of first occurrence of this folder
                // in plan order; derive from the id itself is circular, so
                // count distinct folders seen so far instead.
                let mut order: Vec<String> = Vec::new();
                for p in plan.iter().take(plan_idx + 1) {
                    if !order.contains(&p.chapter_folder) {
                        order.push(p.chapter_folder.clone());
                    }
                }
                order.iter().position(|f| f == &s.chapter_folder).unwrap()
            }, *si);
            assert_eq!(s.id, want_id, "{name} spread_id");
            *si += 1;
            let mut pop = |r: &model::PageRef| -> usize {
                queue
                    .get_mut(&(r.folder.clone(), r.name.clone()))
                    .and_then(|q| q.pop_front())
                    .unwrap_or_else(|| panic!("{name}: plan page not in images: {r:?}"))
            };
            let ri = pop(&s.right);
            idx_to_spread[ri] = plan_idx as i64;
            let mut pages = vec![ri as i64];
            if let Some(left) = &s.left {
                let li = pop(left);
                idx_to_spread[li] = plan_idx as i64;
                pages.push(li as i64);
            }
            spreads_global.push(pages);
        }
        let chapter_start = spreads_global
            .iter()
            .map(|sp| boundaries.iter().skip(1).any(|&b| b == sp[0]))
            .collect();
        (spreads_global, idx_to_spread, chapter_start)
    }

    fn spreads_match(
        name: &str,
        plan: &[model::Spread],
        images: &[Value],
        boundaries: &[i64],
        expect_spreads: &Value,
        expect_idx: &Value,
        expect_starts: &Value,
    ) -> bool {
        let (spreads_global, idx_to_spread, chapter_start) =
            plan_to_global(name, plan, images, boundaries);
        json!(spreads_global) == *expect_spreads
            && json!(idx_to_spread) == *expect_idx
            && json!(chapter_start) == *expect_starts
    }

    #[test]
    fn solo_spreads_vs_spread_plan() {
        let data: Value = serde_json::from_str(SOLO_SPREADS).unwrap();
        assert_eq!(data["format"], 1);
        assert!(data["languages"].as_array().unwrap().contains(&json!("rust")));
        let cases = data["cases"].as_array().unwrap();
        assert_eq!(cases.len(), 10, "solo-spreads case count");
        let mut ran = 0usize;
        let mut actual: Vec<String> = Vec::new();
        for case in cases {
            let name = case["name"].as_str().unwrap();
            let images = case["images"].as_array().unwrap();
            let boundaries: Vec<i64> =
                case["chapterBoundaries"].as_array().unwrap().iter().map(|v| v.as_i64().unwrap()).collect();
            let offset = case["spreadOffset"].as_i64().unwrap();
            let chapters = solo_chapters(images, &boundaries);
            let plan = model::build_spread_plan(&chapters, offset)
                .unwrap_or_else(|e| panic!("{name}: build_spread_plan failed: {e}"));
            ran += 1;
            if !spreads_match(
                name,
                &plan,
                images,
                &boundaries,
                &case["expectSpreads"],
                &case["expectIdxToSpread"],
                &case["expectChapterStart"],
            ) {
                actual.push(name.to_string());
            }
        }
        assert_eq!(ran, cases.len(), "solo-spreads ran all cases");
        assert_eq!(ran, 10, "solo-spreads executed case count");
        let mut actual_sorted = actual.clone();
        actual_sorted.sort();
        let mut expected: Vec<String> = cases
            .iter()
            .filter_map(|c| c["name"].as_str())
            .filter(|n| known_spread_divergence(n).is_some())
            .map(|s| s.to_string())
            .collect();
        expected.sort();
        assert_eq!(actual_sorted, expected, "solo-spreads divergence table mismatch");
        // Every divergent case must carry a classification.
        for n in &actual_sorted {
            assert!(known_spread_divergence(n).is_some(), "missing divergence entry for {n}");
        }
    }

    #[test]
    fn chapter_padding_vs_spread_plan() {
        let data: Value = serde_json::from_str(CHAPTER_PADDING).unwrap();
        assert_eq!(data["format"], 1);
        assert!(data["languages"].as_array().unwrap().contains(&json!("rust")));
        let cases = data["cases"].as_array().unwrap();
        assert_eq!(cases.len(), 9, "chapter-padding case count");
        let mut ran = 0usize;
        let mut actual: Vec<String> = Vec::new();
        for case in cases {
            let name = case["name"].as_str().unwrap();
            let chapters: Vec<Value> = case["chapters"]
                .as_array()
                .unwrap()
                .iter()
                .map(|ch| {
                    json!({
                        "folder": ch["folder"].as_str().unwrap(),
                        "pages": ch["pages"].as_array().unwrap().iter().map(|p| p.as_str().unwrap()).collect::<Vec<_>>(),
                    })
                })
                .collect();
            let offset = case["spreadOffset"].as_i64().unwrap();
            let plan = model::build_spread_plan(&chapters, offset)
                .unwrap_or_else(|e| panic!("{name}: build_spread_plan failed: {e}"));
            ran += 1;
            let images = case["expectImages"].as_array().unwrap();
            let boundaries: Vec<i64> =
                case["expectBoundaries"].as_array().unwrap().iter().map(|v| v.as_i64().unwrap()).collect();
            if !spreads_match(
                name,
                &plan,
                images,
                &boundaries,
                &case["expectSpreads"],
                &case["expectIdxToSpread"],
                &case["expectChapterStart"],
            ) {
                actual.push(name.to_string());
            }
        }
        assert_eq!(ran, cases.len(), "chapter-padding ran all cases");
        assert_eq!(ran, 9, "chapter-padding executed case count");
        let mut actual_sorted = actual.clone();
        actual_sorted.sort();
        let mut expected: Vec<String> = cases
            .iter()
            .filter_map(|c| c["name"].as_str())
            .filter(|n| known_spread_divergence(n).is_some())
            .map(|s| s.to_string())
            .collect();
        expected.sort();
        assert_eq!(actual_sorted, expected, "chapter-padding divergence table mismatch");
        for n in &actual_sorted {
            assert!(known_spread_divergence(n).is_some(), "missing divergence entry for {n}");
        }
    }
}
