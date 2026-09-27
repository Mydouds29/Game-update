"""Association type de source -> collecteur."""

from __future__ import annotations

from .base import Collector
from .blizzard import BlizzardCollector
from .discourse import DiscourseCollector
from .html_generic import HtmlGenericCollector
from .rss import RssCollector
from .steam import SteamCollector

_COLLECTORS: dict[str, Collector] = {
    c.type: c for c in (
        RssCollector(), SteamCollector(), BlizzardCollector(), HtmlGenericCollector(),
        DiscourseCollector())
}


def get_collector(source_type: str) -> Collector:
    try:
        return _COLLECTORS[source_type]
    except KeyError:
        raise KeyError(f"aucun collecteur pour le type {source_type!r}") from None


def source_types() -> list[str]:
    return sorted(_COLLECTORS)
