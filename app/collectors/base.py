"""Interface commune des collecteurs : fetch -> list[RawPatch]."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .http import HttpClient


class CollectorConfigError(ValueError):
    """Paramètres de source invalides pour ce collecteur."""


class ParseError(Exception):
    """Le contenu récupéré n'a pas la structure attendue."""


@dataclass
class SourceConfig:
    id: int
    key: str
    type: str
    params: dict[str, Any]
    etag: str | None = None
    last_modified: str | None = None
    # Identifiants déjà connus en base (évite de retélécharger des articles).
    known_keys: frozenset[str] = frozenset()


@dataclass
class RawPatch:
    """Patch tel qu'extrait d'une source, avant normalisation."""

    source_key: str
    title: str
    url: str
    body: str
    body_format: str  # "bbcode" | "html"
    published_raw: str | None = None
    published_at: datetime | None = None
    tags: list[str] = field(default_factory=list)
    version: str | None = None
    build: str | None = None
    platforms: str | None = None
    # Le collecteur a déjà la certitude qu'il s'agit d'un patch (ex. page
    # dédiée aux patch notes) : le classifieur n'est pas consulté.
    trusted_patch: bool = False


@dataclass
class FetchResult:
    patches: list[RawPatch]
    not_modified: bool = False
    etag: str | None = None
    last_modified: str | None = None


class Collector(ABC):
    type: str = ""

    @abstractmethod
    def validate_params(self, params: dict[str, Any]) -> None:
        """Lève CollectorConfigError si les paramètres sont incomplets."""

    @abstractmethod
    def fetch(self, source: SourceConfig, http: HttpClient) -> FetchResult:
        """Récupère et découpe les patchs publiés par la source."""


def require_param(params: dict[str, Any], name: str, kind: type = str) -> Any:
    value = params.get(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise CollectorConfigError(f"paramètre '{name}' manquant")
    if not isinstance(value, kind):
        raise CollectorConfigError(f"paramètre '{name}' doit être de type {kind.__name__}")
    return value
