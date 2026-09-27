import pytest
import requests

from app.catalog import CatalogError, load_catalog, validate_catalog
from app.collectors.http import FetchError, HttpClient, RobotsDisallowed
from app.config import Config, ConfigError
from app.services.notify import Notification, NotifyError, NtfyNotifier


class FakeResp:
    def __init__(self, status=200, body=b"", headers=None, url="https://x.test/"):
        self.status_code = status
        self._body = body
        self.headers = headers or {"content-type": "text/html; charset=utf-8"}
        self.encoding = "utf-8"
        self.url = url

    def iter_content(self, chunk_size):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.headers = {}
        self.requests = []

    def get(self, url, headers=None, params=None, **kw):
        self.requests.append((url, headers))
        route = self.routes.get(url)
        if isinstance(route, Exception):
            raise route
        return route or FakeResp(404)


def _client(routes, **kw):
    sleeps = []
    client = HttpClient("TestBot/1.0", session=FakeSession(routes), min_interval=5,
                        clock=lambda: 100.0, sleep=sleeps.append, **kw)
    return client, sleeps


def test_robots_disallow():
    client, _ = _client({
        "https://x.test/robots.txt": FakeResp(body=b"User-agent: *\nDisallow: /private\n"),
        "https://x.test/public": FakeResp(body=b"ok"),
    })
    assert client.get("https://x.test/public").text == "ok"
    with pytest.raises(RobotsDisallowed):
        client.get("https://x.test/private/page")


def test_robots_404_allows_and_unreachable_is_transient():
    client, _ = _client({"https://x.test/page": FakeResp(body=b"ok")})
    assert client.get("https://x.test/page").text == "ok"
    client, _ = _client({"https://y.test/robots.txt": requests.ConnectionError("down")})
    with pytest.raises(FetchError) as exc:
        client.get("https://y.test/page")
    assert not isinstance(exc.value, RobotsDisallowed)


def test_rate_limit_per_host():
    client, sleeps = _client({"https://x.test/a": FakeResp(body=b"1"),
                              "https://x.test/b": FakeResp(body=b"2")})
    client.get("https://x.test/a", check_robots=False)
    client.get("https://x.test/b", check_robots=False)
    assert sleeps == [5.0]


def test_conditional_headers_and_304():
    client, _ = _client({"https://x.test/a": FakeResp(304)})
    resp = client.get("https://x.test/a", etag='"e"', last_modified="Mon", check_robots=False)
    assert resp.not_modified
    sent = client.session.requests[-1][1]
    assert sent == {"If-None-Match": '"e"', "If-Modified-Since": "Mon"}


def test_size_limit_and_errors():
    client, _ = _client({"https://x.test/big": FakeResp(body=b"x" * 2000),
                         "https://x.test/429": FakeResp(429, headers={"retry-after": "120"}),
                         "https://x.test/500": FakeResp(500)}, max_bytes=1000)
    with pytest.raises(FetchError, match="volumineuse"):
        client.get("https://x.test/big", check_robots=False)
    with pytest.raises(FetchError) as exc:
        client.get("https://x.test/429", check_robots=False)
    assert exc.value.retry_after == 120
    with pytest.raises(FetchError):
        client.get("https://x.test/500", check_robots=False)
    with pytest.raises(FetchError, match="schéma"):
        client.get("file:///etc/passwd", check_robots=False)


def test_app_catalog_has_no_undecided_games():
    # Aucun jeu n'est ajouté sans décision explicite du propriétaire.
    assert load_catalog()["games"] == []


def test_test_catalog_is_valid():
    from .conftest import FIXTURES
    data = load_catalog(FIXTURES / "catalog_test.toml")
    assert {g["slug"] for g in data["games"]} >= {"diablo-4", "palworld"}


def test_catalog_validation_errors():
    with pytest.raises(CatalogError):
        validate_catalog({"games": [{"slug": "Bad Slug", "name": "x", "short_name": "x",
                                     "tag_color": "#000000"}]})
    with pytest.raises(CatalogError, match="appid"):
        validate_catalog({"games": [{"slug": "g", "name": "x", "short_name": "x",
                                     "tag_color": "#000000",
                                     "sources": [{"key": "g-s", "type": "steam",
                                                  "params": {}}]}]})


def test_config_production_requires_secret(monkeypatch):
    monkeypatch.setenv("GU_ENV", "production")
    monkeypatch.delenv("GU_SECRET_KEY", raising=False)
    with pytest.raises(ConfigError):
        Config.from_env()
    monkeypatch.setenv("GU_SECRET_KEY", "x" * 40)
    monkeypatch.setenv("GU_NOTIFIER", "ntfy")
    monkeypatch.delenv("GU_NTFY_TOPIC", raising=False)
    with pytest.raises(ConfigError, match="NTFY_TOPIC"):
        Config.from_env()


class PostSession:
    def __init__(self, status=200, exc=None):
        self.status, self.exc, self.calls = status, exc, []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json, headers))
        if self.exc:
            raise self.exc
        return type("R", (), {"status_code": self.status})()


def test_ntfy_notifier():
    session = PostSession()
    NtfyNotifier("https://ntfy.test/", "topic", token="tk", session=session).send(
        Notification("Diablo IV — nouveau patch", "Patch 3.2.2 é", "https://gu.test/patch/1"))
    url, payload, headers = session.calls[0]
    assert url == "https://ntfy.test"
    assert payload["topic"] == "topic" and payload["click"] == "https://gu.test/patch/1"
    assert headers["Authorization"] == "Bearer tk"
    with pytest.raises(NotifyError):
        NtfyNotifier("https://n", "t", session=PostSession(status=403)).send(
            Notification("a", "b", "c"))
    with pytest.raises(NotifyError):
        NtfyNotifier("https://n", "t", session=PostSession(
            exc=requests.ConnectionError())).send(Notification("a", "b", "c"))


def test_backup(config, conn):
    from app.cli import backup_database
    path = backup_database(config)
    assert path.exists() and oct(path.stat().st_mode)[-3:] == "600"
