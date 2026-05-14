from __future__ import annotations

import httpx

from app.config import get_app_config, get_settings
from app.sources.base import BaseNewsSource, RawArticle, SourceFetchContext
from app.utils.time import parse_datetime


class NewsAPISource(BaseNewsSource):
    name = "NewsAPI"
    source_type = "newsapi"
    endpoint = "https://newsapi.org/v2/everything"

    def __init__(self, api_key: str | None = None, timeout: float | None = None) -> None:
        settings = get_settings()
        runtime = get_app_config().runtime
        self.api_key = api_key or settings.newsapi_api_key
        self.timeout = timeout or min(runtime.source_fetch_timeout_seconds, runtime.request_timeout_seconds)
        self.user_agent = runtime.user_agent

    async def fetch(self, context: SourceFetchContext) -> list[RawArticle]:
        if not self.api_key:
            return []
        query = " OR ".join(context.keywords) or "stock market OR economy"
        params = {
            "q": query,
            "from": context.since.isoformat(),
            "to": context.until.isoformat(),
            "sortBy": "relevancy",
            "pageSize": min(context.limit, 100),
            "language": context.config.get("language"),
            "apiKey": self.api_key,
        }
        params = {k: v for k, v in params.items() if v is not None}
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
            source_name = (item.get("source") or {}).get("name") or self.name
            articles.append(
                RawArticle(
                    source=source_name,
                    source_type=self.source_type,
                    title=title,
                    url=url,
                    published_at=parse_datetime(item.get("publishedAt")),
                    language=context.config.get("language"),
                    content_snippet=item.get("description") or item.get("content"),
                    raw_json=item,
                )
            )
        return articles
