//! Wire contract for dual-reader sync frames — port of
//! `app/reader_sync/protocol.py`. Versioned frames, transport-agnostic.

use serde_json::{Map, Value};

pub const PROTOCOL_VERSION: i64 = 1;
pub const MAX_FRAME_BYTES: usize = 64 * 1024;
pub const MAX_MANIFEST_BYTES: usize = 32 * 1024 * 1024;

pub const ERR_VERSION: &str = "ERR_VERSION";
pub const ERR_MALFORMED: &str = "ERR_MALFORMED";
pub const ERR_TYPE: &str = "ERR_TYPE";
pub const ERR_RANGE: &str = "ERR_RANGE";
pub const ERR_SIZE: &str = "ERR_SIZE";

pub const REQUEST_TYPES: &[&str] = &[
    "CREATE",
    "JOIN",
    "APPROVE",
    "REJECT",
    "RESUME",
    "LEAVE",
    "MATCH",
    "TURN",
    "GOTO",
    "SET_OFFSET",
    "SWAP_SIDES",
    "OPEN_WORK",
    "SET_ACTIVITY",
    "READY",
    "PING",
    "APPLIED",
    "SIGNAL",
    "TRANSPORT_STATE",
    "UI_SYNC",
];
#[allow(dead_code)]
pub const PHASES: &[&str] = &[
    "waiting",
    "loading",
    "reading",
    "selecting",
    "paused",
    "ended",
];
pub const ROLES: &[&str] = &["leader", "follower"];
pub const SIDES: &[&str] = &["right", "left"];
pub const SIGNAL_KINDS: &[&str] = &["offer", "answer", "ice"];
pub const UI_VIEWS: &[&str] = &["browse"];
pub const UI_MODES: &[&str] = &["tile", "list", "card"];
pub const MAX_UI_PATH_CHARS: usize = 4096;

const REQUEST_KEYS: &[&str] = &[
    "requestId",
    "sessionId",
    "workEpoch",
    "baseRevision",
    "type",
    "payload",
];

/// Typed frame rejection; `code` is one of ERR_*.
#[derive(Debug)]
pub struct ProtocolError {
    pub code: &'static str,
    pub message: String,
}

impl std::fmt::Display for ProtocolError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code, self.message)
    }
}
impl std::error::Error for ProtocolError {}

fn fail<T>(code: &'static str, msg: impl Into<String>) -> Result<T, ProtocolError> {
    Err(ProtocolError {
        code,
        message: msg.into(),
    })
}

/// bytes -> object. Enforces the size cap and the version field.
/// `raw` is the wire payload — text frames arrive as UTF-8 bytes here so the
/// byte-length cap and the utf-8 check match Python's str/bytes handling.
pub fn parse_frame(raw: &[u8]) -> Result<Map<String, Value>, ProtocolError> {
    if raw.len() > MAX_FRAME_BYTES {
        return fail(ERR_SIZE, format!("frame exceeds {MAX_FRAME_BYTES} bytes"));
    }
    let text = match std::str::from_utf8(raw) {
        Ok(t) => t,
        Err(_) => return fail(ERR_MALFORMED, "frame is not utf-8"),
    };
    let v: Value = match serde_json::from_str(text) {
        Ok(v) => v,
        Err(_) => return fail(ERR_MALFORMED, "frame is not json"),
    };
    let obj = match v {
        Value::Object(o) => o,
        _ => return fail(ERR_MALFORMED, "frame must be an object"),
    };
    // Python `raw.get("v") != 1`: True and 1.0 both equal 1 under Python
    // semantics, so they pass the version gate too.
    let v_ok = match obj.get("v") {
        Some(Value::Number(n)) => n.as_f64() == Some(PROTOCOL_VERSION as f64),
        Some(Value::Bool(b)) => *b,
        _ => false,
    };
    if !v_ok {
        return fail(ERR_VERSION, format!("unsupported v={:?}", obj.get("v")));
    }
    Ok(obj)
}

