"""v0 deprecation headers (#407, policy #212).

Legacy /api/* (non-v1) responses must carry `Deprecation: true` (RFC 8594);
v1 responses must not. No `Sunset` header is emitted — shutdown rides with
the viewer stop (#410), date TBD.
"""

import urllib.request


def _headers(server, path):
    with urllib.request.urlopen(server + path, timeout=10) as resp:
        resp.read()
        return resp.status, {k.lower(): v for k, v in resp.headers.items()}


def test_v0_health_carries_deprecation(server):
    status, headers = _headers(server, "/api/health")
    assert status == 200
    assert headers.get("deprecation") == "true"
    assert "sunset" not in headers


def test_v0_version_carries_deprecation(server):
    status, headers = _headers(server, "/api/version")
    assert status == 200
    assert headers.get("deprecation") == "true"


def test_v1_health_has_no_deprecation(server):
    status, headers = _headers(server, "/api/v1/health")
    assert status == 200
    assert "deprecation" not in headers
