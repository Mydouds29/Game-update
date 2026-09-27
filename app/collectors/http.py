"""Client HTTP de collecte : User-Agent explicite, robots.txt, cache
conditionnel (ETag / Last-Modified), limitation de fréquence par hôte,
timeouts et taille de réponse bornée."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import requests

log = logging.getLogger(__name__)

ROBOTS_TTL_SECONDS = 24 * 3600


class FetchError(Exception):
    """Échec de récupération d'une ressource distante."""

    def __init__(self, message: str, *, status: int | None = None,
                 retry_after: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class RobotsDisallowed(FetchError):
    """robots.txt interdit l'accès à l'URL pour notre User-Agent."""


@dataclass
class HttpResponse:
    url: str
    status: int
    text: str
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b""

    @property
    def not_modified(self) -> bool:
        return self.status == 304

    @property
    def etag(self) -> str | None:
        return self.headers.get("etag")

    @property
    def last_modified(self) -> str | None:
        return self.headers.get("last-modified")


def _parse_retry_after(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return max(0, int(value))
    except ValueError:
        return None


class HttpClient:
    def __init__(
        self,
        user_agent: str,
        timeout: int = 20,
        min_interval: float = 5.0,
        max_bytes: int = 5 * 1024 * 1024,
        session: requests.Session | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.min_interval = min_interval
        self.max_bytes = max_bytes
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept-Language": "en-US,en;q=0.8",
        })
        self._clock = clock
        self._sleep = sleep
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, tuple[float, RobotFileParser]] = {}
        self._lock = threading.Lock()

    # -- limitation de fréquence -------------------------------------------

    def _throttle(self, host: str) -> None:
        with self._lock:
            last = self._last_request.get(host)
            now = self._clock()
            if last is not None:
                wait = self.min_interval - (now - last)
                if wait > 0:
                    self._sleep(wait)
                    now = self._clock()
            self._last_request[host] = now

    # -- robots.txt --------------------------------------------------------

    def _robots_for(self, url: str) -> RobotFileParser:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        cached = self._robots.get(origin)
        if cached and self._clock() - cached[0] < ROBOTS_TTL_SECONDS:
            return cached[1]

        parser = RobotFileParser()
        robots_url = origin + "/robots.txt"
        try:
            resp = self._raw_get(robots_url, headers={})
        except FetchError as exc:
            # Échec transitoire : ni mis en cache ni pris pour une interdiction ;
            # la source est simplement reprogrammée avec backoff.
            raise FetchError(f"robots.txt injoignable ({exc})") from exc
        if resp.status >= 500:
            # Serveur en erreur : on n'explore pas pour l'instant (RFC 9309 §2.3.1.4).
            raise FetchError(f"robots.txt en erreur HTTP {resp.status}", status=resp.status)
        if resp.status >= 400:
            # 4xx : robots.txt « indisponible », accès autorisé (RFC 9309 §2.3.1.3).
            parser.allow_all = True
        else:
            parser.parse(resp.text.splitlines())
        self._robots[origin] = (self._clock(), parser)
        return parser

    def allowed_by_robots(self, url: str) -> bool:
        return self._robots_for(url).can_fetch(self.user_agent, url)

    # -- requêtes ----------------------------------------------------------

    def _raw_get(self, url: str, headers: dict[str, str],
                 params: dict | None = None) -> HttpResponse:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"}:
            raise FetchError(f"schéma non supporté : {parts.scheme}")
        self._throttle(parts.netloc)
        try:
            with self.session.get(
                url, headers=headers, params=params, timeout=self.timeout,
                stream=True, allow_redirects=True,
            ) as resp:
                chunks: list[bytes] = []
                size = 0
                for chunk in resp.iter_content(chunk_size=65536):
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise FetchError(
                            f"réponse trop volumineuse (> {self.max_bytes} octets)",
                            status=resp.status_code,
                        )
                    chunks.append(chunk)
                encoding = resp.encoding or "utf-8"
                if "charset" not in resp.headers.get("content-type", "").lower():
                    encoding = "utf-8"
                content = b"".join(chunks)
                text = content.decode(encoding, errors="replace")
                return HttpResponse(
                    url=resp.url,
                    status=resp.status_code,
                    text=text,
                    content=content,
                    headers={k.lower(): v for k, v in resp.headers.items()},
                )
        except requests.RequestException as exc:
            raise FetchError(f"{type(exc).__name__}: {exc}") from exc

    def get(
        self,
        url: str,
        *,
        params: dict | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        check_robots: bool = True,
        accept: str | None = None,
    ) -> HttpResponse:
        if check_robots and not self.allowed_by_robots(url):
            raise RobotsDisallowed(f"robots.txt interdit {url}")
        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        if accept:
            headers["Accept"] = accept
        resp = self._raw_get(url, headers=headers, params=params)
        if resp.status == 304:
            return resp
        if resp.status == 429 or resp.status == 503:
            raise FetchError(
                f"HTTP {resp.status} (limitation côté serveur)",
                status=resp.status,
                retry_after=_parse_retry_after(resp.headers.get("retry-after")),
            )
        if resp.status >= 400:
            raise FetchError(f"HTTP {resp.status} sur {url}", status=resp.status)
        return resp
