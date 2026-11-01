//! #210 — golden-frame conformance: executes the canonical cases in
//! `api/reader-sync-cases.json` against the in-process `Registry` and
//! asserts the exact wire frames each link emits. The same case file
//! drives `tests/test_reader_sync_golden.py` on the Python side, so a
//! protocol change must move both implementations in lockstep — drift
//! fails one side's harness.
//!
//! Dynamic server values (session/member/link ids, codes, resume
//! tokens, digests) are bound at runtime: "@L1.member" resolves to the
//! memberId issued to link L1's CREATE/JOIN ACK, "@session" to the
//! latest session id, and so on — identical semantics to the Python
//! adapter.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Instant;

use serde_json::{json, Map, Value};
use tokio::sync::{mpsc, watch};

use super::{catalog, model, protocol, BulkQ, Link, LoadJob, OutItem, Registry};
use crate::folders::IndexState;

fn cases_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../api/reader-sync-cases.json")
}

// ── resolver manifest construction ───────────────────────────────────

/// The manifest dict a real catalog would resolve — built through the
/// same model/digest code the production resolver uses.
fn build_manifest(spec: &Value) -> Value {
    let mut chapters = Vec::new();
    let mut plan_chapters = Vec::new();
    let mut page_refs = Vec::new();
    for c in spec["chapters"].as_array().unwrap() {
        let folder = c["folder"].as_str().unwrap();
        let pages: Vec<Value> = c["pages"]
            .as_array()
            .unwrap()
            .iter()
            .map(|n| json!({"folder": folder, "name": n}))
            .collect();
        page_refs.extend(pages.iter().cloned());
        chapters.push(json!({"name": c["name"], "folder": folder, "pages": pages}));
        plan_chapters.push(json!({"folder": folder, "pages": c["pages"]}));
    }
    let offset = spec["offset"].as_i64().unwrap();
    let plan = model::build_spread_plan(&plan_chapters, offset).unwrap();
    let spread_table: Vec<Value> = plan.iter().map(model::serialize_spread).collect();
    let mut core = json!({
        "v": protocol::PROTOCOL_VERSION,
        "folder": spec["folder"],
        "offset": spec["offset"],
        "chapters": chapters,
        "pageRefs": page_refs,
        "spreadTable": spread_table,
    });
    let digest = catalog::manifest_digest(&core).unwrap();
    core.as_object_mut()
        .unwrap()
        .insert("digest".into(), Value::String(digest));
    core
}

// ── bindings (@-template resolution + harvesting) ────────────────────

type Bindings = Arc<std::sync::Mutex<HashMap<String, Value>>>;

/// Substitute '@name' references in values and dict keys.
fn resolve_obj(v: &Value, b: &HashMap<String, Value>) -> Value {
    match v {
        Value::String(s) if s.starts_with('@') => b
            .get(&s[1..])
            .cloned()
            .unwrap_or_else(|| panic!("unbound reference {s}")),
        Value::Array(a) => Value::Array(a.iter().map(|x| resolve_obj(x, b)).collect()),
        Value::Object(o) => {
            let mut m = Map::new();
            for (k, val) in o {
                let key = match k.strip_prefix('@') {
                    Some(name) if b.contains_key(name) => b[name]
                        .as_str()
                        .expect("bound key must be a string")
                        .to_string(),
                    _ => k.clone(),
                };
                m.insert(key, resolve_obj(val, b));
            }
            Value::Object(m)
        }
        _ => v.clone(),
    }
}

