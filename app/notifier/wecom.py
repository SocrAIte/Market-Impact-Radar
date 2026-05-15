from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import and_, desc, func, select
from sqlalchemy.orm import Session

from app.config import get_app_config, get_settings
from app.models import Article, EventCluster, MarketImpactAnalysis, PushRecord
from app.schemas import MarketImpactLLMOutput
from app.utils.hashing import stable_hash
from app.utils.logging import logger
from app.utils.time import ensure_aware, utcnow


DISCLAIMER = "AI 自动整理，仅供新闻监测和研究辅助，不构成投资建议。"

WECOM_MARKDOWN_BYTE_LIMIT = 4096
_PUSH_BATCH_LOCK = asyncio.Lock()

OFFICIAL_OR_TIER1_SOURCE_TERMS = {
    "sec",
    "edgar",
    "federal reserve",
    "ecb",
    "boe",
    "pboc",
    "central bank",
    "treasury",
    "exchange",
    "nyse",
    "nasdaq",
    "hkex",
    "sse",
    "szse",
    "reuters",
    "associated press",
    "ap",
    "bloomberg",
    "financial times",
    "wall street journal",
    "wsj",
    "cnbc",
    "bbc",
    "央行",
    "交易所",
    "监管",
    "证监会",
}

OFFICIAL_SOURCE_TYPES = {
    "regulatory_filing",
    "central_bank",
    "exchange",
    "company_ir",
    "official",
}

DOMESTIC_SOURCE_TYPES = {
    "china_site",
    "china_finance",
    "china_regulator",
    "china_exchange",
}

P0_EVENT_TERMS = {
    "央行利率",
    "战争冲突",
    "制裁",
    "出口管制",
    "监管",
    "银行金融风险",
    "地缘政治",
    "能源",
    "系统性金融风险",
    "A股重大政策",
}


@dataclass(slots=True)
class WeComPushResult:
    attempted: int = 0
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] | None = None

    def add_error(self, message: str) -> None:
        if self.errors is None:
            self.errors = []
        self.errors.append(message)


