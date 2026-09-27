"""Collecteur RSS 2.0 / Atom : source à privilégier quand l'éditeur en publie un,
car un flux est explicitement prévu pour être repris par des programmes.

Contient aussi la découverte de flux à partir d'une page web.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from lxml import etree

from .base import (
    Collector, CollectorConfigError, FetchResult, ParseError, RawPatch,
    SourceConfig, require_param,
)
from .blizzard import parse_date, parse_heading
from .http import FetchError, HttpClient

log = logging.getLogger(__name__)

FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/feed+json",
              "application/rdf+xml")
COMMON_FEED_PATHS = ("/feed", "/rss", "/rss.xml", "/feed.xml", "/atom.xml",
                     "/index.xml", "/news/rss", "/news/feed")

ATOM = "{http://www.w3.org/2005/Atom}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"
ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8"


def _parser() -> etree.XMLParser:
    # Pas d'entités externes, pas de DTD, pas de réseau : évite XXE et « billion laughs ».
    return etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False,
                           huge_tree=False, recover=False)


def _text(el: etree._Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def _rss_date(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return parse_date(raw)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_feed(payload: str | bytes, feed_url: str, max_items: int = 30) -> list[RawPatch]:
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    try:
        root = etree.fromstring(data, parser=_parser())
    except etree.XMLSyntaxError as exc:
        raise ParseError(f"flux XML invalide : {exc}") from exc

    patches: list[RawPatch] = []
    if root.tag == f"{ATOM}feed":
        entries = root.findall(f"{ATOM}entry")
        for entry in entries[:max_items]:
            link = ""
            for el in entry.findall(f"{ATOM}link"):
                if el.get("rel", "alternate") == "alternate" and el.get("href"):
                    link = urljoin(feed_url, el.get("href"))
                    break
            body_el = entry.find(f"{ATOM}content")
            if body_el is None:
                body_el = entry.find(f"{ATOM}summary")
            date_raw = _text(entry.find(f"{ATOM}published")) or _text(entry.find(f"{ATOM}updated"))
            patches.append(_make(
                guid=_text(entry.find(f"{ATOM}id")) or link,
                title=_text(entry.find(f"{ATOM}title")), link=link or feed_url,
                body=_text(body_el), date_raw=date_raw, date=parse_date(date_raw),
                tags=[c.get("term", "") for c in entry.findall(f"{ATOM}category")],
            ))
    elif root.tag == "rss" or root.find("channel") is not None:
        channel = root.find("channel")
        if channel is None:
            raise ParseError("flux RSS sans <channel>")
        for item in channel.findall("item")[:max_items]:
            link = urljoin(feed_url, _text(item.find("link")))
            date_raw = _text(item.find("pubDate"))
            patches.append(_make(
                guid=_text(item.find("guid")) or link,
                title=_text(item.find("title")), link=link or feed_url,
                body=_text(item.find(f"{CONTENT}encoded")) or _text(item.find("description")),
                date_raw=date_raw, date=_rss_date(date_raw),
                tags=[_text(c) for c in item.findall("category")],
            ))
    else:
        raise ParseError(f"format de flux non reconnu (racine <{root.tag}>)")
    return [p for p in patches if p.title and p.source_key != "guid:"]


def _make(*, guid: str, title: str, link: str, body: str, date_raw: str,
          date: datetime | None, tags: list[str]) -> RawPatch:
    info = parse_heading(title)
    return RawPatch(
        source_key=f"guid:{guid}", title=title, url=link, body=body, body_format="html",
        published_raw=date_raw or None, published_at=date,
        tags=[t for t in tags if t], version=info["version"], build=info["build"],
        platforms=info["platforms"],
    )


class RssCollector(Collector):
    type = "rss"

    def validate_params(self, params: dict[str, Any]) -> None:
        url = require_param(params, "url")
        if urlsplit(url).scheme not in {"http", "https"}:
            raise CollectorConfigError("'url' doit être une URL http(s)")
        max_items = params.get("max_items", 30)
        if not isinstance(max_items, int) or not 1 <= max_items <= 200:
            raise CollectorConfigError("'max_items' doit être un entier entre 1 et 200")

    def fetch(self, source: SourceConfig, http: HttpClient) -> FetchResult:
        self.validate_params(source.params)
        url = source.params["url"]
        resp = http.get(url, etag=source.etag, last_modified=source.last_modified,
                        accept=ACCEPT)
        if resp.not_modified:
            return FetchResult([], not_modified=True, etag=source.etag,
                               last_modified=source.last_modified)
        return FetchResult(parse_feed(resp.content or resp.text, url,
                                      source.params.get("max_items", 30)),
                           etag=resp.etag, last_modified=resp.last_modified)


# -- découverte -------------------------------------------------------------

def feeds_declared_in(html: str, page_url: str) -> list[tuple[str, str]]:
    """Flux annoncés par <link rel="alternate" type="application/rss+xml">."""
    soup = BeautifulSoup(html, "lxml")
    found = []
    for link in soup.find_all("link", href=True):
        rel = [r.lower() for r in (link.get("rel") or [])]
        kind = (link.get("type") or "").lower()
        if "alternate" in rel and kind in FEED_TYPES:
            found.append((urljoin(page_url, link["href"]), link.get("title") or kind))
    return found


def discover_feeds(http: HttpClient, page_url: str) -> list[dict[str, Any]]:
    """Cherche les flux d'un site : balises <link> de la page, puis chemins usuels.
    Chaque candidat est téléchargé et analysé pour confirmer qu'il est valide."""
    candidates: list[tuple[str, str]] = []
    try:
        page = http.get(page_url)
        candidates += feeds_declared_in(page.text, page.url)
    except FetchError as exc:
        log.warning("rss.discover_page_failed", extra={"url": page_url, "error": str(exc)})
    parts = urlsplit(page_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    candidates += [(origin + path, "chemin usuel") for path in COMMON_FEED_PATHS]

    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for url, origin_label in candidates:
        if url in seen:
            continue
        seen.add(url)
        try:
            resp = http.get(url, accept=ACCEPT)
            items = parse_feed(resp.content or resp.text, url, max_items=10)
        except (FetchError, ParseError):
            continue
        results.append({"url": url, "found_via": origin_label, "items": len(items),
                        "sample": [i.title for i in items[:3]]})
    return results
