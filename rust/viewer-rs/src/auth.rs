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
//! falls through to Bearer exactly as Python's empty-session does. A
//! signed cookie we cannot interpret (unparseable payload, user without
//! an email) delegates rather than guessing — Python would still return
//! that user, and the Bearer on the wire may belong to someone else.
//! Everything we cannot positively identify delegates upstream so the
//! Python side produces its own byte-exact 401.

use std::collections::HashSet;
use std::time::{SystemTime, UNIX_EPOCH};

use axum::http::{header, HeaderMap};
use base64::{
    engine::general_purpose::{STANDARD, URL_SAFE_NO_PAD},
    Engine,
};
use hmac::{Hmac, Mac};
use serde_json::Value;
use sha1::Sha1;
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
/// else delegates upstream. `session_key` is `None` when SECRET_KEY is
/// empty — the single fail-closed guard shared with the WS handshake.
pub fn resolve(
    headers: &HeaderMap,
    test_mode: bool,
    secret: &str,
    session_key: Option<&SessionKey>,
    allowlist: &HashSet<String>,
) -> Caller {
    if test_mode {
        return Caller::User(Identity {
            email: TEST_USER_EMAIL.to_string(),
        });
    }
    // Session cookie wins over Bearer, matching `user_from_connection`.
    // NoUser falls through to Bearer exactly as Python's empty-session
    // does; a signed user we cannot interpret delegates — Python would
    // still return it as the caller, and the Bearer may be someone else.
    match session_lookup(headers, session_key) {
        SessionLookup::User(u) => {
            return match u.get("email").and_then(|e| e.as_str()).filter(|e| !e.is_empty()) {
                Some(email) => Caller::User(Identity {
                    email: email.to_lowercase(),
                }),
                None => Caller::Delegate,
            };
        }
        SessionLookup::Corrupt => return Caller::Delegate,
        SessionLookup::Absent | SessionLookup::NoUser => {}
    }
    match bearer_token(headers).and_then(|t| validate_bearer(t, secret, allowlist)) {
        Some(id) => Caller::User(id),
        None => Caller::Delegate,
    }
}

