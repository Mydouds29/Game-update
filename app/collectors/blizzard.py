"""Collecteur Blizzard News.

Trois modes, choisis par le paramètre ``mode`` de la source :

* ``anchored_page`` : un article unique enrichi à chaque patch,
  avec un bloc repliable (``.panel``) ou un titre par version. La page est
  découpée par version et chaque version devient un patch distinct ;
  l'ingestion ne crée que les nouvelles.
* ``news_api`` : l'API qui alimente la page de news d'un jeu (``product``,
  ex. ``heroes-of-the-storm``), avec tout l'historique des articles ; seuls les
  articles titrés comme une note de patch (hors annonces « Highlights /
  Coming Soon ») sont téléchargés, sans la navigation écrite dans la page ;
* ``news_list`` : une page de liste d'articles ;
  chaque article dont le titre ressemble à une patch note est récupéré.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from ..parsing.classifier import (
    PATCH_TITLE, PREVIEW_TITLE, PTR_TITLE, looks_like_patch_title,
)
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
NEWS_API = "https://news.blizzard.com/{locale}/api/news/{product}"
NEWS_API_MORE = "https://news.blizzard.com/{locale}/api/feed/{product}"
PRODUCT_RE = re.compile(r"^[a-z0-9-]+$")
LOCALE_RE = re.compile(r"^[a-z]{2}-[a-z]{2}$")
JUMP_TO_SECTION_RE = re.compile(
    r"^\s*(jump\s+to\s+(section|topic)s?|quick\s+navigation)\s*:?\s*$", re.I)
DATE_PUBLISHED_RE = re.compile(r'"datePublished"\s*:\s*"([^"]+)"')


def _json_ld_date(soup: BeautifulSoup) -> str | None:
    """Date de publication des données structurées (schema.org) de la page.
    Celles de news.blizzard.com ne sont pas toujours du JSON valide : le champ
    est alors lu directement."""
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        text = script.string or script.get_text()
        try:
            data = json.loads(text)
        except ValueError:
            match = DATE_PUBLISHED_RE.search(text)
            if match:
                return match.group(1)
            continue
        for entry in data if isinstance(data, list) else [data]:
            if isinstance(entry, dict) and isinstance(entry.get("datePublished"), str):
                return entry["datePublished"]
    return None


def strip_navigation(container: Tag) -> None:
    """Retire la navigation écrite dans l'article, pas le contenu : paragraphes
    faits d'un seul lien (« Return to Top », « Click here to discuss… »),
    sommaire « Jump to Section » et ses listes de liens internes."""
    for heading in container.find_all(HEADING_TAGS + ("h5", "h6")):
        if JUMP_TO_SECTION_RE.match(heading.get_text(" ", strip=True)):
            heading.decompose()
    for block in container.find_all(["p", "ul", "ol"]):
        if block.decomposed:
            continue
        links = block.find_all("a")
        if not links:
            continue
        rest = block.get_text("", strip=True)
        for link in links:
            rest = rest.replace(link.get_text("", strip=True), "", 1)
        if rest.strip(" .:;,-–—|•·"):
            continue  # du texte en dehors des liens : c'est du contenu
        internal = all((a.get("href") or "").startswith("#") for a in links)
        if block.name == "p" or internal:
            block.decompose()


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


def _news_json(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ParseError(f"réponse de l'API de news illisible : {exc}") from exc
    if not isinstance(data, dict):
        raise ParseError("réponse de l'API de news inattendue")
    return data


def parse_article(html: str, url: str, *, content_selector: str | None = None,
                  fallback_title: str = "", navigation: bool = True) -> RawPatch:
    """Article de news. ``navigation=False`` retire les liens de navigation
    écrits dans l'article (voir strip_navigation)."""
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
        # Données structurées (schema.org) avant <time>, dont le texte affiché
        # n'est pas toujours une date lisible (news.blizzard.com).
        published_raw = _json_ld_date(soup)
        time_tag = soup.find("time")
        if published_raw is None and time_tag is not None:
            published_raw = time_tag.get("datetime") or time_tag.get_text(strip=True)

    container = find_content(soup, content_selector)
    if h1 is not None and any(p is container for p in h1.parents):
        h1.decompose()
    if not navigation:
        strip_navigation(container)
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
        elif mode == "news_api":
            if not PRODUCT_RE.match(require_param(params, "product")):
                raise CollectorConfigError("'product' doit ressembler à 'heroes-of-the-storm'")
            if not LOCALE_RE.match(params.get("locale", "en-us")):
                raise CollectorConfigError("'locale' doit ressembler à 'en-us'")
            for name, default, top in (("pages", 1, 100), ("max_articles", 5, 500)):
                value = params.get(name, default)
                if not isinstance(value, int) or not 1 <= value <= top:
                    raise CollectorConfigError(f"'{name}' doit être un entier entre 1 et {top}")
        else:
            raise CollectorConfigError(
                "'mode' doit valoir anchored_page, news_list ou news_api")

    def fetch(self, source: SourceConfig, http: HttpClient) -> FetchResult:
        self.validate_params(source.params)
        if source.params["mode"] == "anchored_page":
            return self._fetch_anchored(source, http)
        if source.params["mode"] == "news_api":
            return self._fetch_news_api(source, http)
        return self._fetch_news_list(source, http)

    def _fetch_news_api(self, source: SourceConfig, http: HttpClient) -> FetchResult:
        """Articles « Patch Notes » listés par l'API de news.blizzard.com (celle
        qui alimente la page de news du jeu, construite en JavaScript)."""
        p = source.params
        locale, product = p.get("locale", "en-us"), p["product"]
        first = http.get(NEWS_API.format(locale=locale, product=product),
                         accept="application/json", check_robots=True)
        data = _news_json(first.text)
        product_id = (data.get("context") or {}).get("cxpProductId")
        feed = data.get("feed") or {}
        items = list(feed.get("contentItems") or [])
        pagination = feed.get("pagination") or {}
        for _ in range(1, p.get("pages", 1)):
            if not pagination.get("hasNextPage"):
                break
            offset = int(pagination.get("offset", 0)) + int(pagination.get("limit", 24))
            try:
                more = http.get(NEWS_API_MORE.format(locale=locale, product=product),
                                params={"offset": offset, "feedCxpProductIds[]": product_id},
                                accept="application/json")
                page = _news_json(more.text)
            except (FetchError, ParseError) as exc:
                log.warning("blizzard.news_page_failed",
                            extra={"source": source.key, "offset": offset, "error": str(exc)})
                break
            items += page.get("contentItems") or []
            pagination = page.get("pagination") or {}

        patches: list[RawPatch] = []
        read = 0
        for item in items:
            props = item.get("properties") if isinstance(item, dict) else None
            if not isinstance(props, dict):
                continue
            # Le fil d'un jeu mélange des articles de la franchise (ex. Diablo IV
            # dans celui de D2R) : seuls ceux du jeu demandé comptent.
            if product_id and props.get("cxpProductId") != product_id:
                continue
            title, url = props.get("title") or "", props.get("newsUrl") or ""
            # Le PTR serait refusé à l'ingestion : inutile de le télécharger.
            if (not PATCH_TITLE.search(title) or PREVIEW_TITLE.search(title)
                    or PTR_TITLE.search(title)):
                continue
            if urlsplit(url).netloc != "news.blizzard.com" or f"url:{url}" in source.known_keys:
                continue
            if read >= p.get("max_articles", 5):
                break
            read += 1
            try:
                article = http.get(url)
                raw = parse_article(article.text, url, content_selector="section.blog",
                                    fallback_title=title, navigation=False)
            except (FetchError, ParseError) as exc:
                log.warning("blizzard.article_failed",
                            extra={"source": source.key, "url": url, "error": str(exc)})
                continue
            if raw.published_at is None:
                raw.published_raw = props.get("lastUpdated")
                raw.published_at = parse_date(raw.published_raw)
            raw.trusted_patch = True  # titre déjà filtré, article officiel
            patches.append(raw)
        return FetchResult(patches)

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
