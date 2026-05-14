from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class SourceFetchContext:
    since: datetime
    until: datetime
    keywords: list[str] = field(default_factory=list)
    limit: int = 50
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RawArticle:
    source: str
    source_type: str
    title: str
    url: str
    published_at: datetime | None = None
    language: str | None = None
    content_snippet: str | None = None
    raw_json: dict[str, Any] = field(default_factory=dict)


class BaseNewsSource(ABC):
    name: str
    source_type: str

    @abstractmethod
    async def fetch(self, context: SourceFetchContext) -> list[RawArticle]:
        raise NotImplementedError
