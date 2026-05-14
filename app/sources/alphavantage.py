from __future__ import annotations

import httpx

from app.config import get_app_config, get_settings
from app.sources.base import BaseNewsSource, RawArticle, SourceFetchContext
from app.utils.time import parse_datetime


class AlphaVantageSource(BaseNewsSource):
    name = "Alpha Vantage"
    source_type = "market_news_sentiment"
    endpoint = "https://www.alphavantage.co/query"

    def __init__(self, api_key: str | None = None, timeout: float | None = None) -> None:
        settings = get_settings()
        runtime = get_app_config().runtime
        self.api_key = api_key or settings.alpha_vantage_api_key
        self.timeout = timeout or min(runtime.source_fetch_timeout_seconds, runtime.request_timeout_seconds)
        self.user_agent = runtime.user_agent

    async def fetch(self, context: SourceFetchContext) -> list[RawArticle]:
        if not self.api_key:
            return []
        tickers = context.config.get("tickers") or []
        topics = context.config.get("topics")
        params = {
            "function": "NEWS_SENTIMENT",
            "apikey": self.api_key,
            "limit": min(context.limit, 1000),
            "time_from": context.since.strftime("%Y%m%dT%H%M"),
            "time_to": context.until.strftime("%Y%m%dT%H%M"),
        }
        if tickers:
            params["tickers"] = ",".join(tickers)
        if topics:
            params["topics"] = ",".join(topics)

        async with httpx.AsyncClient(timeout=self.timeout, headers={"User-Agent": self.user_agent}) as client:
            response = await client.get(self.endpoint, params=params)
            response.raise_for_status()
            payload = response.json()

        articles = []
        for item in payload.get("feed", []):
            title = item.get("title")
            url = item.get("url")
            if not title or not url:
                continue
            articles.append(
                RawArticle(
                    source=item.get("source") or self.name,
                    source_type=self.source_type,
                    title=title,
                    url=url,
                    published_at=parse_datetime(item.get("time_published")),
                    language=None,
                    content_snippet=item.get("summary"),
                    raw_json=item,
                )
            )
        return articles
