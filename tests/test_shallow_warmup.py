"""Shallow warmup failing-first tests (plan shallow-warmup-fill T1).

(a) Opaque-shape half: ``scan_tree(base, list_archives=False)`` must return
    archives/PDFs as opaque nodes (``children == []``, ``thumb is None``,
    ``archive is True`` — PDFs additionally ``pdf is True``) while plain
    files/dirs scan unchanged. RED now: ``scan_tree`` takes no
    ``list_archives`` kwarg (TypeError). T2 alone turns this half GREEN.

(b) Generation half: ``GET /api/folders?since=<gen>`` must show
    generation-progressing differential behavior — same generation yields
    ``304`` or ``{"changed": false``, a stale generation yields the tree plus
    the newer generation. RED now: the ``since`` param does not exist (it is
    ignored and the full tree is always returned).

    NOTE on JSON Schema: the exact response envelope (dict keys, ETag mixing,
    ``changed`` flag shape) is owned by the T3 implementation
    (``app/routes.py`` generation counter + ``since`` polling). This test
    asserts *some* generation-progressing differential behavior only; T3 may
    revise this half's assertions to match the implemented schema without
    breaking the contract. This half stays RED until T3.

Run:
    BOOKQ_TEST_MODE=true uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_shallow_warmup.py -q
"""

import os
import sys
from pathlib import Path

import pytest

os.environ["BOOKQ_TEST_MODE"] = "true"

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from app.scanner import scan_tree  # noqa: E402
from tests.test_scanner_perf_todo6 import _make_pdf, _make_png, _make_zip  # noqa: E402


def _build_shallow_tree(root: Path) -> None:
    """Root with a plain image dir + one archive + one PDF (no flattening)."""
    plain = root / "plain"
    plain.mkdir(parents=True)
    _make_png(plain / "a.png")
    _make_zip(root / "book.zip", ["ch1/01.jpg", "ch1/02.jpg", "cover.jpg"])
    _make_pdf(root / "doc.pdf", pages=2)


def _by_name(nodes):
    return {n["name"]: n for n in nodes}


# ── (a) opaque-shape half (T2 turns GREEN) ───────────────────────────────────


def test_shallow_scan_archives_are_opaque(tmp_path):
    _build_shallow_tree(tmp_path)
    tree = scan_tree(tmp_path, list_archives=False)
    by_name = _by_name(tree)
    assert set(by_name) == {"plain", "book.zip", "doc.pdf"}

    book = by_name["book.zip"]
    assert book["type"] == "folder"
    assert book["archive"] is True
    assert book["children"] == []
    assert book["thumb"] is None

    doc = by_name["doc.pdf"]
    assert doc["type"] == "folder"
    assert doc["archive"] is True
    assert doc["pdf"] is True
    assert doc["children"] == []
    assert doc["thumb"] is None


def test_shallow_scan_plain_files_and_dirs_unchanged(tmp_path):
    _build_shallow_tree(tmp_path)
    shallow = _by_name(scan_tree(tmp_path, list_archives=False))
    full = _by_name(scan_tree(tmp_path))

    # Same top-level membership and sort order.
    assert [n["name"] for n in scan_tree(tmp_path, list_archives=False)] == [
        n["name"] for n in scan_tree(tmp_path)
    ]
    # Plain dir node is identical in both modes.
    assert shallow["plain"] == full["plain"]
    assert shallow["plain"]["type"] == "folder"
    assert [c["name"] for c in shallow["plain"]["children"]] == ["a.png"]
    assert shallow["plain"]["children"][0]["type"] == "image"


# ── (b) generation half (stays RED until T3; schema owned by T3) ─────────────


@pytest.fixture
def archive_client(tmp_path, monkeypatch):
    """TestClient serving a tree with one archive (fill target)."""
    _build_shallow_tree(tmp_path)
    monkeypatch.setenv("DOWNLOADS_DIR", str(tmp_path))
    from app.main import create_app
    from fastapi.testclient import TestClient

    return TestClient(create_app())


def _current_generation(first_payload):
    """Extract the tree generation, tolerating the pre-T3 list shape."""
    if isinstance(first_payload, dict) and "generation" in first_payload:
        return first_payload["generation"]
    # Pre-T3 code exposes no generation; per plan warmup completes at gen 1.
    return 1


def test_folders_since_returns_differential_response(archive_client):
    r1 = archive_client.get("/api/folders")
    assert r1.status_code == 200
    gen = _current_generation(r1.json())

    # Same generation → no new content: 304 or an explicit unchanged flag.
    r_same = archive_client.get("/api/folders", params={"since": gen})
    if r_same.status_code != 304:
        body = r_same.json()
        assert isinstance(body, dict) and body.get("changed") is False, (
            f"same-generation since={gen} must be 304 or changed=false, "
            f"got status={r_same.status_code} body-type={type(body).__name__}"
        )

    # Stale generation → tree plus the newer generation (differential fill).
    r_stale = archive_client.get("/api/folders", params={"since": 0})
    assert r_stale.status_code == 200
    stale = r_stale.json()
    assert isinstance(stale, dict) and "generation" in stale, (
        f"stale since=0 must return tree+generation, got {type(stale).__name__}"
    )
    assert stale["generation"] != 0
