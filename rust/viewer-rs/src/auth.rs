//! Bearer JWT validation — the native-auth half of `app/auth.py`.
//!
//! `_user_from_bearer` mirrors: `Authorization: Bearer <jwt>` where the JWT
//! is HMAC-signed with SECRET_KEY (joserfc `OctKey` accepts HS256/384/512),
//! `exp` is required, `sub` becomes the email (lowercased), and the email
//! must sit in `ALLOWED_EMAILS` — an empty allowlist rejects everyone
//! (fail-closed, never fail-open).
//!
//! Auth order matches `user_from_connection`: the signed session cookie
//! wins over Bearer. We cannot verify the cookie natively, so any request
//! carrying a `session` cookie delegates upstream — including Bearer
//! requests, where Python would prefer the cookie identity anyway.
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

/// True when a `Cookie` header carries a `session` entry — starlette's
/// SessionMiddleware default cookie name (`app/main.py` doesn't override).
fn has_session_cookie(headers: &HeaderMap) -> bool {
    headers
        .get_all(header::COOKIE)
        .iter()
        .filter_map(|v| v.to_str().ok())
        .any(|c| {
            c.split(';')
                .map(|kv| kv.trim())
                .any(|kv| kv.starts_with("session="))
        })
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

/// `_user_from_bearer` on an already-extracted token.
/// `allowlist` is `ALLOWED_EMAILS` (trim+lowercase, empties dropped).
pub fn validate_bearer(
    token: &str,
    secret: &str,
    allowlist: &HashSet<String>,
) -> Option<Identity> {
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
    let obj = claims.as_object()?;
    if !claims_valid(obj, now_secs_f64()) {
        return None;
    }
    let email = obj.get("sub")?.as_str()?.to_lowercase();
    if email.is_empty() || !allowlist.contains(&email) {
        return None;
    }
    Some(Identity { email })
}

/// Resolve the caller for a native handler. `test_mode` returns the mock
/// user; a `session` cookie delegates (Python prefers it over Bearer);
/// `Bearer` validates locally; anything else delegates upstream.
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
    if has_session_cookie(headers) {
        return Caller::Delegate;
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
}
