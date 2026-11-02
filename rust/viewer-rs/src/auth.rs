//! Bearer JWT validation — the native-auth half of `app/auth.py`.
//!
//! `_user_from_bearer` mirrors: `Authorization: Bearer <jwt>` where the JWT
//! is HMAC-signed with SECRET_KEY (joserfc `OctKey` accepts HS256/384/512),
//! `exp` is required, `sub` becomes the email (lowercased), and the email
//! must sit in `ALLOWED_EMAILS` — an empty allowlist rejects everyone
//! (fail-closed, never fail-open).
//!
//! Auth order matches `user_from_connection`: the signed session cookie
//! wins over Bearer. The cookie is verified natively via
//! `unsign_session` (itsdangerous.TimestampSigner, HMAC-SHA1 over a
//! django-concat derived key) — an invalid, expired, or userless cookie
//! falls through to Bearer exactly as Python's empty-session does.
//! Everything we cannot positively identify delegates upstream so the
//! Python side produces its own byte-exact 401.

use std::collections::HashSet;
use std::time::{SystemTime, UNIX_EPOCH};

use axum::http::{header, HeaderMap};
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
use hmac::{Hmac, Mac};
use serde_json::Value;
use sha2::{Sha256, Sha384, Sha512};

/// BOOKQ_TEST_MODE mock user (`app/auth.py:61`).
pub const TEST_USER_EMAIL: &str = "test@example.com";

/// A positively identified caller — the email doubles as the user key.
pub struct Identity {
    pub email: String,
}

pub enum Caller {
    /// Serve natively under this identity.
    User(Identity),
    /// Not positively identified — proxy so upstream applies its own
    /// cookie → Bearer → 401 chain.
    Delegate,
}

fn now_secs_f64() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

fn b64url(data: &str) -> Option<Vec<u8>> {
    // joserfc tolerates missing padding; strip '=' so both forms decode.
    URL_SAFE_NO_PAD.decode(data.trim_end_matches('=')).ok()
}

fn mac_verify(alg: &str, secret: &[u8], signing_input: &[u8], sig: &[u8]) -> bool {
    fn check<M: Mac>(mut m: M, input: &[u8], sig: &[u8]) -> bool {
        m.update(input);
        m.verify_slice(sig).is_ok()
    }
    match alg {
        "HS256" => check(
            <Hmac<Sha256> as Mac>::new_from_slice(secret).expect("hmac key"),
            signing_input,
            sig,
        ),
        "HS384" => check(
            <Hmac<Sha384> as Mac>::new_from_slice(secret).expect("hmac key"),
            signing_input,
            sig,
        ),
        "HS512" => check(
            <Hmac<Sha512> as Mac>::new_from_slice(secret).expect("hmac key"),
            signing_input,
            sig,
        ),
        _ => false,
    }
}

/// `JWTClaimsRegistry().validate(claims)` — exp/nbf/iat checks with
/// leeway 0, plus the explicit `exp` requirement `_user_from_bearer` adds.
/// Numeric-date claims must be numbers; a present `iat`/`nbf` may not be
/// in the future, `exp` must still hold (`now <= exp`).
fn claims_valid(claims: &serde_json::Map<String, Value>, now: f64) -> bool {
    fn num(v: &Value) -> Option<f64> {
        match v {
            Value::Number(n) => n.as_f64().filter(|f| f.is_finite()),
            _ => None,
        }
    }
    let exp = match claims.get("exp") {
        Some(v) => match num(v) {
            Some(t) => t,
            None => return false,
        },
        None => return false,
    };
    if now > exp {
        return false;
    }
    if let Some(v) = claims.get("nbf") {
        match num(v) {
            Some(t) if now >= t => {}
            _ => return false,
        }
    }
    if let Some(v) = claims.get("iat") {
        match num(v) {
            Some(t) if now >= t => {}
            _ => return false,
        }
    }
    true
}

