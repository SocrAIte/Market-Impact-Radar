from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.orm import Session

from app.config import SourceBlockConfig, get_app_config, get_settings
from app.db import SessionLocal, init_db
from app.models import Article, EventCluster, MarketImpactAnalysis
from app.notifier.wecom import WeComNotifier
from app.pipeline.cluster import cluster_article
from app.pipeline.dedup import upsert_article
from app.pipeline.llm_analyzer import analyze_with_llm
from app.pipeline.normalize import normalize_article
from app.pipeline.push_decider import evaluate_push
from app.pipeline.scoring import score_event
from app.pipeline.title_translation import ensure_online_title_translation
from app.sources import (
    AlphaVantageSource,
    ChinaSitesSource,
    GDELTSource,
    NewsAPISource,
    RSSSource,
    SECEdgarSource,
    SourceFetchContext,
)
from app.sources.base import BaseNewsSource, RawArticle
from app.utils.logging import logger
from app.utils.time import window_bounds


@dataclass(slots=True)
class IngestResult:
    fetched_count: int = 0
    inserted_count: int = 0
    duplicate_count: int = 0
    cluster_count: int = 0
    analysis_count: int = 0
    push_attempted_count: int = 0
    pushed_count: int = 0
    push_skipped_count: int = 0
    push_failed_count: int = 0
    push_note: str = ""
    llm_attempted_count: int = 0
    llm_succeeded_count: int = 0
    llm_failed_count: int = 0
    llm_status_note: str = ""
    background_analysis_started: bool = False
    background_analysis_count: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ClusterAnalysisRunResult:
    push_status: str | None = None
    llm_attempted: bool = False
    llm_succeeded: bool = False
    llm_failed: bool = False
    llm_error: str = ""


