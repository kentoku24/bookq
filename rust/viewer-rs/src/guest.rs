//! Guest-access grants for dual-reader QR links — an unlogged-in device
//! redeems a short-lived opaque token (`bookq_guest` cookie) and resolves
//! as the pair leader's identity, read-only, until the pair session ends
//! or the grant's TTL backstop expires.
//!
//! Grants are in-memory like the sessions they mirror: a viewer-rs restart
//! fails closed (every grant evaporates with every session). `issue` is
//! called once per pair `CREATE`; `revoke_session` runs inside
//! `Inner::end_session` — the single funnel every session-teardown path
//! (LEAVE / REJECT / code+resume expiry / end_all / shutdown) passes
//! through.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use crate::reader_sync::new_token;

/// Backstop TTL measured from issue. The grant also dies with its pair
/// session (`end_session` revokes), so this only bounds pathological
/// long-lived sessions — 8h covers a full day of shared reading.
pub const GUEST_TTL_SECONDS: u64 = 8 * 3600;

/// Sanity cap on live grants — lazy-purged on every `issue`, oldest
/// `expires_at` evicted first. 256 concurrent pair sessions is far beyond
/// realistic use; the cap only stops a leak from growing unboundedly.
const MAX_GRANTS: usize = 256;

/// Token shape shared with the clients (`pairClient.joinGuest`,
/// `hash.js`, `login.js`): urlsafe base64, 16..=128 chars. `new_token`
/// emits 32.
pub fn valid_token_shape(s: &str) -> bool {
    (16..=128).contains(&s.len())
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

#[derive(Clone)]
pub struct Grant {
    pub owner_email: String,
    pub session_id: String,
    pub expires_at: Instant,
}

impl Grant {
    /// Whole seconds until expiry — drives the cookie `Max-Age`.
    pub fn remaining_secs(&self) -> u64 {
        self.expires_at
            .saturating_duration_since(Instant::now())
            .as_secs()
    }
}

/// `status` distinguishes expired from unknown so redeem can answer
/// "expired" vs "invalid"; `resolve` collapses both to `None` for the
/// auth paths.
pub enum GrantStatus {
    Valid(Grant),
    Expired,
    Unknown,
}

/// Cheap-clone handle onto one shared map — `Registry` and `Inner` hold
/// clones of the same store, like `wake: Arc<Notify>`.
#[derive(Clone, Default)]
pub struct GuestStore {
    inner: Arc<Mutex<HashMap<String, Grant>>>,
}

impl GuestStore {
    pub fn new() -> Self {
        Self::default()
    }

    /// Mint a grant bound to `session_id` for `owner_email`; returns the
    /// bearer token (also the `bookq_guest` cookie value and the
    /// `JOIN{guestToken}` payload).
    pub fn issue(&self, owner_email: &str, session_id: &str, ttl: Duration) -> String {
        let token = new_token();
        let mut g = self.inner.lock().unwrap();
        let now = Instant::now();
        g.retain(|_, v| v.expires_at > now);
        if g.len() >= MAX_GRANTS {
            if let Some(k) = g
                .iter()
                .min_by_key(|(_, v)| v.expires_at)
                .map(|(k, _)| k.clone())
            {
                g.remove(&k);
            }
        }
        g.insert(
            token.clone(),
            Grant {
                owner_email: owner_email.to_string(),
                session_id: session_id.to_string(),
                expires_at: now + ttl,
            },
        );
        token
    }

    pub fn status(&self, token: &str) -> GrantStatus {
        let mut g = self.inner.lock().unwrap();
        match g.get(token) {
            None => GrantStatus::Unknown,
            Some(grant) if grant.expires_at <= Instant::now() => {
                g.remove(token);
                GrantStatus::Expired
            }
            Some(grant) => GrantStatus::Valid(grant.clone()),
        }
    }

    /// Valid grants only — the auth-path lookup.
    pub fn resolve(&self, token: &str) -> Option<Grant> {
        match self.status(token) {
            GrantStatus::Valid(g) => Some(g),
            _ => None,
        }
    }

    /// Drop every grant bound to `session_id` — called from
    /// `Inner::end_session` so all teardown paths revoke together.
    pub fn revoke_session(&self, session_id: &str) {
        let mut g = self.inner.lock().unwrap();
        g.retain(|_, v| v.session_id != session_id);
    }
}

/// Per-IP fixed-window limiter for `POST /api/v1/guest/redeem` — token
/// guessing is already infeasible at 192 bits, this just keeps the
/// endpoint from being used as a free oracle/probe surface.
#[derive(Clone, Default)]
pub struct RedeemRateLimit {
    inner: Arc<Mutex<HashMap<String, (Instant, u32)>>>,
}

impl RedeemRateLimit {
    const MAX_ATTEMPTS: u32 = 30;
    const WINDOW: Duration = Duration::from_secs(60);

    /// `true` when the attempt is allowed (and counted).
    pub fn check(&self, ip: &str) -> bool {
        let mut m = self.inner.lock().unwrap();
        let now = Instant::now();
        let e = m.entry(ip.to_string()).or_insert((now, 0));
        if now.duration_since(e.0) >= Self::WINDOW {
            *e = (now, 0);
        }
        e.1 += 1;
        e.1 <= Self::MAX_ATTEMPTS
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn issue_resolve_roundtrip() {
        let store = GuestStore::new();
        let token = store.issue("a@example.com", "s1", Duration::from_secs(60));
        assert!(valid_token_shape(&token));
        let g = store.resolve(&token).expect("valid grant");
        assert_eq!(g.owner_email, "a@example.com");
        assert_eq!(g.session_id, "s1");
    }

    #[test]
    fn unknown_and_expired_status() {
        let store = GuestStore::new();
        assert!(matches!(store.status("nope"), GrantStatus::Unknown));
        let token = store.issue("a@example.com", "s1", Duration::from_millis(1));
        std::thread::sleep(Duration::from_millis(5));
        assert!(matches!(store.status(&token), GrantStatus::Expired));
        assert!(store.resolve(&token).is_none());
    }

    #[test]
    fn revoke_session_drops_only_that_session() {
        let store = GuestStore::new();
        let t1 = store.issue("a@example.com", "s1", Duration::from_secs(60));
        let t2 = store.issue("b@example.com", "s2", Duration::from_secs(60));
        store.revoke_session("s1");
        assert!(store.resolve(&t1).is_none());
        assert!(store.resolve(&t2).is_some());
    }

    #[test]
    fn rate_limit_window() {
        let rl = RedeemRateLimit::default();
        for _ in 0..RedeemRateLimit::MAX_ATTEMPTS {
            assert!(rl.check("1.2.3.4"));
        }
        assert!(!rl.check("1.2.3.4"));
        assert!(rl.check("5.6.7.8")); // other IP unaffected
    }
}
