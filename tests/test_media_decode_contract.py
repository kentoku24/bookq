"""Media decode 契約テスト — #406 (Phase 2c v1 port, #487)。

api/v1-semantics.md §7 の正本を HTTP 応答契約として検証する
(両 lane: 既定 uvicorn + BOOKQ_TEST_SERVER=rs):

- 共通 native 集合 (jpg/png/…) は /thumb・/view とも両 lane で 200
- Python-only (genuine .avif/.tiff) は Pillow が decode し py lane で 200
  JPEG。rs lane は Serve::Proxy → no-upstream で 501 (gate 自体は cargo
  側 thumbs.rs::decode_gate_truth_table /
  thumb_view_python_only_and_unsupported_proxy が検証)
- 両側 unsupported (.heic/.heif/.jxl の代表 + 破損バイト) は py lane で
  404 (500 ではない)。rs lane は Proxy → no-upstream で 501
- py/rs の集合定数が一致している (両ソースを text parse で比較 —
  片側だけ形式を増やすとここが落ちる。app import なしで両 lane 実行可)

Run:
    BOOKQ_TEST_MODE=true uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_media_decode_contract.py -q
    BOOKQ_TEST_SERVER=rs uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_media_decode_contract.py -q
"""

import io
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_RS = os.environ.get("BOOKQ_TEST_SERVER") == "rs"

# rs lane は no-upstream のため Serve::Proxy が 501 になる — HTTP level でも
# gate の分岐 (native 直撃か proxy か) が観測できる。
_PROXY_STATUS = 501 if _RS else None