class WeComNotifier:
    """Enterprise WeChat robot notifier with market-impact push safeguards."""

    def __init__(
        self,
        webhook_url: str | None = None,
        timeout: float = 15.0,
        threshold: float | None = None,
        max_events_per_run: int = 10,
    ) -> None:
        settings = get_settings()
        scoring_config = get_app_config().scoring
        self.webhook_url = webhook_url if webhook_url is not None else settings.wecom_webhook_url
        self.timeout = timeout
        self.threshold = threshold if threshold is not None else scoring_config.push_score_threshold
        self.duplicate_window = timedelta(hours=scoring_config.duplicate_push_window_hours)
        self.score_delta_for_repush = scoring_config.score_delta_for_repush
        self.source_scope = scoring_config.push_source_scope
        self.max_events_per_run = max_events_per_run

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    async def send_markdown(self, content: str) -> None:
        """Send a Markdown message without exposing the webhook URL in exceptions."""
        if not self.webhook_url:
            raise RuntimeError("WECOM_WEBHOOK_URL is not configured.")

        payload = {"msgtype": "markdown", "markdown": {"content": content}}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(self.webhook_url, json=payload)
        except httpx.TimeoutException as exc:
            raise RuntimeError("WeCom webhook request timed out.") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"WeCom webhook request failed: {exc.__class__.__name__}.") from exc

        if response.status_code >= 400:
            raise RuntimeError(f"WeCom webhook HTTP error: {response.status_code}.")

        try:
            result = response.json()
        except ValueError as exc:
            raise RuntimeError("WeCom webhook returned a non-JSON response.") from exc

        if result.get("errcode") not in (None, 0):
            errmsg = str(result.get("errmsg") or "unknown error")
            raise RuntimeError(f"WeCom webhook returned errcode={result.get('errcode')}: {errmsg[:160]}")

    async def push_event(
        self,
        db: Session,
        cluster: EventCluster,
        analysis: MarketImpactAnalysis | MarketImpactLLMOutput,
        articles: Sequence[Article],
    ) -> PushRecord | None:
        """Push one event and persist a PushRecord only when WeCom is configured."""
        if not self.enabled:
            return None

        message = format_wecom_message(cluster, analysis, articles)
        record = PushRecord(
            event_cluster_id=cluster.id,
            channel="wecom",
            score_at_push=_analysis_score(analysis),
            message_hash=message_hash(f"{cluster.id}:{_analysis_identity(analysis)}:{utcnow().isoformat()}:{message}"),
            status="pending",
        )
        db.add(record)
        db.flush()

        allowed, reason = self.can_push(db, cluster, analysis, articles)
        if not allowed:
            record.status = "skipped"
            record.error_message = reason
            return record

        try:
            await self.send_markdown(message)
        except Exception as exc:  # noqa: BLE001
            record.status = "failed"
            record.error_message = _sanitize_error(str(exc))
            return record

        record.status = "success"
        record.error_message = None
        return record

    async def push_high_impact_events(self, db: Session, limit: int | None = None) -> WeComPushResult:
        """Push at most 10 latest high-impact events sorted by score descending."""
        async with _PUSH_BATCH_LOCK:
            effective_limit = min(limit or self.max_events_per_run, self.max_events_per_run, 10)
            result = WeComPushResult(errors=[])
            candidate_limit = max(effective_limit * 20, 200)
            candidates = _prioritize_high_impact_rows(_latest_high_impact_rows(db, self.threshold, candidate_limit))
            for cluster, analysis in candidates:
                db.expire_all()
                fresh_cluster = db.get(EventCluster, cluster.id)
                fresh_analysis = db.get(MarketImpactAnalysis, analysis.id)
                if fresh_cluster is None or fresh_analysis is None:
                    continue
                articles = _cluster_articles(db, fresh_cluster.id)
                scoped_articles = _filter_articles_by_scope(articles, self.source_scope)
                if not scoped_articles:
                    continue
                fresh_analysis = await self._ensure_llm_analysis_before_push(db, fresh_cluster, fresh_analysis)
                result.attempted += 1
                if _analysis_needs_llm(fresh_analysis):
                    result.skipped += 1
                    logger.warning(
                        "Skipped WeCom push for cluster {} because API analysis is not available.",
                        fresh_cluster.id,
                    )
                    db.commit()
                    if result.attempted >= effective_limit:
                        break
                    continue
                record = await self.push_event(db, fresh_cluster, fresh_analysis, scoped_articles)
                if record is None:
                    continue
                if record.status == "success":
                    result.sent += 1
                elif record.status == "skipped":
                    result.skipped += 1
                else:
                    result.failed += 1
                    if record.error_message:
                        result.add_error(record.error_message)
                db.commit()
                if result.attempted >= effective_limit:
                    break
            return result

    async def _ensure_llm_analysis_before_push(
        self,
        db: Session,
        cluster: EventCluster,
        analysis: MarketImpactAnalysis,
    ) -> MarketImpactAnalysis:
        if not get_settings().llm_enabled or not _analysis_needs_llm(analysis):
            return analysis

        # Lazy import avoids a module-level cycle: ingest imports WeComNotifier for push delivery.
        from app.pipeline.ingest import analyze_existing_cluster_once

        result = await analyze_existing_cluster_once(db, cluster, allow_push=False)
        if result.llm_failed:
            logger.warning(
                "Pre-push LLM analysis failed for cluster {}: {}",
                cluster.id,
                result.llm_error or "unknown error",
            )
        db.flush()
        db.refresh(cluster)
        return _latest_analysis_for_cluster(db, cluster.id) or analysis

    def can_push(
        self,
        db: Session,
        cluster: EventCluster,
        analysis: MarketImpactAnalysis | MarketImpactLLMOutput,
        articles: Sequence[Article],
    ) -> tuple[bool, str]:
        score = _analysis_score(analysis)
        if score < self.threshold:
            return False, f"market_impact_score {score:.1f} is below threshold {self.threshold:.1f}."

        latest_push = _latest_successful_push(db, cluster.id)
        if latest_push is None:
            return True, "No previous successful WeCom push for this event."

        last_pushed_at = ensure_aware(latest_push.pushed_at) or utcnow()
        if utcnow() - last_pushed_at > self.duplicate_window:
            return True, "Previous push is outside duplicate window."

        if score - latest_push.score_at_push >= self.score_delta_for_repush:
            return True, "Score increased enough to repush."

        if bool(cluster.is_major_update or _analysis_attr(analysis, "is_major_update", False)):
            return True, "Event is marked as a major update."

        if has_new_official_or_tier1_source(articles, since=last_pushed_at):
            return True, "Official or tier-1 source was added after the last push."

        return False, "Duplicate event within 12 hours without score jump, major update, or new official/tier-1 source."


