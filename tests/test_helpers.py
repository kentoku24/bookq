import pytest

from tests.helpers import get_folders_ready


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.text = repr(payload)

    def json(self):
        return self._payload


class _Client:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = 0
        self.urls = []

    def get(self, url, params=None):
        self.urls.append((url, params))
        self.calls += 1
        return self._responses[min(self.calls - 1, len(self._responses) - 1)]


def test_202_then_200_returns_json(monkeypatch):
    monkeypatch.setattr("tests.helpers.time.sleep", lambda *_: None)
    client = _Client(_Resp(202), _Resp(202), _Resp(200, [{"name": "x"}]))
    assert get_folders_ready(client) == [{"name": "x"}]
    assert client.calls == 3
    assert client.urls[0] == ("/api/folders", None)


def test_params_forwarded(monkeypatch):
    monkeypatch.setattr("tests.helpers.time.sleep", lambda *_: None)
    client = _Client(_Resp(200, [{"name": "sub"}]))
    params = {"path": "a/b", "depth": 1}
    assert get_folders_ready(client, params=params) == [{"name": "sub"}]
    assert client.urls[0] == ("/api/folders", params)


def test_unexpected_status_fails_without_retry(monkeypatch):
    monkeypatch.setattr("tests.helpers.time.sleep", lambda *_: None)
    client = _Client(_Resp(500), _Resp(200, []))
    with pytest.raises(AssertionError):
        get_folders_ready(client)
    assert client.calls == 1


def test_permanent_202_times_out(monkeypatch):
    ticks = iter([0.0, 10.0, 100.0])
    monkeypatch.setattr("tests.helpers.time.monotonic", lambda: next(ticks, 100.0))
    monkeypatch.setattr("tests.helpers.time.sleep", lambda *_: None)
    client = _Client(_Resp(202))
    with pytest.raises(AssertionError):
        get_folders_ready(client)
    assert client.calls == 2


def test_request_errors_propagate():
    class Boom:
        def get(self, url, params=None):
            raise RuntimeError("connection refused")

    with pytest.raises(RuntimeError):
        get_folders_ready(Boom())
