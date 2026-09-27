import pytest

from app.collectors.base import ParseError, SourceConfig
from app.collectors.http import FetchError, HttpResponse
from app.collectors.rss import RssCollector, discover_feeds, feeds_declared_in, parse_feed
from app.parsing.classifier import classify
from app.parsing.normalizer import normalize

from .conftest import FIXTURES, FakeHttp, fixture_text

FEED = "https://studio.example/news/rss"


def _bytes(name):
    return (FIXTURES / name).read_bytes()


def test_parse_rss_with_declared_encoding():
    items = parse_feed(_bytes("feed_rss.xml"), FEED)
    assert [i.title for i in items] == [
        "Patch 1.4.2 Notes", "Autumn Sale: 30% off", "Hotfix 1.4.1 – Café update"]
    first = items[0]
    assert first.source_key == "guid:news-812"
    assert first.url == "https://studio.example/news/patch-1-4-2"  # lien relatif résolu
    assert first.published_at.isoformat() == "2026-09-22T15:00:00+02:00"
    assert first.published_raw == "Tue, 22 Sep 2026 15:00:00 +0200"
    assert first.tags == ["Patch Notes"]
    # Sans <guid>, le lien sert d'identifiant.
    assert items[2].source_key == "guid:https://studio.example/news/hotfix-1-4-1"


def test_rss_content_is_normalized_and_filtered():
    items = parse_feed(_bytes("feed_rss.xml"), FEED)
    kept = [i for i in items if classify(i.title, i.tags, i.body).is_patch]
    assert [i.title for i in kept] == ["Patch 1.4.2 Notes", "Hotfix 1.4.1 – Café update"]
    patch = normalize(kept[0])
    assert patch.version == "1.4.2"
    assert [s.title for s in patch.sections] == ["Fixes", "Balance"]
    assert [i.kind for i in patch.sections[1].items] == ["balance"]
    assert normalize(kept[1]).sections[0].items[0].text == "Fixed login issues."


def test_parse_atom():
    items = parse_feed(fixture_text("feed_atom.xml"), "https://game.example/feed")
    assert len(items) == 1
    entry = items[0]
    assert entry.source_key == "guid:urn:example:update:201"
    assert entry.url == "https://game.example/updates/2.0.1"
    assert entry.published_at.isoformat() == "2026-09-25T12:00:00+00:00"
    assert normalize(entry).sections[0].items[0].kind == "new"


@pytest.mark.parametrize("payload", [b"not xml", b"<html><body/></html>", b"<rss/>"])
def test_invalid_feeds(payload):
    with pytest.raises(ParseError):
        parse_feed(payload, FEED)


def test_external_entities_are_not_resolved(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET")
    payload = f"""<?xml version="1.0"?>
<!DOCTYPE rss [<!ENTITY xxe SYSTEM "file://{secret}">]>
<rss><channel><item><title>Patch 1.0 &xxe;</title><link>https://x/1</link></item></channel></rss>"""
    try:
        items = parse_feed(payload.encode(), FEED)
    except ParseError:
        return
    assert all("TOP-SECRET" not in i.title for i in items)


def test_collector_uses_conditional_headers():
    http = FakeHttp({FEED: HttpResponse(FEED, 200, "", {"etag": '"2"'},
                                        content=_bytes("feed_rss.xml"))})
    source = SourceConfig(id=1, key="s", type="rss", params={"url": FEED}, etag='"1"')
    result = RssCollector().fetch(source, http)
    assert len(result.patches) == 3 and result.etag == '"2"'
    assert http.calls[0]["etag"] == '"1"'


def test_feeds_declared_in_page():
    html = """<html><head>
      <link rel="alternate" type="application/rss+xml" title="News" href="/news/rss">
      <link rel="alternate" type="text/html" href="/fr">
      <link rel="stylesheet" href="/a.css"></head></html>"""
    assert feeds_declared_in(html, "https://studio.example/") == [
        ("https://studio.example/news/rss", "News")]


def test_discover_feeds_validates_candidates():
    page = '<html><head><link rel="alternate" type="application/rss+xml" href="/news/rss"></head></html>'
    http = FakeHttp({
        "https://studio.example/": page,
        FEED: HttpResponse(FEED, 200, "", {}, content=_bytes("feed_rss.xml")),
        "https://studio.example/feed": "<html>pas un flux</html>",
        "https://studio.example/rss": FetchError("HTTP 404", status=404),
    })
    found = discover_feeds(http, "https://studio.example/")
    assert [f["url"] for f in found] == [FEED]
    assert found[0]["found_via"] == "application/rss+xml"
    assert found[0]["sample"][0] == "Patch 1.4.2 Notes"