def format_wecom_message(
    cluster: EventCluster,
    analysis: MarketImpactAnalysis | MarketImpactLLMOutput,
    articles_or_source_links: Sequence[Article] | Sequence[tuple[str, str]],
) -> str:
    articles, source_links = _split_articles_and_links(articles_or_source_links)
    top_url = source_links[0][1] if source_links else ""
    score = _analysis_score(analysis)
    score_color = "warning" if score >= 85 else "info"
    source_name = _main_source_from_links(source_links) or cluster.main_source or "未知来源"
    original_link = f"[查看原文]({top_url})" if top_url else "暂无原文链接"
    published_at, fetched_at = _message_article_times(cluster, articles)
    summary = _analysis_attr(analysis, "one_sentence_summary_zh", cluster.summary or cluster.title)
    explanation = _analysis_attr(analysis, "impact_explanation_zh", "影响路径仍需进一步核实。")

    message = (
        "# 全球市场新闻雷达\n\n"
        f"> 分数：<font color=\"{score_color}\">{score:.0f}/100</font>｜"
        f"{_direction_label(_analysis_attr(analysis, 'impact_direction', 'uncertain'))}｜"
        f"{_horizon_label(_analysis_attr(analysis, 'impact_horizon', 'short_term'))}\n"
        f"> 类型：<font color=\"comment\">{_analysis_attr(analysis, 'event_type', cluster.event_type or '其他')}</font>｜"
        f"来源：<font color=\"comment\">{source_name}</font>\n"
        f"> 发布时间：<font color=\"comment\">{published_at}</font>｜"
        f"抓取时间：<font color=\"comment\">{fetched_at}</font>\n\n"
        f"**{_trim_text(cluster.title, 96)}**\n\n"
        f"{_trim_text(summary, 180)}\n\n"
        f"> {_trim_text(explanation, 240)}\n\n"
        f"{original_link}\n"
        f"> 免责声明：{DISCLAIMER}"
    )
    return _fit_wecom_markdown(message)


def message_hash(content: str) -> str:
    return stable_hash(content)


def has_new_official_or_tier1_source(articles: Sequence[Article], since: datetime) -> bool:
    since = ensure_aware(since) or utcnow()
    previous_sources = {
        (article.source or "").casefold()
        for article in articles
        if (ensure_aware(article.fetched_at) or ensure_aware(article.published_at) or utcnow()) <= since
    }
    for article in articles:
        seen_at = ensure_aware(article.fetched_at) or ensure_aware(article.published_at)
        if seen_at and seen_at <= since:
            continue
        source_key = (article.source or "").casefold()
        if source_key in previous_sources:
            continue
        if is_official_or_tier1_source(article):
            return True
    return False


def is_official_or_tier1_source(article: Article) -> bool:
    if article.source_type in OFFICIAL_SOURCE_TYPES:
        return True
    haystack = f"{article.source} {article.source_type} {article.url}".casefold()
    return any(term.casefold() in haystack for term in OFFICIAL_OR_TIER1_SOURCE_TERMS)


