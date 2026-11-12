"""Media decode 契約テスト — #406。

api/v1-semantics.md §7 の正本を pytest で検証する:

- 共通 native 集合 (jpg/png/…) は /thumb・/view とも 200
- Python-only (genuine .avif/.tiff) は Pillow が decode し 200 JPEG
  (Rust は Serve::Proxy → upstream 200 で同じ — cargo 側で gate を検証)
- 両側 unsupported (.heic/.heif/.jxl の代表 + 破損バイト) は Python 404
  (500 ではない) — Rust は Proxy → upstream 404 で同じ (cargo 側で検証)
- py/rs の集合定数が一致している (Rust ソースを parse して比較 —
  片側だけ形式を増やすとここが落ちる)

Run:
    BOOKQ_TEST_MODE=true uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_media_decode_contract.py -q
"""

import io
import os
import re
from pathlib import Path

import pytest

os.environ["BOOKQ_TEST_MODE"] = "true"

from PIL import Image  # noqa: E402

import app.thumbs as thumbs_mod  # noqa: E402


def _raster(fmt: str, color=(200, 60, 60)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(buf, format=fmt)
    return buf.getvalue()


@pytest.fixture
def decode_tree(tmp_path: Path) -> Path:
    (tmp_path / "plain").mkdir()
    Image.new("RGB", (64, 64), (10, 20, 30)).save(tmp_path / "plain" / "ok.png", format="PNG")
    try:
        (tmp_path / "plain" / "ok.avif").write_bytes(_raster("AVIF"))
    except Exception:
        pass
    try:
        (tmp_path / "plain" / "ok.tiff").write_bytes(_raster("TIFF"))
    except Exception:
        pass
    (tmp_path / "plain" / "bad.heic").write_bytes(b"not an image at all")
    (tmp_path / "plain" / "bad.avif").write_bytes(b"not an image at all")
    return tmp_path


@pytest.fixture
def client(decode_tree: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DOWNLOADS_DIR", str(decode_tree))
    monkeypatch.setenv("BOOKQ_THUMB_CACHE_DIR", str(tmp_path / "dc-cache"))
    from app.main import create_app

    from fastapi.testclient import TestClient

    return TestClient(create_app())


def _needs(path: Path) -> None:
    if not path.exists():
        pytest.skip(f"Pillow cannot encode {path.suffix} in this env")


def test_common_native_200_thumb_and_view(client):
    r = client.get("/thumb/plain/ok.png?w=200")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    r = client.get("/view/plain/ok.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"


def test_python_only_avif_200_thumb_and_view(client, decode_tree: Path):
    _needs(decode_tree / "plain" / "ok.avif")
    r = client.get("/thumb/plain/ok.avif?w=200")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    r = client.get("/view/plain/ok.avif")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"


def test_python_only_tiff_200_thumb_and_view(client, decode_tree: Path):
    _needs(decode_tree / "plain" / "ok.tiff")
    r = client.get("/thumb/plain/ok.tiff?w=200")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    r = client.get("/view/plain/ok.tiff")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"


@pytest.mark.parametrize("name", ["bad.heic", "bad.avif"])
def test_unsupported_returns_404_not_500(client, name: str):
    """両側 unsupported / 破損バイトは 404 (issue の 500 記述は誤り — 実測 404)。

    Rust は拡張子ゲートまたは decode 失敗で Proxy → upstream のこの 404 を
    そのまま返す (rust/viewer-rs/src/thumbs.rs::thumb_undecodable_proxies /
    thumb_view_python_only_and_unsupported_proxy で gate を検証)。
    """
    r = client.get(f"/thumb/plain/{name}?w=200")
    assert r.status_code == 404
    assert r.headers["content-type"] == "application/json"
    r = client.get(f"/view/plain/{name}")
    assert r.status_code == 404
    assert r.headers["content-type"] == "application/json"


def test_decode_sets_match_rust():
    """py 定数と rs NATIVE_DECODE/DECODE_* が一致している。

    片側だけ対応形式を増やすと native 直撃と proxy 経由で status が割れる
    ため、ここで落として spec §7 + 両定数の同時更新を強制する。
    """
    repo = Path(__file__).resolve().parent.parent
    rs = (repo / "rust/viewer-rs/src/thumbs.rs").read_text()

    def _parse(name: str) -> set[str]:
        m = re.search(r"const " + name + r": &[&str] = &\[([^\]]*)\]", rs)
        assert m, f"{name} not found in thumbs.rs"
        return set(re.findall(r'"(\.[a-z0-9]+)"', m.group(1)))

    assert _parse("NATIVE_DECODE") == set(thumbs_mod.COMMON_NATIVE_SUFFIXES)
    assert _parse("DECODE_PYTHON_ONLY") == set(thumbs_mod.PYTHON_ONLY_SUFFIXES)
    assert _parse("DECODE_UNSUPPORTED_EXAMPLES") == set(thumbs_mod.DECODE_UNSUPPORTED_EXAMPLES)
    # 集合は互いに素でなければならない (status 分岐が一意であるため)
    assert not (set(thumbs_mod.COMMON_NATIVE_SUFFIXES) & set(thumbs_mod.PYTHON_ONLY_SUFFIXES))
    assert not (set(thumbs_mod.COMMON_NATIVE_SUFFIXES) & set(thumbs_mod.DECODE_UNSUPPORTED_EXAMPLES))
