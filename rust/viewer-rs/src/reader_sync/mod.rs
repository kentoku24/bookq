//! Session registry for dual-reader pairing — port of
//! `app/reader_sync/sessions.py` (+ the transport binding in
//! `app/reader_sync/transport.py`).
//!
//! Python's synchronous single-loop dispatch maps onto one
//! `tokio::sync::Mutex<Inner>`: every inbound frame, tick and resolver
//! completion serializes through the same lock, so operation order is
//! identical to the asyncio implementation. Outbound frames go through a
//! bounded per-link mpsc (SEND_QUEUE_MAX) plus a byte-bounded bulk side
//! queue for MANIFEST chunks — a slow peer overflows to a close instead
//! of blocking the registry.

pub mod catalog;
pub mod model;
pub mod protocol;
pub mod state;

use std::collections::{HashMap, HashSet, VecDeque};
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};

use serde_json::{Map, Value};
use tokio::sync::{mpsc, watch, Mutex, Notify};

use crate::folders::IndexState;
use crate::guest::{GrantStatus, GuestStore, GUEST_TTL_SECONDS};
use crate::users::UserDirectory;

// ── wire constants (parent #75 §3.5 initial values) ───────────────────────

pub const HEARTBEAT_INTERVAL: f64 = 5.0;
pub const NO_RESPONSE_TIMEOUT: f64 = 15.0;
pub const CODE_TTL_SECONDS: f64 = 300.0;
pub const RESUME_WINDOW_SECONDS: f64 = 300.0;
pub const MATCH_WINDOW_SECONDS: f64 = 120.0;
pub const LOAD_TIMEOUT_SECONDS: f64 = 120.0;
pub const SEND_QUEUE_MAX: usize = 64;
/// Bulk (manifest-chunk) traffic bypasses the bounded frame queue but stays
/// bounded in bytes: two full manifests is the most one link may buffer.
pub const SEND_BULK_MAX_BYTES: usize = 64 * 1024 * 1024;
pub const CODE_DIGITS: usize = 6;

pub const WS_CLOSE_NORMAL: u16 = 1000;
pub const WS_CLOSE_POLICY: u16 = 1008;
pub const WS_CLOSE_TOO_BIG: u16 = 1009;
#[allow(dead_code)]
pub const WS_CLOSE_GOING_AWAY: u16 = 1001;

// ── REJECT reasons (machine-readable; stable for clients) ──────────────────

pub const REASON_MALFORMED: &str = "malformed";
pub const REASON_NO_SESSION: &str = "no_session";
pub const REASON_WRONG_SESSION: &str = "wrong_session";
pub const REASON_ALREADY_BOUND: &str = "already_bound";
pub const REASON_NOT_PAIRED: &str = "not_paired";
pub const REASON_NOT_LEADER: &str = "not_leader";
pub const REASON_NO_PENDING: &str = "no_pending";
pub const REASON_BAD_CODE: &str = "bad_code";
pub const REASON_CODE_CONSUMED: &str = "code_consumed";
pub const REASON_CODE_EXPIRED: &str = "code_expired";
pub const REASON_BAD_RESUME: &str = "bad_resume";
pub const REASON_RESUME_EXPIRED: &str = "resume_expired";
pub const REASON_RESUME_IN_USE: &str = "resume_in_use";
pub const REASON_PEER_OFFLINE: &str = "peer_offline";
/// The op is well-formed but not permitted for this link — e.g. a
/// guest-authenticated link trying the 6-digit-code JOIN path.
pub const REASON_FORBIDDEN: &str = "forbidden";
/// Generic server-side failure (e.g. entropy source unavailable) — the op
/// is rejected rather than run with degraded security properties.
pub const REASON_INTERNAL: &str = "internal_error";
/// A resume token was presented on a link whose authentication class no
/// longer matches the trust class fixed when the member was admitted
/// (guest bearer vs member session/bearer). This is terminal for that
/// credential on that link: retrying the same RESUME cannot succeed.
pub const REASON_TRUST_MISMATCH: &str = "trust_mismatch";
/// `payload.origin` appeared on a frame outside the sequenced data-plane
/// relay whitelist, or did not resolve to a session member role.
pub const REASON_BAD_ORIGIN: &str = "bad_origin";

// ── member trust / capabilities ────────────────────────────────────────────

pub const TRUST_MEMBER: &str = "member";
pub const TRUST_GUEST: &str = "guest";
/// Phase-1 member-drive capability. The exact string is wire-visible; it is
/// deliberately versioned independently of the transport PROTOCOL_VERSION
/// (which remains v1 because unknown fields are additive).
pub const CAP_MEMBER_DRIVE: &str = "member-drive-v1";

// ── END reasons ────────────────────────────────────────────────────────────

pub const END_REJECTED: &str = "rejected";
pub const END_LEFT: &str = "left";
pub const END_CODE_EXPIRED: &str = "code_expired";
pub const END_RESUME_EXPIRED: &str = "resume_expired";
/// `POST /api/v1/pair-reset` (#405) — the test hook ends every live session
/// with this reason so a stale link-drop session can't hijack the next MATCH.
pub const END_RESET: &str = "reset";
#[allow(dead_code)]
pub const END_SHUTDOWN: &str = "shutdown";
/// The session's leader was disabled/removed — their guest grants and the
/// pair session itself die together.
pub const END_OWNER_DISABLED: &str = "owner_disabled";
/// The load-op tombstone budget ran out — almost always a flood or a
/// client stuck in a retry loop. The session dies rather than choosing
/// between unbounded memory and silently forgetting replay protection.
pub const END_DEDUP_EXHAUSTED: &str = "dedup_exhausted";

// dedup retention: full outcomes for the last DEDUP_WINDOW keys, evicted
// keys remembered as tombstones. Ordinary tombstones are FIFO-capped;
// load-op tombstones are budgeted separately — OPEN_WORK is the one op
// that skips the epoch gate, so losing one would make a replay executable
// again. The load budget is a hard cap, not a FIFO: hitting it condemns
// the session (see dedup_tombstone), because silently aging entries out
// would trade correctness for memory and honest sessions never come
// close — a work pick is a human-scale action.
pub const DEDUP_WINDOW: usize = 256;
pub const DEDUP_TOMBSTONE_MAX: usize = 8192;
pub const DEDUP_LOAD_TOMBSTONE_MAX: usize = 1024;

/// Failed-JOIN throttle per link: past `JOIN_FAIL_MAX` rejects inside the
/// window the transport is closed (6-digit codes are brute-forceable —
/// a persistent socket must not get unlimited guesses).
pub const JOIN_FAIL_MAX: u32 = 8;
pub const JOIN_FAIL_WINDOW_SEC: f64 = 60.0;

/// UI_SYNC rate cap per member — excess frames drop silently so a buggy
/// sender can't fill the peer's bounded outq.
pub const UI_SYNC_WINDOW_SEC: f64 = 1.0;
pub const UI_SYNC_RATE_MAX: u32 = 50;

// ── opaque ids / tokens / codes ──────────────────────────────────────────
//
// Two entropy grades on purpose:
// - `rand_bytes` (non-bearer ids): /dev/urandom, falling back to time+PID
//   when the device is missing. Member/session ids are never secrets —
//   they're always paired with a resume_token or match_token.
// - `secure_bytes` (bearer material): /dev/urandom or NOTHING. Tokens and
//   join codes are secrets; the ~32-bit-guessable fallback must never feed
//   them (a guest token minted from it would be brute-forceable), so every
//   caller gets `Option` and must fail closed.

fn rand_bytes(n: usize) -> Vec<u8> {
    use std::io::Read;
    let mut b = vec![0u8; n];
    if let Ok(mut f) = std::fs::File::open("/dev/urandom") {
        if f.read_exact(&mut b).is_ok() {
            return b;
        }
    }
    // Last-resort entropy (paranoia; /dev/urandom exists on every host we run on).
    let t = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    for (i, x) in b.iter_mut().enumerate() {
        *x = (t
            .subsec_nanos()
            .wrapping_add(t.as_secs() as u32)
            .wrapping_add(std::process::id())
            >> ((i % 8) * 4)) as u8
            ^ (i * 131) as u8;
    }
    b
}

#[cfg(test)]
thread_local! {
    /// Test-only fault injection — flips `secure_bytes` to its failure
    /// branch so callers' fail-closed paths are exercised.
    static ENTROPY_FAIL: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
}

#[cfg(test)]
pub(crate) fn with_entropy_failure<R>(f: impl FnOnce() -> R) -> R {
    ENTROPY_FAIL.with(|c| c.set(true));
    let r = f();
    ENTROPY_FAIL.with(|c| c.set(false));
    r
}

fn secure_bytes(n: usize) -> Option<Vec<u8>> {
    #[cfg(test)]
    if ENTROPY_FAIL.with(|f| f.get()) {
        return None;
    }
    use std::io::Read;
    let mut b = vec![0u8; n];
    std::fs::File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut b))
        .ok()?;
    Some(b)
}

fn new_id() -> String {
    rand_bytes(8).iter().map(|b| format!("{b:02x}")).collect()
}

pub(crate) fn new_token() -> Option<String> {
    use base64::Engine;
    Some(
        base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(secure_bytes(24)?),
    )
}

fn new_code() -> Option<String> {
    // secrets.randbelow(1_000_000) — rejection sampling, no modulo bias.
    loop {
        let b = secure_bytes(4)?;
        let v = u32::from_be_bytes([b[0], b[1], b[2], b[3]]);
        if v < u32::MAX - u32::MAX % 1_000_000 {
            return Some(format!("{:0width$}", v % 1_000_000, width = CODE_DIGITS));
        }
    }
}

// ── outbound plumbing ──────────────────────────────────────────────────────

/// Items the drain task pulls: control frames, the bulk-block sentinel and
/// an ordered transport-close. `Close` rides the queue (frames ahead of it
/// — e.g. END — reach the client first); the watch-channel close remains
/// the bypassing path for policy closes.
pub enum OutItem {
    Text(String),
    Bulk,
    Close(u16, String),
}

pub struct BulkQ {
    pub q: VecDeque<String>,
    pub bytes: usize,
    pub pending: bool,
}

impl BulkQ {
    /// Drain-side pop — decrements the byte counter.
    pub(crate) fn pop(&mut self) -> Option<String> {
        let r = self.q.pop_front()?;
        self.bytes -= r.len();
        Some(r)
    }
}

enum SendOutcome {
    Sent,
    Inactive,
    Full,
    BulkFull,
}

/// One transport attachment. `outq` is the bounded frame queue; `bulk`
/// carries manifest chunks (byte-bounded); `close` is the transport hook.
pub struct Link {
    pub id: String,
    outq: mpsc::Sender<OutItem>,
    pub bulk: Arc<std::sync::Mutex<BulkQ>>,
    pub member_id: Option<String>,
    pub alive: bool,
    pub last_rx: Instant,
    pub next_ping_due: Instant,
    pub ping_seq: u64,
    pub user: Option<Value>,
    /// The `bookq_guest` bearer this link authenticated with — `Some` iff
    /// the link is a guest (read-only follower only: CREATE/MATCH are
    /// rejected in `dispatch` so a guest can never lead a session, since
    /// leading would mint a fresh guest grant bound to their own session,
    /// surviving the leader's disconnect). Re-validated every `tick` so a
    /// revoked/expired grant drops its socket instead of coasting on the
    /// handshake-time snapshot.
    pub guest_token: Option<String>,
    /// Failed-JOIN throttle: brute-forcing 6-digit codes (or fishing for
    /// live grants) rides through reject frames, so a link that keeps
    /// missing gets its transport closed.
    join_fails: u32,
    join_fails_since: Instant,
    /// An ordered `OutItem::Close` is already queued — the link is
    /// terminating and the frames ahead of it (END etc.) must flush
    /// first. `tick` must not let a bypassing watch close race ahead and
    /// truncate them.
    close_queued: bool,
    /// A close that could not be queued yet (queue full) — parked intent
    /// retried by `tick` as the queue drains. It must NEVER fall back to
    /// the bypassing watch close: the bypass truncates exactly the
    /// frames (e.g. END) this ordering exists to protect.
    pending_close: Option<(u16, String, Instant)>,
    close: watch::Sender<Option<(u16, String)>>,
}

impl Link {
    pub fn new(
        outq: mpsc::Sender<OutItem>,
        close: watch::Sender<Option<(u16, String)>>,
        user: Option<Value>,
        guest_token: Option<String>,
        now: Instant,
        heartbeat: Duration,
    ) -> Self {
        Self {
            id: new_id(),
            outq,
            bulk: Arc::new(std::sync::Mutex::new(BulkQ {
                q: VecDeque::new(),
                bytes: 0,
                pending: false,
            })),
            member_id: None,
            alive: true,
            last_rx: now,
            next_ping_due: now + heartbeat,
            ping_seq: 0,
            user,
            guest_token,
            join_fails: 0,
            join_fails_since: now,
            close_queued: false,
            pending_close: None,
            close,
        }
    }

    /// Guest link ⇔ carrying a `bookq_guest` bearer — one field, no
    /// second truth to keep in step.
    pub fn is_guest(&self) -> bool {
        self.guest_token.is_some()
    }

    /// `user` JSON field as `&str` — the ws handshake payload's shape
    /// ({email,name,picture}) is a wire contract, not a typed struct.
    pub fn user_str(&self, key: &str) -> Option<&str> {
        self.user.as_ref()?.get(key)?.as_str()
    }

    /// Queue one frame; overflow means the caller must close the link.
    fn queue_obj(&mut self, obj: &Value) -> SendOutcome {
        if !self.alive {
            return SendOutcome::Inactive;
        }
        let raw = match protocol::encode_frame(obj) {
            Ok(r) => r,
            Err(_) => {
                tracing::warn!("reader-sync link={} drop oversized outbound frame", self.id);
                return SendOutcome::Inactive;
            }
        };
        match self.outq.try_send(OutItem::Text(raw)) {
            Ok(()) => SendOutcome::Sent,
            Err(mpsc::error::TrySendError::Full(_)) => SendOutcome::Full,
            // Receiver gone → drain ended → the transport is already
            // dead; nothing to deliver and nothing to close.
            Err(mpsc::error::TrySendError::Closed(_)) => SendOutcome::Inactive,
        }
    }

    /// Queue a run of frames (manifest chunks) as one bulk block.
    fn queue_bulk(&mut self, objs: &[Value], bulk_max: usize) -> SendOutcome {
        if !self.alive {
            return SendOutcome::Inactive;
        }
        let mut raws = Vec::with_capacity(objs.len());
        for o in objs {
            match protocol::encode_frame(o) {
                Ok(r) => raws.push(r),
                Err(_) => {
                    tracing::warn!("reader-sync link={} drop oversized bulk frame", self.id);
                    return SendOutcome::Inactive;
                }
            }
        }
        if raws.is_empty() {
            return SendOutcome::Sent;
        }
        let size: usize = raws.iter().map(|r| r.len()).sum();
        let n = raws.len();
        {
            let mut b = self.bulk.lock().unwrap();
            if b.bytes + size > bulk_max {
                return SendOutcome::BulkFull;
            }
            b.q.extend(raws);
            b.bytes += size;
            if b.pending {
                return SendOutcome::Sent;
            }
        }
        match self.outq.try_send(OutItem::Bulk) {
            Ok(()) => {
                self.bulk.lock().unwrap().pending = true;
                SendOutcome::Sent
            }
            Err(e) => {
                // Marker failed — roll back only the just-enqueued block.
                let mut b = self.bulk.lock().unwrap();
                for _ in 0..n {
                    if let Some(r) = b.q.pop_back() {
                        b.bytes -= r.len();
                    }
                }
                match e {
                    mpsc::error::TrySendError::Full(_) => SendOutcome::Full,
                    mpsc::error::TrySendError::Closed(_) => {
                        SendOutcome::Inactive
                    }
                }
            }
        }
    }

    /// Transport hook — ask the drain task to close the socket. First close
    /// wins (py `_close_started`); a second close never overwrites the code.
    fn request_transport_close(&self, code: u16, reason: &str) {
        if self.close.borrow().is_none() {
            let _ = self.close.send_replace(Some((code, reason.to_string())));
        }
    }

    /// Ordered close — queued behind frames already enqueued (END reaches
    /// the client before the socket dies). A full queue must NOT fall
    /// back to the bypassing watch close: the bypass truncates exactly
    /// the frames this ordering exists to protect. The intent is parked
    /// in `pending_close` and retried by `tick` until it lands or the
    /// queue proves wedged; `Closed` means the drain (and with it the
    /// transport) is already gone, so nothing needs doing.
    /// First close wins like `request_transport_close` — a second Close
    /// on the wire can only contradict the first's code/reason.
    fn queue_transport_close(&mut self, code: u16, reason: &str) {
        if self.close_queued {
            return;
        }
        match self
            .outq
            .try_send(OutItem::Close(code, reason.to_string()))
        {
            Ok(()) => self.close_queued = true,
            Err(mpsc::error::TrySendError::Full(_)) => {
                self.close_queued = true;
                self.pending_close =
                    Some((code, reason.to_string(), Instant::now()));
            }
            Err(mpsc::error::TrySendError::Closed(_)) => {}
        }
    }
}

// ── members / sessions ─────────────────────────────────────────────────────

#[derive(Debug)]
pub struct Member {
    pub id: String,
    pub role: String, // "leader" | "follower"
    pub name: String,
    pub resume_token: String,
    pub session_id: Option<String>,
    pub link_id: Option<String>,
    pub status: String, // active | suspended | offline | pending
    /// Trust class fixed at admission. Unlike `role` this never changes for
    /// the lifetime of the member credential; RESUME re-checks the link's
    /// authentication class against it instead of re-deriving it.
    pub trust: String, // member | guest
    /// Feature tokens advertised by the client runtime that most recently
    /// admitted/resumed this member. Capabilities are runtime state (not
    /// part of trust) and may change across RESUME.
    pub capabilities: Vec<String>,
    pub offline_since: Option<Instant>,
    pub dc_state: String, // up | down
    pub last_applied: Option<Value>,
    pub ui_sync_window_start: Instant,
    pub ui_sync_count: u32,
}

impl Member {
    /// `None` when the entropy source is gone — the resume_token is bearer
    /// material and must never come out degraded.
    fn new(
        role: &str,
        name: String,
        session_id: &str,
        link_id: &str,
        status: &str,
        trust: &str,
        capabilities: Vec<String>,
    ) -> Option<Self> {
        Some(Self {
            id: new_id(),
            role: role.to_string(),
            name,
            resume_token: new_token()?,
            session_id: Some(session_id.to_string()),
            link_id: Some(link_id.to_string()),
            status: status.to_string(),
            trust: trust.to_string(),
            capabilities,
            offline_since: None,
            dc_state: "down".into(),
            last_applied: None,
            // py default 0.0 (monotonic epoch): the first UI_SYNC always
            // sees elapsed >= the window and starts a fresh quota window.
            ui_sync_window_start: Instant::now()
                .checked_sub(Duration::from_secs_f64(UI_SYNC_WINDOW_SEC + 1.0))
                .unwrap_or_else(Instant::now),
            ui_sync_count: 0,
        })
    }
}

#[derive(Debug, Clone)]
pub struct LoadJob {
    pub seq: i64,
    #[allow(dead_code)]
    pub kind: String,
    pub folder: String,
    pub page: Value,
    pub offset: Value,
    pub request_id: String,
    #[allow(dead_code)]
    pub role: String,
    /// Bootstrap-generated load. An automatic load is best-effort only and
    /// must never displace a deliberate (non-auto) pick.
    pub auto: bool,
    /// The ACK captured when this load was accepted. The dedup window may
    /// evict its copy while the asynchronous resolver is still running, but
    /// the epoch commit must remain able to carry the trigger outcome.
    pub ack_frame: Option<Value>,
}

#[derive(Debug)]
pub struct PairSession {
    pub id: String,
    pub code: String,
    pub code_expires_at: Instant,
    pub created_at: Instant,
    /// 'create' sessions are reserved for the code holder; 'match' sessions
    /// are open one-tap waiters and the only MATCH candidates.
    pub origin: String,
    pub match_token: String,
    pub code_consumed: bool,
    pub members: Vec<Member>,
    pub pending: Option<Member>,
    pub revision: i64,
    pub work_epoch: i64,
    pub manifest_version: i64,
    pub phase: String,
    pub spread_id: Option<String>,
    pub offset: i64,
    pub leader_side: String,
    pub data_plane: String, // server-ws | webrtc-dc
    pub manifest: Option<Value>,
    pub plan: Option<Vec<model::Spread>>,
    pub ready: HashSet<String>,
    pub dedup: HashMap<String, Value>,
    dedup_order: VecDeque<String>,
    pub dedup_seen: HashSet<String>,
    dedup_seen_order: VecDeque<String>,
    /// Tombstones for OPEN_WORK outcomes, hard-capped at
    /// DEDUP_LOAD_TOMBSTONE_MAX. OPEN_WORK is the one op that skips the
    /// epoch gate, so a forgotten load tombstone turns a replay into
    /// executable state again — entries never age out while the session
    /// lives. Overflow condemns the session instead.
    pub dedup_load_seen: HashSet<String>,
    /// Set when the load-op tombstone budget is exceeded. The session is
    /// torn down after the current dispatch/completion unwinds — never
    /// mid-mutation — so replay protection is never silently dropped and
    /// memory stays bounded.
    pub condemned: bool,
    pub load_seq: i64,
    pub pending_load: Option<LoadJob>,
    /// Whether the currently committed work load came from bootstrap. Once a
    /// deliberate (non-auto) load has committed, later automatic bootstrap
    /// loads are stale by definition and must be rejected.
    pub committed_load_auto: Option<bool>,
    pub browse_open: bool,
    /// First opener of the current shared-browse generation. Simultaneous
    /// opens are serialized by the server; only a closed-to-open transition may
    /// set this, so duplicate opens never overwrite the winner.
    pub browse_opener_id: Option<String>,
    pub browse_generation: i64,
    pub apply_seq: i64,
    pub apply_log: Vec<Value>,
}

