from __future__ import annotations

import asyncio
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from app.config import get_app_config
from app.sources.base import BaseNewsSource, RawArticle, SourceFetchContext
from app.utils.logging import logger


class ChinaSitesSource(BaseNewsSource):
    name = "国内新闻网站"
    source_type = "china_site"

    def __init__(self, timeout: float | None = None) -> None:
        runtime = get_app_config().runtime
        self.timeout = timeout or min(runtime.source_fetch_timeout_seconds, runtime.request_timeout_seconds)
        self.user_agent = runtime.user_agent

    async def fetch(self, context: SourceFetchContext) -> list[RawArticle]:
        sites = (context.config.get("extra") or {}).get("sites") or []
        keywords = [keyword.casefold() for keyword in context.keywords if keyword]
        enabled_sites = [site for site in sites if site.get("enabled", True)]
        articles: list[RawArticle] = []
        async with httpx.AsyncClient(
            timeout=self.timeout,
            headers={"User-Agent": self.user_agent},
            follow_redirects=True,
        ) as client:
            results = await asyncio.gather(
                *(self._safe_fetch_site(client, site, keywords) for site in enabled_sites),
                return_exceptions=False,
            )
            failed_count = 0
            for site_articles, failed in results:
                failed_count += int(failed)
                articles.extend(site_articles)
                if len(articles) >= context.limit:
                    return articles[: context.limit]
        if enabled_sites and failed_count == len(enabled_sites) and not articles:
            raise RuntimeError(f"All configured China news sites failed ({len(enabled_sites)}).")
        return articles[: context.limit]

    async def _safe_fetch_site(
        self,
        client: httpx.AsyncClient,
        site: dict,
        keywords: list[str],
    ) -> tuple[list[RawArticle], bool]:
        try:
            return await self._fetch_site(client, site, keywords), False
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "China site {} returned HTTP {} and was skipped",
                site.get("name") or site.get("url"),
                exc.response.status_code,
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "China site {} failed with {} and was skipped",
                site.get("name") or site.get("url"),
                exc.__class__.__name__,
            )
        return [], True

    async def _fetch_site(
        self,
        client: httpx.AsyncClient,
        site: dict,
        keywords: list[str],
    ) -> list[RawArticle]:
        name = str(site.get("name") or "国内新闻网站")
        url = str(site.get("url") or "")
        if not url:
            return []
        response = await client.get(url)
        response.raise_for_status()
        response.encoding = response.encoding or response.apparent_encoding or "utf-8"
        soup = BeautifulSoup(response.text, "html.parser")
        seen: set[str] = set()
        articles: list[RawArticle] = []
        for link in soup.find_all("a"):
            title = " ".join(link.get_text(" ", strip=True).split())
            href = link.get("href")
            if not _valid_title(title) or not href:
                continue
            absolute_url = urljoin(url, str(href))
            if absolute_url in seen or not absolute_url.startswith(("http://", "https://")):
                continue
            if keywords and not _matches_keywords(title, keywords):
                continue
            seen.add(absolute_url)
            articles.append(
                RawArticle(
                    source=name,
                    source_type=self.source_type,
                    title=title,
                    url=absolute_url,
                    language="zh",
                    content_snippet=title,
                    raw_json={"site": site},
                )
            )
            if len(articles) >= int(site.get("max_results") or 20):
                break
        return articles


def _valid_title(title: str) -> bool:
    if len(title) < 8 or len(title) > 120:
        return False
    blocked = {"登录", "注册", "更多", "首页", "广告", "下载", "客户端", "关于我们", "联系我们"}
    return title not in blocked


def _matches_keywords(title: str, keywords: list[str]) -> bool:
    lowered = title.casefold()
    return any(keyword in lowered for keyword in keywords)
