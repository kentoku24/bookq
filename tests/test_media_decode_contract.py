"""Media decode 契約テスト — #406 (Phase 2c v1 port, #487, #503 rs-only)。

api/v1-semantics.md §7 の正本を HTTP 応答契約として検証する:

- 共通 native 集合 (jpg/png/…) は /thumb・/view とも 200
- Python-only 由来 (genuine .avif/.tiff) は Serve::Proxy → no-upstream
  で 501 (gate 自体は cargo 側 thumbs.rs::decode_gate_truth_table /
  thumb_view_python_only_and_unsupported_proxy が検証)
- unsupported (.heic/.heif/.jxl の代表 + 破損バイト) は Proxy →
  no-upstream で 501
- rs の集合定数が自己無矛盾である (NATIVE/ONLY/UNSUP が互いに素 —
  形式追加時は spec §7 + 定数の同時更新を強制)

Run:
    BOOKQ_TEST_MODE=true uv run --with pytest --with playwright --with-requirements requirements-dev.txt pytest tests/test_media_decode_contract.py -q
"""

import io
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

import pytest

# no-upstream のため Serve::Proxy が 501 になる — HTTP level でも
# gate の分岐 (native 直撃か proxy か) が観測できる。
_PROXY_STATUS = 501


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
    """Dedicated viewer-rs backend over the decode tree (via rs_spawn)."""
    from rs_spawn import spawn_backend

    proc, base_url = spawn_backend(
        library_dir=decode_tree, state_dir=os.environ["BOOKQ_STATE_DIR"]
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
    # image crate は avif を decode できない → Proxy → no-upstream 501。
    # gate の正本は cargo 側 (decode_gate_truth_table)。
    for path in ("/thumb/plain/ok.avif?w=200", "/view/plain/ok.avif"):
        status, headers, _ = _get(decode_server, path)
        assert status == 501
        assert headers["content-type"] == "application/json"


def test_python_only_tiff_200_thumb_and_view(decode_server: str, decode_tree: Path):
    _needs(decode_tree / "plain" / "ok.tiff")
    for path in ("/thumb/plain/ok.tiff?w=200", "/view/plain/ok.tiff"):
        status, headers, _ = _get(decode_server, path)
        assert status == 501
        assert headers["content-type"] == "application/json"


@pytest.mark.parametrize("name", ["bad.heic", "bad.avif"])
def test_unsupported_returns_404_not_500(decode_server: str, name: str):
    """unsupported / 破損バイトは 500 ではない。

    rs は拡張子ゲートまたは decode 失敗で Proxy → no-upstream の 501。
    gate 自体は cargo 側 (thumbs.rs::thumb_undecodable_proxies /
    thumb_view_python_only_and_unsupported_proxy) が検証する。
    """
    expected = _PROXY_STATUS
    for path in (f"/thumb/plain/{name}?w=200", f"/view/plain/{name}"):
        status, headers, _ = _get(decode_server, path)
        assert status == expected, path
        assert headers["content-type"] == "application/json"


def test_decode_sets_match_rust():
    """rs NATIVE_DECODE/DECODE_* が自己無矛盾である。

    #503 で app/thumbs.py は削除されたため、py/rs パリティ比較は廃止。
    形式追加時は spec §7 + rs 定数の同時更新を強制するため、集合の
    素性 (status 分岐の一意性) をここで担保する。
    """
    repo = Path(__file__).resolve().parent.parent
    rs_src = (repo / "rust" / "viewer-rs" / "src" / "thumbs.rs").read_text()

    def _parse_rs(name: str) -> set[str]:
        m = re.search(r"const\s+" + name + r"\s*:[^=]*=\s*&\[([^\]]*)\]", rs_src)
        assert m, f"{name} not found in thumbs.rs"
        return set(re.findall(r'"(\.[a-z0-9]+)"', m.group(1)))

    rs_native = _parse_rs("NATIVE_DECODE")
    rs_only = _parse_rs("DECODE_PYTHON_ONLY")
    rs_unsup = _parse_rs("DECODE_UNSUPPORTED_EXAMPLES")
    assert rs_native and rs_only and rs_unsup
    # 集合は互いに素でなければならない (status 分岐が一意であるため)
    assert not (rs_native & rs_only)
    assert not (rs_native & rs_unsup)
    assert not (rs_only & rs_unsup)