impl PairSession {
    /// `None` when the entropy source is gone — match_token is bearer
    /// material and must never come out degraded.
    fn new(origin: &str, code_ttl: Duration) -> Option<Self> {
        let now = Instant::now();
        Some(Self {
            id: new_id(),
            code: String::new(),
            code_expires_at: now + code_ttl,
            created_at: now,
            origin: origin.to_string(),
            match_token: new_token()?,
            code_consumed: false,
            members: Vec::new(),
            pending: None,
            revision: 0,
            work_epoch: 0,
            manifest_version: 0,
            phase: "waiting".into(),
            spread_id: None,
            offset: 0,
            leader_side: "left".into(),
            data_plane: "server-ws".into(),
            manifest: None,
            plan: None,
            ready: HashSet::new(),
            dedup: HashMap::new(),
            dedup_order: VecDeque::new(),
            dedup_seen: HashSet::new(),
            dedup_seen_order: VecDeque::new(),
            dedup_load_seen: HashSet::new(),
            condemned: false,
            load_seq: 0,
            pending_load: None,
            committed_load_auto: None,
            browse_open: false,
            browse_opener_id: None,
            browse_generation: 0,
            apply_seq: 0,
            apply_log: Vec::new(),
        })
    }

    pub fn paired(&self) -> bool {
        self.pending.is_none() && self.members.len() == 2
    }

    fn leader(&self) -> Option<&Member> {
        self.members.iter().find(|m| m.role == "leader")
    }
}

// ── registry ───────────────────────────────────────────────────────────────

/// All pairing/session state. Single process owner.
pub struct Registry {
    pub inner: Mutex<Inner>,
    pub wake: Arc<Notify>,
    /// Guest-access grants (QR-link bearer tokens). Same clone sits in
    /// `Inner` for session-lifecycle hooks; this field is the synchronous
    /// accessor auth/redeem paths use.
    guest: GuestStore,
    pub index: Arc<IndexState>,
    pub library: PathBuf,
    pub manifest_cache: Mutex<catalog::ManifestCache>,
    /// Test-only work-resolver override (golden-frame harness, #210):
    /// replaces `resolve_work` wholesale, like py's `work_resolver` param.
    #[cfg(test)]
    test_resolver: std::sync::Mutex<Option<TestResolver>>,
    heartbeat_interval: Duration,
    code_ttl: Duration,
    match_window: Duration,
    load_timeout: Duration,
    send_queue_max: usize,
}

#[cfg(test)]
pub type TestResolver =
    Arc<dyn Fn(&LoadJob) -> Result<Value, catalog::ManifestError> + Send + Sync>;

pub struct Inner {
    sessions: Vec<PairSession>, // insertion-ordered — presence/member order is wire-visible
    links: HashMap<String, Link>,
    by_code: HashMap<String, String>, // code -> session id
    guest: GuestStore,
    /// Membership snapshot for the tick's guest-grant revalidation — `None`
    /// in tests/harnesses that don't wire the user store.
    directory: Option<Arc<UserDirectory>>,
    bulk_max: usize,
    no_response_timeout: Duration,
    resume_window: Duration,
    /// Operator kill switch for Phase-1 member-drive semantics. Capability
    /// intersection is still reflected, but the server never treats it as
    /// enabled when this is false.
    member_drive_enabled: bool,
    wake: Arc<Notify>,
}

fn member_drive_enabled_from_env() -> bool {
    !matches!(
        std::env::var("BOOKQ_PAIR_MEMBER_DRIVE")
            .unwrap_or_default()
            .to_ascii_lowercase()
            .as_str(),
        "0" | "false" | "off" | "no"
    )
}

impl Registry {
    pub fn new(
        index: Arc<IndexState>,
        library: PathBuf,
        directory: Option<Arc<UserDirectory>>,
    ) -> Arc<Self> {
        let wake = Arc::new(Notify::new());
        let guest = GuestStore::new();
        Arc::new(Self {
            inner: Mutex::new(Inner {
                sessions: Vec::new(),
                links: HashMap::new(),
                by_code: HashMap::new(),
                guest: guest.clone(),
                directory,
                bulk_max: SEND_BULK_MAX_BYTES,
                no_response_timeout: Duration::from_secs_f64(NO_RESPONSE_TIMEOUT),
                resume_window: Duration::from_secs_f64(RESUME_WINDOW_SECONDS),
                member_drive_enabled: member_drive_enabled_from_env(),
                wake: wake.clone(),
            }),
            wake,
            guest,
            index,
            library,
            manifest_cache: Mutex::new(catalog::ManifestCache::new()),
            #[cfg(test)]
            test_resolver: std::sync::Mutex::new(None),
            heartbeat_interval: Duration::from_secs_f64(HEARTBEAT_INTERVAL),
            code_ttl: Duration::from_secs_f64(CODE_TTL_SECONDS),
            match_window: Duration::from_secs_f64(MATCH_WINDOW_SECONDS),
            load_timeout: Duration::from_secs_f64(LOAD_TIMEOUT_SECONDS),
            send_queue_max: SEND_QUEUE_MAX,
        })
    }

    pub fn send_queue_max(&self) -> usize {
        self.send_queue_max
    }

    /// Synchronous guest-grant lookup for the auth/redeem paths — the
    /// store's own mutex means no `inner` lock is needed.
    pub fn guest(&self) -> &GuestStore {
        &self.guest
    }
    #[cfg(test)]
    pub fn set_test_resolver(&self, f: TestResolver) {
        *self.test_resolver.lock().unwrap() = Some(f);
    }
    pub fn heartbeat_interval(&self) -> Duration {
        self.heartbeat_interval
    }

    /// One inbound frame — stamps liveness, size-caps, routes heartbeats
    /// early, then full request validation + dispatch.
    pub async fn handle_raw(self: &Arc<Self>, link_id: &str, raw: &[u8]) {
        let mut g = self.inner.lock().await;
        g.handle_raw(self, link_id, raw);
    }

    pub async fn detach_link(&self, link_id: &str) {
        let mut g = self.inner.lock().await;
        g.detach_link(link_id);
    }

    /// End every session led by `owner_email`, revoke their guest grants
    /// AND drop the member's own sockets — the member-disable/remove hook.
    /// Membership is only checked at handshake time, so without the link
    /// close a disabled member could keep operating on an existing socket
    /// (CREATE → mint fresh guest invites). A re-enable can never
    /// resurrect a live bearer grant.
    pub async fn end_owner_sessions(&self, owner_email: &str) -> usize {
        let mut g = self.inner.lock().await;
        g.guest.revoke_owner(owner_email);
        let sids: Vec<String> = g
            .sessions
            .iter()
            .filter(|s| {
                s.members
                    .iter()
                    .find(|m| m.role == "leader")
                    .and_then(|m| m.link_id.as_ref())
                    .and_then(|lid| g.links.get(lid))
                    .and_then(|l| l.user_str("email"))
                    .is_some_and(|e| e.eq_ignore_ascii_case(owner_email))
            })
            .map(|s| s.id.clone())
            .collect();
        let n = sids.len();
        // END frames queue first — the member's own sockets close behind
        // them (ordered), not via the bypassing watch close that would
        // truncate END before it flushes. A session whose leader link is
        // already detached (offline inside the resume window) doesn't
        // match the filter — safe anyway: `revoke_owner` above already
        // killed its grants, and the tick's owner-membership re-check
        // drops the guest sockets regardless.
        for sid in sids {
            g.end_session(&sid, END_OWNER_DISABLED);
        }
        let owner_links: Vec<String> = g
            .links
            .iter()
            .filter(|(_, l)| {
                l.user_str("email")
                    .is_some_and(|e| e.eq_ignore_ascii_case(owner_email))
            })
            .map(|(id, _)| id.clone())
            .collect();
        for lid in owner_links {
            if let Some(mut link) = g.detach_link(&lid) {
                link.queue_transport_close(WS_CLOSE_POLICY, "owner_disabled");
            }
        }
        n
    }

    /// End every live session with `reason`, returning the count — the
    /// native `POST /api/v1/pair-reset` (#405). A simulator client that went
    /// idle by link-drop never LEAVEd, so its server-side session lingers in
    /// the resume window and would hijack the next MATCH.
    pub async fn end_all(&self, reason: &str) -> usize {
        let sids: Vec<String> = {
            self.inner
                .lock()
                .await
                .sessions
                .iter()
                .map(|s| s.id.clone())
                .collect()
        };
        let n = sids.len();
        for sid in sids {
            self.inner.lock().await.end_session(&sid, reason);
        }
        n
    }

    /// Live session count (gateway tests assert reset empties the registry).
    #[cfg(test)]
    pub async fn session_count(&self) -> usize {
        self.inner.lock().await.sessions.len()
    }

    /// Ticker task — runs every time-driven transition.
    pub async fn run_ticker(self: &Arc<Self>) {
        loop {
            {
                let mut g = self.inner.lock().await;
                g.tick();
            }
            let delay = {
                let g = self.inner.lock().await;
                g.next_wakeup_delay()
            };
            tokio::select! {
                _ = self.wake.notified() => {}
                _ = tokio::time::sleep(delay) => {}
            }
        }
    }

    /// The work resolver — catalog.resolve_work over the shared index.
    /// Always wired in the Rust build (py gates on work_resolver=None).
    async fn resolve_work(
        self: &Arc<Self>,
        job: &LoadJob,
    ) -> Result<Value, catalog::ManifestError> {
        #[cfg(test)]
        if let Some(f) = self.test_resolver.lock().unwrap().clone() {
            return f(job);
        }
        let (folder, pref) =
            catalog::validate_args(&Value::String(job.folder.clone()), &job.page, &job.offset)?;
        let cache =
            match crate::folders::ensure_tree_and_index_wait(&self.index, &self.library).await {
                Some(c) => c,
                None => {
                    return Err(catalog::ManifestError {
                        code: catalog::ERR_MALFORMED,
                        message: "metadata index build failed".into(),
                    })
                }
            };
        // validate_args already admitted this as 0|1 under py `==`
        // (bools never reach here); as_f64 keeps 1.0 on the 1-side — a
        // silent `as_i64().unwrap_or(0)` would flip the spread parity.
        let offset_i64 = job.offset.as_f64().unwrap_or(0.0) as i64;
        let key = (folder.clone(), offset_i64);
        let hit = {
            let mut mc = self.manifest_cache.lock().await;
            mc.reset_for(&cache);
            mc.items.get(&key).cloned()
        };
        let core = match hit {
            Some(c) => c,
            None => {
                let library = self.library.clone();
                // #322: move the Arc'd cache into the task — cloning
                // `cache.tree` used to deep-copy the whole node DOM per
                // manifest build.
                let cache_c = cache.clone();
                let folder_c = folder.clone();
                let built = tokio::task::spawn_blocking(move || {
                    catalog::build_manifest_core(&library, &cache_c.tree, &folder_c, offset_i64)
                })
                .await;
                let built = match built {
                    Ok(Ok(v)) => v,
                    Ok(Err(e)) => return Err(e),
                    Err(_) => {
                        return Err(catalog::ManifestError {
                            code: catalog::ERR_MALFORMED,
                            message: "manifest build task failed".into(),
                        })
                    }
                };
                let mut mc = self.manifest_cache.lock().await;
                // The index may have rotated while the build ran — keep
                // the entry only when the tree identity is still current.
                mc.reset_for(&cache);
                mc.items.insert(key, built.clone());
                built
            }
        };
        catalog::finish_resolve(core, &pref)
    }
}

// ── Inner: all dispatch is synchronous under the registry lock ─────────────

/// Capability advertisement parser for admission/resume payloads. Validation
/// has already bounded the shape; unknown feature tokens are deliberately
/// preserved so a newer peer can be reflected verbatim.
fn payload_features(payload: &Value) -> Vec<String> {
    let mut out: Vec<String> = payload
        .get("features")
        .and_then(|v| v.as_array())
        .map(|items| {
            items
                .iter()
                .filter_map(|v| v.as_str().map(str::to_string))
                .collect()
        })
        .unwrap_or_default();
    out.sort();
    out.dedup();
    out
}

fn link_trust(link: &Link) -> &'static str {
    if link.is_guest() {
        TRUST_GUEST
    } else {
        TRUST_MEMBER
    }
}

impl Inner {
    fn server_features(&self) -> Vec<String> {
        if self.member_drive_enabled {
            vec![CAP_MEMBER_DRIVE.to_string()]
        } else {
            Vec::new()
        }
    }

    fn server_features_json(&self) -> Value {
        Value::Array(
            self.server_features()
                .into_iter()
                .map(Value::String)
                .collect(),
        )
    }

    /// Phase-1 member-drive semantics apply only when the deployment allows
    /// them AND both admitted members currently advertise the capability.
    /// Pending/offline members keep their last advertised runtime capability:
    /// a temporary disconnect must not silently downgrade pair semantics.
    fn member_drive_intersection(&self, s: &PairSession) -> bool {
        self.member_drive_enabled
            && s.members.len() == 2
            && s.members
                .iter()
                .all(|m| m.capabilities.iter().any(|f| f == CAP_MEMBER_DRIVE))
    }

    fn session_mut(&mut self, sid: &str) -> Option<&mut PairSession> {
        self.sessions.iter_mut().find(|s| s.id == sid)
    }
    fn session(&self, sid: &str) -> Option<&PairSession> {
        self.sessions.iter().find(|s| s.id == sid)
    }

    /// `link.send_obj` — encode + bounded enqueue; overflow closes the peer.
    fn send_obj(&mut self, link_id: &str, obj: &Value) -> bool {
        let Some(link) = self.links.get_mut(link_id) else {
            return false;
        };
        match link.queue_obj(obj) {
            SendOutcome::Sent => true,
            SendOutcome::Inactive => false,
            SendOutcome::Full | SendOutcome::BulkFull => {
                self.close_link(link_id, "send_queue_full", WS_CLOSE_POLICY);
                false
            }
        }
    }

    /// `link.send_objs_bulk` — manifest chunks ride the byte-bounded bulkq.
    fn send_objs_bulk(&mut self, link_id: &str, objs: &[Value]) -> bool {
        let Some(link) = self.links.get_mut(link_id) else {
            return false;
        };
        match link.queue_bulk(objs, self.bulk_max) {
            SendOutcome::Sent => true,
            SendOutcome::Inactive => false,
            SendOutcome::BulkFull => {
                self.close_link(link_id, "bulk_queue_full", WS_CLOSE_POLICY);
                false
            }
            SendOutcome::Full => {
                self.close_link(link_id, "send_queue_full", WS_CLOSE_POLICY);
                false
            }
        }
    }

    // ── link lifecycle ───────────────────────────────────────────────

    /// Attach a fresh transport; greets it with HELLO.
    pub fn register_link(&mut self, mut link: Link, heartbeat: Duration) -> String {
        link.last_rx = Instant::now();
        link.next_ping_due = link.last_rx + heartbeat;
        let id = link.id.clone();
        self.links.insert(id.clone(), link);
        let hello = protocol::frame(&[
            ("v", Value::from(protocol::PROTOCOL_VERSION)),
            ("type", Value::String("HELLO".into())),
            ("link", Value::String(id.clone())),
            ("features", self.server_features_json()),
        ]);
        self.send_obj(&id, &hello);
        self.wake.notify_one();
        id
    }

    /// Transport reports the socket is gone; returns the popped link so the
    /// caller can still signal its transport. Membership is kept for the
    /// resume window (approved member → offline) unless it was only a
    /// pending candidate (dropped, leader notified).
    pub fn detach_link(&mut self, link_id: &str) -> Option<Link> {
        let Some(mut link) = self.links.remove(link_id) else {
            return None;
        };
        link.alive = false;
        let member_id = link.member_id.take();
        let Some(mid) = member_id else {
            tracing::debug!("reader-sync link={} detached (anonymous)", link_id);
            return Some(link);
        };
        // Locate the member's session (approved → members[]; pending → .pending).
        let found = self.sessions.iter().find_map(|s| {
            if s.members.iter().any(|m| m.id == mid) {
                Some((s.id.clone(), false))
            } else if s.pending.as_ref().is_some_and(|p| p.id == mid) {
                Some((s.id.clone(), true))
            } else {
                None
            }
        });
        let Some((sid, is_pending)) = found else {
            return Some(link);
        };
        if is_pending {
            self.drop_pending(&sid, &mid);
            tracing::debug!(
                "reader-sync link={} pending candidate lost member={}",
                link_id,
                mid
            );
            return Some(link);
        }
        let now = Instant::now();
        {
            let Some(s) = self.session_mut(&sid) else {
                return Some(link);
            };
            if let Some(m) = s.members.iter_mut().find(|m| m.id == mid) {
                m.link_id = None;
                m.status = "offline".into();
                m.offline_since = Some(now);
                // S05: must READY again after RESUME; its dc report is dead
                // with the link.
                s.ready.remove(&mid);
                m.dc_state = "down".into();
            }
        }
        let flipped = self.eval_data_plane(&sid);
        tracing::debug!("reader-sync link={} member={} offline", link_id, mid);
        self.broadcast_state(&sid);
        if flipped {
            self.broadcast_transport_state(&sid);
        }
        Some(link)
    }

    /// Registry-initiated close: detach membership + ask transport to end.
    pub fn close_link(&mut self, link_id: &str, reason: &str, code: u16) {
        let was_tracked = self.links.contains_key(link_id);
        let was_alive = self.links.get(link_id).is_some_and(|l| l.alive);
        if was_alive || was_tracked {
            if let Some(link) = self.detach_link(link_id) {
                link.request_transport_close(code, reason);
            }
            if was_tracked {
                tracing::debug!("reader-sync link={} closed reason={}", link_id, reason);
            }
        }
        self.wake.notify_one();
    }

    /// Remove an unapproved join candidate: leader notified, claim dropped,
    /// and `code_consumed` reset so the pairing code works for a retry.
    fn drop_pending(&mut self, sid: &str, member_id: &str) {
        let Some(s) = self.session_mut(sid) else {
            return;
        };
        if s.pending.as_ref().is_none_or(|p| p.id != member_id) {
            return;
        }
        if let Some(p) = s.pending.as_mut() {
            p.session_id = None;
        }
        s.pending = None;
        s.code_consumed = false;
        let leader_link = s.leader().and_then(|m| m.link_id.clone());
        if let Some(lid) = leader_link {
            let frame = protocol::frame(&[
                ("v", Value::from(protocol::PROTOCOL_VERSION)),
                ("type", Value::String("JOIN_CANCEL".into())),
                ("sessionId", Value::String(sid.into())),
                ("memberId", Value::String(member_id.into())),
            ]);
            self.send_obj(&lid, &frame);
        }
    }

    // ── inbound dispatch ─────────────────────────────────────────────

    /// One inbound frame. Stamps liveness, enforces the 64KiB cap via
    /// protocol::parse_frame, then routes PING/PONG early (bare-shape
    /// frames are legal for heartbeat) before full request validation.
    pub fn handle_raw(&mut self, reg: &Arc<Registry>, link_id: &str, raw: &[u8]) {
        let Some(link) = self.links.get_mut(link_id) else {
            return;
        };
        if !link.alive {
            return;
        }
        link.last_rx = Instant::now();
        let frame = match protocol::parse_frame(raw) {
            Ok(f) => f,
            Err(e) => {
                if e.code == protocol::ERR_SIZE {
                    self.close_link(link_id, "frame_too_big", WS_CLOSE_TOO_BIG);
                } else {
                    self.reject(link_id, None, None, REASON_MALFORMED, None);
                }
                return;
            }
        };
        let t = frame.get("type").and_then(|v| v.as_str());
        if t == Some("PONG") {
            return; // liveness stamp above is the whole job
        }
        if t == Some("PING") {
            // py: seq = frame.get("seq"); if seq is None and payload is a
            // dict: seq = payload.get("seq") — dict.get returns None for an
            // explicit null too, so the fallback fires for both absent and
            // null top-level seq.
            let mut seq = frame.get("seq").cloned().unwrap_or(Value::Null);
            if seq.is_null() {
                if let Some(v) = frame
                    .get("payload")
                    .and_then(|p| p.as_object())
                    .and_then(|p| p.get("seq"))
                {
                    seq = v.clone();
                }
            }
            let mut pong = protocol::frame(&[
                ("v", Value::from(protocol::PROTOCOL_VERSION)),
                ("type", Value::String("PONG".into())),
            ]);
            if !seq.is_null() {
                pong.as_object_mut().unwrap().insert("seq".into(), seq);
            }
            self.send_obj(link_id, &pong);
            return;
        }
        if let Err(_) = protocol::parse_request(&frame) {
            let rid = frame
                .get("requestId")
                .and_then(|v| v.as_str())
                .map(str::to_string);
            let rtype = t.map(str::to_string);
            self.reject(
                link_id,
                rid.as_deref(),
                rtype.as_deref(),
                REASON_MALFORMED,
                None,
            );
            return;
        }
        self.dispatch(reg, link_id, &frame);
    }