def _latest_high_impact_rows(
    db: Session,
    threshold: float,
    limit: int,
) -> list[tuple[EventCluster, MarketImpactAnalysis]]:
    latest_analysis_subquery = (
        select(
            MarketImpactAnalysis.event_cluster_id.label("cluster_id"),
            func.max(MarketImpactAnalysis.created_at).label("latest_created_at"),
        )
        .group_by(MarketImpactAnalysis.event_cluster_id)
        .subquery()
    )
    return list(
        db.execute(
            select(EventCluster, MarketImpactAnalysis)
            .join(MarketImpactAnalysis, MarketImpactAnalysis.event_cluster_id == EventCluster.id)
            .join(
                latest_analysis_subquery,
                and_(
                    latest_analysis_subquery.c.cluster_id == MarketImpactAnalysis.event_cluster_id,
                    latest_analysis_subquery.c.latest_created_at == MarketImpactAnalysis.created_at,
                ),
            )
            .where(MarketImpactAnalysis.market_impact_score >= threshold)
            .order_by(desc(MarketImpactAnalysis.market_impact_score), desc(EventCluster.last_seen_at))
            .limit(limit)
        ).all()
    )


def _prioritize_high_impact_rows(
    rows: Sequence[tuple[EventCluster, MarketImpactAnalysis]],
) -> list[tuple[EventCluster, MarketImpactAnalysis]]:
    return sorted(rows, key=lambda row: (_analysis_priority(row[0], row[1]), -row[1].market_impact_score, -_cluster_time_key(row[0])))


def _analysis_priority(cluster: EventCluster, analysis: MarketImpactAnalysis) -> int:
    if analysis.market_impact_score >= 90:
        return 0
    haystack = f"{analysis.event_type} {cluster.event_type or ''} {cluster.title}".casefold()
    if any(term.casefold() in haystack for term in P0_EVENT_TERMS):
        return 0
    if analysis.market_impact_score >= 80:
        return 1
    return 2


def _cluster_time_key(cluster: EventCluster) -> float:
    value = ensure_aware(cluster.last_seen_at)
    return value.timestamp() if value else 0.0


def _latest_analysis_for_cluster(db: Session, cluster_id: int) -> MarketImpactAnalysis | None:
    return db.execute(
        select(MarketImpactAnalysis)
        .where(MarketImpactAnalysis.event_cluster_id == cluster_id)
        .order_by(desc(MarketImpactAnalysis.created_at), desc(MarketImpactAnalysis.id))
        .limit(1)
    ).scalar_one_or_none()


def _analysis_needs_llm(analysis: MarketImpactAnalysis) -> bool:
    return analysis.model_name in {"rules-only", "rules-fallback"}


def _cluster_articles(db: Session, cluster_id: int) -> list[Article]:
    return list(
        db.execute(
            select(Article)
            .where(Article.event_cluster_id == cluster_id)
            .order_by(desc(Article.published_at), desc(Article.fetched_at))
            .limit(20)
        ).scalars()
    )


def _filter_articles_by_scope(articles: Sequence[Article], scope: str) -> list[Article]:
    if scope == "domestic":
        return [article for article in articles if _is_domestic_article(article)]
    if scope == "foreign":
        return [article for article in articles if not _is_domestic_article(article)]
    return list(articles)


def _is_domestic_article(article: Article) -> bool:
    if article.source_type in DOMESTIC_SOURCE_TYPES:
        return True
    if (article.language or "").lower().startswith("zh"):
        return True
    url = (article.url or "").casefold()
    return any(token in url for token in (".cn/", ".com.cn/", "cnstock", "stcn", "cls.cn", "eastmoney", "sina.com.cn"))


def _latest_successful_push(db: Session, cluster_id: int) -> PushRecord | None:
    return db.execute(
        select(PushRecord)
        .where(PushRecord.event_cluster_id == cluster_id, PushRecord.channel == "wecom", PushRecord.status == "success")
        .order_by(desc(PushRecord.pushed_at))
        .limit(1)
    ).scalar_one_or_none()


def _split_articles_and_links(
    items: Sequence[Article] | Sequence[tuple[str, str]],
) -> tuple[list[Article], list[tuple[str, str]]]:
    if not items:
        return [], []
    first = items[0]
    if isinstance(first, Article):
        articles = list(items)  # type: ignore[arg-type]
        return articles, [(article.source, article.url) for article in articles]
    return [], list(items)  # type: ignore[arg-type]