def _get(server: str, path: str, headers: dict | None = None):
    req = urllib.request.Request(server + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return (
                resp.status,
                {k.lower(): v for k, v in resp.headers.items()},
                resp.read(),
            )
    except urllib.error.HTTPError as e:
        return (
            e.code,
            {k.lower(): v for k, v in (e.headers or {}).items()},
            e.read(),
        )


def _raster(fmt: str, color=(200, 60, 60)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(buf, format=fmt)
    return buf.getvalue()


@pytest.fixture(scope="module")
def decode_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    from PIL import Image

    root = tmp_path_factory.mktemp("decode")
    plain = root / "plain"
    plain.mkdir()
    Image.new("RGB", (64, 64), (10, 20, 30)).save(plain / "ok.png", format="PNG")
    for name, fmt in (("ok.avif", "AVIF"), ("ok.tiff", "TIFF")):
        try:
            (plain / name).write_bytes(_raster(fmt))
        except Exception:
            pass
    (plain / "bad.heic").write_bytes(b"not an image at all")
    (plain / "bad.avif").write_bytes(b"not an image at all")
    return root


@pytest.fixture(scope="module")
def decode_server(decode_tree: Path):
    """Dedicated backend over the decode tree (both lanes via rs_spawn)."""
    from rs_spawn import spawn_backend

    proc, base_url = spawn_backend(
        downloads_dir=decode_tree, state_dir=os.environ["BOOKQ_STATE_DIR"]
    )
    try:
        yield base_url
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _needs(path: Path) -> None:
    if not path.exists():
        pytest.skip(f"Pillow cannot encode {path.suffix} in this env")


def test_common_native_200_thumb_and_view(decode_server: str):
    status, headers, _ = _get(decode_server, "/thumb/plain/ok.png?w=200")
    assert status == 200
    assert headers["content-type"] == "image/jpeg"
    status, headers, _ = _get(decode_server, "/view/plain/ok.png")
    assert status == 200
    assert headers["content-type"] == "image/png"


def test_common_native_thumb_304(decode_server: str):
    """ETag 再訪は 304 (HTTP level の shape のみ — normalize 契約の正本は
    cargo 側 thumbs.rs::view_w_* tests)。"""
    status, headers, _ = _get(decode_server, "/thumb/plain/ok.png?w=200")
    assert status == 200
    etag = headers.get("etag")
    assert etag
    status, _, _ = _get(
        decode_server, "/thumb/plain/ok.png?w=200", {"If-None-Match": etag}
    )
    assert status == 304


def test_python_only_avif_200_thumb_and_view(decode_server: str, decode_tree: Path):
    _needs(decode_tree / "plain" / "ok.avif")
    if _RS:
        # image crate は avif を decode できない → Proxy → no-upstream 501。
        # gate の正本は cargo 側 (decode_gate_truth_table)。
        for path in ("/thumb/plain/ok.avif?w=200", "/view/plain/ok.avif"):
            status, headers, _ = _get(decode_server, path)
            assert status == 501
            assert headers["content-type"] == "application/json"
        return
    status, headers, _ = _get(decode_server, "/thumb/plain/ok.avif?w=200")
    assert status == 200
    assert headers["content-type"] == "image/jpeg"
    status, headers, _ = _get(decode_server, "/view/plain/ok.avif")
    assert status == 200
    assert headers["content-type"] == "image/jpeg"


def test_python_only_tiff_200_thumb_and_view(decode_server: str, decode_tree: Path):
    _needs(decode_tree / "plain" / "ok.tiff")
    if _RS:
        for path in ("/thumb/plain/ok.tiff?w=200", "/view/plain/ok.tiff"):
            status, headers, _ = _get(decode_server, path)
            assert status == 501
            assert headers["content-type"] == "application/json"
        return
    status, headers, _ = _get(decode_server, "/thumb/plain/ok.tiff?w=200")
    assert status == 200
    assert headers["content-type"] == "image/jpeg"
    status, headers, _ = _get(decode_server, "/view/plain/ok.tiff")
    assert status == 200
    assert headers["content-type"] == "image/jpeg"


@pytest.mark.parametrize("name", ["bad.heic", "bad.avif"])
def test_unsupported_returns_404_not_500(decode_server: str, name: str):
    """両側 unsupported / 破損バイトは py lane 404 (500 ではない)。

    rs lane は拡張子ゲートまたは decode 失敗で Proxy → no-upstream の 501。
    gate 自体は cargo 側 (thumbs.rs::thumb_undecodable_proxies /
    thumb_view_python_only_and_unsupported_proxy) が検証する。
    """
    expected = _PROXY_STATUS or 404
    for path in (f"/thumb/plain/{name}?w=200", f"/view/plain/{name}"):
        status, headers, _ = _get(decode_server, path)
        assert status == expected, path
        assert headers["content-type"] == "application/json"


def test_decode_sets_match_rust():
    """py 定数と rs NATIVE_DECODE/DECODE_* が一致している。

    両ソースを text parse する (import app なし — 両 lane で実行可)。
    片側だけ対応形式を増やすと native 直撃と proxy 経由で status が割れる
    ため、ここで落として spec §7 + 両定数の同時更新を強制する。
    """
    repo = Path(__file__).resolve().parent.parent
    py_src = (repo / "app" / "thumbs.py").read_text()
    rs_src = (repo / "rust" / "viewer-rs" / "src" / "thumbs.rs").read_text()

    def _parse_rs(name: str) -> set[str]:
        m = re.search(r"const\s+" + name + r"\s*:[^=]*=\s*&\[([^\]]*)\]", rs_src)
        assert m, f"{name} not found in thumbs.rs"
        return set(re.findall(r'"(\.[a-z0-9]+)"', m.group(1)))

    def _parse_py(name: str) -> set[str]:
        m = re.search(name + r"\s*=\s*frozenset\(\{([^}]*)\}\)", py_src)
        assert m, f"{name} not found in app/thumbs.py"
        return set(re.findall(r'"(\.[a-z0-9]+)"', m.group(1)))

    assert _parse_rs("NATIVE_DECODE") == _parse_py("COMMON_NATIVE_SUFFIXES")
    assert _parse_rs("DECODE_PYTHON_ONLY") == _parse_py("PYTHON_ONLY_SUFFIXES")
    assert _parse_rs("DECODE_UNSUPPORTED_EXAMPLES") == _parse_py(
        "DECODE_UNSUPPORTED_EXAMPLES"
    )
    # 集合は互いに素でなければならない (status 分岐が一意であるため)
    py_native = _parse_py("COMMON_NATIVE_SUFFIXES")
    py_only = _parse_py("PYTHON_ONLY_SUFFIXES")
    py_unsup = _parse_py("DECODE_UNSUPPORTED_EXAMPLES")
    assert not (py_native & py_only)
    assert not (py_native & py_unsup)
    assert not (py_only & py_unsup)