/// The validated JWT claims object — shared by `validate_bearer` and the
/// WebSocket handshake's `bearer_user`.
fn bearer_claims(
    token: &str,
    secret: &str,
    allowlist: &HashSet<String>,
) -> Option<serde_json::Map<String, Value>> {
    if secret.is_empty() {
        return None;
    }
    let mut it = token.splitn(4, '.');
    let (h, p, s) = (it.next()?, it.next()?, it.next()?);
    if it.next().is_some() || s.is_empty() {
        return None;
    }
    let header: Value = serde_json::from_slice(&b64url(h)?).ok()?;
    let alg = header.get("alg")?.as_str()?;
    let signing_input = format!("{h}.{p}");
    let sig = b64url(s)?;
    if !mac_verify(alg, secret.as_bytes(), signing_input.as_bytes(), &sig) {
        return None;
    }
    let claims: Value = serde_json::from_slice(&b64url(p)?).ok()?;
    let obj = claims.as_object()?.clone();
    if !claims_valid(&obj, now_secs_f64()) {
        return None;
    }
    let email = obj.get("sub")?.as_str()?.to_lowercase();
    if email.is_empty() || !allowlist.contains(&email) {
        return None;
    }
    Some(obj)
}

/// `_user_from_bearer` on an already-extracted token.
/// `allowlist` is `ALLOWED_EMAILS` (trim+lowercase, empties dropped).
pub fn validate_bearer(token: &str, secret: &str, allowlist: &HashSet<String>) -> Option<Identity> {
    let claims = bearer_claims(token, secret, allowlist)?;
    Some(Identity {
        email: claims.get("sub")?.as_str()?.to_lowercase(),
    })
}

/// Resolve the caller for a native handler. `test_mode` returns the mock
/// user; a verifiable `session` cookie identifies the caller natively
/// (Python prefers it over Bearer); `Bearer` validates locally; anything
/// else delegates upstream.
pub fn resolve(
    headers: &HeaderMap,
    test_mode: bool,
    secret: &str,
    allowlist: &HashSet<String>,
) -> Caller {
    if test_mode {
        return Caller::User(Identity {
            email: TEST_USER_EMAIL.to_string(),
        });
    }
    // Session cookie wins over Bearer, matching `user_from_connection`.
    // A verifiable cookie identifies the caller natively; an invalid,
    // expired, or userless cookie falls through to Bearer — Python's
    // SessionMiddleware sees an empty session there and does the same.
    if !secret.is_empty() {
        if let Some(raw) = session_cookie_value(headers) {
            if let Some(payload) = unsign_session(secret, &raw, SESSION_MAX_AGE, now_secs_i64()) {
                if let Ok(session) = serde_json::from_slice::<Value>(&payload) {
                    if let Some(email) = session
                        .get("user")
                        .and_then(|u| u.get("email"))
                        .and_then(|e| e.as_str())
                        .filter(|e| !e.is_empty())
                    {
                        return Caller::User(Identity {
                            email: email.to_lowercase(),
                        });
                    }
                }
            }
        }
    }
    let auth = headers
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    // `partition(" ")` semantics: scheme is up to the first space.
    let (scheme, token) = match auth.split_once(' ') {
        Some((s, t)) => (s, t),
        None => (auth, ""),
    };
    if !scheme.eq_ignore_ascii_case("bearer") || token.trim().is_empty() {
        return Caller::Delegate;
    }
    match validate_bearer(token.trim(), secret, allowlist) {
        Some(id) => Caller::User(id),
        None => Caller::Delegate,
    }
}

// ── WebSocket handshake auth (reader_sync) ────────────────────────────────
//
// `SessionMiddleware` cookie verification — starlette signs
// `base64url(json) + "." + ts_b64` with itsdangerous.TimestampSigner:
// salt `itsdangerous.Signer`, key derivation `django-concat`
// (`SHA1(salt + b"signer" + secret)` is the HMAC-SHA1 key), signature
// `b64url(HMAC-SHA1(key, payload + "." + ts))`. main.py overrides the
// default 14-day max_age with `86400 * 7`.