/// Bind dynamic values an implementation issues into wire frames.
fn harvest(b: &mut HashMap<String, Value>, linkname: &str, frame: &Value,
           senders: &HashMap<String, String>) {
    if frame["type"] == "HELLO" {
        if let Some(l) = frame["link"].as_str() {
            b.insert(format!("{linkname}.link"), l.into());
        }
    }
    if let Some(sid) = frame["sessionId"].as_str() {
        b.insert("session".into(), sid.into());
    }
    if frame["type"] == "ACK" {
        if let Some(rid) = frame["requestId"].as_str() {
            if let Some(sender) = senders.get(rid) {
                let data = &frame["data"];
                if let Some(mid) = data["memberId"].as_str() {
                    b.insert(format!("{sender}.member"), mid.into());
                }
                if let Some(tok) = data["resumeToken"].as_str() {
                    b.insert(format!("{sender}.token"), tok.into());
                }
            }
        }
        if let Some(c) = frame["data"]["code"].as_str() {
            b.insert("code".into(), c.into());
        }
    }
    if frame["type"] == "STATE" {
        if let Some(p) = frame["presence"].as_object() {
            for (mid, m) in p {
                if let Some(r) = m["role"].as_str() {
                    b.insert(format!("{r}.member"), mid.clone().into());
                }
            }
        }
    }
    if frame["type"] == "MANIFEST" {
        if let Some(d) = frame["digest"].as_str() {
            b.insert("digest".into(), d.into());
        }
    }
}

/// Frame equality — Python semantics: dicts compare by key/value,
/// numbers numerically (1 == 1.0), everything else exact.
fn json_eq(a: &Value, b: &Value) -> bool {
    match (a, b) {
        (Value::Number(x), Value::Number(y)) => x.as_f64() == y.as_f64(),
        (Value::Array(x), Value::Array(y)) => {
            x.len() == y.len() && x.iter().zip(y.iter()).all(|(p, q)| json_eq(p, q))
        }
        (Value::Object(x), Value::Object(y)) => {
            x.len() == y.len()
                && x.iter()
                    .all(|(k, v)| y.get(k).map_or(false, |w| json_eq(v, w)))
        }
        _ => a == b,
    }
}

// ── link driver ──────────────────────────────────────────────────────

/// Owns a Link's outbound queue + bulk blocks; the transport-close hook
/// stays a watch channel (recorded, never a socket).
struct LinkDriver {
    link_id: String,
    rx: mpsc::Receiver<OutItem>,
    bulk: Arc<std::sync::Mutex<BulkQ>>,
    #[allow(dead_code)]
    close_rx: watch::Receiver<Option<(u16, String)>>,
}

impl LinkDriver {
    async fn attach(reg: &Arc<Registry>, user: Option<Value>) -> Self {
        let (tx, rx) = mpsc::channel(reg.send_queue_max());
        let (close_tx, close_rx) = watch::channel(None);
        let link = Link::new(tx, close_tx, user, Instant::now(),
                             reg.heartbeat_interval());
        let bulk = link.bulk.clone();
        let link_id = reg
            .inner
            .lock()
            .await
            .register_link(link, reg.heartbeat_interval());
        Self { link_id, rx, bulk, close_rx }
    }

    /// Every pending outbound frame, bulk blocks expanded in place.
    fn drain(&mut self) -> Vec<Value> {
        let mut out = Vec::new();
        while let Ok(item) = self.rx.try_recv() {
            match item {
                OutItem::Text(raw) => {
                    out.push(serde_json::from_str(&raw).unwrap())
                }
                OutItem::Bulk => {
                    self.bulk.lock().unwrap().pending = false;
                    while let Some(raw) = self.bulk.lock().unwrap().pop() {
                        out.push(serde_json::from_str(&raw).unwrap());
                    }
                }
            }
        }
        out
    }
}

// ── case runner ──────────────────────────────────────────────────────

struct Driver {
    reg: Arc<Registry>,
    bindings: Bindings,
    links: HashMap<String, LinkDriver>,
    senders: HashMap<String, String>, // requestId -> link name
}

