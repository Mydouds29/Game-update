"""Collecteur forum Discourse (ex. forums officiels Blizzard).

Certains éditeurs publient leurs hotfixes uniquement sur leur forum, dans
un sujet dédié posté par un compte staff (ex. « HOTFIX 4 - September 22,
2026 - 3.2.1 » sur us.forums.blizzard.com/en/d4). Le collecteur lit la liste
JSON d'une catégorie, garde les sujets dont le titre ressemble à une note de
patch, puis récupère le premier message de chacun s'il vient du staff.

Paramètres :

* ``forum_url`` : racine du forum, ex. ``https://us.forums.blizzard.com/en/d4`` ;
* ``category`` : chemin de la catégorie, ex. ``pc-general-discussion/5`` ;
* ``title_pattern`` : expression régulière sur le titre (défaut : hotfix / patch notes) ;
* ``exclude_pattern`` : titres écartés même s'ils correspondent (défaut : notes
  du PTR, le serveur de test public, qui ne concernent pas le jeu en ligne) ;
* ``staff_only`` : n'accepter que les messages de comptes staff (défaut : true) ;
* ``max_topics`` : nombre maximal de nouveaux sujets lus par collecte (défaut : 5).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

from ..parsing.classifier import PTR_TITLE
from .base import (
    Collector, CollectorConfigError, FetchResult, ParseError, RawPatch,
    SourceConfig, require_param,
)
from .blizzard import parse_heading
from .http import FetchError, HttpClient

log = logging.getLogger(__name__)

DEFAULT_TITLE_PATTERN = r"\b(hotfix(es)?|patch\s*notes?)\b"
# Sujets PTR écartés dès la liste (évite de les télécharger) ; l'ingestion les
# refuse de toute façon, quelle que soit la source.
DEFAULT_EXCLUDE_PATTERN = PTR_TITLE.pattern
CATEGORY_RE = re.compile(r"^[a-z0-9-]+(/\d+)?$")


def _json(text: str, what: str) -> Any:
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ParseError(f"{what} : JSON invalide ({exc})") from exc


def _date(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_topic_list(payload: str, title_re: re.Pattern[str],
                     exclude_re: re.Pattern[str] | None = None) -> list[dict[str, Any]]:
    """Sujets de la catégorie dont le titre correspond, du plus récent au plus ancien."""
    data = _json(payload, "liste de sujets")
    try:
        topics = data["topic_list"]["topics"]
    except (KeyError, TypeError) as exc:
        raise ParseError(f"liste de sujets inattendue : {exc}") from exc
    kept = [
        t for t in topics
        if isinstance(t, dict) and isinstance(t.get("id"), int)
        and isinstance(t.get("title"), str) and title_re.search(t["title"])
        and not (exclude_re and exclude_re.search(t["title"]))
    ]
    kept.sort(key=lambda t: t.get("created_at") or "", reverse=True)
    return kept


def parse_topic(payload: str, forum_url: str, *, staff_only: bool) -> RawPatch | None:
    """Premier message d'un sujet ; None s'il n'est pas du staff (si exigé)."""
    data = _json(payload, "sujet")
    try:
        topic_id = data["id"]
        title = data["title"].strip()
        post = data["post_stream"]["posts"][0]
        body = post["cooked"]
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise ParseError(f"sujet inattendu : {exc}") from exc
    if staff_only and not post.get("staff"):
        return None
    info = parse_heading(title)
    created = post.get("created_at") or data.get("created_at")
    return RawPatch(
        source_key=f"topic:{topic_id}",
        title=title,
        url=f"{forum_url}/t/{data.get('slug') or 'topic'}/{topic_id}",
        body=body,
        body_format="html",
        published_raw=created,
        published_at=_date(created),
        version=info["version"],
        build=info["build"],
        platforms=info["platforms"],
        trusted_patch=True,
    )


class DiscourseCollector(Collector):
    type = "discourse"

    def validate_params(self, params: dict[str, Any]) -> None:
        forum_url = require_param(params, "forum_url")
        if not forum_url.startswith("https://") or forum_url.endswith("/"):
            raise CollectorConfigError("'forum_url' doit être une URL https sans / final")
        if not CATEGORY_RE.match(require_param(params, "category")):
            raise CollectorConfigError("'category' doit ressembler à 'nom-de-categorie/5'")
        for name, default in (("title_pattern", DEFAULT_TITLE_PATTERN),
                              ("exclude_pattern", DEFAULT_EXCLUDE_PATTERN)):
            try:
                re.compile(params.get(name, default))
            except re.error as exc:
                raise CollectorConfigError(f"'{name}' invalide : {exc}") from exc
        if not isinstance(params.get("staff_only", True), bool):
            raise CollectorConfigError("'staff_only' doit valoir true ou false")
        max_topics = params.get("max_topics", 5)
        if not isinstance(max_topics, int) or not 1 <= max_topics <= 20:
            raise CollectorConfigError("'max_topics' doit être un entier entre 1 et 20")

    def fetch(self, source: SourceConfig, http: HttpClient) -> FetchResult:
        self.validate_params(source.params)
        p = source.params
        forum_url = p["forum_url"]
        title_re = re.compile(p.get("title_pattern", DEFAULT_TITLE_PATTERN), re.I)
        exclude_re = re.compile(p.get("exclude_pattern", DEFAULT_EXCLUDE_PATTERN), re.I)
        resp = http.get(f"{forum_url}/c/{p['category']}/l/latest.json",
                        etag=source.etag, last_modified=source.last_modified,
                        accept="application/json")
        if resp.not_modified:
            return FetchResult([], not_modified=True, etag=source.etag,
                               last_modified=source.last_modified)
        patches: list[RawPatch] = []
        read = 0
        for topic in parse_topic_list(resp.text, title_re, exclude_re):
            if read >= p.get("max_topics", 5):
                break
            if f"topic:{topic['id']}" in source.known_keys:
                continue
            read += 1
            try:
                detail = http.get(f"{forum_url}/t/{topic['id']}.json",
                                  accept="application/json")
                patch = parse_topic(detail.text, forum_url,
                                    staff_only=p.get("staff_only", True))
            except (FetchError, ParseError) as exc:
                # Un sujet en échec ne doit pas bloquer les autres.
                log.warning("discourse.topic_failed",
                            extra={"source": source.key, "topic": topic["id"],
                                   "error": str(exc)})
                continue
            if patch is not None:
                patches.append(patch)
        return FetchResult(patches, etag=resp.etag, last_modified=resp.last_modified)