pub const SESSION_MAX_AGE: i64 = 86400 * 7;

fn now_secs_i64() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

/// The raw `session` cookie value, if present.
fn session_cookie_value(headers: &HeaderMap) -> Option<String> {
    for v in headers.get_all(header::COOKIE) {
        let Ok(v) = v.to_str() else { continue };
        for kv in v.split(';') {
            if let Some(rest) = kv.trim().strip_prefix("session=") {
                return Some(rest.trim_matches('"').to_string());
            }
        }
    }
    None
}

/// `TimestampSigner(secret).unsign(data, max_age)` → the session payload
/// bytes (the `{"user": {...}}` JSON document). Future timestamps and ages
/// over max_age are rejected, matching itsdangerous.
pub fn unsign_session(secret: &str, data: &str, max_age: i64, now: i64) -> Option<Vec<u8>> {
    use sha1::{Digest, Sha1};
    let mut parts = data.rsplitn(3, '.');
    let sig_b64 = parts.next()?;
    let ts_b64 = parts.next()?;
    let value_b64 = parts.next()?;
    let mut kd = Sha1::new();
    kd.update(b"itsdangerous.Signer");
    kd.update(b"signer");
    kd.update(secret.as_bytes());
    let key = kd.finalize();
    let mut mac = <Hmac<Sha1> as Mac>::new_from_slice(&key).expect("hmac key");
    mac.update(value_b64.as_bytes());
    mac.update(b".");
    mac.update(ts_b64.as_bytes());
    let sig = b64url(sig_b64)?;
    if mac.verify_slice(&sig).is_err() {
        return None;
    }
    let ts_b = b64url(ts_b64)?;
    if ts_b.len() > 8 {
        return None;
    }
    let mut ts: u64 = 0;
    for &b in &ts_b {
        ts = (ts << 8) | b as u64;
    }
    let age = now - ts as i64;
    if age > max_age || age < 0 {
        return None;
    }
    b64url(value_b64)
}

/// `user_from_connection` for the WS handshake: TEST_MODE mock → signed
/// session cookie's `user` → Bearer (returning the py-shaped
/// {email,name,picture} dict — member names come from `name`).
pub fn resolve_ws_user(
    headers: &HeaderMap,
    test_mode: bool,
    secret: &str,
    allowlist: &HashSet<String>,
) -> Option<Value> {
    if test_mode {
        let mut u = serde_json::Map::new();
        u.insert("email".into(), Value::String(TEST_USER_EMAIL.into()));
        u.insert("name".into(), Value::String("Test User".into()));
        u.insert("picture".into(), Value::String("".into()));
        return Some(Value::Object(u));
    }
    if let Some(raw) = session_cookie_value(headers) {
        if let Some(payload) = unsign_session(secret, &raw, SESSION_MAX_AGE, now_secs_i64()) {
            if let Ok(session) = serde_json::from_slice::<Value>(&payload) {
                match session.get("user") {
                    Some(u) if !u.is_null() => return Some(u.clone()),
                    _ => {}
                }
            }
        }
        // Invalid or userless session → falls to Bearer (py: session={}).
    }
    let auth = headers
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    let (scheme, token) = match auth.split_once(' ') {
        Some((s, t)) => (s, t),
        None => (auth, ""),
    };
    if !scheme.eq_ignore_ascii_case("bearer") || token.trim().is_empty() {
        return None;
    }
    let claims = bearer_claims(token.trim(), secret, allowlist)?;
    let email = claims.get("sub")?.as_str()?.to_lowercase();
    let mut u = serde_json::Map::new();
    u.insert("email".into(), Value::String(email.clone()));
    let name = claims.get("name").and_then(|v| v.as_str()).unwrap_or("");
    u.insert(
        "name".into(),
        Value::String(if name.is_empty() {
            email.clone()
        } else {
            name.to_string()
        }),
    );
    let picture = claims.get("picture").and_then(|v| v.as_str()).unwrap_or("");
    u.insert("picture".into(), Value::String(picture.to_string()));
    Some(Value::Object(u))
}

