from __future__ import annotations

from pathlib import Path

import pytest

from app import create_app
from app.catalog import load_catalog, sync_catalog
from app.collectors.http import FetchError, HttpResponse
from app.config import Config
from app.db.models import connect, migrate

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeHttp:
    """Remplace HttpClient : répond depuis un dictionnaire URL -> réponse."""

    def __init__(self, routes: dict[str, object] | None = None) -> None:
        self.routes = routes or {}
        self.calls: list[dict] = []

    def get(self, url, *, params=None, etag=None, last_modified=None,
            check_robots=True, accept=None):
        self.calls.append({"url": url, "params": params, "etag": etag,
                           "last_modified": last_modified})
        route = self.routes.get(url)
        if route is None:
            raise FetchError(f"HTTP 404 sur {url}", status=404)
        if isinstance(route, Exception):
            raise route
        if isinstance(route, HttpResponse):
            return route
        return HttpResponse(url=url, status=200, text=route,
                            headers={"etag": '"v1"'})

    def allowed_by_robots(self, url):
        return True


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(
        env="development", secret_key="test-secret-key", testing=True,
        database_path=tmp_path / "test.sqlite3", backup_dir=tmp_path / "backups",
        notifier="log", trusted_proxies=0, base_url="https://gu.test",
    )


@pytest.fixture
def conn(config):
    c = connect(config.database_path)
    migrate(c)
    sync_catalog(c, load_catalog(FIXTURES / "catalog_test.toml"))
    yield c
    c.close()


@pytest.fixture
def app(config, conn):
    return create_app(config)


@pytest.fixture
def client(app):
    return app.test_client()
