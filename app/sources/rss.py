from __future__ import annotations

import asyncio

import feedparser
import httpx

from app.config import FeedConfig, get_app_config
from app.sources.base import BaseNewsSource, RawArticle, SourceFetchContext
from app.utils.time import parse_datetime


class RSSSource(BaseNewsSource):
    name = "RSS"
    source_type = "rss"

    def __init__(self, feeds: list[FeedConfig] | None = None, timeout: float | None = None) -> None:
        runtime = get_app_config().runtime
        self.feeds = feeds or []
        self.timeout = timeout or min(runtime.source_fetch_timeout_seconds, runtime.request_timeout_seconds)
        self.user_agent = runtime.user_agent

    async def fetch(self, context: SourceFetchContext) -> list[RawArticle]:
        enabled_feeds = [feed_cfg for feed_cfg in self.feeds if feed_cfg.enabled]
        async with httpx.AsyncClient(timeout=self.timeout, headers={"User-Agent": self.user_agent}) as client:
            results = await asyncio.gather(
                *(self._fetch_feed(client, feed_cfg, context) for feed_cfg in enabled_feeds),
                return_exceptions=False,
            )
        articles: list[RawArticle] = []
        failed_feeds: list[str] = []
        for feed_articles, error in results:
            if error:
                failed_feeds.append(error)
            articles.extend(feed_articles)
            if len(articles) >= context.limit:
                return articles[: context.limit]
        if enabled_feeds and not articles and len(failed_feeds) == len(enabled_feeds):
            raise RuntimeError(f"All RSS feeds failed ({len(enabled_feeds)}): {'; '.join(failed_feeds[:5])}")
        return articles[: context.limit]

    async def _fetch_feed(
        self,
        client: httpx.AsyncClient,
        feed_cfg: FeedConfig,
        context: SourceFetchContext,
    ) -> tuple[list[RawArticle], str | None]:
        try:
            response = await client.get(feed_cfg.url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            return [], f"{feed_cfg.name}: {exc.__class__.__name__}"
        feed = feedparser.parse(response.content)
        if getattr(feed, "bozo", False) and not feed.entries:
            return [], f"{feed_cfg.name}: parse failed"
        articles = []
        for entry in feed.entries:
            published_at = parse_datetime(entry.get("published") or entry.get("updated") or entry.get("created"))
            if published_at and not (context.since <= published_at <= context.until):
                continue
            title = entry.get("title")
            url = entry.get("link")
            if not title or not url:
                continue
            articles.append(
                RawArticle(
                    source=feed_cfg.name,
                    source_type=feed_cfg.source_type,
                    title=title,
                    url=url,
                    published_at=published_at,
                    language=feed_cfg.language,
                    content_snippet=entry.get("summary") or entry.get("description"),
                    raw_json=dict(entry),
                )
            )
        return articles, None
