"""Contract tests for rebuild.sh heal_fqdn_collision (#66).

`heal_fqdn_collision` deletes tailnet nodes through the admin API — a
destructive operation. These tests stub `docker` / `curl` / `sleep` on
PATH and drive the real `./rebuild.sh dev heal-fqdn` entrypoint, so the
shared heal logic used by both the dev path and the prd deploy check is
covered without touching a real tailnet.

Scenarios mirror the issue's acceptance criteria: ambiguous states must
fail closed (no delete), and only a node holding the *expected* FQDN —
not merely "same tag + offline" — may be deleted.
"""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
REBUILD = REPO / "rebuild.sh"

EXPECTED = "dev-03.tail.ts.net"
ACTUAL = "dev-03-1.tail.ts.net"
SELF_ID = "self-device-id"
GHOST_ID = "ghost-device-id"
GHOST_IP = "100.64.0.2"

DOCKER_STUB = """#!/usr/bin/env bash
set -u
STATE="$STUB_STATE"
case "${1:-}" in
  ps) printf 'fakecid123\\n'; exit 0 ;;
  compose)
    printf 'compose %s\\n' "$*" >> "$STATE/docker.log"
    # prd deploy: `compose [profiles/-f...] <cmd>` — ps -q must name the
    # service so viewer-rs and tailscale resolve to distinct cids.
    case " $* " in
      *" ps -q viewer-rs"*) printf 'viewercid\\n'; exit 0 ;;
      *" ps -q tailscale"*) printf 'sidecarcid\\n'; exit 0 ;;
      *" up "*) exit 0 ;;
    esac
    exit 0 ;;
  inspect)
    # wait_healthy asks for .State.Health; verify_prd_version asks for
    # .Config.Image — answer each format distinctly.
    for a in "$@"; do
      case "$a" in
        *Config.Image*) printf 'bookq/rs:%s\\n' "${PRD_EXPECT_SHA:-deadbeef}"; exit 0 ;;
      esac
    done
    printf 'healthy\\n'; exit 0 ;;
  restart) touch "$STATE/restarted"; exit 0 ;;
  exec)
    for a in "$@"; do [ "$a" = "cert" ] && exit 0; done
    if [ -f "$STATE/restarted" ]; then
      cat "$STATE/status_after.json"
    else
      cat "$STATE/status_before.json"
    fi
    exit 0 ;;
esac
exit 0
"""

CURL_STUB = """#!/usr/bin/env bash
set -u
STATE="$STUB_STATE"
url="${!#}"
method="GET"
data=""
prev=""
for a in "$@"; do
  [ "$prev" = "-X" ] && method="$a"
  [ "$prev" = "-d" ] && data="$a"
  prev="$a"
done
case "$url" in
  *oauth/token*) printf '' ;;
  *tailnet/-/devices*)
    if [ -f "$STATE/devices_http_fail" ]; then
      # Valid JSON body + HTTP 500 — exercises the status check itself,
      # not the JSON parser (#604 review).
      cat "$STATE/devices.json"
      printf '\\n500'
    else
      cat "$STATE/devices.json"
      printf '\\n200'
    fi ;;
  *api.tailscale.com*)
    printf '%s %s %s\\n' "$method" "$url" "$data" >> "$STATE/calls.log"
    printf '200' ;;
  */api/v1/health*) printf '200' ;;
  *) printf '000' ;;
esac
exit 0
"""

SLEEP_STUB = """#!/usr/bin/env bash
exit 0
"""


def _stub(bin_dir: Path, name: str, body: str) -> None:
    p = bin_dir / name
    p.write_text(body)
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _status(online: bool | None, peer_dns: str = EXPECTED) -> dict:
    """`tailscale status --json` shape consumed by sidecar_dns_name /
    peer_online. `online=None` omits Peer entirely (status/peer failure)."""
    doc = {"Self": {"DNSName": f"{ACTUAL}."}}
    if online is not None:
        doc["Peer"] = {
            "nodekey:ghost": {
                "DNSName": f"{peer_dns}.",
                "Online": online,
                "TailscaleIPs": [GHOST_IP],
            }
        }
    return doc


def _devices(ghost_tags: list[str] | None = None, include_ghost: bool = True) -> dict:
    """Admin API device list. Ghost holds `expected`; self holds `actual`."""
    devices = [
        {
            "id": SELF_ID,
            "name": f"{ACTUAL}.",
            "addresses": ["100.64.0.1"],
            "tags": ["tag:bookq-dev"],
        }
    ]
    if include_ghost:
        ghost = {
            "id": GHOST_ID,
            "name": f"{EXPECTED}.",
            "addresses": [GHOST_IP],
            "tags": [] if ghost_tags is None else ghost_tags,
        }
        devices.append(ghost)
    return {"devices": devices}


