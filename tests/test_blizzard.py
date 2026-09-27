import pytest

from app.collectors.base import CollectorConfigError, ParseError, SourceConfig
from app.collectors.blizzard import (
    BlizzardCollector, parse_heading, split_anchored_page,
)
from app.parsing.normalizer import normalize

from .conftest import FakeHttp, fixture_text

D4_URL = "https://news.blizzard.com/en-us/article/1/diablo-iv-patch-notes"


def test_parse_heading():
    info = parse_heading("3.2.2 Build #73764 (All Platforms) - September 30, 2026")
    assert info == {"version": "3.2.2", "build": "73764",
                    "platforms": "All Platforms", "date": "September 30, 2026"}


def test_parse_heading_does_not_mistake_build_for_version():
    info = parse_heading("Hotfix Build #71234")
    assert info["version"] is None and info["build"] == "71234"


def test_split_d4_page_by_version():
    patches = split_anchored_page(fixture_text("blizzard_d4_patch_notes.html"), D4_URL)
    assert [p.version for p in patches] == ["3.2.2", "3.2.1", "3.2.0"]
    assert [p.source_key for p in patches] == ["anchor:3.2.2", "anchor:3.2.1", "anchor:3.2.0"]
    assert patches[0].url == D4_URL + "#3.2.2"
    assert patches[0].build == "73764"
    assert patches[0].platforms == "All Platforms"
    assert all(p.trusted_patch for p in patches)
    # Date conservée telle quelle, et parsée.
    assert patches[0].published_raw == "September 30, 2026"
    assert patches[0].published_at.isoformat() == "2026-09-30T00:00:00+00:00"
    # Date dans le paragraphe suivant le titre.
    assert patches[2].published_raw == "August 12, 2026"


def test_chunks_do_not_leak_between_versions():
    patches = split_anchored_page(fixture_text("blizzard_d4_patch_notes.html"), D4_URL)
    assert "Helltide" not in patches[0].body
    assert "Pulverize" not in patches[1].body
    assert "Pinned currency" in patches[2].body


def test_normalize_d4_version_sections_and_subgroups():
    raw = split_anchored_page(fixture_text("blizzard_d4_patch_notes.html"), D4_URL)[0]
    patch = normalize(raw)
    assert [s.title for s in patch.sections] == ["Season", "Classes", "Miscellaneous"]
    classes = patch.sections[1].items
    assert [(i.subgroup, i.kind) for i in classes] == [
        ("Druid", "fix"), ("Sorcerer", "fix"), ("Sorcerer", "balance")]
    assert patch.item_count == 6


def test_normalize_bold_paragraph_and_developer_note():
    raw = split_anchored_page(fixture_text("blizzard_d4_patch_notes.html"), D4_URL)[1]
    patch = normalize(raw)
    assert patch.sections[0].title == "Game Updates"
    kinds = [i.kind for i in patch.sections[0].items]
    assert kinds == ["new", "fix", "note"]


def test_page_without_versions_is_a_parse_error():
    with pytest.raises(ParseError):
        split_anchored_page("<html><body><h2>Hello</h2></body></html>", D4_URL)


def test_news_list_fetches_only_new_patch_articles():
    list_url = "https://news.blizzard.com/en-us/diablo2"
    art1 = "https://news.blizzard.com/en-us/article/24300001/diablo-ii-resurrected-patch-3-1-2"
    art2 = "https://news.blizzard.com/en-us/article/24299999/diablo-ii-resurrected-hotfix-3-1-1"
    http = FakeHttp({list_url: fixture_text("blizzard_d2r_list.html"),
                     art1: fixture_text("blizzard_d2r_article.html")})
    source = SourceConfig(
        id=1, key="d2r", type="blizzard",
        params={"mode": "news_list", "list_url": list_url},
        known_keys=frozenset({f"url:{art2}"}),
    )
    result = BlizzardCollector().fetch(source, http)
    fetched = [c["url"] for c in http.calls]
    # L'événement et le lien externe sont écartés ; l'article connu n'est pas retéléchargé.
    assert fetched == [list_url, art1]
    assert len(result.patches) == 1
    patch = normalize(result.patches[0])
    assert patch.version == "3.1.2"
    assert patch.published_at.isoformat() == "2026-09-10T17:00:00+00:00"
    assert [s.title for s in patch.sections] == ["", "Bug Fixes"]


