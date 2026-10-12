"""Live image bytes + thumbs (#31): first/middle/last image serves, middle
thumbs, /api/images count matches. Asserts carry hash IDs only."""
import json
import urllib.error
import urllib.parse
import urllib.request

import pytest

pytestmark = pytest.mark.skipif(
    not __import__("os").environ.get("BOOKQ_LIVE_ROOT"),
    reason="live-only: set BOOKQ_LIVE_ROOT to run",
)


def _qpath(path):
    return "/".join(urllib.parse.quote(seg, safe="") for seg in path.split("/"))


def _get(base, url, timeout, hid, what):
    req = urllib.request.Request(base + url, headers={"Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            assert r.status == 200, f"{what} status={r.status} on [{hid}]"
            return r.headers.get_content_type(), body
    except urllib.error.HTTPError as e:
        raise AssertionError(f"{what} status={e.code} on [{hid}]")


def test_live_image_bytes(live_server, live_sample):
    assert live_sample, "empty sample"
    for leaf in live_sample:
        names = leaf["images"]
        picks = [names[0], names[len(names) // 2], names[-1]]
        for i, name in enumerate(picks):
            ctype, body = _get(
                live_server, f"/image/{_qpath(leaf['path'])}/{urllib.parse.quote(name, safe='')}",
                60, leaf["id"], f"image {i}",
            )
            assert ctype.startswith("image/"), f"type={ctype} on [{leaf['id']}]"
            assert len(body) > 0, f"empty body on [{leaf['id']}]"


def test_live_images_api_count(live_server, live_sample):
    for leaf in live_sample:
        q = urllib.parse.quote(leaf["path"], safe="")
        with urllib.request.urlopen(
            f"{live_server}/api/images?folder={q}", timeout=60
        ) as r:
            names = json.load(r)
        assert len(names) == len(leaf["images"]), (
            f"api/images={len(names)} tree={len(leaf['images'])} on [{leaf['id']}]"
        )


def test_live_thumb_middle(live_server, live_sample):
    for leaf in live_sample:
        name = leaf["images"][len(leaf["images"]) // 2]
        ctype, body = _get(
            live_server,
            f"/thumb/{_qpath(leaf['path'])}/{urllib.parse.quote(name, safe='')}?w=200",
            120, leaf["id"], "thumb",
        )
        assert ctype.startswith("image/"), f"type={ctype} on [{leaf['id']}]"
        assert len(body) > 0, f"empty thumb on [{leaf['id']}]"
