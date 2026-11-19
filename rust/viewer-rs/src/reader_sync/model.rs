//! dual-reader shared spread model — port of `app/reader_sync/model.py`.
//!
//! offset=0: (p1 R, p2 L) (p3 R, p4 L) …
//! offset=1: (p1 R, blank) (p2 R, p3 L) (p4 R, p5 L) …
//! The right page always leads within a chapter; a missing partner stays
//! blank and chapters never share a spread.

use serde_json::{Map, Value};

/// Typed rejection for invalid model input (distinguishable by `.code`).
#[derive(Debug)]
pub struct ModelError {
    pub code: &'static str,
    pub message: String,
}
impl std::fmt::Display for ModelError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code, self.message)
    }
}
impl std::error::Error for ModelError {}

fn err<T>(code: &'static str, msg: impl Into<String>) -> Result<T, ModelError> {
    Err(ModelError {
        code,
        message: msg.into(),
    })
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PageRef {
    pub folder: String,
    pub name: String,
}

#[derive(Debug, Clone)]
pub struct Spread {
    pub id: String,
    pub chapter_folder: String,
    pub right: PageRef,
    pub left: Option<PageRef>,
}

fn as_ref(chapter_folder: &str, page: &Value) -> Result<PageRef, ModelError> {
    if let Value::String(s) = page {
        return Ok(PageRef {
            folder: chapter_folder.into(),
            name: s.clone(),
        });
    }
    if let Value::Object(o) = page {
        let folder = o.get("folder").and_then(|v| v.as_str());
        let name = o.get("name").and_then(|v| v.as_str());
        return match (folder, name) {
            (Some(f), Some(n)) => Ok(PageRef {
                folder: f.into(),
                name: n.into(),
            }),
            _ => err("ERR_MALFORMED", "page dict missing folder/name or not str"),
        };
    }
    err("ERR_MALFORMED", "unsupported page form")
}

fn spread_id(ci: usize, si: usize) -> String {
    format!("c{ci}:s{si}")
}

/// Build the shared spread table.
/// `chapters`: [{folder, pages:[name|{folder,name}]}].
pub fn build_spread_plan(chapters: &[Value], offset: i64) -> Result<Vec<Spread>, ModelError> {
    if offset != 0 && offset != 1 {
        return err("ERR_RANGE", format!("offset must be 0 or 1, got {offset}"));
    }
    let mut spreads = Vec::new();
    for (ci, ch) in chapters.iter().enumerate() {
        let obj = match ch.as_object() {
            Some(o) => o,
            None => return err("ERR_MALFORMED", format!("chapter {ci} needs folder+pages")),
        };
        let folder = match obj.get("folder").and_then(|v| v.as_str()) {
            Some(f) => f,
            None => return err("ERR_MALFORMED", format!("chapter {ci} folder must be str")),
        };
        let pages = match obj.get("pages").and_then(|v| v.as_array()) {
            Some(p) => p,
            None => {
                return err(
                    "ERR_MALFORMED",
                    format!("chapter {ci} pages must be a list"),
                )
            }
        };
        let refs: Vec<PageRef> = pages
            .iter()
            .map(|p| as_ref(folder, p))
            .collect::<Result<_, _>>()?;
        let n = refs.len();
        let mut si = 0usize;
        let mut i = 0usize;
        if offset == 1 {
            if n >= 1 {
                spreads.push(Spread {
                    id: spread_id(ci, 0),
                    chapter_folder: folder.into(),
                    right: refs[0].clone(),
                    left: None,
                });
                si = 1;
            }
            i = 1;
        }
        while i < n {
            let left = if i + 1 < n {
                Some(refs[i + 1].clone())
            } else {
                None
            };
            spreads.push(Spread {
                id: spread_id(ci, si),
                chapter_folder: folder.into(),
                right: refs[i].clone(),
                left,
            });
            si += 1;
            i += 2;
        }
    }
    Ok(spreads)
}

pub fn spread_index_by_id(plan: &[Spread], id: &str) -> i64 {
    plan.iter()
        .position(|s| s.id == id)
        .map(|i| i as i64)
        .unwrap_or(-1)
}

pub fn spread_index_for_page(plan: &[Spread], target: &PageRef) -> i64 {
    for (i, s) in plan.iter().enumerate() {
        if &s.right == target || s.left.as_ref() == Some(target) {
            return i as i64;
        }
    }
    -1
}

/// GOTO: the spread containing ref — missing page is an explicit error.
pub fn goto_index(plan: &[Spread], page: &Value) -> Result<usize, ModelError> {
    let target = as_ref("", page)?;
    let idx = spread_index_for_page(plan, &target);
    if idx < 0 {
        return err("ERR_RANGE", format!("page not in plan: {target:?}"));
    }
    Ok(idx as usize)
}

fn clamp_index(plan: &[Spread], index: i64) -> i64 {
    if plan.is_empty() {
        return -1;
    }
    index.clamp(0, plan.len() as i64 - 1)
}

/// TURN request resolution. Out-of-range clamps to the boundary — the
/// chapter's own contiguous block when `cross_chapter` is false.
pub fn turn_index(
    plan: &[Spread],
    index: i64,
    delta: i64,
    cross_chapter: bool,
) -> Result<i64, ModelError> {
    if plan.is_empty() {
        return Ok(-1);
    }
    if index < 0 || index >= plan.len() as i64 {
        return err("ERR_RANGE", format!("spread index out of range: {index}"));
    }
    if delta != 1 && delta != -1 {
        return err("ERR_RANGE", format!("delta must be ±1, got {delta}"));
    }
    let target = index + delta;
    if cross_chapter {
        return Ok(clamp_index(plan, target));
    }
    let chapter = &plan[index as usize].chapter_folder;
    let mut lo = index as usize;
    while lo > 0 && plan[lo - 1].chapter_folder == *chapter {
        lo -= 1;
    }
    let mut hi = index as usize;
    while hi + 1 < plan.len() && plan[hi + 1].chapter_folder == *chapter {
        hi += 1;
    }
    Ok((target).clamp(lo as i64, hi as i64))
}

/// Canonical projection: which ref this member renders.
#[allow(dead_code)]
pub fn assigned_ref<'a>(
    spread: &'a Spread,
    role: &str,
    leader_side: &str,
) -> Result<Option<&'a PageRef>, ModelError> {
    if !["right", "left"].contains(&leader_side) {
        return err(
            "ERR_RANGE",
            format!("leader_side must be right|left, got {leader_side}"),
        );
    }
    let side = match role {
        "leader" => leader_side,
        "follower" => {
            if leader_side == "right" {
                "left"
            } else {
                "right"
            }
        }
        _ => {
            return err(
                "ERR_MALFORMED",
                format!("role must be leader|follower, got {role}"),
            )
        }
    };
    Ok(if side == "right" {
        Some(&spread.right)
    } else {
        spread.left.as_ref()
    })
}

