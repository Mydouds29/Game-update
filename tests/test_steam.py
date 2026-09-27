import pytest

from app.collectors.base import ParseError, SourceConfig
from app.collectors.steam import NEWS_URL, SteamCollector, parse_news
from app.parsing.classifier import classify
from app.parsing.normalizer import normalize

from .conftest import FakeHttp, fixture_text


def test_parse_news_keeps_only_official_feed():
    patches = parse_news(fixture_text("steam_palworld.json"), 1623730)
    assert [p.source_key for p in patches] == [
        "5123456789012345601", "5123456789012345602",
        "5123456789012345603", "5123456789012345604",
    ]
    first = patches[0]
    assert first.title == "Palworld v0.6.8 Patch Notes"
    assert first.body_format == "bbcode"
    assert first.tags == ["patchnotes"]
    assert first.published_at.year == 2026


def test_parse_news_ignores_other_appid():
    assert parse_news(fixture_text("steam_palworld.json"), 999) == []


@pytest.mark.parametrize("payload", ["not json", "{}", '{"appnews": {"newsitems": 3}}'])
def test_parse_news_rejects_bad_payload(payload):
    with pytest.raises(ParseError):
        parse_news(payload, 1)


def test_classifier_on_steam_fixture():
    patches = parse_news(fixture_text("steam_palworld.json"), 1623730)
    verdicts = {p.title: classify(p.title, p.tags, p.body).is_patch for p in patches}
    assert verdicts == {
        "Palworld v0.6.8 Patch Notes": True,
        "Hotfix v0.6.7.1 is now live": True,
        "Palworld is 40% OFF during the Autumn Sale!": False,
        "Pal Fest Event: Halloween Costume Contest": False,
    }


def test_normalize_steam_bbcode():
    raw = parse_news(fixture_text("steam_palworld.json"), 1623730)[0]
    patch = normalize(raw)
    assert patch.version == "0.6.8"
    titles = [s.title for s in patch.sections]
    assert titles == ["", "Balance adjustments", "Bug fixes", "Known issues"]
    bugfix = patch.sections[2]
    assert [(i.subgroup, i.kind) for i in bugfix.items] == [
        (None, "fix"), (None, "fix"), ("Multiplayer", "fix")]
    assert all(i.kind == "balance" for i in patch.sections[1].items)
    # Le texte entre crochets qui n'est pas une balise BBCode est conservé.
    assert "[PC]" in patch.sections[3].items[0].text
    # Les images sont retirées.
    assert not any("STEAM_CLAN_IMAGE" in i.text for s in patch.sections for i in s.items)
    # Les phrases d'introduction sont des notes, exclues du compteur.
    assert patch.item_count == 6


def test_normalize_plain_bullets_with_bold_label():
    raw = parse_news(fixture_text("steam_palworld.json"), 1623730)[1]
    patch = normalize(raw)
    assert patch.version == "0.6.7.1"
    assert len(patch.sections) == 1
    assert patch.sections[0].title == "Fixes"
    assert [i.kind for i in patch.sections[0].items] == ["fix", "fix"]


def test_collector_requests_official_feed_with_conditional_headers():
    http = FakeHttp({NEWS_URL: fixture_text("steam_palworld.json")})
    source = SourceConfig(id=1, key="palworld-steam", type="steam",
                          params={"appid": 1623730}, etag='"old"')
    result = SteamCollector().fetch(source, http)
    assert len(result.patches) == 4
    assert result.etag == '"v1"'
    call = http.calls[0]
    assert call["params"]["feeds"] == "steam_community_announcements"
    assert call["params"]["maxlength"] == 0
    assert call["etag"] == '"old"'