    fn dispatch(&mut self, reg: &Arc<Registry>, link_id: &str, req: &Map<String, Value>) {
        let t = req["type"].as_str().unwrap().to_string();
        // link.member (py): pending-drop leaves the member bound but
        // session-less — CREATE-family still answers already_bound, others
        // no_session, so membership must resolve independently of sessions.
        enum Bind {
            Unbound,
            Orphaned,
            Bound(String, String), // (member_id, session_id)
        }
        let bind = self
            .links
            .get(link_id)
            .and_then(|l| l.member_id.clone())
            .map(|mid| {
                self.sessions
                    .iter()
                    .find(|s| {
                        s.members.iter().any(|m| m.id == mid)
                            || s.pending.as_ref().is_some_and(|p| p.id == mid)
                    })
                    .map(|s| Bind::Bound(mid.clone(), s.id.clone()))
                    .unwrap_or(Bind::Orphaned)
            })
            .unwrap_or(Bind::Unbound);

        if matches!(t.as_str(), "CREATE" | "JOIN" | "RESUME" | "MATCH") {
            // Guest links never lead: CREATE (and MATCH's create
            // fallback) would mint a guest grant bound to the guest's
            // own session — surviving the leader's disconnect.
            if matches!(t.as_str(), "CREATE" | "MATCH")
                && self.links.get(link_id).is_some_and(|l| l.is_guest())
            {
                self.reject(link_id, req_id(req), Some(&t), REASON_FORBIDDEN, None);
                return;
            }
            if !matches!(bind, Bind::Unbound) {
                let rtype = t.clone();
                self.reject(
                    link_id,
                    req_id(req),
                    Some(&rtype),
                    REASON_ALREADY_BOUND,
                    None,
                );
                return;
            }
            match t.as_str() {
                "CREATE" => self.create(reg, link_id, req, "create"),
                "JOIN" => self.join(link_id, req),
                "MATCH" => self.match_(reg, link_id, req),
                _ => self.resume(link_id, req),
            }
            return;
        }

        // every other request requires a bound member of the addressed session
        let (mid, sid) = match bind {
            Bind::Bound(m, s) => (m, s),
            _ => {
                let rtype = t.clone();
                self.reject(link_id, req_id(req), Some(&rtype), REASON_NO_SESSION, None);
                return;
            }
        };
        if req["sessionId"].as_str() != Some(sid.as_str()) {
            let rtype = t.clone();
            self.reject(
                link_id,
                req_id(req),
                Some(&rtype),
                REASON_WRONG_SESSION,
                None,
            );
            return;
        }

        let is_pending = self
            .session(&sid)
            .and_then(|s| s.pending.as_ref())
            .is_some_and(|p| p.id == mid);
        if is_pending {
            // Unapproved candidate: no state flows to it; only bail out.
            if t == "LEAVE" {
                self.drop_pending(&sid, &mid);
                self.close_link(link_id, "pending_left", WS_CLOSE_NORMAL);
            } else {
                self.reject(link_id, req_id(req), Some(&t), REASON_NOT_PAIRED, None);
            }
            return;
        }

        // Relay type whitelist: `payload.origin` is meaningful only on the
        // sequenced data-plane ops handled by `sequenced_op`. A relayed
        // control frame must never inherit leader authority — reject it at
        // the boundary rather than silently treating it as a direct frame.
        let has_origin = req["payload"]
            .get("origin")
            .is_some_and(|v| !v.is_null());
        if has_origin && !state::SEQUENCED_TYPES.contains(&t.as_str()) {
            self.reject(link_id, req_id(req), Some(&t), REASON_BAD_ORIGIN, Some(&sid));
            return;
        }

        match t.as_str() {
            "LEAVE" => self.end_session(&sid, END_LEFT),
            "APPROVE" | "REJECT" => {
                self.approve_or_reject(link_id, req, &sid, &mid, t == "APPROVE")
            }
            "SET_ACTIVITY" => self.set_activity(link_id, req, &sid, &mid),
            "SIGNAL" => self.signal(link_id, req, &sid, &mid),
            "UI_SYNC" => self.ui_sync(link_id, req, &sid, &mid),
            "TRANSPORT_STATE" => self.transport_state(link_id, req, &sid, &mid),
            _ if state::SEQUENCED_TYPES.contains(&t.as_str()) => {
                if !self.session(&sid).is_some_and(|s| s.paired()) {
                    self.reject(link_id, req_id(req), Some(&t), REASON_NOT_PAIRED, None);
                } else {
                    self.sequenced_op(reg, link_id, req, &sid, &mid);
                }
            }
            _ => self.reject(link_id, req_id(req), Some(&t), REASON_MALFORMED, None),
        }
        // A condemned session is torn down only after the dispatch unwinds —
        // killing it mid-mutation would strand half-applied state. Whatever
        // this frame validly produced (ACK/REJECT) has already gone out;
        // the END frame is the final word.
        if self.session(&sid).is_some_and(|s| s.condemned) {
            self.end_session(&sid, END_DEDUP_EXHAUSTED);
        }
    }

    // ── session ops ──────────────────────────────────────────────────

    fn ack(
        &mut self,
        link_id: &str,
        req: &Map<String, Value>,
        data: Option<Value>,
        session_id: Option<&str>,
    ) {
        let frame = protocol::frame(&[
            ("v", Value::from(protocol::PROTOCOL_VERSION)),
            ("type", Value::String("ACK".into())),
            (
                "requestId",
                req.get("requestId").cloned().unwrap_or(Value::Null),
            ),
            ("request", req.get("type").cloned().unwrap_or(Value::Null)),
            (
                "sessionId",
                session_id
                    .map(|s| Value::String(s.into()))
                    .unwrap_or(Value::Null),
            ),
            ("data", data.unwrap_or(Value::Object(Map::new()))),
        ]);
        self.send_obj(link_id, &frame);
    }

    fn reject(
        &mut self,
        link_id: &str,
        request_id: Option<&str>,
        rtype: Option<&str>,
        reason: &str,
        session_id: Option<&str>,
    ) {
        let frame = protocol::frame(&[
            ("v", Value::from(protocol::PROTOCOL_VERSION)),
            ("type", Value::String("REJECT".into())),
            (
                "requestId",
                request_id
                    .map(|s| Value::String(s.into()))
                    .unwrap_or(Value::Null),
            ),
            (
                "request",
                rtype
                    .map(|s| Value::String(s.into()))
                    .unwrap_or(Value::Null),
            ),
            ("reason", Value::String(reason.into())),
            (
                "sessionId",
                session_id
                    .map(|s| Value::String(s.into()))
                    .unwrap_or(Value::Null),
            ),
        ]);
        self.send_obj(link_id, &frame);
    }

    fn create(
        &mut self,
        reg: &Arc<Registry>,
        link_id: &str,
        req: &Map<String, Value>,
        origin: &str,
    ) {
        // Every mint below is bearer material — entropy failure rejects the
        // request instead of landing a weak secret on the wire.
        let mut code = match new_code() {
            Some(c) => c,
            None => {
                self.reject(link_id, req_id(req), Some("CREATE"), REASON_INTERNAL, None);
                return;
            }
        };
        while self.by_code.contains_key(&code) {
            code = match new_code() {
                Some(c) => c,
                None => {
                    self.reject(link_id, req_id(req), Some("CREATE"), REASON_INTERNAL, None);
                    return;
                }
            };
        }
        let Some(mut session) = PairSession::new(origin, reg.code_ttl) else {
            self.reject(link_id, req_id(req), Some("CREATE"), REASON_INTERNAL, None);
            return;
        };
        session.code = code.clone();
        let sid = session.id.clone();
        let name = self
            .links
            .get(link_id)
            .and_then(|l| l.user_str("name"))
            .unwrap_or("")
            .to_string();
        let owner_email = self
            .links
            .get(link_id)
            .and_then(|l| l.user_str("email"))
            .map(|e| e.trim().to_lowercase())
            .filter(|e| !e.is_empty());
        let trust = self
            .links
            .get(link_id)
            .map(link_trust)
            .unwrap_or(TRUST_MEMBER);
        let capabilities = payload_features(&req["payload"]);
        let Some(member) = Member::new(
            "leader",
            name,
            &sid,
            link_id,
            "active",
            trust,
            capabilities,
        ) else {
            self.reject(link_id, req_id(req), Some("CREATE"), REASON_INTERNAL, None);
            return;
        };
        let mid = member.id.clone();
        let resume_token = member.resume_token.clone();
        let member_trust = member.trust.clone();
        session.members.push(member);
        // Guest-access grant for the QR link — bound to this session so
        // `end_session` revokes it no matter which teardown path fires.
        // `match`-origin sessions never asked for a QR (one-tap auto-pair
        // is their whole point), so minting a live bearer grant for them
        // would only widen the attack surface — `create` sessions only.
        let guest_token = match (origin, owner_email) {
            ("create", Some(email)) => match self
                .guest
                .issue(&email, &sid, Duration::from_secs(GUEST_TTL_SECONDS))
            {
                Some(t) => Some(t),
                None => {
                    self.reject(link_id, req_id(req), Some("CREATE"), REASON_INTERNAL, None);
                    return;
                }
            },
            _ => None,
        };
        self.sessions.push(session);
        self.by_code.insert(code.clone(), sid.clone());
        if let Some(l) = self.links.get_mut(link_id) {
            l.member_id = Some(mid.clone());
        }
        let mut data = vec![
            ("sessionId", Value::String(sid.clone())),
            ("code", Value::String(code)),
            ("memberId", Value::String(mid.clone())),
            ("role", Value::String("leader".into())),
            ("trust", Value::String(member_trust)),
            ("resumeToken", Value::String(resume_token)),
            ("expiresInSec", Value::from(reg.code_ttl.as_secs() as i64)),
            ("features", self.server_features_json()),
        ];
        if let Some(gt) = guest_token {
            data.push(("guestToken", Value::String(gt)));
        }
        self.ack(link_id, req, Some(protocol::frame(&data)), Some(&sid));
        self.broadcast_state(&sid);
        tracing::info!(
            "reader-sync link={} member={} created session",
            link_id,
            mid
        );
        // Notify keeps one permit — a wake fired while the ticker is between
        // lock and select still shortens its next sleep, like Event.set().
        self.wake.notify_one();
    }

    /// JOIN-path rejection: the frame itself, then the per-link throttle —
    /// a link fishing for codes/grants accumulates failures and gets its
    /// transport closed instead of unlimited guesses.
    fn reject_join(&mut self, link_id: &str, req: &Map<String, Value>, reason: &str, sid: Option<&str>) {
        self.reject(link_id, req_id(req), Some("JOIN"), reason, sid);
        let now = Instant::now();
        let mut flood = false;
        if let Some(l) = self.links.get_mut(link_id) {
            if now.duration_since(l.join_fails_since)
                >= Duration::from_secs_f64(JOIN_FAIL_WINDOW_SEC)
            {
                l.join_fails = 0;
                l.join_fails_since = now;
            }
            l.join_fails += 1;
            flood = l.join_fails > JOIN_FAIL_MAX;
        }
        if flood {
            self.close_link(link_id, "join_flood", WS_CLOSE_POLICY);
        }
    }

    fn join(&mut self, link_id: &str, req: &Map<String, Value>) {
        if req["payload"]["guestToken"]
            .as_str()
            .is_some_and(|t| !t.is_empty())
        {
            return self.join_guest(link_id, req);
        }
        // A guest-authenticated link may only ride its own grant — the code
        // path would let it brute-force 6-digit codes into foreign sessions
        // (leader auto-approvals land the hit instantly).
        if self.links.get(link_id).is_some_and(|l| l.is_guest()) {
            self.reject_join(link_id, req, REASON_FORBIDDEN, None);
            return;
        }
        let code = req["payload"]["code"].as_str().unwrap_or("").to_string();
        let Some(sid) = self.by_code.get(&code).cloned() else {
            self.reject_join(link_id, req, REASON_BAD_CODE, None);
            return;
        };
        let s = self.session(&sid).unwrap();
        if s.code_consumed {
            self.reject_join(link_id, req, REASON_CODE_CONSUMED, Some(&sid));
            return;
        }
        if Instant::now() >= s.code_expires_at {
            self.reject_join(link_id, req, REASON_CODE_EXPIRED, Some(&sid));
            return;
        }
        self.bind_follower(link_id, req, &sid);
    }

    /// `JOIN{guestToken}` — the QR-link path. The grant names its session
    /// directly (no `by_code` lookup); the session-side gates mirror the
    /// code path: session must exist and no join may be live yet
    /// (`code_consumed` is set by `bind_follower` for both paths).
    fn join_guest(&mut self, link_id: &str, req: &Map<String, Value>) {
        let token = req["payload"]["guestToken"].as_str().unwrap_or("");
        // Expired vs unknown IS distinguished here — unlike HTTP redeem
        // (see guest_redeem's oracle comment): join_fails already
        // throttles this oracle, and code_expired tells the client the
        // invite needs re-issue rather than retyping.
        let grant = match self.guest.status(token) {
            GrantStatus::Valid(g) => g,
            GrantStatus::Expired => {
                self.reject_join(link_id, req, REASON_CODE_EXPIRED, None);
                return;
            }
            GrantStatus::Unknown => {
                self.reject_join(link_id, req, REASON_BAD_CODE, None);
                return;
            }
        };
        let sid = grant.session_id;
        match self.session(&sid).map(|s| s.code_consumed) {
            None => {
                self.reject_join(link_id, req, REASON_NO_SESSION, Some(&sid));
            }
            Some(true) => {
                self.reject_join(link_id, req, REASON_CODE_CONSUMED, Some(&sid));
            }
            Some(false) => self.bind_follower(link_id, req, &sid),
        }
    }

    fn bind_follower(&mut self, link_id: &str, req: &Map<String, Value>, sid: &str) {
        let name = self
            .links
            .get(link_id)
            .and_then(|l| l.user_str("name"))
            .unwrap_or("")
            .to_string();
        let trust = self
            .links
            .get(link_id)
            .map(link_trust)
            .unwrap_or(TRUST_MEMBER);
        let capabilities = payload_features(&req["payload"]);
        let Some(member) = Member::new(
            "follower",
            name,
            sid,
            link_id,
            "pending",
            trust,
            capabilities,
        ) else {
            self.reject(
                link_id,
                req_id(req),
                Some("JOIN"),
                REASON_INTERNAL,
                Some(sid),
            );
            return;
        };
        let mid = member.id.clone();
        let resume_token = member.resume_token.clone();
        let member_trust = member.trust.clone();
        let Some(s) = self.session_mut(sid) else {
            return;
        };
        s.pending = Some(member);
        s.code_consumed = true; // single-use while a join is live
        if let Some(l) = self.links.get_mut(link_id) {
            l.member_id = Some(mid.clone());
            // A successful bind forgives earlier fishing attempts — the
            // throttle only punishes links that never get in.
            l.join_fails = 0;
        }
        let leader = self
            .session(sid)
            .and_then(|s| s.leader().map(|m| (m.name.clone(), m.link_id.clone())));
        let join_data = protocol::frame(&[
            ("sessionId", Value::String(sid.into())),
            ("memberId", Value::String(mid.clone())),
            ("role", Value::String("follower".into())),
            ("trust", Value::String(member_trust)),
            ("resumeToken", Value::String(resume_token)),
            (
                "leaderName",
                Value::String(leader.clone().map(|x| x.0).unwrap_or_default()),
            ),
            ("features", self.server_features_json()),
        ]);
        self.ack(link_id, req, Some(join_data), Some(sid));
        if let Some((_, Some(lid))) = leader {
            let frame = protocol::frame(&[
                ("v", Value::from(protocol::PROTOCOL_VERSION)),
                ("type", Value::String("JOIN_REQUEST".into())),
                ("sessionId", Value::String(sid.into())),
                ("memberId", Value::String(mid.clone())),
                (
                    "name",
                    Value::String(
                        self.session(sid)
                            .and_then(|s| s.pending.as_ref())
                            .map(|p| p.name.clone())
                            .unwrap_or_default(),
                    ),
                ),
            ]);
            self.send_obj(&lid, &frame);
        }
        tracing::info!("reader-sync link={} member={} pending join", link_id, mid);
    }

    /// One-tap pairing: join a fresh waiting session if exactly one is
    /// eligible, else become a waiting leader.
    fn match_(&mut self, reg: &Arc<Registry>, link_id: &str, req: &Map<String, Value>) {
        let pick = req["payload"]
            .get("pick")
            .and_then(|v| v.as_str())
            .map(str::to_string);
        if let Some(pick) = pick {
            let target = self
                .sessions
                .iter()
                .find(|s| !s.match_token.is_empty() && s.match_token == pick)
                .map(|s| s.id.clone());
            let ok = target
                .as_ref()
                .is_some_and(|sid| self.matchable(self.session(sid).unwrap(), Instant::now(), reg));
            match (target, ok) {
                (Some(sid), true) => self.bind_follower(link_id, req, &sid),
                _ => self.reject(link_id, req_id(req), Some("MATCH"), REASON_BAD_CODE, None),
            }
            return;
        }
        let now = Instant::now();
        let mut candidates: Vec<&PairSession> = self
            .sessions
            .iter()
            .filter(|s| self.matchable(s, now, reg))
            .collect();
        if candidates.is_empty() {
            self.create(reg, link_id, req, "match");
            return;
        }
        candidates.sort_by_key(|s| s.created_at);
        if candidates.len() > 1 {
            let cands: Vec<Value> = candidates
                .iter()
                .take(8)
                .map(|s| {
                    protocol::frame(&[
                        ("id", Value::String(s.match_token.clone())),
                        (
                            "leaderName",
                            Value::String(s.leader().map(|m| m.name.clone()).unwrap_or_default()),
                        ),
                        (
                            "ageSec",
                            Value::from(now.duration_since(s.created_at).as_secs() as i64),
                        ),
                    ])
                })
                .collect();
            self.ack(
                link_id,
                req,
                Some(protocol::frame(&[
                    ("ambiguous", Value::Bool(true)),
                    ("candidates", Value::Array(cands)),
                ])),
                None,
            );
            return;
        }
        let sid = candidates[0].id.clone();
        self.bind_follower(link_id, req, &sid);
    }

    fn matchable(&self, s: &PairSession, now: Instant, reg: &Arc<Registry>) -> bool {
        if s.origin != "match" || s.pending.is_some() || s.members.len() != 1 || s.code_consumed {
            return false;
        }
        if now >= s.code_expires_at || now.duration_since(s.created_at) > reg.match_window {
            return false;
        }
        match s.leader().and_then(|m| m.link_id.as_ref()) {
            Some(lid) => self.links.get(lid).is_some_and(|l| l.alive),
            None => false,
        }
    }

    fn approve_or_reject(
        &mut self,
        link_id: &str,
        req: &Map<String, Value>,
        sid: &str,
        mid: &str,
        approve: bool,
    ) {
        let t = req["type"].as_str().unwrap().to_string();
        let role = self
            .session(sid)
            .and_then(|s| s.members.iter().find(|m| m.id == mid))
            .map(|m| m.role.clone());
        if role.as_deref() != Some("leader") {
            self.reject(link_id, req_id(req), Some(&t), REASON_NOT_LEADER, Some(sid));
            return;
        }
        let pending_id = self
            .session(sid)
            .and_then(|s| s.pending.as_ref().map(|p| p.id.clone()));
        let Some(pid) = pending_id else {
            self.reject(link_id, req_id(req), Some(&t), REASON_NO_PENDING, Some(sid));
            return;
        };
        let want = req["payload"].get("memberId");
        if want.is_some_and(|w| !w.is_null() && w.as_str() != Some(pid.as_str())) {
            // A stale verdict for a gone candidate must not approve the current one.
            self.reject(link_id, req_id(req), Some(&t), REASON_NO_PENDING, Some(sid));
            return;
        }
        if !approve {
            self.end_session(sid, END_REJECTED);
            return;
        }
        if let Some(s) = self.session_mut(sid) {
            if let Some(mut p) = s.pending.take() {
                p.status = "active".into();
                s.members.push(p);
            }
        }
        self.ack(link_id, req, None, Some(sid));
        self.broadcast_state(sid);
        tracing::info!("reader-sync member={} approved member={}", mid, pid);
    }