def run_heal(
    tmp_path: Path,
    *,
    status_before: dict,
    devices: dict | str,
    status_after: dict | None = None,
    devices_http_fail: bool = False,
) -> tuple[subprocess.CompletedProcess, Path]:
    bin_dir = tmp_path / "bin"
    state = tmp_path / "state"
    bin_dir.mkdir()
    state.mkdir()
    _stub(bin_dir, "docker", DOCKER_STUB)
    _stub(bin_dir, "curl", CURL_STUB)
    _stub(bin_dir, "sleep", SLEEP_STUB)

    (state / "status_before.json").write_text(json.dumps(status_before))
    (state / "status_after.json").write_text(
        json.dumps(status_after if status_after is not None else _status(None))
    )
    if isinstance(devices, str):
        (state / "devices.json").write_text(devices)
    else:
        (state / "devices.json").write_text(json.dumps(devices))
    if devices_http_fail:
        (state / "devices_http_fail").touch()

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "STUB_STATE": str(state),
            "DEV_PROJECT": "test-proj",
            "DEV_FQDN": EXPECTED,
            "TS_API_KEY": "test-api-key",
            # Dummy non-empty values force the OAuth path inside
            # ensure-ts-authkey.sh --token; the curl stub answers the
            # token endpoint with an empty body so it falls through to
            # TS_API_KEY without any real network or .env credential use.
            "TS_OAUTH_CLIENT_ID": "stub-client-id",
            "TS_OAUTH_CLIENT_SECRET": "stub-client-secret",
            # Pin the remaining getv() inputs so ensure-ts-authkey.sh
            # never falls through to the real .env (#604 review).
            "TS_TAILNET": "-",
            "TS_AUTHKEY_TAGS": "tag:bookq-dev",
        }
    )
    proc = subprocess.run(
        ["bash", str(REBUILD), "dev", "heal-fqdn"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc, state


def calls(state: Path) -> str:
    f = state / "calls.log"
    return f.read_text() if f.exists() else ""


def test_matching_name_short_circuits(tmp_path):
    before = {"Self": {"DNSName": f"{EXPECTED}."}}
    proc, state = run_heal(tmp_path, status_before=before, devices=_devices())
    assert proc.returncode == 0
    assert calls(state) == ""
    assert not (state / "restarted").exists()


def test_tagged_offline_ghost_is_deleted_and_self_renamed(tmp_path):
    """Ghost = tagged + holds expected name + confirmed offline → DELETE,
    then self is renamed to the expected label and the sidecar restarts.
    A same-tag offline decoy with a DIFFERENT name must survive — DELETE
    is scoped to the expected-FQDN holder, not the tag (#604 review)."""
    devices = _devices(ghost_tags=["tag:bookq-dev"])
    devices["devices"].append(
        {
            "id": "decoy-device-id",
            "name": "other-99.tail.ts.net.",
            "addresses": ["100.64.0.9"],
            "tags": ["tag:bookq-dev"],
        }
    )
    # An unrelated OFFLINE peer whose name differs from the expected FQDN
    # must not confuse peer_online — it keeps scanning past non-matching
    # peers to the ghost's own entry (#604 review).
    status = _status(online=False)
    status["Peer"]["nodekey:alias"] = {
        "DNSName": "alias-01.tail.ts.net.",
        "Online": False,
        "TailscaleIPs": ["100.64.0.55"],
    }
    proc, state = run_heal(
        tmp_path,
        status_before=status,
        devices=devices,
        status_after={"Self": {"DNSName": f"{EXPECTED}."}},
    )
    assert proc.returncode == 0, proc.stderr
    log = calls(state)
    delete_i = log.index(f"DELETE https://api.tailscale.com/api/v2/device/{GHOST_ID}")
    # Rename POST carries the expected host label and runs AFTER the delete.
    rename = f"POST https://api.tailscale.com/api/v2/device/{SELF_ID}/name"
    assert f'{rename} {{"name":"dev-03"}}' in log
    assert delete_i < log.index(rename)
    # Decoy (same tag, different name) is never DELETEd.
    assert "decoy-device-id" not in log
    assert (state / "restarted").exists()
    assert "復旧" in proc.stderr


def test_untagged_ghost_is_never_deleted(tmp_path):
    """A same-name node without tag:bookq-dev may belong to another
    project/manual enrollment — warn and fail closed."""
    proc, state = run_heal(
        tmp_path,
        status_before=_status(online=False),
        devices=_devices(ghost_tags=[]),
    )
    assert proc.returncode == 1
    assert "DELETE" not in calls(state)
    assert "tag:bookq-dev" in proc.stderr


def test_online_ghost_is_never_deleted(tmp_path):
    """Ghost still online after the settle window → a live environment
    owns the name; do not delete."""
    proc, state = run_heal(
        tmp_path,
        status_before=_status(online=True),
        devices=_devices(ghost_tags=["tag:bookq-dev"]),
    )
    assert proc.returncode == 1
    assert "DELETE" not in calls(state)
    assert "オンライン" in proc.stderr


def test_unknown_peer_state_fails_closed(tmp_path):
    """Peer absent from the netmap (status exit 2) is indistinguishable
    from online → no delete."""
    proc, state = run_heal(
        tmp_path,
        status_before=_status(online=None),
        devices=_devices(ghost_tags=["tag:bookq-dev"]),
    )
    assert proc.returncode == 1
    assert "DELETE" not in calls(state)


def test_devices_api_error_fails_closed(tmp_path):
    proc, state = run_heal(
        tmp_path,
        status_before=_status(online=False),
        devices=_devices(ghost_tags=["tag:bookq-dev"]),
        devices_http_fail=True,
    )
    assert proc.returncode == 1
    assert calls(state) == ""


def test_unparseable_devices_fails_closed(tmp_path):
    proc, state = run_heal(
        tmp_path,
        status_before=_status(online=False),
        devices="this is not json",
    )
    assert proc.returncode == 1
    assert "DELETE" not in calls(state)


def test_prd_deploy_invokes_heal_with_base_url_fqdn(tmp_path):
    """`rebuild.sh prd deploy` derives expected_fqdn from BASE_URL and
    calls the same heal_fqdn_collision — the production call path, not
    only the dev entrypoint (#604 review)."""
    bin_dir = tmp_path / "bin"
    state = tmp_path / "state"
    bin_dir.mkdir()
    state.mkdir()
    _stub(bin_dir, "docker", DOCKER_STUB)
    _stub(bin_dir, "curl", CURL_STUB)
    _stub(bin_dir, "sleep", SLEEP_STUB)

    (state / "status_before.json").write_text(json.dumps(_status(online=False)))
    (state / "status_after.json").write_text(
        json.dumps({"Self": {"DNSName": f"{EXPECTED}."}})
    )
    (state / "devices.json").write_text(
        json.dumps(_devices(ghost_tags=["tag:bookq-dev"]))
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "STUB_STATE": str(state),
            # deploy_prd's required image/sha envs (values unused by stubs).
            "PRD_RS_IMAGE": "bookq/rs:deadbeef",
            "PRD_TAILSCALE_IMAGE": "bookq/tailscale:deadbeef",
            "PRD_EXPECT_SHA": "deadbeef",
            # BASE_URL host becomes the expected FQDN — actual stays
            # ACTUAL so the collision path runs end to end.
            "BASE_URL": f"https://{EXPECTED}",
            "TS_API_KEY": "test-api-key",
            "TS_OAUTH_CLIENT_ID": "stub-client-id",
            "TS_OAUTH_CLIENT_SECRET": "stub-client-secret",
            "TS_TAILNET": "-",
            "TS_AUTHKEY_TAGS": "tag:bookq-dev",
            # deploy_prd falls back to `dotenv_value` (worktree .env then
            # main checkout .env) when unset — and `${VAR:-...}` treats an
            # empty value as unset too, so only a non-empty pin is
            # deterministic. A dummy token forces the `public` profile
            # regardless of local .env / exported env (#604 review).
            "CLOUDFLARE_TUNNEL_TOKEN": "stub-token",
        }
    )
    proc = subprocess.run(
        ["bash", str(REBUILD), "prd", "deploy"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    log = calls(state)
    assert f"DELETE https://api.tailscale.com/api/v2/device/{GHOST_ID}" in log
    assert f"POST https://api.tailscale.com/api/v2/device/{SELF_ID}/name" in log
    assert (state / "restarted").exists()
    # The token pin must actually take the public-profile compose path —
    # otherwise the test silently exercised whichever profile local .env
    # happened to imply.
    docker_log = (state / "docker.log").read_text()
    assert "--profile public" in docker_log


def test_ghost_already_gone_still_renames_self(tmp_path):
    """No ghost rows (externally cleaned) → skip delete, still rename
    self to the expected name."""
    proc, state = run_heal(
        tmp_path,
        status_before=_status(online=False),
        devices=_devices(include_ghost=False),
        status_after={"Self": {"DNSName": f"{EXPECTED}."}},
    )
    assert proc.returncode == 0, proc.stderr
    log = calls(state)
    assert "DELETE" not in log
    assert f"POST https://api.tailscale.com/api/v2/device/{SELF_ID}/name" in log