#[cfg(test)]
mod tests {
    use super::*;
    use base64::engine::general_purpose::URL_SAFE_NO_PAD;
    use hmac::{Hmac, Mac};
    use sha2::Sha256;

    const SECRET: &str = "dev-not-a-secret";

    fn allowlist() -> HashSet<String> {
        ["a@example.com".to_string()].into_iter().collect()
    }

    fn mint(claims: &str, alg: &str, secret: &str) -> String {
        let h = URL_SAFE_NO_PAD.encode(format!("{{\"alg\":\"{alg}\",\"typ\":\"JWT\"}}"));
        let p = URL_SAFE_NO_PAD.encode(claims);
        let input = format!("{h}.{p}");
        let sig = match alg {
            "HS256" => {
                let mut m = <Hmac<Sha256> as Mac>::new_from_slice(secret.as_bytes()).unwrap();
                m.update(input.as_bytes());
                URL_SAFE_NO_PAD.encode(m.finalize().into_bytes())
            }
            "HS384" => {
                let mut m = <Hmac<Sha384> as Mac>::new_from_slice(secret.as_bytes()).unwrap();
                m.update(input.as_bytes());
                URL_SAFE_NO_PAD.encode(m.finalize().into_bytes())
            }
            "HS512" => {
                let mut m = <Hmac<Sha512> as Mac>::new_from_slice(secret.as_bytes()).unwrap();
                m.update(input.as_bytes());
                URL_SAFE_NO_PAD.encode(m.finalize().into_bytes())
            }
            _ => URL_SAFE_NO_PAD.encode(b"bogus".as_slice()),
        };
        format!("{input}.{sig}")
    }

    fn fresh(exp_delta: i64) -> String {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs() as i64;
        mint(
            &format!(
                "{{\"sub\":\"A@Example.com\",\"iat\":{},\"exp\":{}}}",
                now - 10,
                now + exp_delta
            ),
            "HS256",
            SECRET,
        )
    }

    #[test]
    fn valid_bearer_resolves() {
        let id = validate_bearer(&fresh(3600), SECRET, &allowlist()).unwrap();
        assert_eq!(id.email, "a@example.com"); // sub lowercased
    }

    #[test]
    fn rejects_bad_signature_and_alg() {
        assert!(validate_bearer(&fresh(3600), "wrong-key", &allowlist()).is_none());
        assert!(validate_bearer(&mint("{\"sub\":\"a@example.com\",\"exp\":9999999999}", "none", SECRET), SECRET, &allowlist()).is_none());
        assert!(validate_bearer(&mint("{\"sub\":\"a@example.com\",\"exp\":9999999999}", "RS256", SECRET), SECRET, &allowlist()).is_none());
    }

    #[test]
    fn rejects_expired_missing_exp_and_future_iat() {
        assert!(validate_bearer(&fresh(-1), SECRET, &allowlist()).is_none());
        assert!(validate_bearer(
            &mint("{\"sub\":\"a@example.com\",\"iat\":1}", "HS256", SECRET),
            SECRET,
            &allowlist()
        )
        .is_none());
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs() as i64;
        assert!(validate_bearer(
            &mint(
                &format!("{{\"sub\":\"a@example.com\",\"iat\":{},\"exp\":{}}}", now + 999, now + 3600),
                "HS256",
                SECRET
            ),
            SECRET,
            &allowlist()
        )
        .is_none());
    }

    #[test]
    fn allowlist_is_fail_closed() {
        let empty: HashSet<String> = HashSet::new();
        assert!(validate_bearer(&fresh(3600), SECRET, &empty).is_none());
        // sub casing is normalized before the allowlist check.
        let list: HashSet<String> = ["b@example.com".to_string()].into_iter().collect();
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs() as i64;
        let t = mint(
            &format!("{{\"sub\":\"c@example.com\",\"exp\":{}}}", now + 60),
            "HS256",
            SECRET,
        );
        assert!(validate_bearer(&t, SECRET, &list).is_none());
    }