def test_news_list_article_failure_does_not_block_others():
    list_url = "https://news.blizzard.com/en-us/diablo2"
    art2 = "https://news.blizzard.com/en-us/article/24299999/diablo-ii-resurrected-hotfix-3-1-1"
    http = FakeHttp({list_url: fixture_text("blizzard_d2r_list.html"),
                     art2: fixture_text("blizzard_d2r_article.html")})
    source = SourceConfig(id=1, key="d2r", type="blizzard",
                          params={"mode": "news_list", "list_url": list_url})
    result = BlizzardCollector().fetch(source, http)
    assert len(result.patches) == 1


def test_accordion_page_one_patch_per_panel():
    # Format réel de la page Diablo IV : versions dans des blocs repliables, sans titre h1-h4.
    patches = split_anchored_page(fixture_text("blizzard_d4_accordion.html"), D4_URL)
    assert [p.version for p in patches] == ["9.1.1", "9.1.0"]
    first = patches[0]
    assert first.build == "90002" and first.platforms == "All Platforms"
    assert first.published_at.date().isoformat() == "2026-10-02"
    assert first.source_key == "anchor:9.1.1" and first.url == D4_URL + "#9.1.1"
    patch = normalize(first)
    texts = [it.text for s in patch.sections for it in s.items]
    assert len(texts) == 4  # tout le contenu du bloc, rien de plus
    assert "Fixed an issue where the stash could not be opened." not in texts
    druid = [it for s in patch.sections for it in s.items if it.subgroup == "Druid"]
    assert len(druid) == 1


NEWS = "https://news.blizzard.com/en-us/api/news/heroes-of-the-storm"
NEWS_MORE = "https://news.blizzard.com/en-us/api/feed/heroes-of-the-storm"
ARTICLE = ("https://news.blizzard.com/en-us/article/900/"
           "heroes-of-the-storm-live-patch-notes-october-5-2026")
OLD_ARTICLE = ("https://news.blizzard.com/en-us/article/100/"
               "heroes-of-the-storm-hotfix-notes-march-9-2018")


def _news_http():
    return FakeHttp({
        NEWS: fixture_text("blizzard_news_api.json"),
        NEWS_MORE: fixture_text("blizzard_news_api_page2.json"),
        ARTICLE: fixture_text("blizzard_news_article.html"),
        OLD_ARTICLE: fixture_text("blizzard_news_article.html").replace(
            "2026-10-05T17:35:15Z", "2018-03-09T12:00:00Z"),
    })


def _news_source(**params):
    return SourceConfig(id=1, key="hots-news", type="blizzard",
                        params={"mode": "news_api", "product": "heroes-of-the-storm", **params})


def test_news_api_keeps_only_patch_notes_of_the_game():
    http = _news_http()
    result = BlizzardCollector().fetch(_news_source(), http)
    assert [p.url for p in result.patches] == [ARTICLE]
    fetched = [c["url"] for c in http.calls]
    # Ni le PTR, ni l'annonce « Highlights », ni l'événement, ni l'article d'un autre jeu,
    # ni une adresse hors de news.blizzard.com.
    assert not any(u.endswith(("/ptr", "/highlights", "/event", "/other", "/fake"))
                   for u in fetched)
    assert NEWS_MORE not in fetched  # une seule page par défaut


