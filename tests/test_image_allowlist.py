"""Security regression tests for the loose-file /image serving boundary."""

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app


def _client_for_downloads(downloads: Path, monkeypatch) -> TestClient:
    monkeypatch.setenv("DOWNLOADS_DIR", str(downloads))
    return TestClient(create_app())


def test_loose_html_is_not_served_by_image_endpoint(tmp_path: Path, monkeypatch):
    (tmp_path / "payload.html").write_text(
        '<script>document.body.textContent="executed"</script>', encoding="utf-8"
    )
    client = _client_for_downloads(tmp_path, monkeypatch)

    response = client.get("/image/payload.html")

    assert response.status_code == 404
    assert "text/html" not in response.headers.get("content-type", "")


def test_loose_xml_is_not_served_by_image_endpoint(tmp_path: Path, monkeypatch):
    (tmp_path / "payload.xml").write_text(
        '<?xml version="1.0"?><payload/>', encoding="utf-8"
    )
    client = _client_for_downloads(tmp_path, monkeypatch)

    response = client.get("/image/payload.xml")

    assert response.status_code == 404


def test_loose_image_extension_remains_served(tmp_path: Path, monkeypatch):
    image_bytes = b"not-decoded-by-FileResponse"
    (tmp_path / "page.png").write_bytes(image_bytes)
    client = _client_for_downloads(tmp_path, monkeypatch)

    response = client.get("/image/page.png")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")
    assert response.content == image_bytes
