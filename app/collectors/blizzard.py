"""Collecteur Blizzard News.

Deux modes, choisis par le paramètre ``mode`` de la source :

* ``anchored_page`` : un article unique enrichi à chaque patch,
  avec un bloc repliable (``.panel``) ou un titre par version. La page est
  découpée par version et chaque version devient un patch distinct ;
  l'ingestion ne crée que les nouvelles.
* ``news_list`` : une page de liste d'articles ;
  chaque article dont le titre ressemble à une patch note est récupéré.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from ..parsing.classifier import looks_like_patch_title
from .base import (
    Collector, CollectorConfigError, FetchResult, ParseError, RawPatch,
    SourceConfig, require_param,
)
from .http import FetchError, HttpClient

log = logging.getLogger(__name__)

DEFAULT_CONTENT_SELECTORS = (
    "div.Content", "div.article-content", "div.blog-detail", "article", "main",
)
HEADING_TAGS = ("h1", "h2", "h3", "h4")

VERSION_RE = re.compile(r"(?<![\d.#])(\d+\.\d+(?:\.\d+){0,2})(?![\d.])")
BUILD_RE = re.compile(r"\bBuild\s*#?\s*(\d{3,})", re.I)
PLATFORMS_RE = re.compile(
    r"\(([^()]*\b(?:platforms?|pc|xbox|playstation|ps\d|switch|console)[^()]*)\)", re.I
)
DATE_RE = re.compile(
    r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}"
    r"|\d{4}-\d{2}-\d{2})\b"
)
PATCHY_WORDS_RE = re.compile(r"\b(patch|hotfix|update|build)\b", re.I)


def parse_date(text: str | None) -> datetime | None:
    if not text:
        return None
    text = text.strip().replace(".", "")
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_heading(text: str) -> dict[str, str | None]:
    """Extrait version, build, plateformes et date d'un titre de patch."""
    build = BUILD_RE.search(text)
    # Le numéro de build ne doit pas être pris pour une version.
    text_wo_build = BUILD_RE.sub(" ", text)
    version = VERSION_RE.search(text_wo_build)
    platforms = PLATFORMS_RE.search(text)
    date = DATE_RE.search(text)
    return {
        "version": version.group(1) if version else None,
        "build": build.group(1) if build else None,
        "platforms": platforms.group(1).strip() if platforms else None,
        "date": date.group(1) if date else None,
    }


def is_version_heading(tag: Tag) -> bool:
    text = tag.get_text(" ", strip=True)
    if not text or len(text) > 200:
        return False
    info = parse_heading(text)
    if info["build"]:
        return True
    return bool(info["version"] and (info["date"] or PATCHY_WORDS_RE.search(text)))


def find_content(soup: BeautifulSoup, selector: str | None) -> Tag:
    selectors = (selector,) if selector else DEFAULT_CONTENT_SELECTORS
    for sel in selectors:
        found = soup.select_one(sel)
        if found is not None:
            return found
    if soup.body is None:
        raise ParseError("document HTML sans <body>")
    return soup.body


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80]


def _chunk_between(start: Tag, stop: Tag | None, container: Tag) -> list[Tag]:
    """Éléments de premier niveau situés entre deux titres, dans l'ordre du document."""
    # Comparaisons par identité : l'égalité de Tag de bs4 est structurelle.
    included: list[Tag] = []
    # Les descendants du titre lui-même ne font pas partie du contenu.
    included_ids: set[int] = {id(start)}
    stop_ancestors = {id(p) for p in stop.parents} if stop is not None else set()
    for element in start.next_elements:
        if element is stop:
            break
        if not isinstance(element, Tag):
            continue
        parent_ids = {id(p) for p in element.parents}
        if id(container) not in parent_ids:
            break
        if parent_ids & included_ids:
            continue
        # Un conteneur qui englobe le titre suivant n'est pas pris en bloc :
        # on continue à descendre dans ses enfants.
        if id(element) in stop_ancestors:
            continue
        included.append(element)
        included_ids.add(id(element))
    return included


def _accordion_panels(container: Tag, page_url: str) -> list[RawPatch]:
    """Versions rangées dans des blocs repliables (page réelle de Diablo IV) :
    titre dans ``.panel-title``, contenu dans ``.panel-body``."""
    patches: list[RawPatch] = []
    for panel in container.select("div.panel"):
        title_el = panel.select_one(".panel-title")
        body_el = panel.select_one(".panel-body")
        if title_el is None or body_el is None or not is_version_heading(title_el):
            continue
        title = title_el.get_text(" ", strip=True)
        info = parse_heading(title)
        collapse = panel.select_one(".panel-collapse[id]")
        anchor = collapse.get("id") if collapse is not None else None
        if anchor:
            source_key = f"anchor:{anchor}"
        elif info["version"] or info["build"]:
            source_key = f"v:{info['version'] or ''}:b:{info['build'] or ''}"
        else:
            source_key = f"h:{_slug(title)}"
        patches.append(RawPatch(
            source_key=source_key,
            title=title,
            url=f"{page_url}#{anchor}" if anchor else page_url,
            body=body_el.decode_contents(),
            body_format="html",
            published_raw=info["date"],
            published_at=parse_date(info["date"]),
            version=info["version"],
            build=info["build"],
            platforms=info["platforms"],
            trusted_patch=True,
        ))
    return patches