    fn resume(&mut self, link_id: &str, req: &Map<String, Value>) {
        let payload = &req["payload"];
        let sid = req["sessionId"].as_str().unwrap_or("").to_string();
        let member_id = payload
            .get("memberId")
            .and_then(|v| v.as_str())
            .map(str::to_string);
        let token = payload.get("resumeToken").and_then(|v| v.as_str());
        let member_exists = self
            .session(&sid)
            .and_then(|s| s.members.iter().find(|m| Some(&m.id) == member_id.as_ref()))
            .map(|m| {
                (
                    m.resume_token.clone(),
                    m.link_id.clone(),
                    m.offline_since,
                    m.trust.clone(),
                )
            });
        let bad = match &member_exists {
            None => true,
            Some((tok, _, _, _)) => !matches!(token, Some(t) if t == tok),
        };
        if bad {
            self.reject(
                link_id,
                req_id(req),
                Some("RESUME"),
                REASON_BAD_RESUME,
                None,
            );
            return;
        }
        let (_, mlink, offline_since, stored_trust) = member_exists.unwrap();
        // Trust is fixed at admission and is not re-negotiated. The link's
        // current authentication class must match it exactly: a member
        // session/bearer resumes member trust; a valid `bookq_guest` bearer
        // resumes guest trust. A mismatch is terminal for this credential on
        // this link (the same RESUME can never become valid by retrying).
        let current_trust = self
            .links
            .get(link_id)
            .map(link_trust)
            .unwrap_or(TRUST_MEMBER);
        if current_trust != stored_trust {
            self.reject(
                link_id,
                req_id(req),
                Some("RESUME"),
                REASON_TRUST_MISMATCH,
                Some(&sid),
            );
            return;
        }
        if mlink
            .as_ref()
            .is_some_and(|lid| self.links.get(lid).is_some_and(|l| l.alive))
        {
            self.reject(
                link_id,
                req_id(req),
                Some("RESUME"),
                REASON_RESUME_IN_USE,
                Some(&sid),
            );
            return;
        }
        if let Some(off) = offline_since {
            if Instant::now().duration_since(off) >= self.resume_window {
                self.reject(
                    link_id,
                    req_id(req),
                    Some("RESUME"),
                    REASON_RESUME_EXPIRED,
                    Some(&sid),
                );
                return;
            }
        }
        let mid = member_id.unwrap();
        let capabilities = payload_features(payload);
        let intersected_before = self
            .session(&sid)
            .is_some_and(|s| self.member_drive_intersection(s));
        let (role, tok, trust) = {
            let Some(m) = self.member_mut(&sid, &mid) else {
                return;
            };
            m.link_id = Some(link_id.to_string());
            m.status = "active".into();
            m.offline_since = None;
            // Trust itself is immutable; capabilities describe the current
            // runtime and are refreshed by every successful RESUME.
            m.capabilities = capabilities;
            (m.role.clone(), m.resume_token.clone(), m.trust.clone())
        };
        // Capabilities are current runtime facts. If this RESUME downgrades
        // the pair out of member-drive while a shared browse is open, the
        // server must close that browse explicitly: in legacy semantics a
        // follower close is a no-op, so otherwise neither side may have an
        // effective close path and the pre-commit selecting phase wedges.
        let intersected_after = self
            .session(&sid)
            .is_some_and(|s| self.member_drive_intersection(s));
        if intersected_before && !intersected_after {
            if let Some(s) = self.session_mut(&sid) {
                if s.browse_open {
                    s.browse_open = false;
                    s.browse_opener_id = None;
                    tracing::info!(
                        "reader-sync session={} shared browse closed on capability downgrade",
                        sid
                    );
                }
            }
        }
        if let Some(l) = self.links.get_mut(link_id) {
            l.member_id = Some(mid.clone());
        }
        let resume_data = protocol::frame(&[
            ("sessionId", Value::String(sid.clone())),
            ("memberId", Value::String(mid.clone())),
            ("role", Value::String(role)),
            ("trust", Value::String(trust)),
            ("resumeToken", Value::String(tok)),
            ("features", self.server_features_json()),
        ]);
        self.ack(link_id, req, Some(resume_data), Some(&sid));
        self.broadcast_state(&sid);
        // §3.3: the manifest is re-delivered on resume.
        if self.session(&sid).is_some_and(|s| s.manifest.is_some()) {
            self.send_manifest(Some(link_id), &sid, None);
        }
        tracing::info!("reader-sync link={} member={} resumed", link_id, mid);
        self.wake.notify_one();
    }

    fn member_mut(&mut self, sid: &str, mid: &str) -> Option<&mut Member> {
        self.session_mut(sid)?
            .members
            .iter_mut()
            .find(|m| m.id == mid)
    }

    fn set_activity(&mut self, link_id: &str, req: &Map<String, Value>, sid: &str, mid: &str) {
        let st = req["payload"]
            .get("state")
            .and_then(|v| v.as_str())
            .map(str::to_string);
        if !matches!(st.as_deref(), Some("active" | "suspended" | "selecting")) {
            self.reject(
                link_id,
                req_id(req),
                Some("SET_ACTIVITY"),
                REASON_MALFORMED,
                Some(sid),
            );
            return;
        }
        let st = st.unwrap();
        let end_browse = req["payload"]
            .get("endBrowse")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        let (role, trust) = self
            .session(sid)
            .and_then(|s| s.members.iter().find(|m| m.id == mid))
            .map(|m| (m.role.clone(), m.trust.clone()))
            .unwrap_or_else(|| (String::new(), TRUST_MEMBER.to_string()));
        let member_drive = self
            .session(sid)
            .is_some_and(|s| self.member_drive_intersection(s));
        if st == "selecting" {
            // Shared browse open. In legacy pairs only the leader may drive;
            // once both members advertise member-drive-v1 either member may
            // open. Guests are never browse drivers in either mode.
            if trust == TRUST_GUEST {
                self.reject(
                    link_id,
                    req_id(req),
                    Some("SET_ACTIVITY"),
                    REASON_FORBIDDEN,
                    Some(sid),
                );
                return;
            }
            if !member_drive && role != "leader" {
                self.reject(
                    link_id,
                    req_id(req),
                    Some("SET_ACTIVITY"),
                    REASON_NOT_LEADER,
                    Some(sid),
                );
                return;
            }
            if let Some(s) = self.session_mut(sid) {
                // Simultaneous opens are ordered by server serialization. The
                // first closed→open transition owns the generation; duplicate
                // opens remain idempotent and must not overwrite the winner.
                if !s.browse_open {
                    s.browse_generation += 1;
                    s.browse_opener_id = Some(mid.to_string());
                }
                s.browse_open = true;
            }
        } else {
            // Validate the close authority before mutating activity: a
            // rejected explicit close must not become a plain liveness ping.
            if end_browse && trust == TRUST_GUEST {
                self.reject(
                    link_id,
                    req_id(req),
                    Some("SET_ACTIVITY"),
                    REASON_FORBIDDEN,
                    Some(sid),
                );
                return;
            }
            let is_leader = if let Some(m) = self.member_mut(sid, mid) {
                if m.status != "offline" {
                    m.status = st.clone();
                }
                m.role == "leader"
            } else {
                false
            };
            if member_drive {
                // New semantics: active/suspended are liveness only. Only an
                // explicit endBrowse signal closes the shared browse; either
                // member may send it. Suspend/visibility restoration never do.
                if end_browse {
                    if let Some(s) = self.session_mut(sid) {
                        s.browse_open = false;
                        s.browse_opener_id = None;
                    }
                }
            } else if is_leader {
                // Legacy mixed-pair semantics are preserved exactly: the
                // leader leaving the selecting activity clears the browse
                // flag. (Capability-unaware clients depend on this close.)
                if let Some(s) = self.session_mut(sid) {
                    s.browse_open = false;
                    s.browse_opener_id = None;
                }
            }
        }
        self.ack(link_id, req, None, Some(sid));
        self.broadcast_state(sid);
    }

    fn signal(&mut self, link_id: &str, req: &Map<String, Value>, sid: &str, mid: &str) {
        if !self.session(sid).is_some_and(|s| s.paired()) {
            self.reject(
                link_id,
                req_id(req),
                Some("SIGNAL"),
                REASON_NOT_PAIRED,
                Some(sid),
            );
            return;
        }
        let other = self.session(sid).and_then(|s| {
            s.members
                .iter()
                .find(|m| m.id != mid)
                .map(|m| m.link_id.clone())
        });
        let other_link = other.flatten();
        if other_link
            .as_ref()
            .is_none_or(|lid| !self.links.get(lid).is_some_and(|l| l.alive))
        {
            self.reject(
                link_id,
                req_id(req),
                Some("SIGNAL"),
                REASON_PEER_OFFLINE,
                Some(sid),
            );
            return;
        }
        let role = self
            .session(sid)
            .and_then(|s| s.members.iter().find(|m| m.id == mid))
            .map(|m| m.role.clone())
            .unwrap_or_default();
        let frame = protocol::frame(&[
            ("v", Value::from(protocol::PROTOCOL_VERSION)),
            ("type", Value::String("SIGNAL".into())),
            ("sessionId", Value::String(sid.into())),
            ("from", Value::String(role)),
            ("kind", req["payload"]["kind"].clone()),
            (
                "data",
                req["payload"].get("data").cloned().unwrap_or(Value::Null),
            ),
        ]);
        self.send_obj(other_link.as_ref().unwrap(), &frame);
        self.ack(link_id, req, None, Some(sid));
    }

    /// Ephemeral mirrored-UI relay — forwarded verbatim to the peer, never
    /// sequenced, no ACK, silent drop when peer offline or rate-capped.
    fn ui_sync(&mut self, link_id: &str, req: &Map<String, Value>, sid: &str, mid: &str) {
        if !self.session(sid).is_some_and(|s| s.paired()) {
            self.reject(
                link_id,
                req_id(req),
                Some("UI_SYNC"),
                REASON_NOT_PAIRED,
                Some(sid),
            );
            return;
        }
        // UI_SYNC is ephemeral, but on a closed browse it is also the frame
        // that would make the receiver open its list. Nobody — member or
        // guest — may start the shared browse merely by emitting mirror
        // traffic. Participants may relay only after an authorized member
        // opened it through SET_ACTIVITY.
        //
        // Exception: before the first work commit there is no shared browse
        // to open — mirror traffic in that window is the casual
        // work-picking mirror (product decision: the picking process itself
        // is shared). It carries no position in any work and cannot open
        // anything, so it relays exactly as it did before the shared-browse
        // encoding existed — for members. A guest's frames still may not
        // reach the host: the picking share is a member affordance, while a
        // guest watching the owner's picking only ever receives. Once a
        // manifest exists, the strict gate applies: the explicit-open flow
        // (作品変更) is the only way mirror traffic is allowed, and the
        // initial pre-commit phase being `selecting` does not count — the
        // boolean (not the derived phase) is the authority there.
        let pre_commit = self.session(sid).is_some_and(|s| s.manifest.is_none())
            && self
                .session(sid)
                .and_then(|s| s.members.iter().find(|m| m.id == mid))
                .is_some_and(|m| m.trust != TRUST_GUEST);
        let starting_closed_browse =
            !pre_commit && !self.session(sid).is_some_and(|s| s.browse_open);
        if starting_closed_browse {
            self.reject(
                link_id,
                req_id(req),
                Some("UI_SYNC"),
                REASON_FORBIDDEN,
                Some(sid),
            );
            return;
        }
        // A UI_SYNC that carries a generation is causally bound to the
        // browse generation the sender observed. A close→reopen increments
        // the server-side generation, so a frame that was honest when sent
        // but arrives after the boundary must not be relayed into the new
        // generation — the receiver has no other way to tell it apart from
        // the new opener's first seed. Ephemeral traffic is dropped, not
        // rejected: a generation skew is a race outcome, not an error.
        // Frames without the field relay only while the pair cannot tag:
        // once the member-drive intersection is established every participant
        // is running a build that tags, so an untagged frame can only be a
        // pre-upgrade straggler — exactly the in-flight pollution the
        // generation exists to prevent — and is dropped like a mismatch.
        // Pre-commit picking mirrors have no generations at all and pass
        // either way (the current generation is 0 there).
        let intersection = self
            .session(sid)
            .is_some_and(|s| self.member_drive_intersection(s));
        match req["payload"].get("browseGeneration").and_then(|v| v.as_i64()) {
            Some(gen) => {
                let current = self.session(sid).map(|s| s.browse_generation);
                if current != Some(gen) {
                    return;
                }
            }
            None => {
                if intersection && self.session(sid).is_some_and(|s| s.browse_open) {
                    return;
                }
            }
        }
        let other_link = self.session(sid).and_then(|s| {
            s.members
                .iter()
                .find(|m| m.id != mid)
                .and_then(|m| m.link_id.clone())
        });
        let Some(ol) = other_link else { return };
        if !self.links.get(&ol).is_some_and(|l| l.alive) {
            return;
        }
        let now = Instant::now();
        let role;
        {
            let Some(m) = self.member_mut(sid, mid) else {
                return;
            };
            if now.duration_since(m.ui_sync_window_start)
                >= Duration::from_secs_f64(UI_SYNC_WINDOW_SEC)
            {
                m.ui_sync_window_start = now;
                m.ui_sync_count = 0;
            }
            m.ui_sync_count += 1;
            if m.ui_sync_count > UI_SYNC_RATE_MAX {
                return;
            }
            role = m.role.clone();
        }
        let frame = protocol::frame(&[
            ("v", Value::from(protocol::PROTOCOL_VERSION)),
            ("type", Value::String("UI_SYNC".into())),
            ("sessionId", Value::String(sid.into())),
            ("from", Value::String(role)),
            ("payload", req["payload"].clone()),
        ]);
        self.send_obj(&ol, &frame);
    }

    fn transport_state(&mut self, link_id: &str, req: &Map<String, Value>, sid: &str, mid: &str) {
        let st = req["payload"]
            .get("state")
            .and_then(|v| v.as_str())
            .map(str::to_string);
        if !matches!(st.as_deref(), Some("up" | "down")) {
            self.reject(
                link_id,
                req_id(req),
                Some("TRANSPORT_STATE"),
                REASON_MALFORMED,
                Some(sid),
            );
            return;
        }
        if let Some(m) = self.member_mut(sid, mid) {
            m.dc_state = st.unwrap();
        }
        self.ack(link_id, req, None, Some(sid));
        self.announce_plane_flip(sid);
    }

    /// Recompute the data plane; returns true when it flipped.
    fn eval_data_plane(&mut self, sid: &str) -> bool {
        let Some(s) = self.session_mut(sid) else {
            return false;
        };
        let new_plane = if s.paired() && s.members.iter().all(|m| m.dc_state == "up") {
            "webrtc-dc"
        } else {
            "server-ws"
        };
        if new_plane != s.data_plane {
            s.data_plane = new_plane.to_string();
            return true;
        }
        false
    }

    /// Plane switch happens only at a revision boundary.
    fn announce_plane_flip(&mut self, sid: &str) {
        if self.eval_data_plane(sid) {
            self.broadcast_state(sid);
            self.broadcast_transport_state(sid);
            let plane = self
                .session(sid)
                .map(|s| s.data_plane.clone())
                .unwrap_or_default();
            let rev = self.session(sid).map(|s| s.revision).unwrap_or_default();
            tracing::info!("reader-sync session transport={} rev={}", plane, rev);
        }
    }

    fn broadcast_transport_state(&mut self, sid: &str) {
        let Some(s) = self.session(sid) else { return };
        let frame = protocol::frame(&[
            ("v", Value::from(protocol::PROTOCOL_VERSION)),
            ("type", Value::String("TRANSPORT_STATE".into())),
            ("sessionId", Value::String(sid.into())),
            ("dataPlane", Value::String(s.data_plane.clone())),
            ("revision", Value::from(s.revision)),
        ]);
        self.broadcast(sid, &frame);
    }

    // ── sequenced ops (S05): validate -> apply -> commit ────────────

    fn dedup_key(req: &Map<String, Value>, role: &str) -> String {
        format!("{}:{}", role, req["requestId"].as_str().unwrap_or(""))
    }

    fn dedup_store(&mut self, sid: &str, key: &str, frame: Value) {
        let Some(s) = self.session_mut(sid) else {
            return;
        };
        if !s.dedup.contains_key(key) {
            s.dedup_order.push_back(key.to_string());
        }
        s.dedup.insert(key.to_string(), frame);
        while s.dedup.len() > DEDUP_WINDOW {
            let Some(oldest) = s.dedup_order.front().cloned() else {
                break;
            };
            if oldest == key {
                break; // the just-stored entry is never evicted in-place
            }
            s.dedup_order.pop_front();
            let load_op = s
                .dedup
                .get(&oldest)
                .is_some_and(dedup_frame_needs_cross_epoch_tombstone);
            s.dedup.remove(&oldest);
            dedup_tombstone(s, oldest, load_op);
        }
    }

    /// REJECT carrying the latest committed snapshot verbatim; stored under
    /// the dedup key so a resend replays the identical response.
    fn reject_snapshot(
        &mut self,
        link_id: &str,
        req: &Map<String, Value>,
        sid: &str,
        role: &str,
        reason: &'static str,
    ) {
        let browse_visible = self
            .session(sid)
            .is_some_and(|s| self.member_drive_intersection(s));
        let server_features = self.server_features_json();
        let snap = state::snapshot(
            self.session(sid).unwrap(),
            &self.links,
            browse_visible,
            server_features,
        );
        let resp = protocol::frame(&[
            ("v", Value::from(protocol::PROTOCOL_VERSION)),
            ("type", Value::String("REJECT".into())),
            (
                "requestId",
                req.get("requestId").cloned().unwrap_or(Value::Null),
            ),
            ("request", req.get("type").cloned().unwrap_or(Value::Null)),
            ("reason", Value::String(reason.into())),
            ("sessionId", Value::String(sid.into())),
            ("snapshot", snap),
        ]);
        self.dedup_store(sid, &Self::dedup_key(req, role), resp.clone());
        self.send_obj(link_id, &resp);
    }

    /// One sequenced request. Order of checks: origin validity -> dedup ->
    /// epoch -> manifestVersion -> baseRevision -> per-type handler.
    /// OPEN_WORK skips the epoch/manifestVersion/baseRevision checks.
    fn sequenced_op(
        &mut self,
        reg: &Arc<Registry>,
        link_id: &str,
        req: &Map<String, Value>,
        sid: &str,
        mid: &str,
    ) {
        let payload = match req["payload"].as_object() {
            Some(p) => p.clone(),
            None => Map::new(),
        };

        // Effective role / member for relayed (payload.origin) requests.
        let (member_role, sender_trust) = self
            .session(sid)
            .and_then(|s| s.members.iter().find(|m| m.id == mid))
            .map(|m| (m.role.clone(), m.trust.clone()))
            .unwrap_or_else(|| (String::new(), TRUST_MEMBER.to_string()));
        let mut role = member_role.clone();
        let mut eff_id = mid.to_string();
        let mut eff_trust = sender_trust;
        // `origin is not None` — any non-null value is checked; a non-str
        // or non-role value is bad_origin even for a leader (py `in ROLES`).
        let origin = payload.get("origin").filter(|v| !v.is_null());
        if let Some(o) = origin {
            let o = o.as_str();
            if !o.is_some_and(|s| protocol::ROLES.contains(&s)) || member_role != "leader" {
                self.reject_snapshot(link_id, req, sid, &member_role, state::REASON_BAD_ORIGIN);
                return;
            }
            role = o.unwrap().to_string();
            if role != member_role {
                if let Some((target_id, target_trust)) = self.session(sid).and_then(|s| {
                    s.members
                        .iter()
                        .find(|m| m.role == role)
                        .map(|m| (m.id.clone(), m.trust.clone()))
                }) {
                    eff_id = target_id;
                    eff_trust = target_trust;
                } else {
                    eff_id = mid.to_string();
                }
            }
        }

        // Response channel: a relayed request's response belongs to the
        // *origin* member's own link.
        let resp_link = {
            if eff_id != mid {
                self.session(sid)
                    .and_then(|s| s.members.iter().find(|m| m.id == eff_id))
                    .and_then(|m| m.link_id.clone())
                    .filter(|lid| self.links.get(lid).is_some_and(|l| l.alive))
                    .unwrap_or_else(|| link_id.to_string())
            } else {
                link_id.to_string()
            }
        };

        let dkey = Self::dedup_key(req, &role);
        if let Some(cached) = self.session(sid).and_then(|s| s.dedup.get(&dkey)).cloned() {
            self.send_obj(&resp_link, &cached); // identical replay
            return;
        }
        if self
            .session(sid)
            .is_some_and(|s| dedup_has_tombstone(s, &dkey))
        {
            self.reject_snapshot(&resp_link, req, sid, &role, state::REASON_STALE_REQUEST);
            return;
        }

        let t = req["type"].as_str().unwrap().to_string();
        let result = (|| -> Result<(), state::Reject> {
            if t != "OPEN_WORK" {
                state::check_epoch(self.session(sid).unwrap(), req)?;
                state::check_manifest_version(self.session(sid).unwrap(), &payload)?;
                if state::ABSOLUTE_TYPES.contains(&t.as_str()) {
                    state::check_base(self.session(sid).unwrap(), req)?;
                }
            }
            self.apply_op(reg, &resp_link, req, sid, mid, &eff_id, &role, &eff_trust)
        })();
        if let Err(r) = result {
            self.reject_snapshot(&resp_link, req, sid, &role, r.reason);
        }
    }

