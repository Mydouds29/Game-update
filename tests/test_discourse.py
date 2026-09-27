import pytest

from app.collectors.base import CollectorConfigError, SourceConfig
from app.collectors.discourse import DiscourseCollector
from app.parsing.normalizer import normalize

from .conftest import FakeHttp, fixture_text

FORUM = "https://us.forums.blizzard.com/en/d4"
LATEST = f"{FORUM}/c/pc-general-discussion/5/l/latest.json"
PARAMS = {"forum_url": FORUM, "category": "pc-general-discussion/5"}


def _http():
    return FakeHttp({
        LATEST: fixture_text("discourse_latest.json"),
        f"{FORUM}/t/900.json": fixture_text("discourse_topic_staff.json"),
        f"{FORUM}/t/901.json": fixture_text("discourse_topic_player.json"),
    })


def test_keeps_staff_hotfix_topics_only():
    http = _http()
    result = DiscourseCollector().fetch(
        SourceConfig(id=1, key="d4-forum", type="discourse", params=PARAMS), http)
    # 899 n'a pas de réponse dans FakeHttp (404) : ignoré sans bloquer les autres.
    assert [p.source_key for p in result.patches] == ["topic:900"]
    fetched = [c["url"] for c in http.calls]
    assert f"{FORUM}/t/902.json" not in fetched  # titre sans rapport : pas téléchargé
    patch = result.patches[0]
    assert patch.version == "9.1.1" and patch.trusted_patch
    assert patch.url == f"{FORUM}/t/hotfix-2-october-3-2026-911/900"
    assert patch.published_at.isoformat() == "2026-10-03T19:00:00+00:00"


def test_hotfix_content_is_complete():
    result = DiscourseCollector().fetch(
        SourceConfig(id=1, key="d4-forum", type="discourse", params=PARAMS), _http())
    texts = [it.text for s in normalize(result.patches[0]).sections for it in s.items]
    assert "Fixed an issue where a portal would fail to open." in texts
    assert "Fixed an issue where a boss could be spawned infinitely." in texts
    assert any("cooldown for this reward has been increased" in t for t in texts)
    assert not any("Thanks" in t for t in texts)  # les réponses ne font pas partie du patch


def test_known_topics_are_not_refetched():
    http = _http()
    source = SourceConfig(id=1, key="d4-forum", type="discourse", params=PARAMS,
                          known_keys=frozenset({"topic:900"}))
    assert DiscourseCollector().fetch(source, http).patches == []
    assert f"{FORUM}/t/900.json" not in [c["url"] for c in http.calls]


@pytest.mark.parametrize("params", [
    {"category": "pc-general-discussion/5"},
    {"forum_url": "http://forum.example", "category": "x/1"},
    {"forum_url": FORUM + "/", "category": "x/1"},
    {"forum_url": FORUM, "category": "../admin"},
    {**PARAMS, "title_pattern": "("},
    {**PARAMS, "max_topics": 50},
])
def test_invalid_params(params):
    with pytest.raises(CollectorConfigError):
        DiscourseCollector().validate_params(params)


def test_ptr_topics_are_skipped():
    latest = ('{"topic_list": {"topics": ['
              '{"id": 950, "title": "PTR Patch Notes - 9.2.0", "created_at": "2026-10-05T10:00:00Z"},'
              '{"id": 951, "title": "Public Test Realm Hotfix 1", "created_at": "2026-10-06T10:00:00Z"}]}}')
    http = FakeHttp({LATEST: latest})
    result = DiscourseCollector().fetch(
        SourceConfig(id=1, key="d4-forum", type="discourse", params=PARAMS), http)
    assert result.patches == []
    assert [c["url"] for c in http.calls] == [LATEST]  # aucun sujet PTR téléchargé
