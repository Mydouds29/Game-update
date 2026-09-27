import pytest

from app.parsing.classifier import classify


@pytest.mark.parametrize("title", [
    "Patch Notes 1.4.2",
    "Hotfix 3",
    "Update 1.3 is now live",
    "v2.0.4 Patch",
    "Version 2.7 Update Notice",
    "Bug fixes and improvements",
    "Patch 5.1 – Balance changes",
])
def test_patch_titles(title):
    assert classify(title).is_patch, classify(title)


@pytest.mark.parametrize("title", [
    "Summer Sale: 50% off!",
    "Free Weekend starts now",
    "Twitch Drops are back",
    "Season 5 Launch Trailer",
    "Community Spotlight: fan art",
    "Developer Update: our roadmap for 2027",
    "Version 2.7 Launch Event: Login Rewards",
    "Join our livestream tomorrow",
])
def test_promo_titles(title):
    assert not classify(title).is_patch, classify(title)


def test_steam_patchnotes_tag_wins():
    assert classify("Autumn Update Event", tags=["patchnotes"]).is_patch
