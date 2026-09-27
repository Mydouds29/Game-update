from app.collectors.base import SourceConfig
from app.collectors.html_generic import HtmlGenericCollector, dig
from app.parsing.normalizer import normalize

from .conftest import FakeHttp, fixture_text

LIST_URL = "https://game.example.com/api/news"


def test_dig():
    data = {"a": {"b": [{"c": 1}]}}
    assert dig(data, "a.b.0.c") == 1
    assert dig(data, "a.x") is None
    assert dig(data, "a.b.5") is None


def test_json_list_mode():
    http = FakeHttp({LIST_URL: fixture_text("generic_news.json")})
    source = SourceConfig(id=1, key="g", type="html", params={
        "mode": "json_list", "list_url": LIST_URL, "items_path": "data.list",
        "id_field": "id", "title_field": "title", "date_field": "publishTime",
        "content_field": "content", "url_template": "https://game.example.com/news/{id}",
    })
    result = HtmlGenericCollector().fetch(source, http)
    assert [p.title for p in result.patches] == ["Version 2.7 Update Notice"]
    raw = result.patches[0]
    assert raw.url == "https://game.example.com/news/901"
    assert raw.published_at.year == 2026  # horodatage en millisecondes
    patch = normalize(raw)
    assert patch.version == "2.7"
    assert [s.title for s in patch.sections] == ["New Content", "Optimizations"]


def test_html_list_mode():
    list_url = "https://game.example.com/news"
    html = """<ul><li class="n"><a href="/news/1">Patch 1.2 Notes</a><time datetime="2026-09-01">x</time></li>
              <li class="n"><a href="/news/2">Summer Festival Event</a></li></ul>"""
    article = "<html><body><article><h3>Fixes</h3><ul><li>Fixed a crash.</li></ul></article></body></html>"
    http = FakeHttp({list_url: html, "https://game.example.com/news/1": article})
    source = SourceConfig(id=1, key="g", type="html", params={
        "mode": "html_list", "list_url": list_url, "item_selector": "li.n",
        "date_selector": "time", "date_attr": "datetime",
    })
    result = HtmlGenericCollector().fetch(source, http)
    assert len(result.patches) == 1
    assert result.patches[0].published_at.isoformat().startswith("2026-09-01")
    assert normalize(result.patches[0]).sections[0].items[0].kind == "fix"