def test_news_api_article_is_complete_without_navigation():
    patch = BlizzardCollector().fetch(_news_source(), _news_http()).patches[0]
    # Données structurées en JSON invalide : la date est lue quand même.
    assert patch.published_at.isoformat() == "2026-10-05T17:35:15+00:00"
    assert patch.trusted_patch
    normalized = normalize(patch)
    texts = [it.text for s in normalized.sections for it in s.items]
    titles = [s.title for s in normalized.sections]
    for nav in ("Return to Top", "Quick Navigation", "Click here to discuss",
                "Blizzard Entertainment"):
        assert not any(nav in t for t in texts + titles), nav
    assert "Our next patch is live! Read on for more information." in texts
    assert "The ranked season has started." in texts
    # Un lien au milieu d'une phrase fait partie du contenu : gardé.
    assert any(t.startswith("Health increased from 1800 to 1850, as described in the notes")
               for t in texts)


def test_news_api_keeps_every_heading_level():
    # Balance Update > Support > Alexstrasza > Base > Dragonqueen [D] : le nom du
    # héros ne doit pas être écrasé par les niveaux inférieurs.
    normalized = normalize(BlizzardCollector().fetch(_news_source(), _news_http()).patches[0])
    balance = [s for s in normalized.sections if s.title == "Balance Update"][0]
    groups = {it.text[:20]: it.subgroup for it in balance.items}
    assert groups["Cooldown reduced fro"] == "Support › Alexstrasza › Base › Dragonqueen [D]"
    assert groups["Live and Let Live no"] == "Support › Alexstrasza › Talents › Level 1"
    assert groups["Health increased fro"] == "Support › Brightwing › Base"


def test_news_api_history_pages():
    http = _news_http()
    result = BlizzardCollector().fetch(_news_source(pages=3), http)
    assert [p.url for p in result.patches] == [ARTICLE, OLD_ARTICLE]
    more = [c for c in http.calls if c["url"] == NEWS_MORE]
    assert [c["params"] for c in more] == [{"offset": 24, "feedCxpProductIds[]": "prod-hots"}]
    assert result.patches[1].published_at.year == 2018


def test_news_api_skips_known_articles():
    http = _news_http()
    source = _news_source()
    source.known_keys = frozenset({f"url:{ARTICLE}"})
    assert BlizzardCollector().fetch(source, http).patches == []
    assert ARTICLE not in [c["url"] for c in http.calls]


@pytest.mark.parametrize("params", [
    {"mode": "news_api"},
    {"mode": "news_api", "product": "../x"},
    {"mode": "news_api", "product": "hots", "locale": "fr"},
    {"mode": "news_api", "product": "hots", "pages": 0},
    {"mode": "news_api", "product": "hots", "max_articles": 1000},
])
def test_news_api_invalid_params(params):
    with pytest.raises(CollectorConfigError):
        BlizzardCollector().validate_params(params)


def test_empty_nested_list_does_not_swallow_lines():
    # Vu sur les notes Heroes du 14 mars 2017 : une sous-liste vide après la
    # dernière ligne faisait disparaître cette ligne et son titre (« Silver City »).
    from app.parsing.normalizer import extract_sections
    html = ("<h2>Brawl</h2><ul>"
            "<li><strong>Mage Wars</strong><ul><li>Quest reduced to 2 games</li></ul></li>"
            "<li><strong>Silver City</strong><ul>"
            "<li>Now uses Shuffle Pick for hero selection instead of All Random<ul></ul></li>"
            "</ul></li></ul>")
    items = [(i.subgroup, i.text) for s in extract_sections(html) for i in s.items]
    assert items == [
        ("Mage Wars", "Quest reduced to 2 games"),
        ("Silver City", "Now uses Shuffle Pick for hero selection instead of All Random"),
    ]


def test_label_announcing_nothing_is_kept_as_a_line():
    from app.parsing.normalizer import extract_sections
    html = "<ul><li>Short label<ul><li></li></ul></li><li>Other line.</li></ul>"
    texts = [i.text for s in extract_sections(html) for i in s.items]
    assert texts == ["Short label", "Other line."]
