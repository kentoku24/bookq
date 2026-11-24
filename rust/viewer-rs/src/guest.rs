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

/// `status` distinguishes expired from unknown, but redeem answers both
/// with the same 401 — telling them apart would be a token-state oracle.
/// `resolve` collapses both to `None` for the auth paths.
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
    /// `JOIN{guestToken}` payload). `None` when the entropy source is
    /// unavailable — a grant must never mint from degraded randomness.
    pub fn issue(&self, owner_email: &str, session_id: &str, ttl: Duration) -> Option<String> {
        let token = new_token()?;
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
        Some(token)
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

    /// Drop every grant owned by `owner_email` (member disable/remove) —
    /// disabling and re-enabling must never resurrect a bearer grant.
    pub fn revoke_owner(&self, owner_email: &str) {
        let mut g = self.inner.lock().unwrap();
        g.retain(|_, v| !v.owner_email.eq_ignore_ascii_case(owner_email));
    }

    /// Drop one grant by token — `/auth/logout` calls this so the
    /// explicit sign-out gesture kills a live bearer, not just the
    /// cookie copy in this browser.
    pub fn revoke_token(&self, token: &str) {
        self.inner.lock().unwrap().remove(token);
    }
}

/// Fixed-window limiter for `POST /api/v1/guest/redeem` — token guessing
/// is already infeasible at 192 bits, this just keeps the endpoint from
/// being used as a free oracle/probe surface. The per-IP budget alone is
/// bypassable when the client-IP key is rotatable (spoofed forwarding
/// headers on a directly-exposed deploy), so a global window bounds the
/// total; the map is lazily purged past `MAX_TRACKED_IPS` so rotating
/// keys can't grow it unboundedly either.
#[derive(Clone, Default)]
pub struct RedeemRateLimit {
    inner: Arc<Mutex<RedeemRateState>>,
}

#[derive(Default)]
struct RedeemRateState {
    per_ip: HashMap<String, (Instant, u32)>,
    global_start: Option<Instant>,
    global_count: u32,
}

impl RedeemRateLimit {
    const MAX_ATTEMPTS: u32 = 30;
    const WINDOW: Duration = Duration::from_secs(60);
    /// 10x the per-IP budget — ample for a household's guests re-redeeming
    /// while still capping header-rotation abuse.
    const MAX_GLOBAL_ATTEMPTS: u32 = 300;
    /// Past this many tracked IPs, expired windows are purged on check.
    const MAX_TRACKED_IPS: usize = 4096;

    /// `true` when the attempt is allowed (and counted).
    pub fn check(&self, ip: &str) -> bool {
        let mut s = self.inner.lock().unwrap();
        let now = Instant::now();
        if s.per_ip.len() >= Self::MAX_TRACKED_IPS {
            s.per_ip
                .retain(|_, (start, _)| now.duration_since(*start) < Self::WINDOW);
        }
        let g_start = *s.global_start.get_or_insert(now);
        if now.duration_since(g_start) >= Self::WINDOW {
            s.global_start = Some(now);
            s.global_count = 0;
        }
        // Hard cap: a map full of fresh windows stops tracking NEW keys —
        // they still consume the global budget, so rotation stays bounded
        // without the map growing past MAX_TRACKED_IPS.
        if !s.per_ip.contains_key(ip) && s.per_ip.len() < Self::MAX_TRACKED_IPS {
            s.per_ip.insert(ip.to_string(), (now, 0));
        }
        let per_ip_ok = match s.per_ip.get_mut(ip) {
            Some(e) => {
                if now.duration_since(e.0) >= Self::WINDOW {
                    *e = (now, 0);
                }
                e.1 += 1;
                e.1 <= Self::MAX_ATTEMPTS
            }
            None => true,
        };
        let ok = per_ip_ok && s.global_count < Self::MAX_GLOBAL_ATTEMPTS;
        if ok {
            s.global_count += 1;
        }
        ok
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn issue_resolve_roundtrip() {
        let store = GuestStore::new();
        let token = store.issue("a@example.com", "s1", Duration::from_secs(60)).unwrap();
        assert!(valid_token_shape(&token));
        let g = store.resolve(&token).expect("valid grant");
        assert_eq!(g.owner_email, "a@example.com");
        assert_eq!(g.session_id, "s1");
    }

    #[test]
    fn unknown_and_expired_status() {
        let store = GuestStore::new();
        assert!(matches!(store.status("nope"), GrantStatus::Unknown));
        let token = store.issue("a@example.com", "s1", Duration::from_millis(1)).unwrap();
        std::thread::sleep(Duration::from_millis(5));
        assert!(matches!(store.status(&token), GrantStatus::Expired));
        assert!(store.resolve(&token).is_none());
    }

    #[test]
    fn revoke_session_drops_only_that_session() {
        let store = GuestStore::new();
        let t1 = store.issue("a@example.com", "s1", Duration::from_secs(60)).unwrap();
        let t2 = store.issue("b@example.com", "s2", Duration::from_secs(60)).unwrap();
        store.revoke_session("s1");
        assert!(store.resolve(&t1).is_none());
        assert!(store.resolve(&t2).is_some());
    }

    #[test]
    fn revoke_token_drops_single_grant() {
        let store = GuestStore::new();
        let t1 = store.issue("a@example.com", "s1", Duration::from_secs(60)).unwrap();
        let t2 = store.issue("b@example.com", "s2", Duration::from_secs(60)).unwrap();
        store.revoke_token(&t1);
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

    #[test]
    fn rate_limit_global_cap() {
        let rl = RedeemRateLimit::default();
        // Rotating the per-IP key can't exceed the shared window budget.
        for i in 0..RedeemRateLimit::MAX_GLOBAL_ATTEMPTS {
            assert!(rl.check(&format!("10.{}.{}.{}", i / 65536, (i / 256) % 256, i % 256)));
        }
        assert!(!rl.check("192.168.1.1"));
    }
}