pub fn parse_page_ref(x: &Value) -> Result<(), ProtocolError> {
    let ok = matches!(x, Value::Object(o)
        if o.get("folder").is_some_and(|v| v.is_string())
            && o.get("name").is_some_and(|v| v.is_string()));
    if !ok {
        return fail(ERR_MALFORMED, "PageRef needs {folder:str,name:str}");
    }
    Ok(())
}

fn require_int(f: &Map<String, Value>, key: &str) -> Result<i64, ProtocolError> {
    let v = &f[key];
    // serde_json: bools/null are never as_i64 — matches isinstance(int)-not-bool.
    // u64 values beyond i64::MAX saturate — Python bigints pass validation and
    // then lose the epoch/revision equality check, so the value only needs to
    // survive as "some non-negative int".
    let n = match v
        .as_i64()
        .or_else(|| v.as_u64().map(|u| u.min(i64::MAX as u64) as i64))
    {
        Some(n) => n,
        None => return fail(ERR_MALFORMED, format!("{key} must be int")),
    };
    if n < 0 {
        return fail(ERR_RANGE, format!("{key} must be >= 0"));
    }
    Ok(n)
}

/// Full client-request validation: version, keys, types, ranges.
pub fn parse_request(raw: &Map<String, Value>) -> Result<(), ProtocolError> {
    for k in REQUEST_KEYS {
        if !raw.contains_key(*k) {
            return fail(ERR_MALFORMED, format!("request missing key: {k}"));
        }
    }
    if !raw["requestId"].as_str().is_some_and(|s| !s.is_empty()) {
        return fail(ERR_MALFORMED, "requestId must be non-empty str");
    }
    if !(raw["sessionId"].is_null() || raw["sessionId"].is_string()) {
        return fail(ERR_MALFORMED, "sessionId must be str|null");
    }
    require_int(raw, "workEpoch")?;
    require_int(raw, "baseRevision")?;
    let t = raw["type"].as_str();
    if t.is_none() {
        return fail(ERR_MALFORMED, "type must be str");
    }
    let t = t.unwrap();
    if !REQUEST_TYPES.contains(&t) {
        return fail(ERR_TYPE, format!("unknown request type: {t}"));
    }
    let payload = match &raw["payload"] {
        Value::Object(o) => o.clone(),
        _ => return fail(ERR_MALFORMED, "payload must be an object"),
    };
    validate_payload(t, &payload)
}

