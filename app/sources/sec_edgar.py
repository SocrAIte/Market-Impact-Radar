from __future__ import annotations

import asyncio

import feedparser
import httpx

from app.config import get_app_config, get_settings
from app.sources.base import BaseNewsSource, RawArticle, SourceFetchContext
from app.utils.time import parse_datetime


class SECEdgarSource(BaseNewsSource):
    name = "SEC EDGAR"
    source_type = "regulatory_filing"
    endpoint = "https://www.sec.gov/cgi-bin/browse-edgar"

    def __init__(self, timeout: float | None = None) -> None:
        settings = get_settings()
        runtime = get_app_config().runtime
        self.timeout = timeout or min(runtime.source_fetch_timeout_seconds, runtime.request_timeout_seconds)
        self.user_agent = settings.sec_user_agent or runtime.user_agent

    async def fetch(self, context: SourceFetchContext) -> list[RawArticle]:
        forms = context.config.get("forms") or ["8-K", "10-Q", "10-K", "6-K", "20-F"]
        async with httpx.AsyncClient(timeout=self.timeout, headers={"User-Agent": self.user_agent}) as client:
            results = await asyncio.gather(
                *(self._fetch_form(client, form, context) for form in forms),
                return_exceptions=True,
            )
        articles: list[RawArticle] = []
        failures = 0
        for result in results:
            if isinstance(result, Exception):
                failures += 1
                continue
            articles.extend(result)
            if len(articles) >= context.limit:
                return articles[: context.limit]
        if forms and failures == len(forms) and not articles:
            raise RuntimeError("All SEC EDGAR form feeds failed.")
        return articles[: context.limit]

    async def _fetch_form(
        self,
        client: httpx.AsyncClient,
        form: str,
        context: SourceFetchContext,
    ) -> list[RawArticle]:
        params = {
            "action": "getcurrent",
            "type": form,
            "owner": "include",
            "count": min(context.limit, 100),
            "output": "atom",
        }
        response = await client.get(self.endpoint, params=params)
        response.raise_for_status()
        feed = feedparser.parse(response.content)
        articles: list[RawArticle] = []
        for entry in feed.entries:
            published_at = parse_datetime(entry.get("updated") or entry.get("published"))
            if published_at and not (context.since <= published_at <= context.until):
                continue
            title = entry.get("title")
            url = entry.get("link")
            if not title or not url:
                continue
            articles.append(
                RawArticle(
                    source=self.name,
                    source_type=self.source_type,
                    title=title,
                    url=url,
                    published_at=published_at,
                    language="en",
                    content_snippet=entry.get("summary"),
                    raw_json=dict(entry),
                )
            )
        return articles