def _asset_reason_lines(analysis: MarketImpactAnalysis | MarketImpactLLMOutput) -> str:
    assets = _analysis_list(analysis, "affected_assets", "affected_assets_json")
    lines = []
    for asset in assets[:3]:
        if isinstance(asset, dict):
            name = asset.get("name") or "未命名资产"
            reason = asset.get("reason_zh") or "可能受事件传导影响。"
        else:
            name = getattr(asset, "name", str(asset))
            reason = getattr(asset, "reason_zh", "可能受事件传导影响。")
        lines.append(f"- {_trim_text(str(name), 40)}：{_trim_text(str(reason), 150)}")
    return "\n".join(lines) if lines else "- 待识别：相关资产仍需进一步确认。"


def _fit_wecom_markdown(content: str, byte_limit: int = WECOM_MARKDOWN_BYTE_LIMIT) -> str:
    encoded = content.encode("utf-8")
    if len(encoded) <= byte_limit:
        return content

    suffix = "\n\n> 内容较长，已自动精简。"
    available = byte_limit - len(suffix.encode("utf-8"))
    if available <= 0:
        return _truncate_utf8(content, byte_limit)
    return _truncate_utf8(content, available).rstrip() + suffix


def _truncate_utf8(text: str, byte_limit: int) -> str:
    if len(text.encode("utf-8")) <= byte_limit:
        return text
    output: list[str] = []
    used = 0
    for char in text:
        size = len(char.encode("utf-8"))
        if used + size > byte_limit:
            break
        output.append(char)
        used += size
    return "".join(output)


def _trim_text(value: Any, max_chars: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _direction_label(value: Any) -> str:
    return {
        "positive": "正面",
        "negative": "负面",
        "mixed": "多空交织",
        "uncertain": "不确定",
    }.get(str(value or "").lower(), str(value or "不确定"))


def _horizon_label(value: Any) -> str:
    return {
        "intraday": "日内",
        "short_term": "短期",
        "medium_term": "中期",
        "long_term": "长期",
    }.get(str(value or "").lower(), str(value or "短期"))


def _analysis_score(analysis: MarketImpactAnalysis | MarketImpactLLMOutput) -> float:
    return float(_analysis_attr(analysis, "market_impact_score", 0.0))


def _analysis_identity(analysis: MarketImpactAnalysis | MarketImpactLLMOutput) -> str:
    return str(getattr(analysis, "id", "llm-output"))


def _analysis_list(
    analysis: MarketImpactAnalysis | MarketImpactLLMOutput,
    pydantic_name: str,
    orm_name: str,
) -> list[Any]:
    value = getattr(analysis, pydantic_name, None)
    if value is None:
        value = getattr(analysis, orm_name, None)
    return list(value or [])


def _analysis_attr(
    analysis: MarketImpactAnalysis | MarketImpactLLMOutput,
    name: str,
    default: Any,
) -> Any:
    return getattr(analysis, name, default) or default


def _main_source_from_links(source_links: list[tuple[str, str]]) -> str | None:
    if not source_links:
        return None
    return source_links[0][0]


def _message_article_times(cluster: EventCluster, articles: list[Article]) -> tuple[Any, Any]:
    if articles:
        top_article = sorted(
            articles,
            key=lambda article: (
                article.published_at or article.fetched_at or cluster.last_seen_at,
                article.fetched_at or article.published_at or cluster.last_seen_at,
            ),
            reverse=True,
        )[0]
        return _format_message_time(top_article.published_at), _format_message_time(top_article.fetched_at)
    return _format_message_time(cluster.first_seen_at), _format_message_time(cluster.last_seen_at)


def _format_message_time(value: Any) -> str:
    if value is None:
        return "未知"
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    text = str(value)
    if "." in text:
        text = text.split(".", 1)[0]
    return text[:19] if len(text) >= 19 else text


def _sanitize_error(message: str) -> str:
    webhook = get_settings().wecom_webhook_url
    if webhook:
        message = message.replace(webhook, "[redacted-wecom-webhook]")
    return message[:500]