fn validate_payload(t: &str, p: &Map<String, Value>) -> Result<(), ProtocolError> {
    match t {
        "TURN" => {
            // `delta not in (1,-1)` — Python `True == 1` lets `true` and
            // 1.0 through; `False == 0` fails and `1.5`/`2` miss.
            let delta_ok = match p.get("delta") {
                Some(Value::Bool(b)) => *b,
                Some(v) => v.as_f64().is_some_and(|f| f == 1.0 || f == -1.0),
                None => false,
            };
            if !delta_ok {
                return fail(ERR_RANGE, "TURN.delta must be ±1");
            }
            if !p.get("crossChapter").is_some_and(|v| v.is_boolean()) {
                return fail(ERR_MALFORMED, "TURN.crossChapter must be bool");
            }
            if let Some(fc) = p.get("fromChapter") {
                if !fc.is_string() && !fc.is_null() {
                    return fail(ERR_MALFORMED, "TURN.fromChapter must be str|null");
                }
            }
        }
        "GOTO" => {
            parse_page_ref(p.get("page").unwrap_or(&Value::Null))?;
        }
        "SET_OFFSET" => {
            // `offset not in (0,1)` — bools pass (True==1, False==0) and
            // floats 0.0/1.0 equal; bools are rejected deeper in
            // catalog.resolve_work (isinstance check).
            let offset_ok = match p.get("offset") {
                Some(Value::Bool(_)) => true,
                Some(v) => v.as_f64().is_some_and(|f| f == 0.0 || f == 1.0),
                None => false,
            };
            if !offset_ok {
                return fail(ERR_RANGE, "SET_OFFSET.offset must be 0 or 1");
            }
        }
        "SWAP_SIDES" => {
            if !p
                .get("leaderSide")
                .and_then(|v| v.as_str())
                .is_some_and(|s| SIDES.contains(&s))
            {
                return fail(ERR_RANGE, "SWAP_SIDES.leaderSide must be right|left");
            }
        }
        "MATCH" => {
            if let Some(pick) = p.get("pick") {
                if !pick.is_null() {
                    let ok = pick
                        .as_str()
                        .is_some_and(|s| (1..=64).contains(&s.chars().count()));
                    if !ok {
                        return fail(ERR_MALFORMED, "MATCH.pick must be a token str");
                    }
                }
            }
        }
        "JOIN" => {
            // str.isdigit() is Unicode-aware (Arabic-Indic digits etc. pass)
            // and len() counts chars, not bytes.
            let ok = p
                .get("code")
                .and_then(|v| v.as_str())
                .is_some_and(|s| s.chars().count() == 6 && s.chars().all(|c| c.is_numeric()));
            if !ok {
                return fail(ERR_MALFORMED, "JOIN.code must be a 6-digit str");
            }
        }
        "OPEN_WORK" => {
            if !p.get("folder").is_some_and(|v| v.is_string()) {
                return fail(ERR_MALFORMED, "OPEN_WORK.folder must be str");
            }
            if let Some(page) = p.get("page") {
                if !page.is_null() {
                    parse_page_ref(page)?;
                }
            }
        }
        "SIGNAL" => {
            if !p
                .get("kind")
                .and_then(|v| v.as_str())
                .is_some_and(|s| SIGNAL_KINDS.contains(&s))
            {
                return fail(ERR_TYPE, "SIGNAL.kind must be offer|answer|ice");
            }
        }
        "APPLIED" => {
            if !p.get("requestId").is_some_and(|v| v.is_string()) {
                return fail(ERR_MALFORMED, "APPLIED.requestId must be str");
            }
        }
        "UI_SYNC" => validate_ui_sync(p)?,
        _ => {}
    }
    Ok(())
}

fn validate_ui_sync(p: &Map<String, Value>) -> Result<(), ProtocolError> {
    if !p
        .get("view")
        .and_then(|v| v.as_str())
        .is_some_and(|s| UI_VIEWS.contains(&s))
    {
        return fail(ERR_TYPE, "UI_SYNC.view must be 'browse'");
    }
    if let Some(path) = p.get("path") {
        let ok = path.is_null()
            || path
                .as_str()
                .is_some_and(|s| s.chars().count() <= MAX_UI_PATH_CHARS);
        if !ok {
            return fail(ERR_MALFORMED, "UI_SYNC.path must be str|null");
        }
    }
    let top = p.get("top").and_then(|v| v.as_f64());
    if !top.is_some_and(|v| v.is_finite() && v >= 0.0) {
        return fail(ERR_RANGE, "UI_SYNC.top must be finite >= 0");
    }
    let view_h = p.get("viewH").and_then(|v| v.as_f64());
    if !view_h.is_some_and(|v| v.is_finite() && v > 0.0 && v <= 100000.0) {
        return fail(ERR_RANGE, "UI_SYNC.viewH must be finite 0<h<=100000");
    }
    if let Some(sh) = p.get("scrollH") {
        if !sh.is_null() {
            let ok = sh
                .as_f64()
                .is_some_and(|v| v.is_finite() && v > 0.0 && v <= 100000000.0);
            if !ok {
                return fail(ERR_RANGE, "UI_SYNC.scrollH must be finite 0<h<=1e8");
            }
        }
    }
    if let Some(mode) = p.get("mode") {
        let ok = mode.is_null() || mode.as_str().is_some_and(|s| UI_MODES.contains(&s));
        if !ok {
            return fail(ERR_RANGE, "UI_SYNC.mode must be tile|list|card");
        }
    }
    Ok(())
}

