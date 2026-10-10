"""テストモード可視化の回帰テスト: BOOKQ_TEST_MODE=true のとき全レスポンスに
X-Test-Mode: 1 が付くこと。server fixture は tests/conftest.py:272
(`env["BOOKQ_TEST_MODE"] = "true"`) でテストモード起動している。"""
import httpx


def test_test_mode_header_on_health_and_image(server):
    with httpx.Client(base_url=server, timeout=10) as c:
        health = c.get("/api/health")
        assert health.headers.get("x-test-mode") == "1", dict(health.headers)
        img = c.get("/image/simple_manga/1.png")
        assert img.status_code == 200
        assert img.headers.get("x-test-mode") == "1", dict(img.headers)