    /// Dispatch one validated sequenced op.
    fn apply_op(
        &mut self,
        reg: &Arc<Registry>,
        resp_link: &str,
        req: &Map<String, Value>,
        sid: &str,
        _mid: &str,
        eff_id: &str,
        role: &str,
        eff_trust: &str,
    ) -> Result<(), state::Reject> {
        let t = req["type"].as_str().unwrap().to_string();
        let payload = req["payload"].as_object().cloned().unwrap_or_default();
        match t.as_str() {
            "OPEN_WORK" => {
                // Authorization is against the effective actor resolved from
                // payload.origin — never against the relay transport. A guest
                // is not a work-selection driver even when a member leader
                // relays the frame.
                if eff_trust == TRUST_GUEST {
                    return Err(state::Reject {
                        reason: REASON_FORBIDDEN,
                    });
                }
                let member_drive = self
                    .session(sid)
                    .is_some_and(|s| self.member_drive_intersection(s));
                if !member_drive && role != "leader" {
                    return Err(state::Reject {
                        reason: state::REASON_NOT_LEADER,
                    });
                }
                let auto = payload
                    .get("auto")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false);
                if auto {
                    let deliberate_in_flight = self
                        .session(sid)
                        .and_then(|s| s.pending_load.as_ref())
                        .is_some_and(|job| !job.auto);
                    let deliberate_committed = self
                        .session(sid)
                        .and_then(|s| s.committed_load_auto)
                        == Some(false);
                    if deliberate_in_flight || deliberate_committed {
                        // Bootstrap is best-effort: once a deliberate pick is
                        // committed or merely in flight, an automatic load is
                        // stale by definition and must not overwrite it.
                        return Err(state::Reject {
                            reason: state::REASON_STALE_REQUEST,
                        });
                    }
                }
                let page = match payload.get("page") {
                    Some(p) if !p.is_null() => p.clone(),
                    _ => {
                        return Err(state::Reject {
                            reason: state::REASON_NO_INITIAL_PAGE,
                        })
                    }
                };
                let offset = match payload.get("offset") {
                    Some(v) => v.clone(),
                    None => Value::from(self.session(sid).map(|s| s.offset).unwrap_or(0)),
                };
                // `offset not in (0,1)` — Python bools and 0.0/1.0 equal
                // ints, so they pass; resolve_work's isinstance(bool)
                // rejects them at manifest resolution.
                let offset_ok =
                    offset.is_boolean() || offset.as_f64().is_some_and(|f| f == 0.0 || f == 1.0);
                if !offset_ok {
                    return Err(state::Reject {
                        reason: state::REASON_RANGE,
                    });
                }
                let folder = payload["folder"].as_str().unwrap_or("").to_string();
                self.begin_load(reg, resp_link, req, sid, role, folder, page, offset, auto);
                Ok(())
            }
            "TURN" | "GOTO" => {
                if let Some(gate) = state::gate_page_turn(self.session(sid).unwrap(), &self.links) {
                    return Err(state::Reject { reason: gate });
                }
                {
                    let s = self.session_mut(sid).unwrap();
                    if t == "TURN" {
                        state::apply_turn(s, &payload)?;
                    } else {
                        state::apply_goto(s, &payload)?;
                    }
                }
                self.commit_op(resp_link, req, sid, role);
                Ok(())
            }
            "SET_OFFSET" => {
                if self.session(sid).is_none_or(|s| s.manifest.is_none()) {
                    return Err(state::Reject {
                        reason: state::REASON_NO_WORK,
                    });
                }
                let offset = payload["offset"].clone(); // int 0|1 or bool
                if state::py_num_eq(&offset, self.session(sid).map(|s| s.offset).unwrap_or(-1)) {
                    self.commit_op(resp_link, req, sid, role); // accepted no-op
                    return Ok(());
                }
                let idx = state::current_index(self.session(sid).unwrap());
                if idx < 0 {
                    return Err(state::Reject {
                        reason: state::REASON_NO_WORK,
                    });
                }
                let s = self.session(sid).unwrap();
                let anchor = s.plan.as_ref().unwrap()[idx as usize].right.clone();
                let folder = s.manifest.as_ref().unwrap()["folder"]
                    .as_str()
                    .unwrap_or("")
                    .to_string();
                let page = model::ref_json(&anchor);
                // raw offset (may be bool — resolve_work rejects it like py)
                self.begin_load(reg, resp_link, req, sid, role, folder, page, offset, false);
                Ok(())
            }
            "SWAP_SIDES" => {
                {
                    let s = self.session_mut(sid).unwrap();
                    state::apply_swap_sides(s, &payload)?;
                }
                self.commit_op(resp_link, req, sid, role);
                Ok(())
            }
            "READY" => {
                if self.session(sid).is_none_or(|s| s.manifest.is_none()) {
                    return Err(state::Reject {
                        reason: state::REASON_NO_WORK,
                    });
                }
                if let Some(s) = self.session_mut(sid) {
                    s.ready.insert(eff_id.to_string());
                }
                self.commit_op(resp_link, req, sid, role);
                Ok(())
            }
            "APPLIED" => {
                // client-side apply report: acknowledged, never a broadcast.
                if let Some(m) = self.member_mut(sid, eff_id) {
                    m.last_applied = Some(protocol::frame(&[
                        (
                            "requestId",
                            payload.get("requestId").cloned().unwrap_or(Value::Null),
                        ),
                        (
                            "revision",
                            req.get("baseRevision").cloned().unwrap_or(Value::Null),
                        ),
                    ]));
                }
                let resp = protocol::frame(&[
                    ("v", Value::from(protocol::PROTOCOL_VERSION)),
                    ("type", Value::String("ACK".into())),
                    (
                        "requestId",
                        req.get("requestId").cloned().unwrap_or(Value::Null),
                    ),
                    ("request", Value::String("APPLIED".into())),
                    ("sessionId", Value::String(sid.into())),
                    ("data", Value::Object(Map::new())),
                ]);
                self.dedup_store(sid, &Self::dedup_key(req, role), resp.clone());
                self.send_obj(resp_link, &resp);
                Ok(())
            }
            _ => Err(state::Reject {
                reason: state::REASON_MALFORMED,
            }),
        }
    }

    /// Accepted sequenced op: one revision bump, apply-order log, ACK to
    /// the requester, then APPLIED + STATE commit broadcast to both members.
    fn commit_op(&mut self, resp_link: &str, req: &Map<String, Value>, sid: &str, role: &str) {
        {
            let Some(s) = self.session_mut(sid) else {
                return;
            };
            s.apply_seq += 1;
        }
        let Some(frame) = self.state_frame_for(sid) else {
            return;
        };
        {
            let Some(s) = self.session_mut(sid) else {
                return;
            };
            let mut log_entry = Map::new();
            log_entry.insert("seq".into(), Value::from(s.apply_seq));
            log_entry.insert(
                "type".into(),
                req.get("type").cloned().unwrap_or(Value::Null),
            );
            log_entry.insert("role".into(), Value::String(role.into()));
            log_entry.insert(
                "requestId".into(),
                req.get("requestId").cloned().unwrap_or(Value::Null),
            );
            log_entry.insert("revision".into(), frame["revision"].clone());
            log_entry.insert("spreadId".into(), frame["spreadId"].clone());
            s.apply_log.push(Value::Object(log_entry));
        }
        tracing::info!(
            "reader-sync apply rev={} type={} role={} link={}",
            frame["revision"],
            req["type"].as_str().unwrap_or(""),
            role,
            resp_link
        );
        let resp = protocol::frame(&[
            ("v", Value::from(protocol::PROTOCOL_VERSION)),
            ("type", Value::String("ACK".into())),
            (
                "requestId",
                req.get("requestId").cloned().unwrap_or(Value::Null),
            ),
            ("request", req.get("type").cloned().unwrap_or(Value::Null)),
            ("sessionId", Value::String(sid.into())),
            ("data", Value::Object(Map::new())),
        ]);
        self.dedup_store(sid, &Self::dedup_key(req, role), resp.clone());
        self.send_obj(resp_link, &resp);
        let applied = protocol::frame(&[
            ("v", Value::from(protocol::PROTOCOL_VERSION)),
            ("type", Value::String("APPLIED".into())),
            ("sessionId", Value::String(sid.into())),
            (
                "requestId",
                req.get("requestId").cloned().unwrap_or(Value::Null),
            ),
            ("revision", frame["revision"].clone()),
            ("by", Value::String(role.into())),
        ]);
        self.broadcast(sid, &applied);
        self.broadcast(sid, &frame);
    }

    // ── async work loading (OPEN_WORK / SET_OFFSET) ─────────────────

    /// Accept a work change: new generation + loading STATE now, manifest
    /// commit only once the resolver finishes.
    fn begin_load(
        &mut self,
        reg: &Arc<Registry>,
        resp_link: &str,
        req: &Map<String, Value>,
        sid: &str,
        role: &str,
        folder: String,
        page: Value,
        offset: Value,
        auto: bool,
    ) {
        let job = {
            let Some(s) = self.session_mut(sid) else {
                return;
            };
            s.load_seq += 1;
            let job = LoadJob {
                seq: s.load_seq,
                kind: req["type"].as_str().unwrap_or("").to_lowercase(),
                folder,
                page,
                offset: offset.clone(),
                request_id: req["requestId"].as_str().unwrap_or("").to_string(),
                role: role.to_string(),
                auto,
                ack_frame: None,
            };
            s.pending_load = Some(job.clone());
            job
        };
        self.commit_op(resp_link, req, sid, role);
        // The resolver may outlive the trigger ACK's occupancy in the fixed
        // dedup window. Retain the exact accepted outcome on the pending job
        // so the eventual epoch commit can carry it independent of eviction
        // timing. This is not a second response path — it is the same frame
        // commit_op just stored and sent.
        let dkey = Self::dedup_key(req, role);
        if let Some(s) = self.session_mut(sid) {
            if s.pending_load.as_ref().is_some_and(|j| j.seq == job.seq) {
                let frame = s.dedup.get(&dkey).cloned();
                if let (Some(pending), Some(frame)) = (s.pending_load.as_mut(), frame) {
                    pending.ack_frame = Some(frame);
                }
            }
        }
        let reg2 = reg.clone();
        let sid2 = sid.to_string();
        let load_timeout = reg.load_timeout;
        tokio::spawn(async move {
            let result = tokio::time::timeout(load_timeout, reg2.resolve_work(&job)).await;
            let result = match result {
                Ok(r) => r,
                Err(_) => Err(catalog::ManifestError {
                    code: catalog::ERR_TIMEOUT,
                    message: "work resolve timed out".into(),
                }),
            };
            reg2.inner
                .lock()
                .await
                .finish_load_by_id(&sid2, &job, result);
        });
    }

    fn finish_load_by_id(
        &mut self,
        sid: &str,
        job: &LoadJob,
        result: Result<Value, catalog::ManifestError>,
    ) {
        if self.session(sid).is_none() {
            return;
        }
        self.finish_load(sid, job, result);
    }

    /// Resolve completion. Generation guard first: only the newest
    /// accepted load may commit.
    fn finish_load(
        &mut self,
        sid: &str,
        job: &LoadJob,
        result: Result<Value, catalog::ManifestError>,
    ) {
        {
            let Some(s) = self.session(sid) else { return };
            if s.pending_load.as_ref().map(|j| j.seq) != Some(job.seq) || job.seq != s.load_seq {
                tracing::info!("reader-sync stale load discarded seq={}", job.seq);
                return;
            }
        }
        let trigger_ack = if let Some(s) = self.session_mut(sid) {
            let ack = s
                .pending_load
                .as_ref()
                .and_then(|j| j.ack_frame.clone());
            s.pending_load = None;
            ack
        } else {
            None
        };
        let manifest = match result {
            Err(e) => {
                self.load_failed(sid, &e);
                return;
            }
            Ok(m) => m,
        };
        // Validate and split BEFORE mutating session state — a build failure
        // surfaces as an ERROR frame, never strands members at 'loading'.
        let plan = match state::plan_from_manifest(&manifest) {
            Ok(p) => p,
            Err(e) => {
                self.load_failed(
                    sid,
                    &catalog::ManifestError {
                        code: e.code,
                        message: e.message,
                    },
                );
                return;
            }
        };
        let chunks = match catalog::split_manifest(&manifest) {
            Ok(c) => c,
            Err(e) => {
                self.load_failed(sid, &e);
                return;
            }
        };
        {
            let Some(s) = self.session_mut(sid) else {
                return;
            };
            s.work_epoch += 1;
            s.manifest_version += 1;
            s.manifest = Some(manifest.clone());
            s.plan = Some(plan);
            s.offset = manifest
                .get("offset")
                .and_then(|v| v.as_i64())
                .unwrap_or(s.offset);
            s.spread_id = manifest
                .get("initial")
                .and_then(|i| i.get("spreadId"))
                .and_then(|v| v.as_str())
                .map(str::to_string);
            s.ready.clear();
            // The epoch reset carries exactly one outcome: the ACK of the
            // load request whose resolver generation actually committed.
            // Other load-request outcomes do not cross as replayable ACKs —
            // OPEN_WORK skips the epoch gate, so a replayable stale ACK would
            // make a superseded pick look accepted, and dropping the key
            // entirely would make its resend execute again. Tombstone those
            // keys instead. Ordinary epoch-gated op entries still reset with
            // the epoch; pre-existing tombstones never do.
            let carry_key = format!("{}:{}", job.role, job.request_id);
            // The job-retained ACK is authoritative: unlike the fixed dedup
            // window, it cannot be evicted by unrelated traffic while the
            // resolver is in flight. Keep a cache scan only as a defensive
            // fallback for internally constructed sessions that predate the
            // retention field being populated.
            // The trigger is the request whose resolution committed this
            // epoch — whatever op started the job (OPEN_WORK or SET_OFFSET).
            // This is deliberately NOT dedup_frame_needs_cross_epoch_tombstone: that predicate
            // scopes cross-epoch tombstone protection to OPEN_WORK (the only
            // epoch-gate-exempt op), while the carry must cover the job kind
            // that actually triggered the rollover. A SET_OFFSET load ACK
            // must ride the epoch too, or its resend is answered
            // stale_request (evicted pre-commit) or stale_epoch (dropped at
            // the rollover) instead of the identical ACK.
            let valid_trigger = |frame: &Value| {
                frame
                    .get("request")
                    .and_then(|v| v.as_str())
                    .map(str::to_lowercase)
                    .as_deref()
                    == Some(job.kind.as_str())
                    && frame.get("type").and_then(|v| v.as_str()) == Some("ACK")
                    && frame.get("requestId").and_then(|v| v.as_str())
                        == Some(job.request_id.as_str())
            };
            let mut carried: Option<(String, Value)> = trigger_ack
                .as_ref()
                .filter(|frame| valid_trigger(frame))
                .map(|frame| (carry_key.clone(), frame.clone()));
            let mut new_tombstones: Vec<String> = Vec::new();
            for key in &s.dedup_order {
                let Some(frame) = s.dedup.get(key) else {
                    continue;
                };
                let is_load = dedup_frame_needs_cross_epoch_tombstone(frame);
                let is_trigger_ack = *key == carry_key && valid_trigger(frame);
                if is_trigger_ack {
                    if carried.is_none() {
                        carried = Some((key.clone(), frame.clone()));
                    }
                } else if is_load {
                    new_tombstones.push(key.clone());
                }
            }
            for key in new_tombstones {
                dedup_tombstone(s, key, true);
            }
            s.dedup.clear();
            s.dedup_order.clear();
            if let Some((key, frame)) = carried {
                s.dedup.insert(key.clone(), frame);
                s.dedup_order.push_back(key);
            } else if job.kind == "open_work" {
                // Losing the trigger outcome must never make its retransmission
                // executable again. This should be unreachable now that the job
                // retains the ACK, but fail closed if the invariant is broken.
                // Only OPEN_WORK needs the tombstone: epoch-gated trigger kinds
                // (SET_OFFSET) are already stopped by the epoch check, so
                // spending the OPEN_WORK-only budget on them would be pure loss.
                dedup_tombstone(s, carry_key, true);
            }
            // A successful OPEN_WORK commit is the authoritative end of the
            // shared browse regardless of which member opened it.
            s.browse_open = false;
            s.committed_load_auto = Some(job.auto);
            s.apply_seq += 1;
        }
        let Some(frame) = self.state_frame_for(sid) else {
            return;
        };
        {
            let Some(s) = self.session_mut(sid) else {
                return;
            };
            let mut le = Map::new();
            le.insert("seq".into(), Value::from(s.apply_seq));
            le.insert("type".into(), Value::String("MANIFEST_COMMIT".into()));
            le.insert("role".into(), Value::String("server".into()));
            le.insert("requestId".into(), Value::String(job.request_id.clone()));
            le.insert("revision".into(), frame["revision"].clone());
            le.insert("spreadId".into(), frame["spreadId"].clone());
            s.apply_log.push(Value::Object(le));
        }
        self.send_manifest(None, sid, Some(chunks));
        tracing::info!(
            "reader-sync apply rev={} type=MANIFEST_COMMIT",
            frame["revision"]
        );
        self.broadcast(sid, &frame);
        // The epoch rollover above can push surviving window entries into
        // the load tombstone set; if that crossed the budget the session is
        // already fully committed, so teardown here loses nothing.
        if self.session(sid).is_some_and(|s| s.condemned) {
            self.end_session(sid, END_DEDUP_EXHAUSTED);
        }
    }

    /// Work-load failure: typed ERROR frame + current STATE.
    fn load_failed(&mut self, sid: &str, error: &catalog::ManifestError) {
        let reason = state::manifest_err_reason(error.code);
        tracing::info!("reader-sync load failed code={}", error.code);
        let frame = protocol::frame(&[
            ("v", Value::from(protocol::PROTOCOL_VERSION)),
            ("type", Value::String("ERROR".into())),
            ("sessionId", Value::String(sid.into())),
            ("code", Value::String(reason.into())),
        ]);
        self.broadcast(sid, &frame);
        self.broadcast_state(sid);
    }

    /// MANIFEST chunk frames — broadcast when `link` is None, unicast
    /// otherwise (the resume path). Chunks go through the bulk queue so a
    /// multi-chunk manifest can never overflow the bounded control outq.
    fn send_manifest(&mut self, link_id: Option<&str>, sid: &str, chunks: Option<Vec<Value>>) {
        let Some(s) = self.session(sid) else { return };
        let Some(manifest) = s.manifest.clone() else {
            return;
        };
        let chunks = match chunks {
            Some(c) => c,
            None => match catalog::split_manifest(&manifest) {
                Ok(c) => c,
                Err(_) => return,
            },
        };
        let s = self.session(sid).unwrap();
        let frames: Vec<Value> = chunks
            .into_iter()
            .map(|c| {
                // py: dict(chunk) then .update(type/sessionId/workEpoch/
                // manifestVersion) — chunk keys lead, envelope appended.
                let mut o = c.as_object().cloned().unwrap_or_default();
                o.insert("type".into(), Value::String("MANIFEST".into()));
                o.insert("sessionId".into(), Value::String(sid.into()));
                o.insert("workEpoch".into(), Value::from(s.work_epoch));
                o.insert("manifestVersion".into(), Value::from(s.manifest_version));
                Value::Object(o)
            })
            .collect();
        match link_id {
            Some(lid) => {
                self.send_objs_bulk(lid, &frames);
            }
            None => {
                let lids: Vec<String> = self
                    .session(sid)
                    .map(|s| s.members.iter().filter_map(|m| m.link_id.clone()).collect())
                    .unwrap_or_default();
                for lid in lids {
                    self.send_objs_bulk(&lid, &frames);
                }
            }
        }
    }

    // ── broadcast / teardown ─────────────────────────────────────────

    /// `_state_frame` — bumps revision, derives phase, emits STATE.
    fn state_frame_for(&mut self, sid: &str) -> Option<Value> {
        let server_features = self.server_features_json();
        let browse_visible = self
            .session(sid)
            .is_some_and(|s| self.member_drive_intersection(s));
        let Inner {
            sessions, links, ..
        } = self;
        let s = sessions.iter_mut().find(|s| s.id == sid)?;
        s.revision += 1;
        s.phase = state::derive_phase(s, links).to_string();
        let mut presence = Map::new();
        for m in &s.members {
            let mut p = Map::new();
            p.insert("role".into(), Value::String(m.role.clone()));
            p.insert("status".into(), Value::String(m.status.clone()));
            p.insert("trust".into(), Value::String(m.trust.clone()));
            p.insert(
                "features".into(),
                Value::Array(
                    m.capabilities
                        .iter()
                        .cloned()
                        .map(Value::String)
                        .collect(),
                ),
            );
            p.insert("name".into(), Value::String(m.name.clone()));
            p.insert("ready".into(), Value::Bool(s.ready.contains(&m.id)));
            presence.insert(m.id.clone(), Value::Object(p));
        }
        let mut frame = protocol::frame(&[
            ("v", Value::from(protocol::PROTOCOL_VERSION)),
            ("type", Value::String("STATE".into())),
            ("sessionId", Value::String(s.id.clone())),
            ("revision", Value::from(s.revision)),
            ("workEpoch", Value::from(s.work_epoch)),
            ("manifestVersion", Value::from(s.manifest_version)),
            ("phase", Value::String(s.phase.clone())),
            (
                "spreadId",
                s.spread_id
                    .clone()
                    .map(Value::String)
                    .unwrap_or(Value::Null),
            ),
            ("offset", Value::from(s.offset)),
            ("leaderSide", Value::String(s.leader_side.clone())),
            ("presence", Value::Object(presence)),
            ("dataPlane", Value::String(s.data_plane.clone())),
            ("features", server_features),
        ]);
        if browse_visible && s.browse_open {
            // `paused` outranks selecting in the derived phase, so the
            // phase alone cannot represent a still-open shared browse. In
            // member-drive semantics expose the orthogonal boolean; the
            // true-only open encoding keeps legacy/mixed-pair open frames
            // unchanged. Opener metadata is additive alongside `browse:true`.
            if let Value::Object(ref mut obj) = frame {
                obj.insert("browse".into(), Value::Bool(true));
                obj.insert(
                    "browseGeneration".into(),
                    Value::from(s.browse_generation),
                );
                if let Some(opener) = &s.browse_opener_id {
                    obj.insert("browseOpener".into(), Value::String(opener.clone()));
                    if let Some(role) = s
                        .members
                        .iter()
                        .find(|m| &m.id == opener)
                        .map(|m| m.role.clone())
                    {
                        obj.insert("browseOpenerRole".into(), Value::String(role));
                    }
                }
            }
        } else if !s.browse_open && s.browse_generation > 0 {
            // A close must remain observable even when the same transition
            // removed the member-drive intersection (a RESUME capability
            // downgrade), and even to a client that missed the close while
            // offline. Once a generation has existed, closed states therefore
            // carry an additive explicit false plus the monotonically
            // increasing generation. Capability-unaware clients ignore both
            // fields; pre-first-open frames carry no browse metadata, preserving
            // the legacy browse encoding (the STATE as a whole still has other
            // additive fields such as features/trust).
            if let Value::Object(ref mut obj) = frame {
                obj.insert("browse".into(), Value::Bool(false));
                obj.insert(
                    "browseGeneration".into(),
                    Value::from(s.browse_generation),
                );
            }
        }
        Some(frame)
    }

    fn broadcast(&mut self, sid: &str, frame: &Value) {
        let lids: Vec<String> = self
            .session(sid)
            .map(|s| s.members.iter().filter_map(|m| m.link_id.clone()).collect())
            .unwrap_or_default();
        for lid in lids {
            self.send_obj(&lid, frame);
        }
    }

    fn broadcast_state(&mut self, sid: &str) {
        if let Some(frame) = self.state_frame_for(sid) {
            self.broadcast(sid, &frame);
        }
    }

    pub fn end_session(&mut self, sid: &str, reason: &str) {
        let Some(pos) = self.sessions.iter().position(|s| s.id == sid) else {
            return;
        };
        // Guest grants die with their session — every teardown path
        // (LEAVE/REJECT/expiry/end_all/shutdown) funnels through here.
        self.guest.revoke_session(sid);
        let mut session = self.sessions.remove(pos);
        if self.by_code.get(&session.code).is_some_and(|v| *v == sid) {
            self.by_code.remove(&session.code);
        }
        let frame = protocol::frame(&[
            ("v", Value::from(protocol::PROTOCOL_VERSION)),
            ("type", Value::String("END".into())),
            ("sessionId", Value::String(sid.into())),
            ("reason", Value::String(reason.into())),
        ]);
        let mut targets: Vec<Member> = std::mem::take(&mut session.members);
        if let Some(p) = session.pending.take() {
            targets.push(p);
        }
        let n = targets.len();
        for m in targets {
            if let Some(lid) = &m.link_id {
                self.send_obj(lid, &frame);
                if let Some(l) = self.links.get_mut(lid) {
                    l.member_id = None;
                    // A guest's grant died with this session — its socket is
                    // now an unauthenticated transport, so close it instead
                    // of leaving a live link that only ping/pong can see.
                    // Queued (not the watch bypass): the END frame just sent
                    // must reach the client first.
                    if l.is_guest() {
                        l.queue_transport_close(WS_CLOSE_NORMAL, "session_end");
                    }
                }
            }
        }
        tracing::info!("reader-sync session end reason={} members={}", reason, n);
        self.wake.notify_one();
    }

    // ── clock-driven transitions ─────────────────────────────────────

    /// All time-based transitions.
    /// Whether a guest link's grant still holds — WS auth was a
    /// handshake-time snapshot, so a revoked/expired grant must drop the
    /// socket rather than coast until disconnect. When a directory is
    /// wired the grant OWNER's membership is checked too and permanent-
    /// revoked on removal, like `end_owner_sessions`' `revoke_owner`:
    /// the `viewer-rs users remove` CLI path can't reach that call site,
    /// and closing the socket alone would let a re-enable resurrect the
    /// bearer grant.
    fn guest_link_valid(&mut self, lid: &str) -> bool {
        let Some(token) = self
            .links
            .get(lid)
            .and_then(|l| l.guest_token.as_deref())
        else {
            return false;
        };
        let GrantStatus::Valid(g) = self.guest.status(token) else {
            return false;
        };
        if matches!(&self.directory, Some(d) if !d.is_member(&g.owner_email)) {
            self.guest.revoke_owner(&g.owner_email);
            return false;
        }
        true
    }

    pub fn tick(&mut self) {
        let now = Instant::now();
        let link_ids: Vec<String> = self.links.keys().cloned().collect();
        for lid in link_ids {
            let Some(link) = self.links.get(&lid) else {
                continue;
            };
            if !link.alive {
                continue;
            }
            if link.close_queued {
                // A parked ordered close retries as the queue drains —
                // the bypassing watch close is the last resort because it
                // truncates the frames the ordering exists to protect. A
                // queue that stays full past the no-response budget means
                // a wedged transport; give up and bypass.
                if let Some((code, reason, since)) = link.pending_close.clone() {
                    let wedged =
                        now.duration_since(since) >= self.no_response_timeout;
                    match link
                        .outq
                        .try_send(OutItem::Close(code, reason.clone()))
                    {
                        Ok(())
                        | Err(mpsc::error::TrySendError::Closed(_)) => {
                            if let Some(l) = self.links.get_mut(&lid) {
                                l.pending_close = None;
                            }
                        }
                        Err(mpsc::error::TrySendError::Full(_)) if wedged => {
                            link.request_transport_close(code, &reason);
                            if let Some(l) = self.links.get_mut(&lid) {
                                l.pending_close = None;
                            }
                        }
                        Err(mpsc::error::TrySendError::Full(_)) => {}
                    }
                }
                continue;
            }
            if now.duration_since(link.last_rx) >= self.no_response_timeout {
                self.close_link(&lid, "no_response", WS_CLOSE_POLICY);
                continue;
            }
            if link.is_guest() && !self.guest_link_valid(&lid) {
                self.close_link(&lid, "guest_revoked", WS_CLOSE_POLICY);
                continue;
            }
            let Some(link) = self.links.get_mut(&lid) else {
                continue;
            };
            if now >= link.next_ping_due {
                link.ping_seq += 1;
                let seq = link.ping_seq;
                link.next_ping_due = now + Duration::from_secs_f64(HEARTBEAT_INTERVAL);
                let frame = protocol::frame(&[
                    ("v", Value::from(protocol::PROTOCOL_VERSION)),
                    ("type", Value::String("PING".into())),
                    ("seq", Value::from(seq as i64)),
                ]);
                self.send_obj(&lid, &frame);
            }
        }
        let sids: Vec<String> = self.sessions.iter().map(|s| s.id.clone()).collect();
        for sid in sids {
            let Some(s) = self.session(&sid) else {
                continue;
            };
            if !s.paired() && now >= s.code_expires_at {
                self.end_session(&sid, END_CODE_EXPIRED);
                continue;
            }
            if s.members.iter().any(|m| {
                m.status == "offline"
                    && m.offline_since
                        .is_some_and(|o| now.duration_since(o) >= self.resume_window)
            }) {
                self.end_session(&sid, END_RESUME_EXPIRED);
            }
        }
    }

    /// Seconds until the next due tick event.
    pub fn next_wakeup_delay(&self) -> Duration {
        let now = Instant::now();
        let mut dues: Vec<Instant> = Vec::new();
        for l in self.links.values() {
            if !l.alive {
                continue;
            }
            dues.push(l.next_ping_due);
            dues.push(l.last_rx + self.no_response_timeout);
        }
        for s in &self.sessions {
            if !s.paired() {
                dues.push(s.code_expires_at);
            }
            for m in &s.members {
                if m.status == "offline" {
                    if let Some(o) = m.offline_since {
                        dues.push(o + self.resume_window);
                    }
                }
            }
        }
        match dues.into_iter().min() {
            None => Duration::from_secs_f64(60.0),
            Some(d) => {
                let rem = d.saturating_duration_since(now);
                std::cmp::max(rem, Duration::from_millis(50))
            }
        }
    }
}

