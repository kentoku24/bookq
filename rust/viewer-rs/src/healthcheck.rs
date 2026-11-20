//! `viewer-rs --healthcheck` — the container health probe (#573).
//!
//! The distroless runtime has no shell and no `curl`, so the compose
//! healthcheck cannot be the historic `bash -c` + `/dev/tcp` one-liner.
//! Instead the binary probes itself over loopback and exits 0/1, which keeps
//! `depends_on: service_healthy` and the deploy checks in `rebuild.sh`
//! working without adding a second binary to the image.
//!
//! Target resolution:
//! * `BOOKQ_HEALTHCHECK_ADDR` wins (tests / odd layouts),
//! * else the port of `BOOKQ_BIND` (the address the server listens on) on
//!   loopback — a wildcard bind is not a reliable connect target,
//! * else `127.0.0.1:8080`.

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::time::Duration;

const DEFAULT_ADDR: &str = "127.0.0.1:8080";
const TIMEOUT: Duration = Duration::from_secs(2);
const MAX_RESPONSE_BYTES: u64 = 8 * 1024;
const PATH: &str = "/api/v1/health";

/// Probe address for the self-check (see the module docs for precedence).
pub fn target_addr() -> String {
    if let Ok(addr) = std::env::var("BOOKQ_HEALTHCHECK_ADDR") {
        if !addr.trim().is_empty() {
            return addr.trim().to_string();
        }
    }
    match std::env::var("BOOKQ_BIND") {
        Ok(bind) => probe_addr_for(&bind),
        Err(_) => DEFAULT_ADDR.to_string(),
    }
}

/// Map a `BOOKQ_BIND` listen address to a connect address: wildcard (`0.0.0.0`,
/// `[::]`) means loopback with the same port, an explicit address is probed
/// as-is, and anything unparsable falls back to the default.
pub fn probe_addr_for(bind: &str) -> String {
    match bind.trim().parse::<SocketAddr>() {
        Ok(addr) if addr.ip().is_unspecified() => format!("127.0.0.1:{}", addr.port()),
        Ok(addr) => addr.to_string(),
        Err(_) => DEFAULT_ADDR.to_string(),
    }
}

/// Parse the status code out of the first line of an HTTP/1.x response.
pub fn status_of(response: &[u8]) -> Option<u16> {
    let end = response.iter().position(|&b| b == b'\n')?;
    let line = std::str::from_utf8(&response[..end]).ok()?;
    let mut parts = line.split_whitespace();
    if !parts.next()?.starts_with("HTTP/") {
        return None;
    }
    parts.next()?.parse().ok()
}

/// Send one `GET /api/v1/health` and return the status code.
pub fn probe(addr: &str) -> Result<u16, String> {
    let sock: SocketAddr = addr
        .parse()
        .map_err(|e| format!("bad healthcheck address {addr}: {e}"))?;
    let mut stream =
        TcpStream::connect_timeout(&sock, TIMEOUT).map_err(|e| format!("connect {addr}: {e}"))?;
    let _ = stream.set_read_timeout(Some(TIMEOUT));
    let _ = stream.set_write_timeout(Some(TIMEOUT));
    // HTTP/1.0 + Connection: close — but read only the status line instead
    // of waiting for EOF, so a server that keeps the socket open (or a slow
    // body) can never stall the probe past the read timeout.
    let request = format!("GET {PATH} HTTP/1.0\r\nHost: localhost\r\nConnection: close\r\n\r\n");
    stream
        .write_all(request.as_bytes())
        .map_err(|e| format!("write {addr}: {e}"))?;
    let mut response = Vec::with_capacity(256);
    let mut chunk = [0u8; 256];
    loop {
        let read = stream
            .read(&mut chunk)
            .map_err(|e| format!("read {addr}: {e}"))?;
        if read == 0 {
            break;
        }
        let room = MAX_RESPONSE_BYTES as usize - response.len();
        response.extend_from_slice(&chunk[..read.min(room)]);
        if response.contains(&b'\n') || response.len() >= MAX_RESPONSE_BYTES as usize {
            break;
        }
    }
    status_of(&response).ok_or_else(|| format!("{addr}: no HTTP status line"))
}

/// Entry point for `--healthcheck`: 0 when healthy, 1 otherwise.
pub fn run() -> i32 {
    let addr = target_addr();
    match probe(&addr) {
        Ok(200) => 0,
        Ok(code) => {
            eprintln!("healthcheck: {addr}{PATH} answered {code}");
            1
        }
        Err(e) => {
            eprintln!("healthcheck: {e}");
            1
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;

    #[test]
    fn status_line_ok() {
        assert_eq!(
            status_of(b"HTTP/1.1 200 OK\r\ncontent-length: 15\r\n\r\n"),
            Some(200)
        );
        assert_eq!(
            status_of(b"HTTP/1.0 503 Service Unavailable\r\n"),
            Some(503)
        );
    }

    #[test]
    fn status_line_rejects_non_http() {
        assert_eq!(status_of(b""), None);
        assert_eq!(status_of(b"garbage\r\n"), None);
        assert_eq!(status_of(b"HTTP/1.1\r\n"), None);
        assert_eq!(status_of(b"HTTP/1.1 abc OK\r\n"), None);
    }

    #[test]
    fn wildcard_bind_probes_loopback() {
        assert_eq!(probe_addr_for("0.0.0.0:8080"), "127.0.0.1:8080");
        assert_eq!(probe_addr_for("[::]:9000"), "127.0.0.1:9000");
        assert_eq!(probe_addr_for("127.0.0.1:8081"), "127.0.0.1:8081");
        assert_eq!(probe_addr_for("10.0.0.5:8080"), "10.0.0.5:8080");
        assert_eq!(probe_addr_for("nonsense"), DEFAULT_ADDR);
    }

    /// Serve exactly one request with `status`, then close.
    fn spawn_server(status: &'static str) -> String {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind");
        let addr = listener.local_addr().expect("addr").to_string();
        std::thread::spawn(move || {
            if let Ok((mut sock, _)) = listener.accept() {
                let mut buf = [0u8; 512];
                let _ = sock.read(&mut buf);
                let _ = sock.write_all(
                    format!("HTTP/1.1 {status}\r\ncontent-length: 0\r\n\r\n").as_bytes(),
                );
            }
        });
        addr
    }

    #[test]
    fn probe_reports_healthy_and_unhealthy() {
        assert_eq!(probe(&spawn_server("200 OK")), Ok(200));
        assert_eq!(probe(&spawn_server("503 Service Unavailable")), Ok(503));
    }

    #[test]
    fn probe_reports_connection_failure() {
        // Port 1 is not a listening HTTP server on the test host; either the
        // connect fails outright or the read times out.
        let err = probe("127.0.0.1:1").expect_err("must not report healthy");
        assert!(err.contains("127.0.0.1:1"), "unexpected error: {err}");
    }
}
