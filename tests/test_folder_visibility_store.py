"""Visibility persistence over live HTTP (both lanes).

The unit-level store tests (sqlite revisions, key isolation, schema stamps)
live in the sibling py-only module, which stays quarantined until it
retires. This file covers only what is visible through the v1 HTTP
contract: writes are readable, revisions only bump on real changes, and
the hidden set starts and ends clean.

The hidden set is library-global and shared: every test unhides its roots
in a finally block (the roundtrip pattern from test_api_v1).
"""

import json
import urllib.error
import urllib.request

import pytest

VIS_PATH = "/api/v1/folder-visibility"

ROOT_A = "long"
ROOT_B = "series"


def _headers(resp):
    return {k.lower(): v for k, v in resp.headers.items()}


def _req(server, method, path, body=None, headers=None):
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(server + path, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None), _headers(resp)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null"), _headers(e)
        except Exception:
            return e.code, None, _headers(e)


def _snap(server):
    status, body, _ = _req(server, "GET", VIS_PATH)
    assert status == 200
    return body


def _put(server, root, hidden, owner):
    return _req(
        server, "PUT", VIS_PATH, {"root": root, "hidden": hidden},
        headers={"x-bookq-visibility-owner": owner},
    )


def _ensure_visible(server, owner, *roots):
    for root in roots:
        try:
            _put(server, root, False, owner)
        except Exception:
            pass


def test_write_is_readable(server):
    owner = _snap(server)["owner"]
    _ensure_visible(server, owner, ROOT_A)
    try:
        status, body, _ = _put(server, ROOT_A, True, owner)
        assert status == 200
        assert body["revision"] >= 1
        assert body["hiddenRoots"] == [ROOT_A]
        # a fresh read over the same state sees the write
        assert _snap(server)["hiddenRoots"] == [ROOT_A]
        assert _snap(server)["revision"] == body["revision"]
    finally:
        _ensure_visible(server, owner, ROOT_A)
    assert _snap(server)["hiddenRoots"] == [] or ROOT_A not in _snap(server)["hiddenRoots"]


def test_revision_only_bumps_on_real_change(server):
    owner = _snap(server)["owner"]
    _ensure_visible(server, owner, ROOT_A)
    try:
        base = _snap(server)["revision"]
        _, first, _ = _put(server, ROOT_A, True, owner)
        assert first["revision"] == base + 1
        # same value again: no bump
        _, again, _ = _put(server, ROOT_A, True, owner)
        assert again["revision"] == first["revision"]
        # unhide of a never-hidden sibling root is also a no-op
        _, noop, _ = _put(server, "never-was-xyz", False, owner)
        assert noop["revision"] == first["revision"]
        # real change bumps again
        _, last, _ = _put(server, ROOT_A, False, owner)
        assert last["revision"] == first["revision"] + 1
        assert last["hiddenRoots"] == []
    finally:
        _ensure_visible(server, owner, ROOT_A)


def test_multiple_roots_persist_sorted(server):
    owner = _snap(server)["owner"]
    _ensure_visible(server, owner, ROOT_A, ROOT_B)
    try:
        _put(server, ROOT_B, True, owner)
        _, body, _ = _put(server, ROOT_A, True, owner)
        assert sorted(body["hiddenRoots"]) == sorted([ROOT_A, ROOT_B])
        assert _snap(server)["hiddenRoots"] == body["hiddenRoots"]
        _, body, _ = _put(server, ROOT_A, False, owner)
        assert body["hiddenRoots"] == [ROOT_B]
    finally:
        _ensure_visible(server, owner, ROOT_A, ROOT_B)


def test_unhide_clears_row(server):
    owner = _snap(server)["owner"]
    _ensure_visible(server, owner, ROOT_A)
    try:
        _put(server, ROOT_A, True, owner)
        assert ROOT_A in _snap(server)["hiddenRoots"]
        _, body, _ = _put(server, ROOT_A, False, owner)
        assert body["hiddenRoots"] == []
        assert _snap(server)["hiddenRoots"] == []
    finally:
        _ensure_visible(server, owner, ROOT_A)


@pytest.mark.parametrize("root", [ROOT_A, ROOT_B])
def test_persistence_roundtrip_per_root(server, root):
    owner = _snap(server)["owner"]
    _ensure_visible(server, owner, root)
    try:
        _, hid, _ = _put(server, root, True, owner)
        assert root in hid["hiddenRoots"]
        assert root in _snap(server)["hiddenRoots"]
    finally:
        status, body, _ = _put(server, root, False, owner)
        assert status == 200
        assert root not in body["hiddenRoots"]
