import pytest

from app.collectors.base import ParseError, SourceConfig
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
    assert [s.title for s in patch.sections] == ["General", "Bug Fixes"]


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
