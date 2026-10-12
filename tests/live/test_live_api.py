"""Live API smoke + repo hygiene (#31)."""
import json
import os
import urllib.parse
import urllib.request

import pytest

from live.discover import get_tree as _get_tree

pytestmark = pytest.mark.skipif(
    not os.environ.get("BOOKQ_LIVE_ROOT"),
    reason="live-only: set BOOKQ_LIVE_ROOT to run",
)


def test_live_folders_and_search(live_server, live_sample):
    tree = _get_tree(live_server)
    assert isinstance(tree, list) and tree, "empty tree"
    probe = live_sample[0]["images"][0]
    term = next((c for c in probe if c.isalnum()), "a")
    with urllib.request.urlopen(
        live_server + "/api/search?q=" + urllib.parse.quote(term, safe=""), timeout=120
    ) as r:
        assert r.status == 200
        body = json.load(r)
    assert isinstance(body, (list, dict)), "search shape"


def test_live_hygiene(live_sample):
    """No real folder/file name may appear in this directory, the wrapper
    script, or this suite's docs. Components longer than 6 chars from
    discovered paths must not occur there."""
    repo_files = [
        "tests/live/conftest.py",
        "tests/live/test_live_viewer.py",
        "tests/live/test_live_images.py",
        "tests/live/test_live_api.py",
        "tests/live/README.md",
        "scripts/live-regress.sh",
    ]
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    corpus = ""
    for rel in repo_files:
        p = os.path.join(here, rel)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                corpus += f.read() + "\n"
    needles = set()
    for leaf in live_sample:
        for part in leaf["path"].split("/") + leaf["images"]:
            if len(part) > 6:
                needles.add(part)
    hits = sorted(n for n in needles if n in corpus)
    assert not hits, f"live names leaked into repo: {len(hits)} hit(s)"