    #[test]
    fn hs384_and_hs512_verify() {
        for alg in ["HS384", "HS512"] {
            let t = mint(
                "{\"sub\":\"a@example.com\",\"exp\":9999999999}",
                alg,
                SECRET,
            );
            assert!(validate_bearer(&t, SECRET, &allowlist()).is_some(), "{alg}");
        }
    }

    // ── resolve(): session-cookie callers ────────────────────────────────

    /// Mint an itsdangerous.TimestampSigner cookie value — mirrors
    /// `unsign_session`'s derivation: SHA1(salt + b"signer" + secret) is
    /// the HMAC-SHA1 key, signed over `value_b64 + "." + ts_b64`.
    fn sign_session(secret: &str, payload: &[u8], ts: u64) -> String {
        use sha1::{Digest, Sha1};
        let value_b64 = URL_SAFE_NO_PAD.encode(payload);
        let ts_bytes = ts.to_be_bytes();
        let start = ts_bytes.iter().position(|&b| b != 0).unwrap_or(7);
        let ts_b64 = URL_SAFE_NO_PAD.encode(&ts_bytes[start..]);
        let mut kd = Sha1::new();
        kd.update(b"itsdangerous.Signer");
        kd.update(b"signer");
        kd.update(secret.as_bytes());
        let key = kd.finalize();
        let mut mac = <Hmac<Sha1> as Mac>::new_from_slice(&key).unwrap();
        mac.update(value_b64.as_bytes());
        mac.update(b".");
        mac.update(ts_b64.as_bytes());
        let sig = URL_SAFE_NO_PAD.encode(mac.finalize().into_bytes());
        format!("{value_b64}.{ts_b64}.{sig}")
    }

    fn headers_with(cookie: Option<&str>, auth: Option<&str>) -> HeaderMap {
        let mut h = HeaderMap::new();
        if let Some(c) = cookie {
            h.insert(header::COOKIE, header::HeaderValue::from_str(c).unwrap());
        }
        if let Some(a) = auth {
            h.insert(header::AUTHORIZATION, header::HeaderValue::from_str(a).unwrap());
        }
        h
    }

    fn now_ts() -> u64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs()
    }

    const USER_JSON: &str =
        "{\"user\":{\"email\":\"A@Example.com\",\"name\":\"Alice\",\"picture\":\"p\"}}";

    #[test]
    fn session_cookie_identifies_caller() {
        let cookie = sign_session(SECRET, USER_JSON.as_bytes(), now_ts());
        let h = headers_with(Some(&format!("session={cookie}")), None);
        match resolve(&h, false, SECRET, &allowlist()) {
            Caller::User(u) => assert_eq!(u.email, "a@example.com"), // lowercased
            Caller::Delegate => panic!("valid session delegated"),
        }
    }

    #[test]
    fn session_wins_over_bearer() {
        // Bearer identifies a different allowlisted user — the cookie wins,
        // matching user_from_connection's session-first order.
        let cookie = sign_session(SECRET, USER_JSON.as_bytes(), now_ts());
        let h = headers_with(
            Some(&format!("session={cookie}")),
            Some(&format!("Bearer {}", fresh(3600))),
        );
        match resolve(&h, false, SECRET, &allowlist()) {
            Caller::User(u) => assert_eq!(u.email, "a@example.com"),
            Caller::Delegate => panic!("valid session delegated"),
        }
    }

    #[test]
    fn invalid_session_falls_through_to_bearer() {
        // Tampered signature → Python sees session={} → Bearer applies.
        // A different allowlisted Bearer email proves the Bearer path ran.
        let list: HashSet<String> =
            ["a@example.com".into(), "b@example.com".into()].into_iter().collect();
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs() as i64;
        let bearer = mint(
            &format!("{{\"sub\":\"b@example.com\",\"exp\":{}}}", now + 3600),
            "HS256",
            SECRET,
        );
        let cookie = sign_session("wrong-secret", USER_JSON.as_bytes(), now_ts());
        let h = headers_with(
            Some(&format!("session={cookie}")),
            Some(&format!("Bearer {bearer}")),
        );
        match resolve(&h, false, SECRET, &list) {
            Caller::User(u) => assert_eq!(u.email, "b@example.com"),
            Caller::Delegate => panic!("bearer after bad session delegated"),
        }
    }

    #[test]
    fn userless_or_expired_session_delegates() {
        // Signed but no `user` — a logged-out cookie.
        let cookie = sign_session(SECRET, b"{\"other\":1}", now_ts());
        let h = headers_with(Some(&format!("session={cookie}")), None);
        assert!(matches!(resolve(&h, false, SECRET, &allowlist()), Caller::Delegate));
        // Signed but older than SESSION_MAX_AGE.
        let old = sign_session(SECRET, USER_JSON.as_bytes(), now_ts() - (86400 * 8) as u64);
        let h = headers_with(Some(&format!("session={old}")), None);
        assert!(matches!(resolve(&h, false, SECRET, &allowlist()), Caller::Delegate));
    }

    #[test]
    fn resolve_test_mode_returns_mock() {
        let h = headers_with(None, None);
        match resolve(&h, true, "", &allowlist()) {
            Caller::User(u) => assert_eq!(u.email, TEST_USER_EMAIL),
            Caller::Delegate => panic!("test mode delegated"),
        }
    }
}