pub fn serialize_spread(s: &Spread) -> Value {
    let mut m = Map::new();
    m.insert("id".into(), Value::String(s.id.clone()));
    m.insert(
        "chapterFolder".into(),
        Value::String(s.chapter_folder.clone()),
    );
    m.insert("right".into(), ref_json(&s.right));
    m.insert(
        "left".into(),
        s.left.as_ref().map(ref_json).unwrap_or(Value::Null),
    );
    Value::Object(m)
}

pub fn ref_json(r: &PageRef) -> Value {
    let mut m = Map::new();
    m.insert("folder".into(), Value::String(r.folder.clone()));
    m.insert("name".into(), Value::String(r.name.clone()));
    Value::Object(m)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn chapter(folder: &str, pages: &[&str]) -> Value {
        let mut ch = Map::new();
        ch.insert("folder".into(), Value::String(folder.into()));
        ch.insert(
            "pages".into(),
            Value::Array(pages.iter().map(|p| Value::String((*p).into())).collect()),
        );
        Value::Object(ch)
    }

    #[test]
    fn spread_plan_offsets() {
        let chs = vec![chapter("a", &["p1", "p2", "p3", "p4"])];
        let plan = build_spread_plan(&chs, 0).unwrap();
        assert_eq!(plan.len(), 2);
        assert_eq!(plan[0].right.name, "p1");
        assert_eq!(plan[0].left.as_ref().unwrap().name, "p2");
        assert_eq!(plan[1].right.name, "p3");
        assert!(plan[1].left.is_some());
        let plan = build_spread_plan(&chs, 1).unwrap();
        assert_eq!(plan.len(), 3);
        assert!(plan[0].left.is_none()); // cover page alone
        assert_eq!(plan[1].right.name, "p2");
        assert_eq!(plan[1].left.as_ref().unwrap().name, "p3");
        assert!(build_spread_plan(&chs, 2).is_err());
    }

    #[test]
    fn turn_goto_index() {
        let chs = vec![
            chapter("a", &["p1", "p2", "p3", "p4"]),
            chapter("b", &["q1", "q2"]),
        ];
        let plan = build_spread_plan(&chs, 0).unwrap();
        // a-block is spreads 0,1; b is 2 — no-cross clamps inside the chapter.
        assert_eq!(turn_index(&plan, 0, -1, false).unwrap(), 0);
        assert_eq!(turn_index(&plan, 1, 1, false).unwrap(), 1); // clamps to chapter end
        assert_eq!(turn_index(&plan, 1, 1, true).unwrap(), 2); // crosses to b
        let page = frame_page("b", "q2");
        assert_eq!(goto_index(&plan, &page).unwrap(), 2);
    }

    fn frame_page(f: &str, n: &str) -> Value {
        let mut m = Map::new();
        m.insert("folder".into(), Value::String(f.into()));
        m.insert("name".into(), Value::String(n.into()));
        Value::Object(m)
    }
}