/// The `Bearer` token portion of `Authorization`, if well-formed —
/// `partition(" ")` semantics: the scheme ends at the first space.
fn bearer_token(headers: &HeaderMap) -> Option<&str> {
    let auth = headers.get(header::AUTHORIZATION)?.to_str().ok()?;
    let (scheme, token) = auth.split_once(' ').unwrap_or((auth, ""));
    if !scheme.eq_ignore_ascii_case("bearer") || token.trim().is_empty() {
        return None;
    }
    Some(token.trim())
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

/// itsdangerous `derive_key` (`django-concat`): `SHA1(salt + b"signer" +
/// secret)` is the HMAC-SHA1 key. One SHA1, derived once at startup —
/// per-request verification only needs the HMAC itself.
fn derive_session_key(secret: &str) -> [u8; 20] {
    use sha1::Digest;
    let mut kd = Sha1::new();
    kd.update(b"itsdangerous.Signer");
    kd.update(b"signer");
    kd.update(secret.as_bytes());
    kd.finalize().into()
}

/// The session-cookie signing key, derived once from SECRET_KEY.
/// `session_key()` returns `None` on an empty secret — the single
/// fail-closed guard for both HTTP `resolve` and the WS handshake (an
/// empty key would let a forged cookie identify anyone; Python refuses
/// to start with an empty SECRET_KEY).
#[derive(Clone)]
pub struct SessionKey([u8; 20]);

pub fn session_key(secret: &str) -> Option<SessionKey> {
    if secret.is_empty() {
        None
    } else {
        Some(SessionKey(derive_session_key(secret)))
    }
}

/// The raw `session` cookie value — last wins, matching starlette's
/// `HTTPConnection.cookies` (a dict update across all Cookie headers,
/// left to right). First-wins would let `session=<valid>; session=<x>`
/// authenticate where Python sees the tampered value and falls to 401.
fn session_cookie_value(headers: &HeaderMap) -> Option<String> {
    cookie_value(headers, "session")
}

/// `<name>=<value>` lookup over all Cookie headers — last wins (see
/// `session_cookie_value`).
pub(crate) fn cookie_value(headers: &HeaderMap, name: &str) -> Option<String> {
    let mut last = None;
    for v in headers.get_all(header::COOKIE) {
        let Ok(v) = v.to_str() else { continue };
        for kv in v.split(';') {
            let kv = kv.trim();
            if let Some(rest) = kv.strip_prefix(name) {
                if let Some(rest) = rest.strip_prefix('=') {
                    last = Some(rest.trim_matches('"').to_string());
                }
            }
        }
    }
    last
}

/// `TimestampSigner(secret).sign(data)` — mint the cookie value starlette
/// emits: `b64std(payload) + "." + b64url(ts) + "." + b64url(sig)`. The
/// payload base64 is STANDARD (`b64encode`, padding kept) — `+`/`/`/`=`
/// all appear; ts is the big-endian integer with leading zero bytes
/// stripped (`int_to_bytes`); the signature is URL_SAFE_NO_PAD HMAC-SHA1
/// over `value + "." + ts`.
pub fn sign_session(key: &SessionKey, payload: &[u8], ts: u64) -> String {
    let value_b64 = STANDARD.encode(payload);
    let ts_bytes = ts.to_be_bytes();
    let start = ts_bytes.iter().position(|&b| b != 0).unwrap_or(7);
    let ts_b64 = URL_SAFE_NO_PAD.encode(&ts_bytes[start..]);
    let mut mac = <Hmac<Sha1> as Mac>::new_from_slice(&key.0).expect("hmac key");
    mac.update(value_b64.as_bytes());
    mac.update(b".");
    mac.update(ts_b64.as_bytes());
    let sig = URL_SAFE_NO_PAD.encode(mac.finalize().into_bytes());
    format!("{value_b64}.{ts_b64}.{sig}")
}

/// `TimestampSigner(secret).unsign(data, max_age)` → the session payload
/// bytes (the `{"user": {...}}` JSON document). Future timestamps and ages
/// over max_age are rejected, matching itsdangerous.
pub fn unsign_session(key: &SessionKey, data: &str, max_age: i64, now: i64) -> Option<Vec<u8>> {
    let mut parts = data.rsplitn(3, '.');
    let sig_b64 = parts.next()?;
    let ts_b64 = parts.next()?;
    let value_b64 = parts.next()?;
    let mut mac = <Hmac<Sha1> as Mac>::new_from_slice(&key.0).expect("hmac key");
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
    // starlette stores the payload as STANDARD base64 (`b64encode`) —
    // `+`/`/`/`=` all appear (e.g. a `>` in a name). itsdangerous's own
    // ts/sig parts are urlsafe. Accept either alphabet for the payload.
    STANDARD
        .decode(value_b64)
        .or_else(|_| URL_SAFE_NO_PAD.decode(value_b64))
        .ok()
}

/// What the request's `session` cookie resolves to — mirrors starlette's
/// middleware load + `session.get("user")`.
pub(crate) enum SessionLookup {
    /// No cookie, bad signature, or expired — `session={}` upstream →
    /// the Bearer path applies.
    Absent,
    /// Signed cookie whose payload isn't a session object — starlette's
    /// `json.loads` (or `.get` on a non-dict) raises on load; delegate
    /// so upstream produces its own error rather than guessing.
    Corrupt,
    /// Valid session without `user` (absent or null) — Python falls
    /// through to Bearer.
    NoUser,
    /// `session["user"]`, non-null — Python returns it as the caller.
    User(Value),
}

pub(crate) fn session_lookup(headers: &HeaderMap, key: Option<&SessionKey>) -> SessionLookup {
    let Some(key) = key else {
        return SessionLookup::Absent;
    };
    let Some(raw) = session_cookie_value(headers) else {
        return SessionLookup::Absent;
    };
    let Some(payload) = unsign_session(key, &raw, SESSION_MAX_AGE, now_secs_i64()) else {
        return SessionLookup::Absent;
    };
    match serde_json::from_slice::<Value>(&payload) {
        // Absent key and explicit null both read as
        // `session.get("user")` → None → the Bearer path upstream.
        Ok(session) if session.is_object() => match session.get("user") {
            Some(u) if !u.is_null() => SessionLookup::User(u.clone()),
            _ => SessionLookup::NoUser,
        },
        // Valid JSON that isn't a dict makes `.get` raise upstream;
        // unparseable payloads fail `json.loads` — either way Python
        // errors on load rather than falling to Bearer.
        Ok(_) | Err(_) => SessionLookup::Corrupt,
    }
}

/// `user_from_connection` for the WS handshake: TEST_MODE mock → signed
/// session cookie's `user` → Bearer (returning the py-shaped
/// {email,name,picture} dict — member names come from `name`).
pub fn resolve_ws_user(
    headers: &HeaderMap,
    test_mode: bool,
    secret: &str,
    session_key: Option<&SessionKey>,
    allowlist: &HashSet<String>,
) -> Option<Value> {
    if test_mode {
        let mut u = serde_json::Map::new();
        u.insert("email".into(), Value::String(TEST_USER_EMAIL.into()));
        u.insert("name".into(), Value::String("Test User".into()));
        u.insert("picture".into(), Value::String("".into()));
        return Some(Value::Object(u));
    }
    match session_lookup(headers, session_key) {
        // Any non-null `user` is the caller upstream — return it whole.
        SessionLookup::User(u) => return Some(u),
        // Signed but not a session object — starlette raises on load and
        // the handshake dies; fail closed.
        SessionLookup::Corrupt => return None,
        SessionLookup::Absent | SessionLookup::NoUser => {}
    }
    let claims = bearer_claims(bearer_token(headers)?, secret, allowlist)?;
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

    /// Mint a cookie the way starlette does — delegates to the prod
    /// `sign_session` so tests exercise the same code path the OAuth
    /// callback mints with.
    fn sign_session(secret: &str, payload: &[u8], ts: u64) -> String {
        super::sign_session(&session_key(secret).unwrap(), payload, ts)
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

    fn bearer_for(sub: &str, exp_delta: i64) -> String {
        mint(
            &format!(
                "{{\"sub\":\"{sub}\",\"exp\":{}}}",
                now_ts() as i64 + exp_delta
            ),
            "HS256",
            SECRET,
        )
    }

    const USER_JSON: &str =
        "{\"user\":{\"email\":\"A@Example.com\",\"name\":\"Alice\",\"picture\":\"p\"}}";

    #[test]
    fn session_cookie_identifies_caller() {
        let cookie = sign_session(SECRET, USER_JSON.as_bytes(), now_ts());
        let h = headers_with(Some(&format!("session={cookie}")), None);
        match resolve(&h, false, SECRET, session_key(SECRET).as_ref(), &allowlist()) {
            Caller::User(u) => assert_eq!(u.email, "a@example.com"), // lowercased
            Caller::Delegate => panic!("valid session delegated"),
        }
    }

    /// starlette writes STANDARD b64 — a `>`/`?`/`~` in the payload puts
    /// `+`/`/` into the value part, which urlsafe decode would reject.
    #[test]
    fn standard_b64_payload_resolves() {
        let payload = b"{\"user\":{\"email\":\"a@example.com\",\"name\":\">>~\"}}";
        let cookie = sign_session(SECRET, payload, now_ts());
        assert!(cookie.split('.').next().unwrap().contains('+'));
        let h = headers_with(Some(&format!("session={cookie}")), None);
        match resolve(&h, false, SECRET, session_key(SECRET).as_ref(), &allowlist()) {
            Caller::User(u) => assert_eq!(u.email, "a@example.com"),
            Caller::Delegate => panic!("standard-b64 session delegated"),
        }
    }

    #[test]
    fn session_wins_over_bearer() {
        // Bearer identifies a DIFFERENT allowlisted user — the cookie
        // still wins, matching user_from_connection's session-first order.
        let list: HashSet<String> =
            ["a@example.com".into(), "b@example.com".into()].into_iter().collect();
        let cookie = sign_session(SECRET, USER_JSON.as_bytes(), now_ts());
        let h = headers_with(
            Some(&format!("session={cookie}")),
            Some(&format!("Bearer {}", bearer_for("b@example.com", 3600))),
        );
        match resolve(&h, false, SECRET, session_key(SECRET).as_ref(), &list) {
            Caller::User(u) => assert_eq!(u.email, "a@example.com"),
            Caller::Delegate => panic!("valid session delegated"),
        }
    }

    /// starlette's cookie dict is last-wins across `;` pairs and across
    /// Cookie headers — first-wins would authenticate a value Python
    /// rejects.
    #[test]
    fn duplicate_session_last_wins() {
        let good = sign_session(SECRET, USER_JSON.as_bytes(), now_ts());
        let forged = sign_session("wrong-secret", USER_JSON.as_bytes(), now_ts());
        // Forged second: Python sees the forgery → session={} → no bearer
        // → Delegate.
        let h = headers_with(Some(&format!("session={good}; session={forged}")), None);
        assert!(matches!(
            resolve(&h, false, SECRET, session_key(SECRET).as_ref(), &allowlist()),
            Caller::Delegate
        ));
        // Forged first, across separate Cookie headers: the valid one wins.
        let mut h = HeaderMap::new();
        h.append(
            header::COOKIE,
            header::HeaderValue::from_str(&format!("session={forged}")).unwrap(),
        );
        h.append(
            header::COOKIE,
            header::HeaderValue::from_str(&format!("session={good}")).unwrap(),
        );
        match resolve(&h, false, SECRET, session_key(SECRET).as_ref(), &allowlist()) {
            Caller::User(u) => assert_eq!(u.email, "a@example.com"),
            Caller::Delegate => panic!("last valid session lost"),
        }
    }

    /// Signed but uninterpretable (`user` present, no usable email) —
    /// Python returns it as the caller; delegate instead of falling to a
    /// Bearer that may belong to someone else.
    #[test]
    fn signed_uninterpretable_user_delegates() {
        let list: HashSet<String> =
            ["a@example.com".into(), "b@example.com".into()].into_iter().collect();
        for payload in [
            &b"{\"user\":{\"email\":null}}"[..],
            b"{\"user\":\"not-an-object\"}",
        ] {
            let cookie = sign_session(SECRET, payload, now_ts());
            let h = headers_with(
                Some(&format!("session={cookie}")),
                Some(&format!("Bearer {}", bearer_for("b@example.com", 3600))),
            );
            assert!(
                matches!(
                    resolve(&h, false, SECRET, session_key(SECRET).as_ref(), &list),
                    Caller::Delegate
                ),
                "payload {payload:?} fell through to bearer"
            );
        }
    }

    /// Signed but the payload isn't JSON — starlette raises on load;
    /// delegate rather than authenticate the Bearer.
    #[test]
    fn signed_corrupt_session_delegates() {
        let cookie = sign_session(SECRET, b"not json at all", now_ts());
        let h = headers_with(
            Some(&format!("session={cookie}")),
            Some(&format!("Bearer {}", bearer_for("a@example.com", 3600))),
        );
        assert!(matches!(
            resolve(&h, false, SECRET, session_key(SECRET).as_ref(), &allowlist()),
            Caller::Delegate
        ));
    }

    #[test]
    fn invalid_session_falls_through_to_bearer() {
        // Tampered signature → Python sees session={} → Bearer applies.
        // A different allowlisted Bearer email proves the Bearer path ran.
        let list: HashSet<String> =
            ["a@example.com".into(), "b@example.com".into()].into_iter().collect();
        let bearer = bearer_for("b@example.com", 3600);
        let cookie = sign_session("wrong-secret", USER_JSON.as_bytes(), now_ts());
        let h = headers_with(
            Some(&format!("session={cookie}")),
            Some(&format!("Bearer {bearer}")),
        );
        match resolve(&h, false, SECRET, session_key(SECRET).as_ref(), &list) {
            Caller::User(u) => assert_eq!(u.email, "b@example.com"),
            Caller::Delegate => panic!("bearer after bad session delegated"),
        }
    }

    /// `SESSION_MAX_AGE` is hand-mirrored from `SessionMiddleware(max_age=)`
    /// — a Python-side change must trip this test, not silently diverge
    /// (Rust would accept cookies Python already revoked).
    #[test]
    fn session_max_age_matches_python() {
        let main_py = include_str!("../../../app/main.py");
        assert!(
            main_py.contains("max_age=86400 * 7"),
            "app/main.py SessionMiddleware max_age changed — update SESSION_MAX_AGE"
        );
        assert_eq!(SESSION_MAX_AGE, 86400 * 7);
    }

    #[test]
    fn userless_or_expired_session_delegates() {
        // Signed but no `user` — a logged-out cookie.
        let cookie = sign_session(SECRET, b"{\"other\":1}", now_ts());
        let h = headers_with(Some(&format!("session={cookie}")), None);
        assert!(matches!(
            resolve(&h, false, SECRET, session_key(SECRET).as_ref(), &allowlist()),
            Caller::Delegate
        ));
        // Signed but older than SESSION_MAX_AGE.
        let old = sign_session(SECRET, USER_JSON.as_bytes(), now_ts() - (86400 * 8) as u64);
        let h = headers_with(Some(&format!("session={old}")), None);
        assert!(matches!(
            resolve(&h, false, SECRET, session_key(SECRET).as_ref(), &allowlist()),
            Caller::Delegate
        ));
    }

    #[test]
    fn resolve_test_mode_returns_mock() {
        let h = headers_with(None, None);
        match resolve(&h, true, "", None, &allowlist()) {
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
        let payload = unsign_session(
            &session_key("ci-not-a-secret").unwrap(),
            COOKIE,
            SESSION_MAX_AGE,
            1700001000,
        )
        .unwrap();
        let session: Value = serde_json::from_slice(&payload).unwrap();
        assert_eq!(session["user"]["email"], "a@example.com");
        assert_eq!(session["user"]["name"], "Alice");
    }

    #[test]
    fn session_cookie_rejects_tamper_wrong_key_and_expiry() {
        let key = session_key("ci-not-a-secret").unwrap();
        let wrong = session_key("wrong-secret").unwrap();
        assert!(unsign_session(&wrong, COOKIE, SESSION_MAX_AGE, 1700001000).is_none());
        let mut tampered = COOKIE.to_string();
        tampered.replace_range(tampered.len() - 1.., "B");
        assert!(unsign_session(&key, &tampered, SESSION_MAX_AGE, 1700001000).is_none());
        let mut tampered_mid = COOKIE.to_string();
        tampered_mid.replace_range(5..6, "x");
        assert!(unsign_session(&key, &tampered_mid, SESSION_MAX_AGE, 1700001000).is_none());
        // past max_age and future timestamps both fail (itsdangerous).
        assert!(
            unsign_session(&key, COOKIE, SESSION_MAX_AGE, 1700000000 + 86400 * 7 + 1).is_none()
        );
        assert!(unsign_session(&key, COOKIE, SESSION_MAX_AGE, 1699990000).is_none());
        // truncated / malformed structures never panic
        assert!(unsign_session(&key, "a.b", SESSION_MAX_AGE, 0).is_none());
        assert!(unsign_session(&key, "", SESSION_MAX_AGE, 0).is_none());
        assert!(unsign_session(&key, "x.y.z", SESSION_MAX_AGE, 0).is_none());
    }
}