async def run_ingest_once(time_window: str | None = None, *, defer_llm: bool = False) -> IngestResult:
    init_db()
    app_config = get_app_config()
    settings = get_settings()
    selected_window = time_window or app_config.runtime.default_time_window
    since, until = window_bounds(selected_window)
    raw_articles, errors = await fetch_all_sources(since=since, until=until)
    result = IngestResult(fetched_count=len(raw_articles), errors=errors)
    touched_cluster_ids: set[int] = set()
    llm_budget = max(int(getattr(app_config.runtime, "max_llm_analysis_per_run", 5) or 0), 0)
    cluster_ids: list[int] = []

    with SessionLocal() as db:
        for raw in raw_articles:
            try:
                normalized = normalize_article(raw)
                article, is_new = upsert_article(db, normalized)
                if is_new:
                    result.inserted_count += 1
                    cluster = cluster_article(db, article)
                    touched_cluster_ids.add(cluster.id)
                else:
                    result.duplicate_count += 1
                    if article.event_cluster_id is None:
                        cluster = cluster_article(db, article)
                        touched_cluster_ids.add(cluster.id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to persist article {}: {}", raw.url, exc)
                result.errors.append(f"{raw.url}: {exc}")
        db.commit()
        if app_config.runtime.backfill_unanalyzed_clusters:
            remaining = max(app_config.runtime.max_clusters_per_run - len(touched_cluster_ids), 0)
            touched_cluster_ids.update(_clusters_without_analysis(db, since, limit=remaining))
        if settings.llm_enabled:
            remaining = max(app_config.runtime.max_clusters_per_run - len(touched_cluster_ids), 0)
            touched_cluster_ids.update(
                _clusters_needing_api_analysis(
                    db,
                    since,
                    limit=min(remaining, llm_budget),
                    exclude=touched_cluster_ids,
                )
            )
        cluster_ids = _rank_cluster_ids_for_analysis(db, touched_cluster_ids, app_config.runtime.max_clusters_per_run)

    analysis_settings = settings
    analysis_llm_budget = llm_budget
    allow_push = False
    if defer_llm and settings.llm_enabled:
        analysis_settings = settings.model_copy(update={"llm_api_key": None})
        analysis_llm_budget = 0
        result.background_analysis_started = bool(cluster_ids)
        result.background_analysis_count = min(len(cluster_ids), llm_budget)
        result.llm_status_note = (
            f"已先完成快速扫描，{result.background_analysis_count} 个事件将继续在后台进行 API 分析。"
            if result.background_analysis_count
            else "已先完成快速扫描，本轮没有需要后台 API 分析的事件。"
        )

    analysis_results = await _analyze_cluster_ids_concurrently(
        cluster_ids=cluster_ids,
        app_config=app_config,
        settings=analysis_settings,
        llm_budget=analysis_llm_budget,
        allow_push=allow_push,
    )
    for cluster_id, analysis_result, exc in analysis_results:
        if exc is not None:
            logger.warning("Failed to analyze cluster {}: {}", cluster_id, exc)
            result.errors.append(_friendly_cluster_error(cluster_id, exc))
            continue
        if analysis_result is None:
            continue
        result.analysis_count += 1
        if analysis_result.llm_attempted:
            result.llm_attempted_count += 1
        if analysis_result.llm_succeeded:
            result.llm_succeeded_count += 1
        if analysis_result.llm_failed:
            result.llm_failed_count += 1
            if not result.llm_status_note:
                result.llm_status_note = _friendly_llm_error(analysis_result.llm_error)
                logger.warning("LLM disabled for queued analysis tasks: {}", result.llm_status_note)
        push_status = analysis_result.push_status
        if push_status:
            result.push_attempted_count += 1
            if push_status == "success":
                result.pushed_count += 1
            elif push_status == "failed":
                result.push_failed_count += 1
            else:
                result.push_skipped_count += 1

    with SessionLocal() as db:
        if settings.wecom_webhook_url and not result.background_analysis_started:
            push_result = await WeComNotifier().push_high_impact_events(db)
            result.push_attempted_count += push_result.attempted
            result.pushed_count += push_result.sent
            result.push_skipped_count += push_result.skipped
            result.push_failed_count += push_result.failed
            db.commit()
        elif settings.wecom_webhook_url and result.background_analysis_started:
            result.push_note = "后台 API 分析完成后，将按影响分从高到低推送符合条件的事件。"
        elif not settings.wecom_webhook_url:
            result.push_note = "企业微信未配置，未执行实际推送。"
    if settings.llm_enabled and not result.llm_status_note:
        if result.llm_succeeded_count:
            result.llm_status_note = f"LLM 分析成功 {result.llm_succeeded_count} 个事件。"
        elif result.llm_attempted_count == 0:
            result.llm_status_note = "本轮没有需要调用 LLM 的事件。"
    result.cluster_count = len(cluster_ids)
    return result


async def run_deferred_llm_analysis(time_window: str | None = None, *, limit: int | None = None) -> IngestResult:
    init_db()
    app_config = get_app_config()
    settings = get_settings()
    result = IngestResult()
    if not settings.llm_enabled:
        result.llm_status_note = "LLM API 未配置，后台分析已跳过。"
        return result

    selected_window = time_window or app_config.runtime.default_time_window
    since, _ = window_bounds(selected_window)
    llm_budget = max(int(getattr(app_config.runtime, "max_llm_analysis_per_run", 5) or 0), 0)
    effective_limit = min(limit or llm_budget, llm_budget, app_config.runtime.max_clusters_per_run)
    with SessionLocal() as db:
        cluster_ids = _rank_cluster_ids_for_analysis(
            db,
            _clusters_needing_api_analysis(db, since, limit=effective_limit),
            effective_limit,
        )

    analysis_results = await _analyze_cluster_ids_concurrently(
        cluster_ids=cluster_ids,
        app_config=app_config,
        settings=settings,
        llm_budget=llm_budget,
        allow_push=False,
    )
    result.cluster_count = len(cluster_ids)
    for cluster_id, analysis_result, exc in analysis_results:
        if exc is not None:
            logger.warning("Deferred LLM analysis failed for cluster {}: {}", cluster_id, exc)
            result.errors.append(_friendly_cluster_error(cluster_id, exc))
            continue
        if analysis_result is None:
            continue
        result.analysis_count += 1
        if analysis_result.llm_attempted:
            result.llm_attempted_count += 1
        if analysis_result.llm_succeeded:
            result.llm_succeeded_count += 1
        if analysis_result.llm_failed:
            result.llm_failed_count += 1
            if not result.llm_status_note:
                result.llm_status_note = _friendly_llm_error(analysis_result.llm_error)
        if analysis_result.push_status:
            result.push_attempted_count += 1
            if analysis_result.push_status == "success":
                result.pushed_count += 1
            elif analysis_result.push_status == "failed":
                result.push_failed_count += 1
            else:
                result.push_skipped_count += 1
    with SessionLocal() as db:
        if settings.wecom_webhook_url:
            push_result = await WeComNotifier().push_high_impact_events(db)
            result.push_attempted_count += push_result.attempted
            result.pushed_count += push_result.sent
            result.push_skipped_count += push_result.skipped
            result.push_failed_count += push_result.failed
            db.commit()
    if settings.llm_enabled and not result.llm_status_note:
        result.llm_status_note = (
            f"后台 API 分析完成 {result.llm_succeeded_count} 个事件。"
            if result.llm_succeeded_count
            else "后台没有需要 API 分析的事件。"
        )
    return result


async def analyze_existing_cluster_once(
    db: Session,
    cluster: EventCluster,
    *,
    allow_push: bool = False,
) -> ClusterAnalysisRunResult:
    return await _analyze_cluster(db, cluster, get_app_config(), get_settings(), allow_push=allow_push)


async def _analyze_cluster_ids_concurrently(
    *,
    cluster_ids: list[int],
    app_config,
    settings,
    llm_budget: int,
    allow_push: bool = True,
) -> list[tuple[int, ClusterAnalysisRunResult | None, Exception | None]]:
    if not cluster_ids:
        return []
    max_concurrent = min(max(int(getattr(app_config.runtime, "max_concurrent_llm_analysis", 10) or 10), 1), 10)
    semaphore = asyncio.Semaphore(max_concurrent)
    budget_lock = asyncio.Lock()
    llm_disabled = asyncio.Event()
    remaining_llm_budget = max(llm_budget, 0)

    async def next_settings_for_task():
        nonlocal remaining_llm_budget
        if not settings.llm_enabled or llm_disabled.is_set():
            return settings.model_copy(update={"llm_api_key": None})
        async with budget_lock:
            if remaining_llm_budget <= 0 or llm_disabled.is_set():
                return settings.model_copy(update={"llm_api_key": None})
            remaining_llm_budget -= 1
            return settings

    async def worker(cluster_id: int) -> tuple[int, ClusterAnalysisRunResult | None, Exception | None]:
        async with semaphore:
            analysis_settings = await next_settings_for_task()
            try:
                with SessionLocal() as db:
                    cluster = db.get(EventCluster, cluster_id)
                    if cluster is None:
                        return cluster_id, None, None
                    analysis_result = await _analyze_cluster(db, cluster, app_config, analysis_settings, allow_push=allow_push)
                    db.commit()
                    if analysis_result.llm_failed:
                        llm_disabled.set()
                    return cluster_id, analysis_result, None
            except Exception as exc:  # noqa: BLE001
                return cluster_id, None, exc

    return await asyncio.gather(*(worker(cluster_id) for cluster_id in cluster_ids))


async def _analyze_cluster(
    db: Session,
    cluster: EventCluster,
    app_config,
    settings,
    *,
    allow_push: bool = True,
) -> ClusterAnalysisRunResult:
    articles = list(
        db.execute(
            select(Article)
            .where(Article.event_cluster_id == cluster.id)
            .order_by(desc(Article.published_at), desc(Article.fetched_at))
            .limit(20)
        ).scalars()
    )
    rule_output = score_event(cluster, articles, app_config)
    llm_output, llm_raw, model_name = await analyze_with_llm(cluster, articles, rule_output, settings)
    llm_translation_would_repeat_failed_call = model_name == "rules-fallback" and settings.translation_provider.strip().lower() in {
        "llm",
        "model",
    }
    if settings.title_translation_enabled and not llm_translation_would_repeat_failed_call:
        llm_output, llm_raw = await ensure_online_title_translation(cluster.title, llm_output, llm_raw, settings)
    cluster.event_type = llm_output.event_type
    cluster.summary = llm_output.one_sentence_summary_zh
    cluster.is_major_update = cluster.is_major_update or llm_output.is_major_update
    decision = evaluate_push(db, cluster, llm_output, app_config.scoring)
    if decision.should_push and not llm_output.should_push:
        decision.should_push = False
        decision.reason = f"LLM 分析不建议推送：{llm_output.push_reason or '影响路径或置信度不足。'}"
    elif llm_output.push_reason:
        decision.reason = f"{decision.reason} LLM 意见：{llm_output.push_reason}"
    analysis = MarketImpactAnalysis(
        event_cluster_id=cluster.id,
        one_sentence_summary_zh=llm_output.one_sentence_summary_zh,
        impact_explanation_zh=llm_output.impact_explanation_zh,
        event_type=llm_output.event_type,
        affected_assets_json=[asset.model_dump(mode="json") for asset in llm_output.affected_assets],
        affected_countries_json=llm_output.affected_countries,
        affected_sectors_json=llm_output.affected_sectors,
        impact_direction=llm_output.impact_direction,
        impact_horizon=llm_output.impact_horizon,
        event_severity_score=llm_output.event_severity_score,
        market_scope_score=llm_output.market_scope_score,
        asset_sensitivity_score=llm_output.asset_sensitivity_score,
        credibility_score=llm_output.credibility_score,
        novelty_score=llm_output.novelty_score,
        timeliness_score=llm_output.timeliness_score,
        confidence_score=llm_output.confidence_score,
        market_impact_score=llm_output.market_impact_score,
        confidence_level=llm_output.confidence_level,
        uncertainties_json=llm_output.uncertainties,
        should_push=decision.should_push,
        push_reason=decision.reason,
        model_name=model_name,
        llm_raw_json=llm_raw,
    )
    db.add(analysis)
    db.flush()
    push_status = None
    if decision.should_push and allow_push and settings.wecom_webhook_url:
        push_status = await _push_if_configured(db, cluster, llm_output, articles)
    llm_attempted = bool(settings.llm_enabled)
    return ClusterAnalysisRunResult(
        push_status=push_status,
        llm_attempted=llm_attempted,
        llm_succeeded=llm_attempted and model_name not in {"rules-only", "rules-fallback"},
        llm_failed=llm_attempted and model_name == "rules-fallback",
        llm_error=str(llm_raw.get("error") or "") if isinstance(llm_raw, dict) else "",
    )


def _clusters_without_analysis(db: Session, since, limit: int = 100) -> set[int]:
    if limit <= 0:
        return set()
    rows = db.execute(
        select(EventCluster.id)
        .outerjoin(MarketImpactAnalysis, MarketImpactAnalysis.event_cluster_id == EventCluster.id)
        .where(EventCluster.last_seen_at >= since, MarketImpactAnalysis.id.is_(None))
        .order_by(desc(EventCluster.last_seen_at))
        .limit(limit)
    ).scalars()
    return {int(cluster_id) for cluster_id in rows}


def _clusters_needing_api_analysis(
    db: Session,
    since,
    limit: int = 100,
    exclude: set[int] | None = None,
) -> set[int]:
    if limit <= 0:
        return set()
    latest_analysis_subquery = (
        select(
            MarketImpactAnalysis.event_cluster_id.label("cluster_id"),
            func.max(MarketImpactAnalysis.created_at).label("latest_created_at"),
        )
        .group_by(MarketImpactAnalysis.event_cluster_id)
        .subquery()
    )
    statement = (
        select(EventCluster.id)
        .outerjoin(latest_analysis_subquery, latest_analysis_subquery.c.cluster_id == EventCluster.id)
        .outerjoin(
            MarketImpactAnalysis,
            and_(
                latest_analysis_subquery.c.cluster_id == MarketImpactAnalysis.event_cluster_id,
                latest_analysis_subquery.c.latest_created_at == MarketImpactAnalysis.created_at,
            ),
        )
        .where(
            EventCluster.last_seen_at >= since,
            or_(
                MarketImpactAnalysis.id.is_(None),
                MarketImpactAnalysis.model_name.in_(("rules-only", "rules-fallback")),
            ),
        )
        .order_by(desc(EventCluster.last_seen_at))
        .limit(limit)
    )
    if exclude:
        statement = statement.where(~EventCluster.id.in_(exclude))
    rows = db.execute(statement).scalars()
    return {int(cluster_id) for cluster_id in rows}


def _rank_cluster_ids_for_analysis(db: Session, cluster_ids: set[int], limit: int) -> list[int]:
    if not cluster_ids:
        return []
    rows = db.execute(
        select(EventCluster.id)
        .where(EventCluster.id.in_(cluster_ids))
        .order_by(desc(EventCluster.is_major_update), desc(EventCluster.last_seen_at))
        .limit(max(limit, 1))
    ).scalars()
    return [int(cluster_id) for cluster_id in rows]


async def fetch_all_sources(since, until) -> tuple[list[RawArticle], list[str]]:
    app_config = get_app_config()
    tasks = []
    for source, block in _enabled_sources(app_config.sources):
        context = SourceFetchContext(
            since=since,
            until=until,
            keywords=block.keywords,
            limit=block.max_results,
            config=block.model_dump(),
        )
        tasks.append(_safe_fetch(source, context))
    results = await asyncio.gather(*tasks)
    articles: list[RawArticle] = []
    errors: list[str] = []
    for source_articles, source_errors in results:
        articles.extend(source_articles)
        errors.extend(source_errors)
    return articles, errors


def _enabled_sources(blocks: dict[str, SourceBlockConfig]) -> list[tuple[BaseNewsSource, SourceBlockConfig]]:
    enabled: list[tuple[BaseNewsSource, SourceBlockConfig]] = []
    for name, block in blocks.items():
        if not block.enabled:
            continue
        lowered = name.lower()
        if lowered == "gdelt":
            enabled.append((GDELTSource(), block))
        elif lowered == "newsapi":
            enabled.append((NewsAPISource(), block))
        elif lowered == "alphavantage":
            enabled.append((AlphaVantageSource(), block))
        elif lowered in {"china_sites", "china_news", "domestic_news"}:
            enabled.append((ChinaSitesSource(), block))
        elif lowered in {"sec", "sec_edgar", "edgar"}:
            enabled.append((SECEdgarSource(), block))
        elif lowered == "rss":
            enabled.append((RSSSource(feeds=block.feeds), block))
    return enabled


async def _safe_fetch(source: BaseNewsSource, context: SourceFetchContext) -> tuple[list[RawArticle], list[str]]:
    try:
        return await source.fetch(context), []
    except Exception as exc:  # noqa: BLE001
        logger.warning("Source {} failed: {}", source.name, repr(exc))
        return [], [_friendly_source_error(source.name)]


def _friendly_source_error(source_name: str) -> str:
    return f"{source_name} 暂时不可用，请稍后重试或检查网络配置。"


def _friendly_cluster_error(cluster_id: int, exc: Exception) -> str:
    return f"事件 {cluster_id} 暂时无法完成评分，已跳过并保留原始新闻。原因：{exc.__class__.__name__}"


def _friendly_llm_error(error: str) -> str:
    lowered = (error or "").lower()
    if "401" in lowered or "unauthorized" in lowered:
        return "LLM 接口鉴权失败：请检查 API Key、Base URL 和模型权限。本轮已停止继续调用 LLM，避免扫描卡住。"
    if "403" in lowered or "forbidden" in lowered:
        return "LLM 接口无访问权限：请检查 API Key 或模型权限。本轮已停止继续调用 LLM。"
    if "404" in lowered or "not found" in lowered:
        return "LLM 接口地址或模型名称可能不正确。本轮已停止继续调用 LLM。"
    if "429" in lowered or "quota" in lowered or "rate" in lowered:
        return "LLM 接口限流或额度不足。本轮已停止继续调用 LLM。"
    if "timeout" in lowered or "connect" in lowered:
        return "LLM 接口连接失败或超时。本轮已停止继续调用 LLM，规则评分已保留。"
    return "LLM 分析失败，本轮已停止继续调用 LLM，规则评分已保留。"


async def _push_if_configured(
    db: Session,
    cluster: EventCluster,
    output,
    articles: list[Article],
) -> str:
    notifier = WeComNotifier()
    record = await notifier.push_event(db, cluster, output, articles)
    if record is None:
        return "not_configured"
    if record.status == "failed":
        logger.warning("WeCom push failed: {}", record.error_message)
    return record.status
