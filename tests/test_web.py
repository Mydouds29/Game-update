import json
import re

from app.collectors.steam import NEWS_URL
from app.db import repository as repo
from app.services import ingest

from .conftest import FakeHttp, fixture_text


def _seed(conn, config):
    http = FakeHttp({NEWS_URL: fixture_text("steam_palworld.json")})
    ingest.run_source(conn, repo.get_source(conn, "palworld-steam"), http, config,
                      notify_channel=None)


def _csrf(client, path):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def test_empty_feed(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Aucun patch pour l" in resp.get_data(as_text=True)


def test_feed_lists_patches_and_filters(client, conn, config):
    _seed(conn, config)
    html = client.get("/").get_data(as_text=True)
    assert "Palworld v0.6.8 Patch Notes" in html
    assert "Hotfix v0.6.7.1 is now live" in html
    assert "Autumn Sale" not in html
    assert client.get("/?game=palworld").status_code == 200
    assert "Aucun patch" in client.get("/?game=diablo-4").get_data(as_text=True)
    assert client.get("/?game=inconnu").status_code == 404


def test_patch_detail(client, conn, config):
    _seed(conn, config)
    patch_id = conn.execute("SELECT id FROM patches WHERE title LIKE 'Palworld v0.6.8%'").fetchone()[0]
    resp = client.get(f"/patch/{patch_id}")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Balance adjustments" in html and "Multiplayer" in html
    assert "Voir la note officielle" in html
    assert 'rel="noopener noreferrer nofollow"' in html
    assert client.get("/patch/99999").status_code == 404


def test_toggle_game_requires_csrf(client):
    assert client.post("/games/palworld/active", data={"active": "0"}).status_code == 400


def test_toggle_game(client, conn):
    token = _csrf(client, "/games")
    resp = client.post("/games/palworld/active",
                       data={"active": "0", "csrf_token": token, "next": "//evil.com"})
    assert resp.status_code == 303
    assert resp.headers["Location"].endswith("/games")  # pas de redirection ouverte
    assert repo.get_game(conn, "palworld")["active"] == 0
    assert client.post("/games/nope/active",
                       data={"active": "1", "csrf_token": token}).status_code == 404


def test_inactive_game_hidden_from_feed(client, conn, config):
    _seed(conn, config)
    repo.set_game_active(conn, "palworld", False)
    assert "Palworld v0.6.8" not in client.get("/").get_data(as_text=True)


def test_security_headers(client):
    resp = client.get("/")
    csp = resp.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"


def test_no_inline_styles_or_scripts(client, conn, config):
    _seed(conn, config)
    for path in ["/", "/games", "/settings", "/patch/1"]:
        html = client.get(path).get_data(as_text=True)
        assert "style=" not in html, path
        assert not re.search(r"<script(?![^>]*\bsrc=)", html), path


def test_tags_css(client):
    css = client.get("/tags.css").get_data(as_text=True)
    assert ".tag-diablo-4{--tag:#F29B94}" in css


def test_settings_and_health(client):
    assert client.get("/settings").status_code == 200
    assert client.get("/healthz").get_json() == {"status": "ok"}


def test_pwa_files(client):
    manifest = client.get("/manifest.webmanifest")
    assert manifest.status_code == 200
    assert json.loads(manifest.get_data())["start_url"] == "/"
    assert client.get("/sw.js").status_code == 200


def test_basic_auth(config, conn):
    from dataclasses import replace

    from werkzeug.security import generate_password_hash

    from app import create_app
    cfg = replace(config, basic_auth_user="fred",
                  basic_auth_password_hash=generate_password_hash("s3cret"))
    client = create_app(cfg).test_client()
    assert client.get("/").status_code == 401
    assert client.get("/healthz").status_code == 200
    import base64
    bad = base64.b64encode(b"fred:nope").decode()
    good = base64.b64encode(b"fred:s3cret").decode()
    assert client.get("/", headers={"Authorization": f"Basic {bad}"}).status_code == 401
    assert client.get("/", headers={"Authorization": f"Basic {good}"}).status_code == 200
