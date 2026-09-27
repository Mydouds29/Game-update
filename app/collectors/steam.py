"""Collecteur Steam : endpoint public ISteamNews/GetNewsForApp/v2."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from .base import (
    Collector, CollectorConfigError, FetchResult, ParseError, RawPatch,
    SourceConfig,
)
from .http import HttpClient

log = logging.getLogger(__name__)

NEWS_URL = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
OFFICIAL_FEED = "steam_community_announcements"


class SteamCollector(Collector):
    type = "steam"

    def validate_params(self, params: dict[str, Any]) -> None:
        appid = params.get("appid")
        if not isinstance(appid, int) or appid <= 0:
            raise CollectorConfigError("paramètre 'appid' (entier > 0) manquant")
        count = params.get("count", 20)
        if not isinstance(count, int) or not 1 <= count <= 100:
            raise CollectorConfigError("'count' doit être un entier entre 1 et 100")

    def fetch(self, source: SourceConfig, http: HttpClient) -> FetchResult:
        self.validate_params(source.params)
        appid = source.params["appid"]
        resp = http.get(
            NEWS_URL,
            params={
                "appid": appid,
                "count": source.params.get("count", 20),
                "maxlength": 0,
                "feeds": OFFICIAL_FEED,
                "format": "json",
            },
            etag=source.etag,
            last_modified=source.last_modified,
            # API publique prévue pour un usage programmatique.
            check_robots=False,
            accept="application/json",
        )
        if resp.not_modified:
            return FetchResult(patches=[], not_modified=True,
                               etag=source.etag, last_modified=source.last_modified)
        return FetchResult(
            patches=parse_news(resp.text, appid),
            etag=resp.etag,
            last_modified=resp.last_modified,
        )


def parse_news(payload: str, appid: int) -> list[RawPatch]:
    try:
        data = json.loads(payload)
        items = data["appnews"]["newsitems"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ParseError(f"réponse Steam inattendue : {exc}") from exc
    if not isinstance(items, list):
        raise ParseError("réponse Steam inattendue : newsitems n'est pas une liste")

    patches = []
    for item in items:
        if not isinstance(item, dict):
            continue
        # Uniquement le flux officiel du jeu demandé (pas la presse externe).
        if item.get("feedname") != OFFICIAL_FEED or item.get("appid") != appid:
            continue
        gid = str(item.get("gid") or "").strip()
        title = str(item.get("title") or "").strip()
        if not gid or not title:
            log.warning("steam.item_incomplete", extra={"appid": appid, "gid": gid})
            continue
        ts = item.get("date")
        published = (
            datetime.fromtimestamp(ts, tz=timezone.utc)
            if isinstance(ts, (int, float)) and ts > 0 else None
        )
        patches.append(RawPatch(
            source_key=gid,
            title=title,
            url=str(item.get("url") or f"https://store.steampowered.com/news/app/{appid}"),
            body=str(item.get("contents") or ""),
            body_format="bbcode",
            published_raw=str(ts) if ts is not None else None,
            published_at=published,
            tags=[str(t) for t in item.get("tags") or [] if isinstance(t, str)],
        ))
    return patches


def fetch_app_name(http: HttpClient, appid: int) -> str | None:
    """Nom officiel d'une application Steam (pour vérifier les AppID du catalogue)."""
    resp = http.get(
        APPDETAILS_URL, params={"appids": appid, "filters": "basic"},
        check_robots=False, accept="application/json",
    )
    try:
        entry = json.loads(resp.text)[str(appid)]
    except (ValueError, KeyError, TypeError) as exc:
        raise ParseError(f"appdetails inattendu : {exc}") from exc
    if not entry.get("success"):
        return None
    return entry.get("data", {}).get("name")
