"""Collecteur générique pour les sites officiels, piloté par la configuration.

Mode ``html_list`` : une page liste des articles, repérés par sélecteurs CSS.
Mode ``json_list`` : les sites rendus en JavaScript exposent souvent une API
JSON de news ; on y lit les champs indiqués par des chemins pointés.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..parsing.classifier import looks_like_patch_title
from .base import (
    Collector, CollectorConfigError, FetchResult, ParseError, RawPatch,
    SourceConfig, require_param,
)
from .blizzard import find_content, parse_date, parse_heading
from .http import FetchError, HttpClient

log = logging.getLogger(__name__)


def dig(data: Any, path: str) -> Any:
    """Lit ``a.b.0.c`` dans une structure JSON ; None si absent."""
    current = data
    for part in path.split(".") if path else []:
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
        if current is None:
            return None
    return current


class HtmlGenericCollector(Collector):
    type = "html"

    def validate_params(self, params: dict[str, Any]) -> None:
        mode = params.get("mode", "html_list")
        require_param(params, "list_url")
        if mode == "html_list":
            require_param(params, "item_selector")
        elif mode == "json_list":
            for name in ("items_path", "id_field", "title_field"):
                require_param(params, name)
            if not params.get("url_template") and not params.get("url_field"):
                raise CollectorConfigError("url_template ou url_field requis")
        else:
            raise CollectorConfigError("'mode' doit valoir html_list ou json_list")

    def fetch(self, source: SourceConfig, http: HttpClient) -> FetchResult:
        self.validate_params(source.params)
        params = source.params
        resp = http.get(params["list_url"], etag=source.etag,
                        last_modified=source.last_modified)
        if resp.not_modified:
            return FetchResult([], not_modified=True, etag=source.etag,
                               last_modified=source.last_modified)
        if params.get("mode", "html_list") == "json_list":
            patches = self._from_json(resp.text, source, http)
        else:
            patches = self._from_html(resp.text, source, http)
        return FetchResult(patches, etag=resp.etag, last_modified=resp.last_modified)

    # -- HTML --------------------------------------------------------------

    def _from_html(self, html: str, source: SourceConfig, http: HttpClient) -> list[RawPatch]:
        params = source.params
        soup = BeautifulSoup(html, "lxml")
        entries = soup.select(params["item_selector"])
        if not entries:
            raise ParseError(f"aucun élément pour {params['item_selector']!r}")
        max_articles = int(params.get("max_articles", 5))
        patches: list[RawPatch] = []
        for entry in entries:
            if len(patches) >= max_articles:
                break
            link = entry if entry.name == "a" else entry.select_one(
                params.get("link_selector", "a[href]"))
            if link is None or not link.get("href"):
                continue
            url = urljoin(params["list_url"], link["href"])
            title_el = entry.select_one(params["title_selector"]) if params.get(
                "title_selector") else entry
            title = title_el.get_text(" ", strip=True) if title_el else ""
            if not title or f"url:{url}" in source.known_keys:
                continue
            if not looks_like_patch_title(title):
                continue
            date_raw = None
            if params.get("date_selector"):
                date_el = entry.select_one(params["date_selector"])
                if date_el is not None:
                    date_raw = (date_el.get(params["date_attr"]) if params.get("date_attr")
                                else date_el.get_text(" ", strip=True))
            try:
                article = http.get(url)
                container = find_content(BeautifulSoup(article.text, "lxml"),
                                         params.get("content_selector"))
            except (FetchError, ParseError) as exc:
                log.warning("html.article_failed",
                            extra={"source": source.key, "url": url, "error": str(exc)})
                continue
            patches.append(_make_patch(url, title, container.decode_contents(), date_raw))
        return patches

    # -- JSON --------------------------------------------------------------

    def _from_json(self, payload: str, source: SourceConfig, http: HttpClient) -> list[RawPatch]:
        params = source.params
        try:
            data = json.loads(payload)
        except ValueError as exc:
            raise ParseError(f"JSON invalide : {exc}") from exc
        items = dig(data, params["items_path"])
        if not isinstance(items, list):
            raise ParseError(f"{params['items_path']!r} n'est pas une liste")
        max_articles = int(params.get("max_articles", 5))
        patches: list[RawPatch] = []
        for item in items:
            if len(patches) >= max_articles:
                break
            item_id = dig(item, params["id_field"])
            title = dig(item, params["title_field"])
            if item_id is None or not isinstance(title, str) or not title.strip():
                continue
            if params.get("url_field"):
                url = urljoin(params["list_url"], str(dig(item, params["url_field"]) or ""))
            else:
                url = params["url_template"].format(id=item_id)
            if f"url:{url}" in source.known_keys or not looks_like_patch_title(title):
                continue
            date_raw = dig(item, params["date_field"]) if params.get("date_field") else None
            body = dig(item, params["content_field"]) if params.get("content_field") else None
            if not isinstance(body, str):
                if not params.get("content_url_template"):
                    log.warning("html.no_content", extra={"source": source.key, "url": url})
                    continue
                try:
                    detail = http.get(params["content_url_template"].format(id=item_id))
                    if params.get("content_json_path"):
                        body = dig(json.loads(detail.text), params["content_json_path"])
                    else:
                        body = find_content(BeautifulSoup(detail.text, "lxml"),
                                            params.get("content_selector")).decode_contents()
                except (FetchError, ParseError, ValueError) as exc:
                    log.warning("html.article_failed",
                                extra={"source": source.key, "url": url, "error": str(exc)})
                    continue
                if not isinstance(body, str):
                    continue
            patches.append(_make_patch(url, title.strip(), body,
                                       str(date_raw) if date_raw is not None else None))
        return patches


def _make_patch(url: str, title: str, body_html: str, date_raw: str | None) -> RawPatch:
    info = parse_heading(title)
    published = parse_date(date_raw)
    if published is None and date_raw and date_raw.isdigit():
        ts = int(date_raw)
        ts = ts / 1000 if ts > 10**11 else ts  # millisecondes
        published = datetime.fromtimestamp(ts, tz=timezone.utc)
    return RawPatch(
        source_key=f"url:{url}",
        title=title,
        url=url,
        body=body_html,
        body_format="html",
        published_raw=date_raw,
        published_at=published,
        version=info["version"],
        build=info["build"],
        platforms=info["platforms"],
    )