fn dedup_frame_needs_cross_epoch_tombstone(frame: &Value) -> bool {
    // OPEN_WORK is the one op that skips the epoch/manifestVersion/base
    // gates (see sequenced_op), so it is the only op whose replay needs
    // tombstone protection past an epoch rollover. SET_OFFSET replays are
    // already stopped by those gates and stay in the ordinary FIFO set.
    matches!(
        frame.get("request").and_then(|v| v.as_str()),
        Some("OPEN_WORK")
    )
}

/// Remember an evicted dedup key without storing its outcome. Ordinary
/// tombstones are FIFO-capped; OPEN_WORK tombstones are budgeted in a
/// separate set because a forgotten one turns a replay into executable
/// state again. The budget is a hard cap: once it is exceeded the session
/// is condemned and torn down after the current dispatch, rather than
/// silently aging entries out (correctness loss) or growing memory
/// without bound (availability loss).
fn dedup_tombstone(s: &mut PairSession, key: String, load_op: bool) {
    if load_op {
        s.dedup_load_seen.insert(key);
        if s.dedup_load_seen.len() > DEDUP_LOAD_TOMBSTONE_MAX {
            s.condemned = true;
        }
        return;
    }
    if s.dedup_seen.insert(key.clone()) {
        s.dedup_seen_order.push_back(key);
    }
    while s.dedup_seen.len() > DEDUP_TOMBSTONE_MAX {
        if let Some(o) = s.dedup_seen_order.pop_front() {
            s.dedup_seen.remove(&o);
        } else {
            break;
        }
    }
}

fn dedup_has_tombstone(s: &PairSession, key: &str) -> bool {
    s.dedup_seen.contains(key) || s.dedup_load_seen.contains(key)
}