def split_anchored_page(html: str, page_url: str, *,
                        content_selector: str | None = None,
                        max_versions: int = 15) -> list[RawPatch]:
    soup = BeautifulSoup(html, "lxml")
    container = find_content(soup, content_selector)
    panels = _accordion_panels(container, page_url)
    if panels:
        return panels[:max_versions]
    markers = [h for h in container.find_all(HEADING_TAGS) if is_version_heading(h)]
    if not markers:
        raise ParseError("aucun titre de version trouvé dans la page")

    patches: list[RawPatch] = []
    for index, heading in enumerate(markers[:max_versions]):
        stop = markers[index + 1] if index + 1 < len(markers) else None
        chunk = _chunk_between(heading, stop, container)
        title = heading.get_text(" ", strip=True)
        info = parse_heading(title)

        date_text = info["date"]
        if not date_text:
            # Certaines versions affichent la date dans le paragraphe suivant.
            for element in chunk[:2]:
                match = DATE_RE.search(element.get_text(" ", strip=True))
                if match:
                    date_text = match.group(1)
                    break

        anchor = heading.get("id") or (heading.find("a", id=True) or {}).get("id")
        if anchor:
            source_key = f"anchor:{anchor}"
        elif info["version"] or info["build"]:
            source_key = f"v:{info['version'] or ''}:b:{info['build'] or ''}"
        else:
            source_key = f"h:{_slug(title)}"
        url = f"{page_url}#{anchor}" if anchor else page_url

        patches.append(RawPatch(
            source_key=source_key,
            title=title,
            url=url,
            body="".join(str(el) for el in chunk),
            body_format="html",
            published_raw=date_text,
            published_at=parse_date(date_text),
            version=info["version"],
            build=info["build"],
            platforms=info["platforms"],
            trusted_patch=True,
        ))
    return patches


def extract_article_links(html: str, list_url: str, pattern: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    regex = re.compile(pattern)
    host = urlsplit(list_url).netloc
    seen: set[str] = set()
    links: list[tuple[str, str]] = []
    for anchor in soup.find_all("a", href=True):
        url = urljoin(list_url, anchor["href"]).split("#", 1)[0]
        if urlsplit(url).netloc != host or not regex.search(url) or url in seen:
            continue
        title = anchor.get_text(" ", strip=True) or anchor.get("aria-label", "")
        if not title:
            continue
        seen.add(url)
        links.append((url, title))
    return links


def parse_article(html: str, url: str, *, content_selector: str | None = None,
                  fallback_title: str = "") -> RawPatch:
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else fallback_title
    if not title:
        raise ParseError(f"article sans titre : {url}")

    published_raw = None
    meta = soup.find("meta", attrs={"property": "article:published_time"})
    if meta and meta.get("content"):
        published_raw = meta["content"]
    else:
        time_tag = soup.find("time")
        if time_tag is not None:
            published_raw = time_tag.get("datetime") or time_tag.get_text(strip=True)

    container = find_content(soup, content_selector)
    if h1 is not None and any(p is container for p in h1.parents):
        h1.decompose()
    info = parse_heading(title)
    return RawPatch(
        source_key=f"url:{url}",
        title=title,
        url=url,
        body=container.decode_contents(),
        body_format="html",
        published_raw=published_raw,
        published_at=parse_date(published_raw),
        version=info["version"],
        build=info["build"],
        platforms=info["platforms"],
    )


class BlizzardCollector(Collector):
    type = "blizzard"

    def validate_params(self, params: dict[str, Any]) -> None:
        mode = params.get("mode")
        if mode == "anchored_page":
            require_param(params, "url")
        elif mode == "news_list":
            require_param(params, "list_url")
            try:
                re.compile(params.get("article_url_pattern", r"/article/\d+"))
            except re.error as exc:
                raise CollectorConfigError(f"article_url_pattern invalide : {exc}") from exc
        else:
            raise CollectorConfigError("'mode' doit valoir anchored_page ou news_list")

    def fetch(self, source: SourceConfig, http: HttpClient) -> FetchResult:
        self.validate_params(source.params)
        if source.params["mode"] == "anchored_page":
            return self._fetch_anchored(source, http)
        return self._fetch_news_list(source, http)

    def _fetch_anchored(self, source: SourceConfig, http: HttpClient) -> FetchResult:
        url = source.params["url"]
        resp = http.get(url, etag=source.etag, last_modified=source.last_modified)
        if resp.not_modified:
            return FetchResult([], not_modified=True, etag=source.etag,
                               last_modified=source.last_modified)
        patches = split_anchored_page(
            resp.text, url,
            content_selector=source.params.get("content_selector"),
            max_versions=int(source.params.get("max_versions", 15)),
        )
        return FetchResult(patches, etag=resp.etag, last_modified=resp.last_modified)

    def _fetch_news_list(self, source: SourceConfig, http: HttpClient) -> FetchResult:
        list_url = source.params["list_url"]
        resp = http.get(list_url, etag=source.etag, last_modified=source.last_modified)
        if resp.not_modified:
            return FetchResult([], not_modified=True, etag=source.etag,
                               last_modified=source.last_modified)
        links = extract_article_links(
            resp.text, list_url, source.params.get("article_url_pattern", r"/article/\d+"),
        )
        max_articles = int(source.params.get("max_articles", 5))
        patches: list[RawPatch] = []
        for url, title in links:
            if len(patches) >= max_articles:
                break
            if f"url:{url}" in source.known_keys:
                continue
            if not looks_like_patch_title(title):
                continue
            try:
                article = http.get(url)
                patches.append(parse_article(
                    article.text, url,
                    content_selector=source.params.get("content_selector"),
                    fallback_title=title,
                ))
            except (FetchError, ParseError) as exc:
                # Un article en échec ne doit pas bloquer les autres.
                log.warning("blizzard.article_failed",
                            extra={"source": source.key, "url": url, "error": str(exc)})
        return FetchResult(patches, etag=resp.etag, last_modified=resp.last_modified)