/// json-encode a frame; reject anything over the per-frame cap.
/// `separators=(",",":")` + `ensure_ascii=False` == serde_json::to_string.
/// Keys go on the wire in insertion order (preserve_order feature).
pub fn encode_frame(obj: &Value) -> Result<String, ProtocolError> {
    let s = match serde_json::to_string(obj) {
        Ok(s) => s,
        Err(e) => return fail(ERR_MALFORMED, e.to_string()),
    };
    if s.len() > MAX_FRAME_BYTES {
        return fail(ERR_SIZE, format!("frame exceeds {MAX_FRAME_BYTES} bytes"));
    }
    Ok(s)
}

/// Construct a frame object with the given key order — mirrors dict insertion
/// order in the Python emitters so wire bytes stay identical.
pub fn frame(pairs: &[(&str, Value)]) -> Value {
    let mut m = Map::with_capacity(pairs.len());
    for (k, v) in pairs {
        m.insert((*k).to_string(), v.clone());
    }
    Value::Object(m)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn req(t: &str, payload: Value) -> Value {
        frame(&[
            ("v", Value::from(PROTOCOL_VERSION)),
            ("type", Value::String(t.into())),
            ("requestId", Value::String("r1".into())),
            ("sessionId", Value::String("s".into())),
            ("workEpoch", Value::from(0)),
            ("baseRevision", Value::from(0)),
            ("payload", payload),
        ])
    }

    #[test]
    fn request_validation_and_null_semantics() {
        // MATCH.pick null → Python None → skips validation (must PASS).
        let r = req("MATCH", frame(&[("pick", Value::Null)]));
        parse_request(r.as_object().unwrap()).unwrap();
        let r = req("MATCH", frame(&[("pick", Value::String("tok".into()))]));
        parse_request(r.as_object().unwrap()).unwrap();
        // UI_SYNC.scrollH null → skips validation (must PASS).
        let r = req(
            "UI_SYNC",
            frame(&[
                ("view", Value::String("browse".into())),
                ("top", Value::from(0)),
                ("viewH", Value::from(100)),
                ("scrollH", Value::Null),
            ]),
        );
        parse_request(r.as_object().unwrap()).unwrap();
        // missing required key → ERR_MALFORMED
        let mut bad = req(
            "GOTO",
            frame(&[(
                "page",
                frame(&[("folder", "f".into()), ("name", "n".into())]),
            )]),
        );
        bad.as_object_mut().unwrap().remove("workEpoch");
        assert_eq!(
            parse_request(bad.as_object().unwrap()).unwrap_err().code,
            ERR_MALFORMED
        );
        // unknown type → ERR_TYPE
        let bad = req("NOPE", Value::Object(Map::new()));
        assert_eq!(
            parse_request(bad.as_object().unwrap()).unwrap_err().code,
            ERR_TYPE
        );
    }

    #[test]
    fn frame_size_cap_and_version() {
        let big = " ".repeat(MAX_FRAME_BYTES + 1).into_bytes();
        assert_eq!(parse_frame(&big).unwrap_err().code, ERR_SIZE);
        let v0 = frame(&[
            ("v", Value::from(0)),
            ("type", Value::String("PING".into())),
        ]);
        assert_eq!(
            parse_frame(serde_json::to_string(&v0).unwrap().as_bytes())
                .unwrap_err()
                .code,
            ERR_VERSION
        );
        assert_eq!(parse_frame(b"not json").unwrap_err().code, ERR_MALFORMED);
        assert_eq!(parse_frame(b"[1]").unwrap_err().code, ERR_MALFORMED);
    }
}