#[cfg(test)]
mod ws_tests {
    use super::*;

    // Golden itsdangerous.TimestampSigner vector — generated by
    // TimestampSigner("ci-not-a-secret") at ts=1700000000, verified by
    // unsign(max_age=604800) in a Python container.
    const COOKIE: &str = "eyJ1c2VyIjp7ImVtYWlsIjoiYUBleGFtcGxlLmNvbSIsIm5hbWUiOiJBbGljZSIsInBpY3R1cmUiOiJwIn0sImV4dHJhIjoxfQ.ZVPxAA.SMJzSX9igsW-JCDo_GlMdykBDtA";

    #[test]
    fn session_cookie_verifies() {
        let payload =
            unsign_session("ci-not-a-secret", COOKIE, SESSION_MAX_AGE, 1700001000).unwrap();
        let session: Value = serde_json::from_slice(&payload).unwrap();
        assert_eq!(session["user"]["email"], "a@example.com");
        assert_eq!(session["user"]["name"], "Alice");
    }

    #[test]
    fn session_cookie_rejects_tamper_wrong_key_and_expiry() {
        assert!(unsign_session("wrong-secret", COOKIE, SESSION_MAX_AGE, 1700001000).is_none());
        let mut tampered = COOKIE.to_string();
        tampered.replace_range(tampered.len() - 1.., "B");
        assert!(
            unsign_session("ci-not-a-secret", &tampered, SESSION_MAX_AGE, 1700001000).is_none()
        );
        let mut tampered_mid = COOKIE.to_string();
        tampered_mid.replace_range(5..6, "x");
        assert!(unsign_session(
            "ci-not-a-secret",
            &tampered_mid,
            SESSION_MAX_AGE,
            1700001000
        )
        .is_none());
        // past max_age and future timestamps both fail (itsdangerous).
        assert!(unsign_session(
            "ci-not-a-secret",
            COOKIE,
            SESSION_MAX_AGE,
            1700000000 + 86400 * 7 + 1
        )
        .is_none());
        assert!(unsign_session("ci-not-a-secret", COOKIE, SESSION_MAX_AGE, 1699990000).is_none());
        // truncated / malformed structures never panic
        assert!(unsign_session("ci-not-a-secret", "a.b", SESSION_MAX_AGE, 0).is_none());
        assert!(unsign_session("ci-not-a-secret", "", SESSION_MAX_AGE, 0).is_none());
        assert!(unsign_session("ci-not-a-secret", "x.y.z", SESSION_MAX_AGE, 0).is_none());
    }
}