impl Driver {
    fn b(&self) -> std::sync::MutexGuard<'_, HashMap<String, Value>> {
        self.bindings.lock().unwrap()
    }

    async fn attach(&mut self, name: &str, user: Option<Value>) {
        let d = LinkDriver::attach(&self.reg, user).await;
        self.links.insert(name.to_string(), d);
    }

    async fn send(&mut self, name: &str, frame: Value) {
        let f = resolve_obj(&frame, &self.b());
        if let Some(rid) = f.get("requestId").and_then(|v| v.as_str()) {
            self.senders.insert(rid.to_string(), name.to_string());
        }
        let raw = serde_json::to_string(&f).unwrap();
        let lid = &self.links[name].link_id;
        self.reg.handle_raw(lid, raw.as_bytes()).await;
    }

    async fn detach(&mut self, name: &str) {
        let lid = self.links[name].link_id.clone();
        self.reg.detach_link(&lid).await;
    }

    /// Drain+harvest a link until quiesced — loads commit through a
    /// spawned task, so yield until no session has a pending load and
    /// the queue is empty. Frames return in wire order.
    async fn take(&mut self, name: &str) -> Vec<Value> {
        let mut all = Vec::new();
        for _ in 0..4000 {
            for f in self.links.get_mut(name).expect("unknown link").drain() {
                self.harvest_one(name, &f);
                all.push(f);
            }
            let pending = {
                let g = self.reg.inner.lock().await;
                g.sessions.iter().any(|s| s.pending_load.is_some())
            };
            if !pending && self.links[name].rx.is_empty() {
                return all;
            }
            tokio::task::yield_now().await;
        }
        panic!("registry did not quiesce");
    }

    fn harvest_one(&mut self, name: &str, f: &Value) {
        harvest(&mut self.b(), name, f, &self.senders);
    }
}

fn user_json(u: &Value) -> Option<Value> {
    if u.is_null() {
        None
    } else {
        Some(u.clone())
    }
}

