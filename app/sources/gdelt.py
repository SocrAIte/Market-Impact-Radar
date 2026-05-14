from __future__ import annotations

from datetime import datetime

import httpx

from app.config import get_app_config
from app.sources.base import BaseNewsSource, RawArticle, SourceFetchContext
from app.utils.time import parse_datetime


class GDELTSource(BaseNewsSource):
    name = "GDELT"
    source_type = "global_news"
    endpoint = "https://api.gdeltproject.org/api/v2/doc/doc"

    def __init__(self, timeout: float | None = None) -> None:
        runtime = get_app_config().runtime
        self.timeout = timeout or min(runtime.source_fetch_timeout_seconds, runtime.request_timeout_seconds)
        self.user_agent = runtime.user_agent

    async def fetch(self, context: SourceFetchContext) -> list[RawArticle]:
        query = " OR ".join(f'"{kw}"' if " " in kw else kw for kw in context.keywords) or "market OR economy"
        params = {
            "query": query,
            "mode": "ArtList",
            "format": "json",
            "sort": "HybridRel",
            "maxrecords": min(context.limit, 250),
            "startdatetime": _gdelt_dt(context.since),
            "enddatetime": _gdelt_dt(context.until),
        }
        async with httpx.AsyncClient(timeout=self.timeout, headers={"User-Agent": self.user_agent}) as client:
            response = await client.get(self.endpoint, params=params)
            response.raise_for_status()
            payload = response.json()

        articles = []
        for item in payload.get("articles", []):
            title = item.get("title")
            url = item.get("url")
            if not title or not url:
                continue
            articles.append(
                RawArticle(
                    source=item.get("sourceCountry") or item.get("domain") or self.name,
                    source_type=self.source_type,
                    title=title,
                    url=url,
                    published_at=parse_datetime(item.get("seendate")),
                    language=item.get("language"),
                    content_snippet=item.get("snippet") or item.get("title"),
                    raw_json=item,
                )
            )
        return articles


def _gdelt_dt(value: datetime) -> str:
    return value.strftime("%Y%m%d%H%M%S")