fn req_id<'a>(req: &'a Map<String, Value>) -> Option<&'a str> {
    req.get("requestId").and_then(|v| v.as_str())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn mk_link(inner: &mut Inner) -> (String, mpsc::Receiver<OutItem>) {
        let (id, rx, _) = mk_link_full(inner, Some(Value::Null), None);
        (id, rx)
    }

    /// Full-handle variant: carries the close-watch receiver so tests can
    /// observe transport-close signals (guest revocation, join flood).
    fn mk_link_full(
        inner: &mut Inner,
        user: Option<Value>,
        guest_token: Option<String>,
    ) -> (
        String,
        mpsc::Receiver<OutItem>,
        watch::Receiver<Option<(u16, String)>>,
    ) {
        let (tx, rx) = mpsc::channel(SEND_QUEUE_MAX);
        let (close_tx, close_rx) = watch::channel(None);
        let link = Link::new(
            tx,
            close_tx,
            user,
            guest_token,
            Instant::now(),
            Duration::from_secs_f64(HEARTBEAT_INTERVAL),
        );
        let id = inner.register_link(link, Duration::from_secs_f64(HEARTBEAT_INTERVAL));
        (id, rx, close_rx)
    }

    fn next_json(rx: &mut mpsc::Receiver<OutItem>) -> Value {
        match rx.try_recv() {
            Ok(OutItem::Text(s)) => serde_json::from_str(&s).unwrap(),
            other => panic!("expected text frame, got {:?}", other.is_ok()),
        }
    }

    fn req(t: &str, sid: &str, payload: Value) -> Map<String, Value> {
        protocol::frame(&[
            ("v", Value::from(protocol::PROTOCOL_VERSION)),
            ("type", Value::String(t.into())),
            ("requestId", Value::String("r1".into())),
            ("sessionId", Value::String(sid.into())),
            ("workEpoch", Value::from(0)),
            ("baseRevision", Value::from(0)),
            ("payload", payload),
        ])
        .as_object()
        .unwrap()
        .clone()
    }

    fn reg_for_test() -> Arc<Registry> {
        Registry::new(
            Arc::new(IndexState::new()),
            PathBuf::from("/nonexistent"),
            None,
        )
    }

    #[tokio::test]
    async fn disabled_member_drive_is_not_advertised() {
        let reg = reg_for_test();
        let mut inner = reg.inner.lock().await;
        inner.member_drive_enabled = false;
        assert!(inner.server_features().is_empty());
        assert_eq!(inner.server_features_json(), Value::Array(Vec::new()));
    }

    fn feature_payload(caps: &[&str]) -> Value {
        protocol::frame(&[(
            "features",
            Value::Array(caps.iter().map(|c| Value::String((*c).into())).collect()),
        )])
    }

    fn req_named(t: &str, sid: &str, payload: Value, rid: &str) -> Map<String, Value> {
        let mut r = req(t, sid, payload);
        r.insert("requestId".into(), Value::String(rid.into()));
        r
    }

    fn drain_texts(rx: &mut mpsc::Receiver<OutItem>) -> Vec<Value> {
        let mut out = Vec::new();
        while let Ok(item) = rx.try_recv() {
            if let OutItem::Text(s) = item {
                out.push(serde_json::from_str(&s).unwrap());
            }
        }
        out
    }

    /// Small but fully valid resolver output — digest, spread table and the
    /// resolver-added initial ref all pass finish_load's validation.
    fn test_manifest() -> Value {
        let pages = vec![
            json!({"folder": "work", "name": "p001.png"}),
            json!({"folder": "work", "name": "p002.png"}),
        ];
        let chapters = json!([{
            "name": "c0",
            "folder": "work",
            "pages": pages.clone(),
        }]);
        let plan_chapters = json!([{
            "folder": "work",
            "pages": ["p001.png", "p002.png"],
        }]);
        let plan = model::build_spread_plan(plan_chapters.as_array().unwrap(), 0).unwrap();
        let spread_table: Vec<Value> = plan.iter().map(model::serialize_spread).collect();
        let mut manifest = json!({
            "v": protocol::PROTOCOL_VERSION,
            "folder": "work",
            "offset": 0,
            "chapters": chapters,
            "pageRefs": pages,
            "spreadTable": spread_table,
        });
        let digest = catalog::manifest_digest(&manifest).unwrap();
        manifest
            .as_object_mut()
            .unwrap()
            .insert("digest".into(), Value::String(digest));
        catalog::finish_resolve(
            manifest,
            &model::PageRef {
                folder: "work".into(),
                name: "p001.png".into(),
            },
        )
        .unwrap()
    }

    fn sequenced_ack(request: &str, request_id: &str, sid: &str) -> Value {
        protocol::frame(&[
            ("v", Value::from(protocol::PROTOCOL_VERSION)),
            ("type", Value::String("ACK".into())),
            ("requestId", Value::String(request_id.into())),
            ("request", Value::String(request.into())),
            ("sessionId", Value::String(sid.into())),
            ("data", Value::Object(Map::new())),
        ])
    }

    /// Paired two-member session whose admission-time capability lists are
    /// controlled by the test. Returns (session, leader link, follower link,
    /// follower member).
    fn paired_session(
        inner: &mut Inner,
        reg: &Arc<Registry>,
        l1: &str,
        r1: &mut mpsc::Receiver<OutItem>,
        l2: &str,
        r2: &mut mpsc::Receiver<OutItem>,
        leader_caps: &[&str],
        follower_caps: &[&str],
    ) -> (String, String) {
        inner.dispatch(reg, l1, &req_named("CREATE", "", feature_payload(leader_caps), "pc-create"));
        let ack1 = next_json(r1);
        let sid = ack1["data"]["sessionId"].as_str().unwrap().to_string();
        let code = ack1["data"]["code"].as_str().unwrap().to_string();
        let join_payload = protocol::frame(&[
            ("code", Value::String(code)),
            ("features", feature_payload(follower_caps)["features"].clone()),
        ]);
        inner.dispatch(reg, l2, &req_named("JOIN", "", join_payload, "pc-join"));
        let ack2 = next_json(r2);
        let mid2 = ack2["data"]["memberId"].as_str().unwrap().to_string();
        let approve = protocol::frame(&[("memberId", Value::String(mid2.clone()))]);
        inner.dispatch(reg, l1, &req_named("APPROVE", &sid, approve, "pc-approve"));
        (sid, mid2)
    }

    #[tokio::test]
    async fn create_join_approve_leave_flow() {
        let reg = reg_for_test();
        let mut inner = reg.inner.lock().await;
        let (l1, mut r1) = mk_link(&mut inner);
        let hello = next_json(&mut r1);
        assert_eq!(hello["type"], "HELLO");
        assert_eq!(hello["link"], l1);

        // CREATE → ACK with code + STATE waiting
        inner.dispatch(&reg, &l1, &req("CREATE", "", Value::Object(Map::new())));
        let ack = next_json(&mut r1);
        assert_eq!(ack["type"], "ACK");
        assert_eq!(ack["data"]["role"], "leader");
        let code = ack["data"]["code"].as_str().unwrap().to_string();
        let sid = ack["data"]["sessionId"].as_str().unwrap().to_string();
        assert_eq!(code.len(), 6);
        let state = next_json(&mut r1);
        assert_eq!(state["type"], "STATE");
        assert_eq!(state["phase"], "waiting");

        // second link JOINs with the code → pending + leader notified
        let (l2, mut r2) = mk_link(&mut inner);
        let _ = next_json(&mut r2); // HELLO
        inner.dispatch(
            &reg,
            &l2,
            &req(
                "JOIN",
                &sid,
                protocol::frame(&[("code", Value::String(code.clone()))]),
            ),
        );
        let ack = next_json(&mut r2);
        assert_eq!(ack["data"]["role"], "follower");
        let join_req = next_json(&mut r1);
        assert_eq!(join_req["type"], "JOIN_REQUEST");

        // leader APPROVEs → paired
        inner.dispatch(&reg, &l1, &req("APPROVE", &sid, Value::Object(Map::new())));
        let _ack = next_json(&mut r1);
        let st = next_json(&mut r1);
        assert_eq!(st["phase"], "selecting");
        let st2 = next_json(&mut r2);
        assert_eq!(st2["phase"], "selecting");

        // sequenced op on unpaired request while paired is fine — but a
        // member bound link cannot CREATE again
        inner.dispatch(&reg, &l1, &req("CREATE", "", Value::Object(Map::new())));
        let rej = next_json(&mut r1);
        assert_eq!(rej["type"], "REJECT");
        assert_eq!(rej["reason"], REASON_ALREADY_BOUND);

        // follower LEAVE → both get END left
        inner.dispatch(&reg, &l2, &req("LEAVE", &sid, Value::Object(Map::new())));
        let e1 = next_json(&mut r1);
        let e2 = next_json(&mut r2);
        assert_eq!(e1["type"], "END");
        assert_eq!(e2["reason"], END_LEFT);
    }

    #[tokio::test]
    async fn join_rejects_and_resume() {
        let reg = reg_for_test();
        let mut inner = reg.inner.lock().await;
        let (l1, mut r1) = mk_link(&mut inner);
        let _ = next_json(&mut r1); // HELLO
                                    // JOIN bad code
        inner.dispatch(
            &reg,
            &l1,
            &req("JOIN", "", protocol::frame(&[("code", "000000".into())])),
        );
        let rej = next_json(&mut r1);
        assert_eq!(rej["reason"], REASON_BAD_CODE);

        // CREATE + RESUME bookkeeping: member offline → RESUME accepted
        inner.dispatch(&reg, &l1, &req("CREATE", "", Value::Object(Map::new())));
        let ack = next_json(&mut r1);
        let _ = next_json(&mut r1); // STATE
        let mid = ack["data"]["memberId"].as_str().unwrap().to_string();
        let sid = ack["data"]["sessionId"].as_str().unwrap().to_string();
        let token = ack["data"]["resumeToken"].as_str().unwrap().to_string();

        inner.detach_link(&l1);
        let (l2, mut r2) = mk_link(&mut inner);
        let _ = next_json(&mut r2);
        inner.dispatch(
            &reg,
            &l2,
            &req(
                "RESUME",
                &sid,
                protocol::frame(&[
                    ("memberId", Value::String(mid)),
                    ("resumeToken", Value::String(token)),
                ]),
            ),
        );
        let ack = next_json(&mut r2);
        assert_eq!(ack["type"], "ACK");
        assert_eq!(ack["data"]["role"], "leader");
    }

    #[tokio::test]
    async fn end_all_resets_every_live_session() {
        let reg = reg_for_test();
        let mut inner = reg.inner.lock().await;
        let (l1, mut r1) = mk_link(&mut inner);
        let _ = next_json(&mut r1); // HELLO
        inner.dispatch(&reg, &l1, &req("CREATE", "", Value::Object(Map::new())));
        let ack = next_json(&mut r1);
        assert_eq!(ack["type"], "ACK");
        let _ = next_json(&mut r1); // STATE
        drop(inner);
        let n = reg.end_all(END_RESET).await;
        assert_eq!(n, 1);
        let end = next_json(&mut r1);
        assert_eq!(end["type"], "END");
        assert_eq!(end["reason"], END_RESET);
        assert!(reg.inner.lock().await.sessions.is_empty());
        // Nothing left to end.
        assert_eq!(reg.end_all(END_RESET).await, 0);
    }

    #[tokio::test]
    async fn guest_link_cannot_join_by_code() {
        let reg = reg_for_test();
        let mut inner = reg.inner.lock().await;
        let leader = protocol::frame(&[
            ("name", "Leader".into()),
            ("email", "leader@example.com".into()),
        ]);
        let (l1, mut r1, _c) = mk_link_full(&mut inner, Some(leader), None);
        let _ = next_json(&mut r1); // HELLO
        inner.dispatch(&reg, &l1, &req("CREATE", "", Value::Object(Map::new())));
        let ack = next_json(&mut r1);
        let _ = next_json(&mut r1); // STATE
        let code = ack["data"]["code"].as_str().unwrap().to_string();
        let gt = ack["data"]["guestToken"].as_str().unwrap().to_string();

        let (l2, mut r2, _) = mk_link_full(&mut inner, Some(Value::Null), Some(gt.clone()));
        let _ = next_json(&mut r2); // HELLO
        // A guest link JOINing a 6-digit code is refused — the code path is
        // members-only; the grant is the only ticket a guest holds.
        inner.dispatch(
            &reg,
            &l2,
            &req("JOIN", "", protocol::frame(&[("code", code.into())])),
        );
        let rej = next_json(&mut r2);
        assert_eq!(rej["type"], "REJECT");
        assert_eq!(rej["reason"], REASON_FORBIDDEN);
        assert!(inner.links.get(&l2).is_some_and(|l| l.alive));

        // Its own grant still joins normally.
        inner.dispatch(
            &reg,
            &l2,
            &req("JOIN", "", protocol::frame(&[("guestToken", gt.into())])),
        );
        let ack = next_json(&mut r2);
        assert_eq!(ack["type"], "ACK");
        assert_eq!(ack["data"]["role"], "follower");
    }

    #[tokio::test]
    async fn join_flood_closes_link() {
        let reg = reg_for_test();
        let mut inner = reg.inner.lock().await;
        let (l1, mut r1, mut close_rx) = mk_link_full(&mut inner, Some(Value::Null), None);
        let _ = next_json(&mut r1); // HELLO
        for _ in 0..JOIN_FAIL_MAX {
            inner.dispatch(
                &reg,
                &l1,
                &req("JOIN", "", protocol::frame(&[("code", "000000".into())])),
            );
            let rej = next_json(&mut r1);
            assert_eq!(rej["reason"], REASON_BAD_CODE);
        }
        assert!(inner.links.get(&l1).is_some_and(|l| l.alive));
        // One guess past the cap: rejected and the transport is closed.
        inner.dispatch(
            &reg,
            &l1,
            &req("JOIN", "", protocol::frame(&[("code", "000000".into())])),
        );
        let _ = next_json(&mut r1);
        close_rx.changed().await.unwrap();
        assert_eq!(close_rx.borrow().as_ref().unwrap().0, WS_CLOSE_POLICY);
        assert!(!inner.links.get(&l1).is_some_and(|l| l.alive));
    }

    #[tokio::test]
    async fn tick_closes_link_when_guest_grant_dies() {
        let reg = reg_for_test();
        let mut inner = reg.inner.lock().await;
        // Unknown bearer → not Valid → the next tick closes the socket.
        let (_lg, mut rg, mut gclose) =
            mk_link_full(&mut inner, Some(Value::Null), Some("stale".into()));
        let _ = next_json(&mut rg); // HELLO
        // A normal link is untouched.
        let (l1, _r1) = mk_link(&mut inner);
        inner.tick();
        gclose.changed().await.unwrap();
        assert_eq!(gclose.borrow().as_ref().unwrap().0, WS_CLOSE_POLICY);
        assert!(inner.links.get(&l1).is_some_and(|l| l.alive));
    }

    #[tokio::test]
    async fn tick_closes_guest_link_when_owner_not_member() {
        // The `viewer-rs users remove` CLI path can't reach
        // end_owner_sessions — the tick's membership re-check is what
        // stops a removed owner's still-Valid grant (and socket) from
        // surviving until a re-enable resurrects it.
        let directory = Arc::new(crate::users::UserDirectory::test_with(&[(
            "member@example.com",
            crate::users::Role::Member,
        )]));
        let reg = Registry::new(
            Arc::new(IndexState::new()),
            PathBuf::from("/nonexistent"),
            Some(directory),
        );
        let mut inner = reg.inner.lock().await;
        // Grant is Valid but its owner isn't a member → dies on tick.
        let ghost = reg
            .guest()
            .issue("ghost@example.com", "s1", Duration::from_secs(3600))
            .unwrap();
        // A second, never-socketed grant for the same owner must die too —
        // revoke_owner is permanent, so a re-enable can't resurrect either.
        let ghost2 = reg
            .guest()
            .issue("ghost@example.com", "s1", Duration::from_secs(3600))
            .unwrap();
        let (_lg, mut rg, mut gclose) =
            mk_link_full(&mut inner, Some(Value::Null), Some(ghost.clone()));
        let _ = next_json(&mut rg); // HELLO
        // A member-owned grant survives the same tick.
        let member = reg
            .guest()
            .issue("member@example.com", "s1", Duration::from_secs(3600))
            .unwrap();
        let (l1, _r1, _c1) =
            mk_link_full(&mut inner, Some(Value::Null), Some(member.clone()));
        inner.tick();
        gclose.changed().await.unwrap();
        assert_eq!(gclose.borrow().as_ref().unwrap().0, WS_CLOSE_POLICY);
        assert!(inner.links.get(&l1).is_some_and(|l| l.alive));
        // Both ghost grants are revoked outright; the member's stays Valid.
        assert!(matches!(
            reg.guest().status(&ghost),
            GrantStatus::Unknown
        ));
        assert!(matches!(
            reg.guest().status(&ghost2),
            GrantStatus::Unknown
        ));
        assert!(matches!(
            reg.guest().status(&member),
            GrantStatus::Valid(_)
        ));
    }

    #[tokio::test]
    async fn end_session_closes_guest_socket() {
        let reg = reg_for_test();
        let mut inner = reg.inner.lock().await;
        let leader = protocol::frame(&[
            ("name", "Leader".into()),
            ("email", "leader@example.com".into()),
        ]);
        let (l1, mut r1, _c) = mk_link_full(&mut inner, Some(leader), None);
        let _ = next_json(&mut r1); // HELLO
        inner.dispatch(&reg, &l1, &req("CREATE", "", Value::Object(Map::new())));
        let ack = next_json(&mut r1);
        let _ = next_json(&mut r1); // STATE
        let sid = ack["data"]["sessionId"].as_str().unwrap().to_string();
        let gt = ack["data"]["guestToken"].as_str().unwrap().to_string();
        let (l2, mut r2, _gclose) =
            mk_link_full(&mut inner, Some(Value::Null), Some(gt.clone()));
        let _ = next_json(&mut r2); // HELLO
        inner.dispatch(
            &reg,
            &l2,
            &req("JOIN", "", protocol::frame(&[("guestToken", gt.into())])),
        );
        let _ = next_json(&mut r2); // ACK
        inner.end_session(&sid, END_LEFT);
        let _ = next_json(&mut r2); // END frame reaches the client first
        match r2.try_recv() {
            Ok(OutItem::Close(code, _)) => assert_eq!(code, WS_CLOSE_NORMAL),
            other => panic!("expected queued close after END, got {:?}", other.is_ok()),
        }
    }

    #[tokio::test]
    async fn tick_does_not_bypass_a_queued_ordered_close() {
        // END is queued ahead of the ordered close — a tick racing the
        // drain must not fire the watch-channel bypass close (that would
        // truncate the END and leave the guest retrying a dead grant
        // until its own deadline).
        let reg = reg_for_test();
        let mut inner = reg.inner.lock().await;
        let leader = protocol::frame(&[
            ("name", "Leader".into()),
            ("email", "leader@example.com".into()),
        ]);
        let (l1, mut r1, _c) = mk_link_full(&mut inner, Some(leader), None);
        let _ = next_json(&mut r1); // HELLO
        inner.dispatch(&reg, &l1, &req("CREATE", "", Value::Object(Map::new())));
        let ack = next_json(&mut r1);
        let _ = next_json(&mut r1); // STATE
        let sid = ack["data"]["sessionId"].as_str().unwrap().to_string();
        let gt = ack["data"]["guestToken"].as_str().unwrap().to_string();
        let (l2, mut r2, gclose) =
            mk_link_full(&mut inner, Some(Value::Null), Some(gt.clone()));
        let _ = next_json(&mut r2); // HELLO
        inner.dispatch(
            &reg,
            &l2,
            &req("JOIN", "", protocol::frame(&[("guestToken", gt.into())])),
        );
        let _ = next_json(&mut r2); // ACK
        inner.end_session(&sid, END_LEFT);
        // The grant is dead NOW — tick's revalidation must skip the link
        // (close_queued) instead of firing the bypass watch close.
        inner.tick();
        assert!(!gclose.has_changed().unwrap());
        let _ = next_json(&mut r2); // END flushes ahead of the ordered close
        match r2.try_recv() {
            Ok(OutItem::Close(code, _)) => assert_eq!(code, WS_CLOSE_NORMAL),
            other => panic!("expected queued close after END, got {:?}", other.is_ok()),
        }
    }

    #[tokio::test]
    async fn parked_ordered_close_retries_without_truncating() {
        // A full outq must park the ordered close — the watch-channel
        // bypass would truncate the frames it is ordered behind (the
        // owner_disabled END flake). Once the drain frees a slot the
        // parked close lands at the queue tail.
        let reg = reg_for_test();
        let mut inner = reg.inner.lock().await;
        let (lid, mut rx, close_rx) =
            mk_link_full(&mut inner, Some(Value::Null), None);
        let _ = next_json(&mut rx); // HELLO
        {
            let l = inner.links.get_mut(&lid).unwrap();
            while l
                .outq
                .try_send(OutItem::Text("x".to_string()))
                .is_ok()
            {}
            l.queue_transport_close(WS_CLOSE_NORMAL, "session_end");
            assert!(l.close_queued && l.pending_close.is_some());
        }
        // Full → no Close item and NO watch-channel bypass.
        assert!(rx.try_recv().is_ok());
        assert!(rx.try_recv().is_ok()); // one slot drained below
        assert!(!close_rx.has_changed().unwrap());
        // First freed slot must be taken by the retried Close, not by a
        // bypass (the watch stays silent until the Close item delivers).
        inner.tick();
        let mut saw_close = false;
        while let Ok(item) = rx.try_recv() {
            if matches!(item, OutItem::Close(..)) {
                saw_close = true;
            }
        }
        assert!(saw_close);
        let l = inner.links.get(&lid).unwrap();
        assert!(l.pending_close.is_none());
        assert!(!close_rx.has_changed().unwrap());
    }

    #[tokio::test]
    async fn wedged_ordered_close_eventually_bypasses() {
        // If the queue never drains past the no-response budget the
        // transport is dead — the parked close gives up and fires the
        // bypass (nothing left to protect).
        let reg = reg_for_test();
        let mut inner = reg.inner.lock().await;
        let (lid, _rx, close_rx) =
            mk_link_full(&mut inner, Some(Value::Null), None);
        {
            let l = inner.links.get_mut(&lid).unwrap();
            // The retry must still see a FULL queue — only then does the
            // grace-expired park give up and fire the bypass.
            while l
                .outq
                .try_send(OutItem::Text("x".to_string()))
                .is_ok()
            {}
            l.pending_close = Some((
                WS_CLOSE_NORMAL,
                "session_end".to_string(),
                Instant::now()
                    - Duration::from_secs_f64(NO_RESPONSE_TIMEOUT + 1.0),
            ));
            l.close_queued = true;
        }
        inner.tick();
        assert_eq!(
            close_rx.borrow().as_ref().map(|(c, _)| *c),
            Some(WS_CLOSE_NORMAL)
        );
    }

    #[tokio::test]
    async fn match_origin_session_mints_no_guest_token() {
        let reg = reg_for_test();
        let mut inner = reg.inner.lock().await;
        // Email-bearing user — a Null-user link could never mint a token,
        // which would make this assertion vacuous.
        let mut u = Map::new();
        u.insert("email".into(), Value::String("owner@example.com".into()));
        u.insert("name".into(), Value::String("Owner".into()));
        let (l1, mut r1, _close) = mk_link_full(&mut inner, Some(Value::Object(u)), None);
        let _ = next_json(&mut r1); // HELLO
        inner.dispatch(&reg, &l1, &req("MATCH", "", Value::Object(Map::new())));
        let ack = next_json(&mut r1);
        assert_eq!(ack["type"], "ACK");
        assert!(ack["data"]["guestToken"].is_null());
        assert_eq!(ack["data"]["role"], "leader");
    }

    #[tokio::test]
    async fn guest_ui_sync_cannot_start_closed_shared_browse() {
        let reg = reg_for_test();
        let mut inner = reg.inner.lock().await;
        let leader_user = protocol::frame(&[
            ("name", "Leader".into()),
            ("email", "leader@example.com".into()),
        ]);
        let (l1, mut r1, _c1) = mk_link_full(&mut inner, Some(leader_user), None);
        let _ = next_json(&mut r1); // HELLO
        inner.dispatch(
            &reg,
            &l1,
            &req_named("CREATE", "", feature_payload(&[CAP_MEMBER_DRIVE]), "gus-create"),
        );
        let ack = next_json(&mut r1);
        let sid = ack["data"]["sessionId"].as_str().unwrap().to_string();
        let gt = ack["data"]["guestToken"].as_str().unwrap().to_string();
        let _ = next_json(&mut r1); // waiting STATE

        let (l2, mut r2, _c2) =
            mk_link_full(&mut inner, Some(Value::Null), Some(gt.clone()));
        let _ = next_json(&mut r2); // HELLO
        let join_payload = protocol::frame(&[
            ("guestToken", Value::String(gt)),
            ("features", feature_payload(&[CAP_MEMBER_DRIVE])["features"].clone()),
        ]);
        inner.dispatch(&reg, &l2, &req_named("JOIN", "", join_payload, "gus-join"));
        let ack2 = next_json(&mut r2);
        let mid2 = ack2["data"]["memberId"].as_str().unwrap().to_string();
        let approve = protocol::frame(&[("memberId", Value::String(mid2))]);
        inner.dispatch(&reg, &l1, &req_named("APPROVE", &sid, approve, "gus-approve"));

        let ui = || {
            protocol::frame(&[
                ("view", "browse".into()),
                ("path", Value::Null),
                ("top", Value::from(0)),
                ("viewH", Value::from(600)),
            ])
        };
        // No member has opened shared browse yet (the initial pre-commit
        // selecting phase is not itself an open browse). A guest UI_SYNC must
        // not become a de facto open on the host.
        inner.dispatch(&reg, &l2, &req_named("UI_SYNC", &sid, ui(), "guest-ui-closed"));
        let frames: Vec<Value> = std::iter::from_fn(|| match r2.try_recv() {
            Ok(OutItem::Text(raw)) => serde_json::from_str(&raw).ok(),
            _ => None,
        })
        .collect();
        assert!(frames.iter().any(|f| {
            f["type"] == "REJECT"
                && f["requestId"] == "guest-ui-closed"
                && f["reason"] == REASON_FORBIDDEN
        }), "guest UI_SYNC while browse is closed must be forbidden: {frames:?}");
        let leader_frames: Vec<Value> = std::iter::from_fn(|| match r1.try_recv() {
            Ok(OutItem::Text(raw)) => serde_json::from_str(&raw).ok(),
            _ => None,
        })
        .collect();
        assert!(!leader_frames.iter().any(|f| f["type"] == "UI_SYNC"),
            "closed-browse guest frame must not reach the host: {leader_frames:?}");

        // Once a member really opens shared browse, ordinary ephemeral mirror
        // frames remain relayable — the prohibition is on STARTING it, not on
        // participating in an already-open mirror.
        inner.dispatch(
            &reg,
            &l1,
            &req_named(
                "SET_ACTIVITY",
                &sid,
                protocol::frame(&[("state", "selecting".into())]),
                "member-open",
            ),
        );
        // Both sides advertise member-drive, so legitimate frames are
        // generation-tagged — an untagged frame would drop as a straggler.
        let mut tagged = ui();
        tagged["browseGeneration"] = Value::from(1);
        inner.dispatch(&reg, &l2, &req_named("UI_SYNC", &sid, tagged, "guest-ui-open"));
        let mut forwarded = false;
        while let Ok(OutItem::Text(raw)) = r1.try_recv() {
            let f: Value = serde_json::from_str(&raw).unwrap();
            if f["type"] == "UI_SYNC" {
                forwarded = true;
                break;
            }
        }
        assert!(forwarded, "open shared browse must still relay guest mirror frames");
    }

    #[tokio::test]
    async fn member_ui_sync_pre_commit_picking_mirror_relays() {
        // Before the first work commit there is no shared browse to open:
        // member mirror traffic is the casual picking mirror (the picking
        // process itself is shared) and relays even though nothing was
        // opened — exactly as it did before the shared-browse encoding.
        let reg = reg_for_test();
        let mut inner = reg.inner.lock().await;
        let (l1, mut r1) = mk_link(&mut inner);
        let _ = next_json(&mut r1);
        let (l2, mut r2) = mk_link(&mut inner);
        let _ = next_json(&mut r2);
        let (sid, _mid2) = paired_session(
            &mut inner, &reg, &l1, &mut r1, &l2, &mut r2,
            &[CAP_MEMBER_DRIVE], &[CAP_MEMBER_DRIVE],
        );
        assert!(inner.session(&sid).unwrap().manifest.is_none());
        let ui = || {
            protocol::frame(&[
                ("view", "browse".into()),
                ("path", Value::Null),
                ("top", Value::from(0)),
                ("viewH", Value::from(600)),
            ])
        };
        inner.dispatch(&reg, &l2, &req_named("UI_SYNC", &sid, ui(), "member-ui-precommit"));
        let sender_frames: Vec<Value> = std::iter::from_fn(|| match r2.try_recv() {
            Ok(OutItem::Text(raw)) => serde_json::from_str(&raw).ok(),
            _ => None,
        }).collect();
        assert!(
            !sender_frames.iter().any(|f| f["type"] == "REJECT"),
            "a member picking mirror must not be forbidden: {sender_frames:?}"
        );
        let leader_frames: Vec<Value> = std::iter::from_fn(|| match r1.try_recv() {
            Ok(OutItem::Text(raw)) => serde_json::from_str(&raw).ok(),
            _ => None,
        }).collect();
        assert!(
            leader_frames.iter().any(|f| f["type"] == "UI_SYNC"),
            "the picking mirror must relay to the peer pre-commit: {leader_frames:?}"
        );

        inner.dispatch(
            &reg,
            &l2,
            &req_named(
                "SET_ACTIVITY",
                &sid,
                protocol::frame(&[("state", "selecting".into())]),
                "member-open-ui",
            ),
        );
        let mut tagged = ui();
        tagged["browseGeneration"] = Value::from(1);
        inner.dispatch(&reg, &l2, &req_named("UI_SYNC", &sid, tagged, "member-ui-open"));
        let mut forwarded = false;
        while let Ok(OutItem::Text(raw)) = r1.try_recv() {
            let f: Value = serde_json::from_str(&raw).unwrap();
            if f["type"] == "UI_SYNC" {
                forwarded = true;
                break;
            }
        }
        assert!(forwarded);
    }

    #[tokio::test]
    async fn guest_member_cannot_resume_with_member_credential() {
        // Reverse direction of the trust-mismatch matrix: admission fixed the
        // follower's trust as guest, so presenting a member session cookie on
        // RESUME must still fail closed rather than upgrading the bearer.
        let reg = reg_for_test();
        let mut inner = reg.inner.lock().await;
        let leader_user = protocol::frame(&[
            ("name", "Leader".into()),
            ("email", "leader@example.com".into()),
        ]);
        let (l1, mut r1, _c1) = mk_link_full(&mut inner, Some(leader_user.clone()), None);
        let _ = next_json(&mut r1);
        inner.dispatch(&reg, &l1, &req_named("CREATE", "", feature_payload(&[]), "tm-create"));
        let ack = next_json(&mut r1);
        let sid = ack["data"]["sessionId"].as_str().unwrap().to_string();
        let gt = ack["data"]["guestToken"].as_str().unwrap().to_string();

        let (lg, mut rg, _cg) = mk_link_full(&mut inner, Some(Value::Null), Some(gt.clone()));
        let _ = next_json(&mut rg);
        inner.dispatch(
            &reg,
            &lg,
            &req_named(
                "JOIN",
                "",
                protocol::frame(&[("guestToken", Value::String(gt))]),
                "tm-join",
            ),
        );
        let join_ack = next_json(&mut rg);
        let mid = join_ack["data"]["memberId"].as_str().unwrap().to_string();
        let token = join_ack["data"]["resumeToken"].as_str().unwrap().to_string();
        inner.dispatch(
            &reg,
            &l1,
            &req_named(
                "APPROVE",
                &sid,
                protocol::frame(&[("memberId", Value::String(mid.clone()))]),
                "tm-approve",
            ),
        );
        inner.detach_link(&lg);

        let (lm, mut rm, _cm) = mk_link_full(&mut inner, Some(leader_user), None);
        let _ = next_json(&mut rm);
        inner.dispatch(
            &reg,
            &lm,
            &req_named(
                "RESUME",
                &sid,
                protocol::frame(&[
                    ("memberId", Value::String(mid)),
                    ("resumeToken", Value::String(token)),
                ]),
                "tm-resume",
            ),
        );
        let mut rej = Value::Null;
        while let Ok(OutItem::Text(raw)) = rm.try_recv() {
            let f: Value = serde_json::from_str(&raw).unwrap();
            if f["type"] == "REJECT" && f["requestId"] == "tm-resume" {
                rej = f;
                break;
            }
        }
        assert_eq!(rej["reason"], REASON_TRUST_MISMATCH);
    }

    #[tokio::test]
    async fn mixed_capability_pair_keeps_leader_only_browse_close() {
        let reg = reg_for_test();
        let mut inner = reg.inner.lock().await;
        let (l1, mut r1) = mk_link(&mut inner);
        let _ = next_json(&mut r1);
        let (l2, mut r2) = mk_link(&mut inner);
        let _ = next_json(&mut r2);
        let (sid, _mid2) = paired_session(
            &mut inner, &reg, &l1, &mut r1, &l2, &mut r2, &[CAP_MEMBER_DRIVE], &[],
        );
        // A one-sided advertisement is not an intersection: follower open is
        // still leader-only even though the new-capable leader is present.
        inner.dispatch(
            &reg,
            &l2,
            &req_named(
                "SET_ACTIVITY",
                &sid,
                protocol::frame(&[("state", "selecting".into())]),
                "legacy-open",
            ),
        );
        let mut rej = Value::Null;
        for _ in 0..8 {
            match r2.try_recv() {
                Ok(OutItem::Text(raw)) => {
                    let f: Value = serde_json::from_str(&raw).unwrap();
                    if f["type"] == "REJECT" && f["requestId"] == "legacy-open" {
                        rej = f;
                        break;
                    }
                }
                _ => break,
            }
        }
        assert_eq!(rej["type"], "REJECT");
        assert_eq!(rej["reason"], REASON_NOT_LEADER);
        assert!(!inner.session(&sid).unwrap().browse_open);
        // Leader opens; follower close is inert; leader non-selecting activity closes.
        inner.dispatch(
            &reg,
            &l1,
            &req_named(
                "SET_ACTIVITY",
                &sid,
                protocol::frame(&[("state", "selecting".into())]),
                "leader-open",
            ),
        );
        assert!(inner.session(&sid).unwrap().browse_open);
        inner.dispatch(
            &reg,
            &l2,
            &req_named(
                "SET_ACTIVITY",
                &sid,
                protocol::frame(&[
                    ("state", "active".into()),
                    ("endBrowse", Value::Bool(true)),
                ]),
                "follower-close",
            ),
        );
        assert!(inner.session(&sid).unwrap().browse_open);
        inner.dispatch(
            &reg,
            &l1,
            &req_named(
                "SET_ACTIVITY",
                &sid,
                protocol::frame(&[("state", "active".into())]),
                "leader-close",
            ),
        );
        assert!(!inner.session(&sid).unwrap().browse_open);
    }

    #[tokio::test]
    async fn resume_capability_downgrade_closes_shared_browse() {
        let reg = reg_for_test();
        let mut inner = reg.inner.lock().await;
        let (l1, mut r1) = mk_link(&mut inner);
        let _ = next_json(&mut r1);
        let (l2, mut r2) = mk_link(&mut inner);
        let _ = next_json(&mut r2);
        let (sid, mid2) = paired_session(
            &mut inner, &reg, &l1, &mut r1, &l2, &mut r2,
            &[CAP_MEMBER_DRIVE], &[CAP_MEMBER_DRIVE],
        );
        inner.dispatch(
            &reg,
            &l2,
            &req_named(
                "SET_ACTIVITY",
                &sid,
                protocol::frame(&[("state", "selecting".into())]),
                "downgrade-open",
            ),
        );
        assert!(inner.session(&sid).unwrap().browse_open);

        let token = inner
            .session(&sid)
            .and_then(|s| s.members.iter().find(|m| m.id == mid2))
            .map(|m| m.resume_token.clone())
            .unwrap();
        inner.detach_link(&l2);
        let (l3, mut r3) = mk_link(&mut inner);
        let _ = next_json(&mut r3); // HELLO
        inner.dispatch(
            &reg,
            &l3,
            &req_named(
                "RESUME",
                &sid,
                protocol::frame(&[
                    ("memberId", Value::String(mid2.clone())),
                    ("resumeToken", Value::String(token)),
                    ("features", Value::Array(vec![])),
                ]),
                "downgrade-resume",
            ),
        );
        let ack = next_json(&mut r3);
        assert_eq!(ack["type"], "ACK");
        let state = next_json(&mut r3);
        assert_eq!(state["type"], "STATE");
        assert!(!inner.session(&sid).unwrap().browse_open);
        assert_eq!(state.get("browse"), Some(&Value::Bool(false)),
            "capability-loss close must stay explicit after the intersection is gone: {state:?}");
        assert_eq!(state.get("browseGeneration"), Some(&Value::from(1)));
        assert_eq!(state["presence"][mid2]["features"], Value::Array(vec![]));
    }

    #[tokio::test]
    async fn trigger_ack_retained_on_job_survives_dedup_eviction_until_commit() {
        let reg = reg_for_test();
        let mut inner = reg.inner.lock().await;
        let (l1, mut r1) = mk_link(&mut inner);
        let _ = next_json(&mut r1);
        let (l2, mut r2) = mk_link(&mut inner);
        let _ = next_json(&mut r2);
        let (sid, _mid2) = paired_session(
            &mut inner,
            &reg,
            &l1,
            &mut r1,
            &l2,
            &mut r2,
            &[CAP_MEMBER_DRIVE],
            &[CAP_MEMBER_DRIVE],
        );
        drain_texts(&mut r1);
        drain_texts(&mut r2);

        let page = protocol::frame(&[
            ("folder", "work".into()),
            ("name", "p001.png".into()),
        ]);
        let payload = protocol::frame(&[
            ("folder", "work".into()),
            ("page", page),
        ]);
        let open = req_named("OPEN_WORK", &sid, payload, "trigger-open");
        inner.dispatch(&reg, &l1, &open);
        let job = inner
            .session(&sid)
            .and_then(|s| s.pending_load.clone())
            .expect("accepted OPEN_WORK leaves a pending load");
        assert!(
            job.ack_frame.is_some(),
            "the accepted trigger outcome must be retained outside the cache"
        );

        // unrelated sequenced outcomes fill the fixed window while the load is
        // still unresolved. The trigger ACK leaves `dedup` and becomes only a
        // tombstone there; the job copy is the sole replayable outcome.
        for i in 0..DEDUP_WINDOW {
            let rid = format!("filler-{i}");
            let frame = sequenced_ack("TURN", &rid, &sid);
            let key = format!("leader:{rid}");
            inner.dedup_store(&sid, &key, frame);
        }
        let trigger_key = "leader:trigger-open";
        {
            let s = inner.session(&sid).unwrap();
            assert!(!s.dedup.contains_key(trigger_key));
            assert!(dedup_has_tombstone(s, trigger_key));
        }

        inner.finish_load(&sid, &job, Ok(test_manifest()));
        {
            let s = inner.session(&sid).unwrap();
            assert_eq!(s.work_epoch, 1);
            let carried = s
                .dedup
                .get(trigger_key)
                .expect("trigger ACK must carry across the epoch commit");
            assert_eq!(carried["type"], "ACK");
            assert_eq!(carried["requestId"], "trigger-open");
        }

        drain_texts(&mut r1);
        drain_texts(&mut r2);
        inner.dispatch(&reg, &l1, &open);
        let frames = drain_texts(&mut r1);
        assert_eq!(
            frames.len(),
            1,
            "a replay must be the cached ACK alone, never a second commit: {frames:?}"
        );
        assert_eq!(frames[0]["type"], "ACK");
        assert_eq!(frames[0]["requestId"], "trigger-open");
        assert!(inner.session(&sid).unwrap().pending_load.is_none());
    }

    #[tokio::test]
    async fn set_offset_trigger_ack_carries_across_epoch_commit() {
        // SET_OFFSET is a load job too: a non-no-op flip starts a resolver
        // whose commit rolls the epoch. Its trigger ACK must carry across the
        // rollover exactly like OPEN_WORK's — otherwise a post-commit resend
        // is answered stale_request instead of the identical ACK, and the
        // fail-closed path would burn OPEN_WORK-only tombstone budget on a
        // key the epoch gate already protects.
        let reg = reg_for_test();
        let mut inner = reg.inner.lock().await;
        let (l1, mut r1) = mk_link(&mut inner);
        let _ = next_json(&mut r1);
        let (l2, mut r2) = mk_link(&mut inner);
        let _ = next_json(&mut r2);
        let (sid, _mid2) = paired_session(
            &mut inner,
            &reg,
            &l1,
            &mut r1,
            &l2,
            &mut r2,
            &[CAP_MEMBER_DRIVE],
            &[CAP_MEMBER_DRIVE],
        );
        drain_texts(&mut r1);
        drain_texts(&mut r2);

        // Open a work so SET_OFFSET has something to resolve against.
        let page = protocol::frame(&[
            ("folder", "work".into()),
            ("name", "p001.png".into()),
        ]);
        let open_payload = protocol::frame(&[
            ("folder", "work".into()),
            ("page", page),
        ]);
        let open = req_named("OPEN_WORK", &sid, open_payload, "open-1");
        inner.dispatch(&reg, &l1, &open);
        let job1 = inner
            .session(&sid)
            .and_then(|s| s.pending_load.clone())
            .expect("OPEN_WORK leaves a pending load");
        inner.finish_load(&sid, &job1, Ok(test_manifest()));
        assert_eq!(inner.session(&sid).unwrap().work_epoch, 1);
        drain_texts(&mut r1);
        drain_texts(&mut r2);

        // A non-no-op flip starts a second load job. The session is at epoch
        // 1 after the open commit, and SET_OFFSET is epoch-gated.
        let flip_payload = protocol::frame(&[("offset", Value::from(1))]);
        let mut flip = req_named("SET_OFFSET", &sid, flip_payload, "flip-1");
        {
            let s = inner.session(&sid).unwrap();
            flip.insert("workEpoch".into(), Value::from(s.work_epoch));
            flip.insert("baseRevision".into(), Value::from(s.revision));
        }
        inner.dispatch(&reg, &l1, &flip);
        let job2 = inner
            .session(&sid)
            .and_then(|s| s.pending_load.clone())
            .expect("non-no-op SET_OFFSET starts a load job");
        assert_eq!(job2.kind, "set_offset");
        inner.finish_load(&sid, &job2, Ok(test_manifest()));
        {
            let s = inner.session(&sid).unwrap();
            assert_eq!(s.work_epoch, 2);
            let carried = s
                .dedup
                .get("leader:flip-1")
                .expect("SET_OFFSET trigger ACK must carry across its epoch commit");
            assert_eq!(carried["type"], "ACK");
            assert_eq!(carried["request"], "SET_OFFSET");
            assert!(
                !s.dedup_load_seen.contains("leader:flip-1"),
                "an epoch-gated trigger must not consume OPEN_WORK tombstone budget"
            );
        }

        // Resend: the identical cached ACK alone, never stale_request,
        // never a second load.
        drain_texts(&mut r1);
        drain_texts(&mut r2);
        inner.dispatch(&reg, &l1, &flip);
        let frames = drain_texts(&mut r1);
        assert_eq!(
            frames.len(),
            1,
            "resend must replay the cached ACK alone: {frames:?}"
        );
        assert_eq!(frames[0]["type"], "ACK");
        assert_eq!(frames[0]["requestId"], "flip-1");
        assert!(inner.session(&sid).unwrap().pending_load.is_none());
    }

    #[tokio::test]
    async fn load_tombstone_survives_overflow_and_replay_fails_closed() {
        let reg = reg_for_test();
        let mut inner = reg.inner.lock().await;
        let (l1, mut r1) = mk_link(&mut inner);
        let _ = next_json(&mut r1);
        let (l2, mut r2) = mk_link(&mut inner);
        let _ = next_json(&mut r2);
        let (sid, _mid2) = paired_session(
            &mut inner,
            &reg,
            &l1,
            &mut r1,
            &l2,
            &mut r2,
            &[CAP_MEMBER_DRIVE],
            &[CAP_MEMBER_DRIVE],
        );
        drain_texts(&mut r1);
        drain_texts(&mut r2);

        let load_key = "leader:evicted-load";
        inner.dedup_store(
            &sid,
            load_key,
            sequenced_ack("OPEN_WORK", "evicted-load", &sid),
        );
        // Evict the full load outcome through the real fixed-window path.
        // Classification at this edge — not a test-only backdoor — is what
        // places the key in the dedicated load tombstone area.
        for i in 0..DEDUP_WINDOW {
            let rid = format!("evict-filler-{i}");
            let frame = sequenced_ack("TURN", &rid, &sid);
            inner.dedup_store(&sid, &format!("leader:{rid}"), frame);
        }
        {
            let s = inner.session_mut(&sid).unwrap();
            assert!(!s.dedup.contains_key(load_key));
            assert!(s.dedup_load_seen.contains(load_key));
            s.dedup_seen.clear();
            s.dedup_seen_order.clear();
            for i in 0..=DEDUP_TOMBSTONE_MAX {
                dedup_tombstone(s, format!("leader:filler-{i}"), false);
            }
            assert!(
                dedup_has_tombstone(s, load_key),
                "ordinary tombstone FIFO overflow must not erase a load key"
            );
            assert!(
                !s.dedup_seen.contains("leader:filler-0"),
                "the ordinary FIFO really overflowed"
            );
        }

        let page = protocol::frame(&[
            ("folder", "work".into()),
            ("name", "p001.png".into()),
        ]);
        let payload = protocol::frame(&[
            ("folder", "work".into()),
            ("page", page),
        ]);
        let resend = req_named("OPEN_WORK", &sid, payload, "evicted-load");
        inner.dispatch(&reg, &l1, &resend);
        let frames = drain_texts(&mut r1);
        assert_eq!(frames.len(), 1, "unexpected frames: {frames:?}");
        assert_eq!(frames[0]["type"], "REJECT");
        assert_eq!(frames[0]["reason"], state::REASON_STALE_REQUEST);
        assert!(inner.session(&sid).unwrap().pending_load.is_none());
    }

    #[tokio::test]
    async fn load_tombstone_overflow_condemns_and_ends_session() {
        let reg = reg_for_test();
        let mut inner = reg.inner.lock().await;
        let (l1, mut r1) = mk_link(&mut inner);
        let _ = next_json(&mut r1);
        let (l2, mut r2) = mk_link(&mut inner);
        let _ = next_json(&mut r2);
        let (sid, _mid2) = paired_session(
            &mut inner,
            &reg,
            &l1,
            &mut r1,
            &l2,
            &mut r2,
            &[CAP_MEMBER_DRIVE],
            &[CAP_MEMBER_DRIVE],
        );
        drain_texts(&mut r1);
        drain_texts(&mut r2);

        // Exceed the dedicated budget through the tombstone primitive itself.
        // Honest sessions never come close — a work pick is human-scale — so
        // crossing it means a flood, and the session must die rather than
        // choose between unbounded memory and dropped replay protection.
        {
            let s = inner.session_mut(&sid).unwrap();
            for i in 0..=DEDUP_LOAD_TOMBSTONE_MAX {
                dedup_tombstone(s, format!("leader:flood-{i}"), true);
            }
            assert!(s.condemned);
        }
        // The teardown fires only after the next dispatch unwinds.
        inner.dispatch(
            &reg,
            &l1,
            &req_named(
                "SET_ACTIVITY",
                &sid,
                protocol::frame(&[("state", "active".into())]),
                "post-flood-ping",
            ),
        );
        let frames1 = drain_texts(&mut r1);
        let frames2 = drain_texts(&mut r2);
        let end1 = frames1.iter().find(|f| f["type"] == "END");
        let end2 = frames2.iter().find(|f| f["type"] == "END");
        assert!(
            end1.is_some_and(|f| f["reason"] == END_DEDUP_EXHAUSTED),
            "leader must see the exhausted END: {frames1:?}"
        );
        assert!(
            end2.is_some_and(|f| f["reason"] == END_DEDUP_EXHAUSTED),
            "follower must see the exhausted END: {frames2:?}"
        );
        assert!(inner.session(&sid).is_none());
    }

    #[tokio::test]
    async fn ui_sync_generation_boundary_drop() {
        let reg = reg_for_test();
        let mut inner = reg.inner.lock().await;
        let (l1, mut r1) = mk_link(&mut inner);
        let _ = next_json(&mut r1);
        let (l2, mut r2) = mk_link(&mut inner);
        let _ = next_json(&mut r2);
        let (sid, _mid2) = paired_session(
            &mut inner,
            &reg,
            &l1,
            &mut r1,
            &l2,
            &mut r2,
            &[CAP_MEMBER_DRIVE],
            &[CAP_MEMBER_DRIVE],
        );
        drain_texts(&mut r1);
        drain_texts(&mut r2);

        let ui = |gen: Option<i64>| {
            let mut p = protocol::frame(&[
                ("view", "browse".into()),
                ("top", Value::from(10)),
                ("viewH", Value::from(100)),
            ]);
            if let Some(g) = gen {
                p["browseGeneration"] = Value::from(g);
            }
            req_named("UI_SYNC", &sid, p, "ui")
        };
        let relayed_gen = |rx: &mut mpsc::Receiver<OutItem>| -> Option<i64> {
            drain_texts(rx)
                .iter()
                .find(|f| f["type"] == "UI_SYNC")
                .and_then(|f| f["payload"]["browseGeneration"].as_i64())
        };

        // Open the shared browse — generation 1 begins.
        inner.dispatch(
            &reg,
            &l1,
            &req_named(
                "SET_ACTIVITY",
                &sid,
                protocol::frame(&[("state", "selecting".into())]),
                "open-1",
            ),
        );
        drain_texts(&mut r1);
        drain_texts(&mut r2);
        assert_eq!(
            inner.session(&sid).unwrap().browse_generation,
            1,
            "the first closed-to-open transition owns generation 1"
        );

        // Matching generation relays; mismatched drops. Untagged drops too:
        // both members advertise member-drive, so every legitimate build
        // tags — an untagged frame can only be a pre-upgrade straggler.
        inner.dispatch(&reg, &l1, &ui(Some(1)));
        assert_eq!(relayed_gen(&mut r2), Some(1));
        inner.dispatch(&reg, &l1, &ui(Some(99)));
        assert_eq!(relayed_gen(&mut r2), None, "future/stale gen must drop");
        inner.dispatch(&reg, &l1, &ui(None));
        assert!(
            !drain_texts(&mut r2).iter().any(|f| f["type"] == "UI_SYNC"),
            "untagged frames drop once the member-drive intersection holds"
        );

        // Close, reopen — generation 2. The old generation is dead on arrival.
        inner.dispatch(
            &reg,
            &l1,
            &req_named(
                "SET_ACTIVITY",
                &sid,
                protocol::frame(&[
                    ("state", "active".into()),
                    ("endBrowse", Value::Bool(true)),
                ]),
                "close-1",
            ),
        );
        drain_texts(&mut r1);
        drain_texts(&mut r2);
        inner.dispatch(
            &reg,
            &l1,
            &req_named(
                "SET_ACTIVITY",
                &sid,
                protocol::frame(&[("state", "selecting".into())]),
                "open-2",
            ),
        );
        drain_texts(&mut r1);
        drain_texts(&mut r2);
        assert_eq!(inner.session(&sid).unwrap().browse_generation, 2);

        inner.dispatch(&reg, &l1, &ui(Some(1)));
        assert_eq!(
            relayed_gen(&mut r2),
            None,
            "a frame causally bound to the dead generation must not cross the boundary"
        );
        inner.dispatch(&reg, &l1, &ui(Some(2)));
        assert_eq!(relayed_gen(&mut r2), Some(2));
    }

    #[tokio::test]
    async fn bootstrap_auto_rejects_while_deliberate_load_in_flight() {
        let reg = reg_for_test();
        let mut inner = reg.inner.lock().await;
        let (l1, mut r1) = mk_link(&mut inner);
        let _ = next_json(&mut r1);
        let (l2, mut r2) = mk_link(&mut inner);
        let _ = next_json(&mut r2);
        let (sid, _mid2) = paired_session(
            &mut inner,
            &reg,
            &l1,
            &mut r1,
            &l2,
            &mut r2,
            &[CAP_MEMBER_DRIVE],
            &[CAP_MEMBER_DRIVE],
        );
        let page = protocol::frame(&[
            ("folder", "work".into()),
            ("name", "p001.png".into()),
        ]);
        let manual = protocol::frame(&[
            ("folder", "work".into()),
            ("page", page.clone()),
        ]);
        inner.dispatch(&reg, &l1, &req_named("OPEN_WORK", &sid, manual, "manual-open"));
        assert_eq!(
            inner.session(&sid).unwrap().pending_load.as_ref().map(|j| j.request_id.as_str()),
            Some("manual-open")
        );
        let auto = protocol::frame(&[
            ("folder", "work".into()),
            ("page", page),
            ("auto", Value::Bool(true)),
        ]);
        inner.dispatch(&reg, &l1, &req_named("OPEN_WORK", &sid, auto, "auto-open"));
        let frames: Vec<Value> = std::iter::from_fn(|| match r1.try_recv() {
            Ok(OutItem::Text(s)) => serde_json::from_str(&s).ok(),
            _ => None,
        })
        .collect();
        assert!(frames.iter().any(|f| {
            f["type"] == "REJECT"
                && f["requestId"] == "auto-open"
                && f["reason"] == state::REASON_STALE_REQUEST
        }));
        // The deliberate load remains the live generation.
        assert_eq!(
            inner.session(&sid).unwrap().pending_load.as_ref().map(|j| j.request_id.as_str()),
            Some("manual-open")
        );
    }

    #[tokio::test]
    async fn entropy_failure_fails_closed() {
        let reg = reg_for_test();
        let mut inner = reg.inner.lock().await;
        let (l1, mut r1) = mk_link(&mut inner);
        let _ = next_json(&mut r1); // HELLO
        with_entropy_failure(|| {
            inner.dispatch(&reg, &l1, &req("CREATE", "", Value::Object(Map::new())));
        });
        let rej = next_json(&mut r1);
        assert_eq!(rej["type"], "REJECT");
        assert_eq!(rej["reason"], REASON_INTERNAL);
        assert!(inner.sessions.is_empty());
        assert!(inner.by_code.is_empty());
    }

    #[tokio::test]
    async fn disabling_owner_ends_their_sessions_and_grants() {
        let reg = reg_for_test();
        let mut inner = reg.inner.lock().await;
        let user = protocol::frame(&[
            ("name", "Leader".into()),
            ("email", "owner@example.com".into()),
        ]);
        let (l1, mut r1, _c) = mk_link_full(&mut inner, Some(user), None);
        let _ = next_json(&mut r1); // HELLO
        inner.dispatch(&reg, &l1, &req("CREATE", "", Value::Object(Map::new())));
        let ack = next_json(&mut r1);
        let _ = next_json(&mut r1); // STATE
        let gt = ack["data"]["guestToken"].as_str().unwrap().to_string();
        drop(inner);
        let n = reg.end_owner_sessions("OWNER@example.com").await;
        assert_eq!(n, 1);
        let end = next_json(&mut r1);
        assert_eq!(end["type"], "END");
        assert_eq!(end["reason"], END_OWNER_DISABLED);
        let inner = reg.inner.lock().await;
        assert!(inner.sessions.is_empty());
        assert!(matches!(inner.guest.status(&gt), GrantStatus::Unknown));
    }
}

#[cfg(test)]
mod golden;