async fn run_case(doc: &Value, case: &Value) {
    let cid = case["id"].as_str().unwrap();
    let specs = &doc["resolver"];
    let rkey = case.get("resolver").and_then(|v| v.as_str());

    let bindings: Bindings = Arc::new(std::sync::Mutex::new(HashMap::new()));
    if let Some(k) = rkey {
        bindings.lock().unwrap().insert(
            "manifest.canonical".into(),
            Value::String(catalog::canonical(&build_manifest(&specs[k]))),
        );
    }

    let reg = Registry::new(Arc::new(IndexState::new()), PathBuf::from("/nonexistent"));
    if rkey.is_some() {
        let specs_c = specs.clone();
        let b = bindings.clone();
        reg.set_test_resolver(Arc::new(move |job: &LoadJob| {
            let spec = match specs_c.get(&job.folder) {
                Some(s) => s.clone(),
                None => {
                    return Err(catalog::ManifestError {
                        code: catalog::ERR_NOT_FOUND,
                        message: format!("no resolver entry: {}", job.folder),
                    })
                }
            };
            let manifest = build_manifest(&spec);
            // Mirror catalog.resolve_work: the committed initial spread
            // comes from the requested page ref.
            let pref = model::PageRef {
                folder: job.page["folder"].as_str().unwrap_or("").to_string(),
                name: job.page["name"].as_str().unwrap_or("").to_string(),
            };
            let delivered = catalog::finish_resolve(manifest, &pref)?;
            // MANIFEST chunk data is the canonical bytes of the *delivered*
            // manifest (including the resolver-added "initial") — rebind so
            // @manifest.canonical always mirrors the latest resolve.
            b.lock().unwrap().insert(
                "manifest.canonical".into(),
                Value::String(catalog::canonical(&delivered)),
            );
            Ok(delivered)
        }));
    }

    let mut drv = Driver {
        reg,
        bindings,
        links: HashMap::new(),
        senders: HashMap::new(),
    };

    // Canonical shared setup — identical to the Python adapter's.
    if let Some(setup) = case.get("setup").and_then(|v| v.as_str()) {
        let users = case.get("users").cloned().unwrap_or(json!({}));
        drv.attach("L1", user_json(&users["L1"]).or_else(|| {
            Some(json!({"email": "alice@example.com", "name": "Alice",
                        "picture": ""}))
        }))
        .await;
        drain_harvest(&mut drv, "L1").await;
        drv.send("L1", json!({"v":1,"requestId":"c1","sessionId":null,
                 "workEpoch":0,"baseRevision":0,"type":"CREATE",
                 "payload":{}})).await;
        drain_harvest(&mut drv, "L1").await;
        drv.attach("L2", user_json(&users["L2"]).or_else(|| {
            Some(json!({"email": "bob@example.com", "name": "Bob",
                        "picture": ""}))
        }))
        .await;
        drain_harvest(&mut drv, "L2").await;
        drv.send("L2", json!({"v":1,"requestId":"j1","sessionId":null,
                 "workEpoch":0,"baseRevision":0,"type":"JOIN",
                 "payload":{"code":"@code"}})).await;
        drain_harvest(&mut drv, "L1").await;
        drain_harvest(&mut drv, "L2").await;
        drv.send("L1", json!({"v":1,"requestId":"a1","sessionId":"@session",
                 "workEpoch":0,"baseRevision":1,"type":"APPROVE",
                 "payload":{"memberId":"@L2.member"}})).await;
        drain_harvest(&mut drv, "L1").await;
        drain_harvest(&mut drv, "L2").await;
        if setup == "paired_open" || setup == "reading" {
            let spec = &specs[rkey.unwrap()];
            drv.send("L1", json!({"v":1,"requestId":"o1",
                     "sessionId":"@session","workEpoch":0,"baseRevision":2,
                     "type":"OPEN_WORK",
                     "payload":{"folder": rkey,
                                "page": {"folder": rkey,
                                         "name": spec["chapters"][0]["pages"][0]},
                                "offset": spec["offset"]}})).await;
            drain_harvest(&mut drv, "L1").await;
            drain_harvest(&mut drv, "L2").await;
        }
        if setup == "reading" {
            drv.send("L1", json!({"v":1,"requestId":"r1",
                     "sessionId":"@session","workEpoch":1,"baseRevision":4,
                     "type":"READY","payload":{"manifestVersion":1}})).await;
            drain_harvest(&mut drv, "L1").await;
            drain_harvest(&mut drv, "L2").await;
            drv.send("L2", json!({"v":1,"requestId":"r2",
                     "sessionId":"@session","workEpoch":1,"baseRevision":5,
                     "type":"READY","payload":{"manifestVersion":1}})).await;
            drain_harvest(&mut drv, "L1").await;
            drain_harvest(&mut drv, "L2").await;
        }
    }

    for (i, step) in case["steps"].as_array().unwrap().iter().enumerate() {
        let op = step["op"].as_str().unwrap();
        let at = format!("{cid} step[{i}] {op}");
        match op {
            "link" => {
                drv.attach(step["as"].as_str().unwrap(),
                           user_json(&step["user"]))
                    .await;
            }
            "detach" => drv.detach(step["link"].as_str().unwrap()).await,
            "send" => {
                let (l, f) = (step["link"].as_str().unwrap(), step["frame"].clone());
                drv.send(l, f).await
            }
            "expect" | "drain" => {
                let name = step["link"].as_str().unwrap();
                let got = drv.take(name).await;
                if op == "drain" {
                    continue;
                }
                let want: Vec<Value> = step["frames"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|f| resolve_obj(f, &drv.b()))
                    .collect();
                assert!(
                    got.len() == want.len()
                        && got.iter().zip(want.iter()).all(|(g, w)| json_eq(g, w)),
                    "{at}: frame mismatch\n--- want ---\n{}\n--- got ----\n{}",
                    serde_json::to_string_pretty(&want).unwrap(),
                    serde_json::to_string_pretty(&got).unwrap(),
                );
            }
            "tick" => panic!("{at}: tick unsupported (real-clock core)"),
            other => panic!("{at}: unknown op {other}"),
        }
    }
}

async fn drain_harvest(drv: &mut Driver, name: &str) {
    drv.take(name).await;
}

#[tokio::test(flavor = "current_thread")]
async fn golden_cases_conform() {
    let raw = std::fs::read_to_string(cases_path()).unwrap();
    let doc: Value = serde_json::from_str(&raw).unwrap();
    assert_eq!(doc["format"], json!(1));
    for case in doc["cases"].as_array().unwrap() {
        run_case(&doc, case).await;
    }
}
