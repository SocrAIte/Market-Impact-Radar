from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import yaml
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.orm import Session

from app.config import AppYamlConfig, PROJECT_ROOT, ScoringConfig, SourceBlockConfig, _deep_merge, get_app_config, get_settings
from app.db import get_db
from app.models import Article, EventCluster, MarketImpactAnalysis, PushRecord
from app.pipeline.ingest import IngestResult, analyze_existing_cluster_once, run_deferred_llm_analysis, run_ingest_once
from app.pipeline.title_translation import translate_title_online
from app.schemas import EventClusterRead, MarketImpactAnalysisRead
from app.utils.logging import logger
from app.utils.time import window_bounds
from app.web.presentation import event_view, label_push_status


router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

ENV_PATH = PROJECT_ROOT / ".env"
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
CONFIG_EXAMPLE_PATH = PROJECT_ROOT / "config.example.yaml"
MIN_DASHBOARD_IMPACT_SCORE = 60.0

SCORING_WEIGHT_LABELS = {
    "event_severity_score": "事件严重度",
    "market_scope_score": "市场覆盖范围",
    "asset_sensitivity_score": "资产敏感度",
    "credibility_score": "来源可信度",
    "novelty_score": "新颖度",
    "timeliness_score": "时效性",
}

SCORING_WEIGHT_HELP = {
    "event_severity_score": "衡量事件本身的冲击强度，例如战争升级、制裁、央行利率、重大监管或破产风险。",
    "market_scope_score": "衡量影响范围是单家公司、一个行业、一个国家市场，还是可能扩散到全球多类资产。",
    "asset_sensitivity_score": "衡量相关资产对这类事件通常有多敏感，例如能源对供给冲击、银行对信用风险、芯片对出口管制。",
    "credibility_score": "衡量来源可靠性。官方公告、监管文件、一线媒体更高，传言、转载和低可信来源更低。",
    "novelty_score": "衡量是否包含新事实或重大进展。重复转载会降低，新公告、新数据、新政策会提高。",
    "timeliness_score": "衡量事件是否仍在影响窗口内并持续发酵；不是越新越高，旧事件仍在扩散也可以较高。",
}


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    window: str = Query(default="24h"),
    sort: str = Query(default="impact_score"),
    db: Session = Depends(get_db),
):
    app_config = get_app_config()
    rows = _event_rows(db, window=window, limit=100, sort=sort)
    await _ensure_title_translations(db, rows)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "rows": _with_event_views(rows),
            "window": window,
            "sort": _normalize_sort(sort),
            "allowed_windows": _time_window_options(app_config),
            "sort_options": _sort_options(),
            "stats": _dashboard_stats(rows, app_config),
        },
    )


@router.get("/events/fragment", response_class=HTMLResponse)
async def event_cards_fragment(
    request: Request,
    window: str = Query(default="24h"),
    sort: str = Query(default="impact_score"),
    db: Session = Depends(get_db),
):
    app_config = get_app_config()
    rows = _event_rows(db, window=window, limit=100, sort=sort)
    await _ensure_title_translations(db, rows)
    response = templates.TemplateResponse(
        request=request,
        name="_event_cards.html",
        context={
            "rows": _with_event_views(rows),
            "stats": _dashboard_stats(rows, app_config),
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/events/{cluster_id}/card", response_class=HTMLResponse)
async def event_card_fragment(
    request: Request,
    cluster_id: int,
    db: Session = Depends(get_db),
):
    app_config = get_app_config()
    row = _event_row_by_cluster_id(db, cluster_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Event card not found.")
    response = templates.TemplateResponse(
        request=request,
        name="_event_cards.html",
        context={
            "rows": _with_event_views([row]),
            "stats": _dashboard_stats([row], app_config),
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/events/{cluster_id}", response_class=HTMLResponse)
def event_detail(request: Request, cluster_id: int, db: Session = Depends(get_db)):
    cluster = db.get(EventCluster, cluster_id)
    if not cluster:
        raise HTTPException(status_code=404, detail="Event cluster not found.")
    analysis = _latest_analysis(db, cluster_id)
    articles = list(
        db.execute(
            select(Article)
            .where(Article.event_cluster_id == cluster_id)
            .order_by(desc(Article.published_at), desc(Article.fetched_at))
        ).scalars()
    )
    pushes = _visible_push_records(db, cluster_id)
    return templates.TemplateResponse(
        request=request,
        name="event_detail.html",
        context={
            "cluster": cluster,
            "analysis": analysis,
            "articles": articles,
            "pushes": pushes,
            "view": event_view(cluster, analysis, llm_configured=get_settings().llm_enabled),
            "label_push_status": label_push_status,
            "format_time": _format_display_time,
        },
    )


@router.post("/api/events/{cluster_id}/reanalyze")
async def api_reanalyze_event(
    request: Request,
    cluster_id: int,
    window: str = Query(default="24h"),
    sort: str = Query(default="impact_score"),
    db: Session = Depends(get_db),
):
    settings = get_settings()
    if not settings.llm_enabled:
        return JSONResponse(
            {"ok": False, "message": "LLM API 未配置，请先在设置页填写并测试。"},
            status_code=200,
        )
    cluster = db.get(EventCluster, cluster_id)
    if not cluster:
        raise HTTPException(status_code=404, detail="Event cluster not found.")
    try:
        result = await analyze_existing_cluster_once(db, cluster, allow_push=False)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.exception("Manual event reanalysis failed")
        return {"ok": False, "message": f"重新分析失败：{_safe_error(exc)}"}
    if result.llm_succeeded:
        card_html = _render_event_card_html(request, db, cluster_id)
        return {
            "ok": True,
            "message": "已生成中文标题、摘要、影响路径和评分。",
            "card_html": card_html,
            "hidden": not bool(card_html),
            "stats": _dashboard_stats_for_window(db, window, sort),
        }
    return {
        "ok": False,
        "message": _friendly_runtime_error(result.llm_error or "LLM analysis failed"),
    }


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    app_config = get_app_config()
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "config": app_config,
            "settings_view": _settings_view(app_config, db),
        },
    )


@router.post("/api/config/integrations")
async def api_update_integrations(request: Request):
    form = await _read_urlencoded_form(request)
    updates = {
        "LLM_BASE_URL": _form_text(form, "llm_base_url"),
        "LLM_API_KEY": _form_text(form, "llm_api_key"),
        "LLM_MODEL": _form_text(form, "llm_model"),
        "WECOM_WEBHOOK_URL": _form_text(form, "wecom_webhook_url"),
        "TRANSLATION_PROVIDER": _form_text(form, "translation_provider"),
        "TRANSLATION_BASE_URL": _form_text(form, "translation_base_url"),
        "TRANSLATION_API_KEY": _form_text(form, "translation_api_key"),
    }
    _update_env_file({key: value for key, value in updates.items() if value is not None})
    _clear_config_caches()
    return {"ok": True, "message": "配置已保存。"}


@router.post("/api/config/scoring")
async def api_update_scoring(request: Request):
    form = await _read_urlencoded_form(request)
    config_data = _load_config_document()
    scoring = config_data.setdefault("scoring", {})
    scoring["push_score_threshold"] = _form_float(form, "push_score_threshold", 70.0, minimum=0, maximum=100)
    source_scope = _form_text(form, "push_source_scope") or scoring.get("push_source_scope") or "all"
    scoring["push_source_scope"] = source_scope if source_scope in {"all", "domestic", "foreign"} else "all"
    scoring["duplicate_push_window_hours"] = _form_int(
        form,
        "duplicate_push_window_hours",
        default=int(scoring.get("duplicate_push_window_hours") or 12),
        minimum=1,
    )
    scoring["score_delta_for_repush"] = _form_float(form, "score_delta_for_repush", 15.0, minimum=0, maximum=100)
    weights = scoring.setdefault("weights", {})
    for key in SCORING_WEIGHT_LABELS:
        weights[key] = _form_float(form, f"weight_{key}", ScoringConfig().weights[key], minimum=0, maximum=1)
    scheduler = config_data.setdefault("scheduler", {})
    scheduler["enabled"] = _form_bool(form, "scheduler_enabled")
    scheduler_mode = _form_text(form, "scheduler_mode") or scheduler.get("mode") or "interval"
    scheduler["mode"] = scheduler_mode if scheduler_mode in {"interval", "noon", "pre_open"} else "interval"
    scheduler["interval_minutes"] = _form_int(
        form,
        "interval_minutes",
        default=int(scheduler.get("interval_minutes") or 30),
        minimum=5,
        maximum=1440,
    )
    scheduler["timezone"] = _form_text(form, "scheduler_timezone") or scheduler.get("timezone") or "Asia/Shanghai"
    noon_hour, noon_minute = _form_time_parts(form, "noon_time", scheduler.get("noon_hour", 12), scheduler.get("noon_minute", 0))
    pre_open_hour, pre_open_minute = _form_time_parts(
        form,
        "pre_open_time",
        scheduler.get("pre_open_hour", 9),
        scheduler.get("pre_open_minute", 0),
    )
    scheduler["noon_hour"] = noon_hour
    scheduler["noon_minute"] = noon_minute
    scheduler["pre_open_hour"] = pre_open_hour
    scheduler["pre_open_minute"] = pre_open_minute
    _write_config_document(config_data)
    _clear_config_caches()
    return {"ok": True, "message": "评分与推送配置已保存，重启服务后自动推送时间生效。"}


@router.post("/api/config/scoring/defaults")
async def api_reset_scoring_defaults():
    config_data = _load_config_document()
    scoring = config_data.setdefault("scoring", {})
    default = ScoringConfig()
    scoring["push_score_threshold"] = default.push_score_threshold
    scoring["duplicate_push_window_hours"] = default.duplicate_push_window_hours
    scoring["score_delta_for_repush"] = default.score_delta_for_repush
    scoring["push_source_scope"] = default.push_source_scope
    scoring["weights"] = dict(default.weights)
    _write_config_document(config_data)
    _clear_config_caches()
    return {"ok": True, "message": "已恢复默认评分配置。", "scoring": scoring}


@router.post("/api/config/runtime")
async def api_update_runtime(request: Request):
    form = await _read_urlencoded_form(request)
    config_data = _load_config_document()
    runtime = config_data.setdefault("runtime", {})
    scheduler = config_data.setdefault("scheduler", {})
    runtime["default_time_window"] = _form_text(form, "default_time_window") or runtime.get("default_time_window") or "24h"
    runtime["request_timeout_seconds"] = _form_float(
        form,
        "request_timeout_seconds",
        float(runtime.get("request_timeout_seconds") or 20),
        minimum=3,
        maximum=120,
    )
    runtime["source_fetch_timeout_seconds"] = _form_float(
        form,
        "source_fetch_timeout_seconds",
        float(runtime.get("source_fetch_timeout_seconds") or 6),
        minimum=1,
        maximum=60,
    )
    runtime["max_clusters_per_run"] = _form_int(
        form,
        "max_clusters_per_run",
        default=int(runtime.get("max_clusters_per_run") or 40),
        minimum=1,
    )
    runtime["max_llm_analysis_per_run"] = _form_int(
        form,
        "max_llm_analysis_per_run",
        default=int(runtime.get("max_llm_analysis_per_run") or 5),
        minimum=0,
    )
    runtime["max_concurrent_llm_analysis"] = _form_int(
        form,
        "max_concurrent_llm_analysis",
        default=int(runtime.get("max_concurrent_llm_analysis") or 10),
        minimum=1,
        maximum=10,
    )
    runtime["backfill_unanalyzed_clusters"] = _form_bool(form, "backfill_unanalyzed_clusters")
    scheduler["enabled"] = _form_bool(form, "scheduler_enabled")
    scheduler["interval_minutes"] = _form_int(
        form,
        "interval_minutes",
        default=int(scheduler.get("interval_minutes") or 30),
        minimum=1,
    )
    _write_config_document(config_data)
    _clear_config_caches()
    return {"ok": True, "message": "运行配置已保存。"}


@router.post("/api/config/source/{source_name}")
async def api_update_source(source_name: str, request: Request):
    if source_name not in {"gdelt", "rss", "sec_edgar", "newsapi", "alphavantage", "china_sites"}:
        raise HTTPException(status_code=400, detail="This source cannot be edited from the UI.")

    form = await _read_urlencoded_form(request)
    config_data = _load_config_document()
    sources = config_data.setdefault("sources", {})
    source = sources.setdefault(source_name, deepcopy(_example_source_config(source_name)))
    source["enabled"] = _form_bool(form, "enabled")
    source["max_results"] = _form_int(form, "max_results", default=int(source.get("max_results") or 50), minimum=1)

    if source_name == "newsapi":
        source["keywords"] = _split_lines(_form_text(form, "keywords") or "")
        extra = source.setdefault("extra", {})
        language = _form_text(form, "language")
        if language:
            extra["language"] = language
        api_key = _form_text(form, "newsapi_api_key")
        if api_key:
            _update_env_file({"NEWSAPI_API_KEY": api_key})
    elif source_name == "alphavantage":
        source["tickers"] = _split_csv(_form_text(form, "tickers") or "")
        extra = source.setdefault("extra", {})
        topics = _split_csv(_form_text(form, "topics") or "")
        if topics:
            extra["topics"] = topics
        api_key = _form_text(form, "alpha_vantage_api_key")
        if api_key:
            _update_env_file({"ALPHA_VANTAGE_API_KEY": api_key})
    elif source_name == "gdelt":
        source["keywords"] = _split_lines(_form_text(form, "keywords") or "")
    elif source_name == "sec_edgar":
        source["forms"] = _split_csv(_form_text(form, "forms") or "")
    elif source_name == "rss":
        for index, feed in enumerate(source.get("feeds") or []):
            if isinstance(feed, dict):
                feed["enabled"] = _form_bool(form, f"feed_{index}")
    elif source_name == "china_sites":
        extra = source.setdefault("extra", {})
        sites = extra.setdefault("sites", [])
        for index, site in enumerate(sites):
            if isinstance(site, dict):
                site["enabled"] = _form_bool(form, f"site_{index}")
        new_name = _form_text(form, "new_site_name")
        new_url = _form_text(form, "new_site_url")
        if new_name and new_url:
            if not new_url.startswith(("http://", "https://")):
                raise HTTPException(status_code=400, detail="新闻源地址必须以 http:// 或 https:// 开头。")
            if not any(isinstance(site, dict) and site.get("url") == new_url for site in sites):
                sites.append({"name": new_name, "url": new_url, "enabled": True})

    _write_config_document(config_data)
    _clear_config_caches()
    return {"ok": True, "message": f"{source_name} 数据源配置已保存。"}


@router.post("/api/config/test/{service_name}")
async def api_test_integration(service_name: str, request: Request):
    form = await _read_urlencoded_form(request)
    provided_updates: dict[str, str] = {}
    try:
        if service_name == "newsapi":
            api_key = _form_text(form, "api_key")
            if api_key:
                provided_updates["NEWSAPI_KEY"] = api_key
            result = await _test_newsapi(api_key or get_settings().newsapi_api_key)
        elif service_name == "alphavantage":
            api_key = _form_text(form, "api_key")
            if api_key:
                provided_updates["ALPHAVANTAGE_API_KEY"] = api_key
            result = await _test_alphavantage(api_key or get_settings().alpha_vantage_api_key)
        elif service_name == "llm":
            base_url = _form_text(form, "base_url")
            api_key = _form_text(form, "api_key")
            model = _form_text(form, "model")
            if base_url:
                provided_updates["LLM_BASE_URL"] = base_url
            if api_key:
                provided_updates["LLM_API_KEY"] = api_key
            if model:
                provided_updates["LLM_MODEL"] = model
            result = await _test_llm(
                base_url=base_url or get_settings().llm_base_url,
                api_key=api_key or get_settings().llm_api_key,
                model=model or get_settings().llm_model,
            )
        elif service_name == "wecom":
            webhook_url = _form_text(form, "webhook_url")
            if webhook_url:
                provided_updates["WECOM_WEBHOOK_URL"] = webhook_url
            result = await _test_wecom(webhook_url or get_settings().wecom_webhook_url)
        elif service_name == "translation":
            result = await _test_translation(
                provider=_form_text(form, "provider") or get_settings().translation_provider,
                base_url=_form_text(form, "base_url") or get_settings().translation_base_url,
                api_key=_form_text(form, "api_key") or get_settings().translation_api_key,
            )
        else:
            raise HTTPException(status_code=404, detail="Unknown integration.")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "message": _safe_error(exc)}
    if result.get("ok") and provided_updates:
        _update_env_file(provided_updates)
        _clear_config_caches()
        result["saved"] = True
    return JSONResponse(result)


@router.get("/api/events")
def api_events(
    window: str = Query(default="24h"),
    sort: str = Query(default="impact_score"),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    rows = _event_rows(db, window=window, limit=limit, sort=sort)
    return [
        {
            "cluster": EventClusterRead.model_validate(row["cluster"]).model_dump(mode="json"),
            "analysis": MarketImpactAnalysisRead.model_validate(row["analysis"]).model_dump(mode="json")
            if row["analysis"]
            else None,
        }
        for row in rows
    ]


@router.get("/api/events/{cluster_id}")
def api_event_detail(cluster_id: int, db: Session = Depends(get_db)):
    cluster = db.get(EventCluster, cluster_id)
    if not cluster:
        raise HTTPException(status_code=404, detail="Event cluster not found.")
    analysis = _latest_analysis(db, cluster_id)
    articles = list(db.execute(select(Article).where(Article.event_cluster_id == cluster_id)).scalars())
    return {
        "cluster": EventClusterRead.model_validate(cluster).model_dump(mode="json"),
        "analysis": MarketImpactAnalysisRead.model_validate(analysis).model_dump(mode="json") if analysis else None,
        "articles": [
            {
                "id": article.id,
                "source": article.source,
                "source_type": article.source_type,
                "title": article.title,
                "url": article.url,
                "published_at": article.published_at,
                "language": article.language,
                "content_snippet": article.content_snippet,
            }
            for article in articles
        ],
    }


@router.post("/api/ingest/run")
async def api_run_ingest(
    background_tasks: BackgroundTasks,
    window: str = Query(default="24h"),
    defer_llm: bool = Query(default=True),
):
    try:
        result = await run_ingest_once(time_window=window, defer_llm=defer_llm)
        if result.background_analysis_started and result.background_analysis_count > 0:
            background_tasks.add_task(
                run_deferred_llm_analysis,
                time_window=window,
                limit=result.background_analysis_count,
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Manual ingest failed")
        result = IngestResult(errors=[f"扫描流程异常：{_safe_error(exc)}"])
    return asdict(result)


def _event_rows(db: Session, window: str, limit: int, sort: str = "impact_score") -> list[dict]:
    app_config = get_app_config()
    source_conditions = _enabled_article_source_conditions(app_config)
    if not source_conditions:
        return []
    since, _ = window_bounds(window)
    latest_analysis_subquery = (
        select(
            MarketImpactAnalysis.event_cluster_id.label("cluster_id"),
            func.max(MarketImpactAnalysis.id).label("latest_analysis_id"),
        )
        .group_by(MarketImpactAnalysis.event_cluster_id)
        .subquery()
    )
    order_by = (
        [desc(EventCluster.last_seen_at), desc(MarketImpactAnalysis.market_impact_score)]
        if _normalize_sort(sort) == "time"
        else [desc(MarketImpactAnalysis.market_impact_score), desc(EventCluster.last_seen_at)]
    )
    active_source_clusters = (
        select(Article.event_cluster_id.label("cluster_id"))
        .where(Article.event_cluster_id.is_not(None), or_(*source_conditions))
        .distinct()
        .subquery()
    )
    rows = db.execute(
        select(EventCluster, MarketImpactAnalysis)
        .join(MarketImpactAnalysis, MarketImpactAnalysis.event_cluster_id == EventCluster.id)
        .join(
            latest_analysis_subquery,
            and_(
                latest_analysis_subquery.c.cluster_id == MarketImpactAnalysis.event_cluster_id,
                latest_analysis_subquery.c.latest_analysis_id == MarketImpactAnalysis.id,
            ),
        )
        .join(active_source_clusters, active_source_clusters.c.cluster_id == EventCluster.id)
        .where(
            EventCluster.last_seen_at >= since,
            MarketImpactAnalysis.market_impact_score >= MIN_DASHBOARD_IMPACT_SCORE,
        )
        .order_by(*order_by)
        .limit(limit)
    ).all()
    output = [
        {"cluster": cluster, "analysis": analysis}
        for cluster, analysis in rows
        if analysis.market_impact_score >= MIN_DASHBOARD_IMPACT_SCORE
    ]
    _attach_top_source_links(db, output)
    return output


def _event_row_by_cluster_id(db: Session, cluster_id: int) -> dict | None:
    app_config = get_app_config()
    source_conditions = _enabled_article_source_conditions(app_config)
    if not source_conditions:
        return None
    cluster = db.get(EventCluster, cluster_id)
    analysis = _latest_analysis(db, cluster_id)
    if cluster is None or analysis is None:
        return None
    if analysis.market_impact_score < MIN_DASHBOARD_IMPACT_SCORE:
        return None
    has_enabled_source = db.execute(
        select(Article.id)
        .where(Article.event_cluster_id == cluster_id, or_(*source_conditions))
        .limit(1)
    ).scalar_one_or_none()
    if has_enabled_source is None:
        return None
    rows = [{"cluster": cluster, "analysis": analysis}]
    _attach_top_source_links(db, rows)
    return rows[0]


def _render_event_card_html(request: Request, db: Session, cluster_id: int) -> str:
    row = _event_row_by_cluster_id(db, cluster_id)
    if row is None:
        return ""
    app_config = get_app_config()
    return templates.get_template("_event_cards.html").render(
        request=request,
        rows=_with_event_views([row]),
        stats=_dashboard_stats([row], app_config),
    )


def _dashboard_stats_for_window(db: Session, window: str, sort: str = "impact_score") -> dict:
    app_config = get_app_config()
    rows = _event_rows(db, window=window, limit=100, sort=sort)
    return _dashboard_stats(rows, app_config)


def _enabled_article_source_conditions(app_config: AppYamlConfig):
    conditions = []
    gdelt = app_config.sources.get("gdelt")
    if gdelt and gdelt.enabled:
        conditions.append(Article.source_type == "global_news")
    newsapi = app_config.sources.get("newsapi")
    if newsapi and newsapi.enabled:
        conditions.append(Article.source_type == "newsapi")
    alphavantage = app_config.sources.get("alphavantage")
    if alphavantage and alphavantage.enabled:
        conditions.append(Article.source_type == "market_news_sentiment")
    sec_edgar = app_config.sources.get("sec_edgar")
    if sec_edgar and sec_edgar.enabled:
        conditions.append(Article.source_type == "regulatory_filing")

    china_sites = app_config.sources.get("china_sites")
    if china_sites and china_sites.enabled:
        sites = (china_sites.extra or {}).get("sites") or []
        site_names = [site.get("name") for site in sites if isinstance(site, dict) and site.get("enabled", True)]
        if site_names:
            conditions.append(and_(Article.source_type == "china_site", Article.source.in_(site_names)))
        else:
            conditions.append(Article.source_type == "china_site")

    rss = app_config.sources.get("rss")
    if rss and rss.enabled:
        feed_names = [feed.name for feed in rss.feeds if feed.enabled]
        if feed_names:
            conditions.append(Article.source.in_(feed_names))
    return conditions


def _with_event_views(rows: list[dict]) -> list[dict]:
    llm_configured = get_settings().llm_enabled
    return [
        {
            "cluster": row["cluster"],
            "analysis": row["analysis"],
            "view": event_view(row["cluster"], row["analysis"], llm_configured=llm_configured),
            "top_source_url": row.get("top_source_url"),
            "top_source_title": row.get("top_source_title"),
            "top_source_published_at": _format_display_time(row.get("top_source_published_at")),
            "top_source_fetched_at": _format_display_time(row.get("top_source_fetched_at")),
            "source_labels": row.get("source_labels") or [],
        }
        for row in rows
    ]


def _format_display_time(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    text = str(value)
    if "." in text:
        text = text.split(".", 1)[0]
    return text[:19] if len(text) >= 19 else text


def _attach_top_source_links(db: Session, rows: list[dict]) -> None:
    cluster_ids = [row["cluster"].id for row in rows if row.get("cluster")]
    if not cluster_ids:
        return
    articles = list(
        db.execute(
            select(Article)
            .where(Article.event_cluster_id.in_(cluster_ids))
            .order_by(desc(Article.published_at), desc(Article.fetched_at))
        ).scalars()
    )
    grouped: dict[int, dict[tuple[str, str], dict[str, Any]]] = {}
    for article in articles:
        if article.event_cluster_id is None:
            continue
        key = (article.source, article.source_type)
        source_group = grouped.setdefault(article.event_cluster_id, {})
        item = source_group.setdefault(
            key,
            {
                "name": article.source,
                "type": article.source_type,
                "label": _source_type_label(article.source_type),
                "count": 0,
                "is_domestic": _is_domestic_source(article),
            },
        )
        item["count"] += 1
    top_seen: set[int] = set()
    for article in articles:
        if article.event_cluster_id in top_seen:
            continue
        top_seen.add(article.event_cluster_id)
        for row in rows:
            if row["cluster"].id == article.event_cluster_id:
                row["top_source_url"] = article.url
                row["top_source_title"] = article.title
                row["top_source_published_at"] = article.published_at
                row["top_source_fetched_at"] = article.fetched_at
                labels = list((grouped.get(article.event_cluster_id) or {}).values())
                labels.sort(key=lambda item: (not item["is_domestic"], -item["count"], item["name"]))
                row["source_labels"] = labels[:4]
                break


def _source_type_label(source_type: str | None) -> str:
    labels = {
        "china_site": "国内新闻",
        "china_finance": "国内财经",
        "china_regulator": "监管公告",
        "china_exchange": "交易所",
        "regulatory_filing": "公司公告",
        "central_bank": "央行",
        "exchange": "交易所",
        "global_news": "全球新闻",
        "rss": "RSS",
    }
    return labels.get((source_type or "").strip().lower(), source_type or "来源")


def _is_domestic_source(article: Article) -> bool:
    source_type = (article.source_type or "").lower()
    if source_type.startswith("china_") or source_type in {"china_site", "china_finance", "china_regulator", "china_exchange"}:
        return True
    source_text = f"{article.source} {article.url}".lower()
    return any(domain in source_text for domain in (".cn", "sina.com.cn", "eastmoney.com", "cls.cn", "yicai.com"))


async def _ensure_title_translations(db: Session, rows: list[dict]) -> None:
    settings = get_settings()
    if not settings.title_translation_enabled:
        return
    if settings.translation_provider.strip().lower() in {"llm", "model"}:
        return
    candidates = [
        row
        for row in rows[:5]
        if row.get("cluster") and row.get("analysis") and not _analysis_has_title_translation(row["analysis"])
    ]
    if not candidates:
        return
    translations = await asyncio.gather(
        *(translate_title_online(row["cluster"].title, settings=settings) for row in candidates),
        return_exceptions=True,
    )
    changed = False
    for row, result in zip(candidates, translations, strict=False):
        if isinstance(result, Exception) or result is None:
            continue
        analysis = row.get("analysis")
        if not analysis:
            continue
        raw = dict(analysis.llm_raw_json or {})
        parsed = raw.get("parsed_json")
        parsed = dict(parsed) if isinstance(parsed, dict) else {}
        parsed["event_title_zh"] = result.text
        raw["parsed_json"] = parsed
        raw["title_translation"] = {"provider": result.provider, "source": "online_translation"}
        analysis.llm_raw_json = raw
        changed = True
    if changed:
        db.commit()


def _analysis_has_title_translation(analysis: MarketImpactAnalysis) -> bool:
    raw = analysis.llm_raw_json if isinstance(analysis.llm_raw_json, dict) else {}
    parsed = raw.get("parsed_json")
    if not isinstance(parsed, dict):
        return False
    title = str(parsed.get("event_title_zh") or "")
    return sum(1 for char in title if "\u4e00" <= char <= "\u9fff") >= 3


def _latest_analysis(db: Session, cluster_id: int) -> MarketImpactAnalysis | None:
    return db.execute(
        select(MarketImpactAnalysis)
        .where(MarketImpactAnalysis.event_cluster_id == cluster_id)
        .order_by(desc(MarketImpactAnalysis.created_at), desc(MarketImpactAnalysis.id))
        .limit(1)
    ).scalar_one_or_none()


def _visible_push_records(db: Session, cluster_id: int) -> list[PushRecord]:
    if not get_settings().wecom_webhook_url:
        return []
    return list(
        db.execute(
            select(PushRecord)
            .where(
                PushRecord.event_cluster_id == cluster_id,
                or_(
                    PushRecord.error_message.is_(None),
                    ~PushRecord.error_message.contains("WECOM_WEBHOOK_URL 未配置"),
                ),
            )
            .order_by(desc(PushRecord.pushed_at))
        ).scalars()
    )


def _time_window_options(app_config: AppYamlConfig) -> list[dict[str, str]]:
    labels = {
        "6h": "最近 6 小时",
        "24h": "最近 24 小时",
        "3d": "最近 3 天",
        "7d": "最近 7 天",
    }
    return [{"value": item, "label": labels.get(item, item)} for item in app_config.runtime.allowed_time_windows]


def _sort_options() -> list[dict[str, str]]:
    return [
        {"value": "impact_score", "label": "按影响分数排序"},
        {"value": "time", "label": "按最新时间排序"},
    ]


def _normalize_sort(value: str) -> str:
    return "time" if value == "time" else "impact_score"


def _dashboard_stats(rows: list[dict], app_config: AppYamlConfig) -> dict:
    scores = [row["analysis"].market_impact_score for row in rows if row.get("analysis")]
    high_impact = [score for score in scores if score >= app_config.scoring.push_score_threshold]
    return {
        "event_count": len(rows),
        "high_impact_count": len(high_impact),
        "top_score": max(scores) if scores else 0,
        "push_threshold": app_config.scoring.push_score_threshold,
    }


def _default_llm_runtime_status(settings) -> dict[str, str]:
    if not settings.llm_enabled:
        return {"level": "muted", "label": "未配置", "message": "未启用 API 分析。"}
    return {"level": "warn", "label": "已配置，待验证", "message": "请点击测试或运行扫描确认接口可用。"}


def _latest_llm_runtime_status(db: Session, settings) -> dict[str, str]:
    if not settings.llm_enabled:
        return _default_llm_runtime_status(settings)
    analysis = db.execute(select(MarketImpactAnalysis).order_by(desc(MarketImpactAnalysis.created_at)).limit(1)).scalar_one_or_none()
    if analysis is None:
        return _default_llm_runtime_status(settings)
    model_name = str(analysis.model_name or "")
    if model_name == "rules-fallback":
        raw = analysis.llm_raw_json if isinstance(analysis.llm_raw_json, dict) else {}
        return {
            "level": "fail",
            "label": "最近失败",
            "message": _friendly_runtime_error(str(raw.get("error") or "")),
        }
    if model_name == "rules-only":
        return _default_llm_runtime_status(settings)
    return {
        "level": "ok",
        "label": "已配置",
        "message": "最近有事件完成 API 分析。",
    }


def _settings_view(app_config: AppYamlConfig, db: Session | None = None) -> dict:
    settings = get_settings()
    llm_runtime_status = _latest_llm_runtime_status(db, settings) if db is not None else _default_llm_runtime_status(settings)
    newsapi = app_config.sources.get("newsapi")
    alphavantage = app_config.sources.get("alphavantage")
    gdelt = app_config.sources.get("gdelt")
    sec_edgar = app_config.sources.get("sec_edgar")
    china_sites = app_config.sources.get("china_sites") or SourceBlockConfig.model_validate(
        _example_source_config("china_sites")
    )
    rss = app_config.sources.get("rss")
    default_scoring = ScoringConfig()
    return {
        "runtime": {
            "default_time_window": app_config.runtime.default_time_window,
            "request_timeout_seconds": app_config.runtime.request_timeout_seconds,
            "source_fetch_timeout_seconds": app_config.runtime.source_fetch_timeout_seconds,
            "max_clusters_per_run": app_config.runtime.max_clusters_per_run,
            "max_llm_analysis_per_run": app_config.runtime.max_llm_analysis_per_run,
            "max_concurrent_llm_analysis": app_config.runtime.max_concurrent_llm_analysis,
            "backfill_unanalyzed_clusters": app_config.runtime.backfill_unanalyzed_clusters,
            "scheduler_enabled": app_config.scheduler.enabled,
            "interval_minutes": app_config.scheduler.interval_minutes,
            "database_label": _database_label(settings.database_url),
            "window_options": _time_window_options(app_config),
            "rows": [
                ("默认窗口", _time_window_label(app_config.runtime.default_time_window)),
                ("可选窗口", " / ".join(option["label"] for option in _time_window_options(app_config))),
                ("自动扫描", f"每 {app_config.scheduler.interval_minutes} 分钟" if app_config.scheduler.enabled else "关闭"),
                ("请求超时", f"{app_config.runtime.request_timeout_seconds:g} 秒"),
                ("数据源超时", f"{app_config.runtime.source_fetch_timeout_seconds:g} 秒"),
                ("单次最多分析", f"{app_config.runtime.max_clusters_per_run} 个事件"),
                ("单次最多 API 分析", f"{app_config.runtime.max_llm_analysis_per_run} 个事件"),
                ("API 分析并发数", f"{app_config.runtime.max_concurrent_llm_analysis} 个"),
                ("数据库", _database_label(settings.database_url)),
            ],
        },
        "scoring": {
            "push_score_threshold": app_config.scoring.push_score_threshold,
            "duplicate_push_window_hours": app_config.scoring.duplicate_push_window_hours,
            "score_delta_for_repush": app_config.scoring.score_delta_for_repush,
            "push_source_scope": app_config.scoring.push_source_scope,
            "push_source_scope_options": [
                {"value": "all", "label": "国内和国外都推送"},
                {"value": "domestic", "label": "只推送国内来源"},
                {"value": "foreign", "label": "只推送国外来源"},
            ],
            "scheduler_enabled": app_config.scheduler.enabled,
            "scheduler_mode": app_config.scheduler.mode,
            "scheduler_mode_options": [
                {"value": "interval", "label": "每半小时"},
                {"value": "noon", "label": "中午一次"},
                {"value": "pre_open", "label": "开盘前半小时"},
            ],
            "interval_minutes": app_config.scheduler.interval_minutes,
            "scheduler_timezone": app_config.scheduler.timezone,
            "noon_time": _time_input_value(app_config.scheduler.noon_hour, app_config.scheduler.noon_minute),
            "pre_open_time": _time_input_value(app_config.scheduler.pre_open_hour, app_config.scheduler.pre_open_minute),
            "weights": [
                {
                    "key": key,
                    "name": SCORING_WEIGHT_LABELS[key],
                    "help": SCORING_WEIGHT_HELP[key],
                    "value": app_config.scoring.weights.get(key, default_scoring.weights[key]),
                    "default": default_scoring.weights[key],
                }
                for key in SCORING_WEIGHT_LABELS
            ],
        },
        "llm": {
            "configured": settings.llm_enabled,
            "base_url": settings.llm_base_url or "",
            "model": settings.llm_model,
            "key_placeholder": "已配置，留空不修改" if settings.llm_api_key else "粘贴 API Key",
            "status": llm_runtime_status,
        },
        "wecom": {
            "configured": bool(settings.wecom_webhook_url),
            "placeholder": "已配置，留空不修改" if settings.wecom_webhook_url else "粘贴企业微信机器人 Webhook",
        },
        "translation": {
            "enabled": settings.title_translation_enabled,
            "provider": settings.translation_provider.strip().lower(),
            "base_url": settings.translation_base_url or "",
            "key_placeholder": "已配置，留空不修改" if settings.translation_api_key else "可选 API Key",
        },
        "newsapi": {
            "configured": bool(settings.newsapi_api_key),
            "enabled": bool(newsapi and newsapi.enabled),
            "max_results": newsapi.max_results if newsapi else 50,
            "keywords": "\n".join(newsapi.keywords if newsapi else []),
            "language": (newsapi.extra or {}).get("language", "en") if newsapi else "en",
            "key_placeholder": "已配置，留空不修改" if settings.newsapi_api_key else "粘贴 NewsAPI key",
        },
        "alphavantage": {
            "configured": bool(settings.alpha_vantage_api_key),
            "enabled": bool(alphavantage and alphavantage.enabled),
            "max_results": alphavantage.max_results if alphavantage else 100,
            "tickers": ", ".join(alphavantage.tickers if alphavantage else []),
            "topics": ", ".join((alphavantage.extra or {}).get("topics", []) if alphavantage else []),
            "key_placeholder": "已配置，留空不修改" if settings.alpha_vantage_api_key else "粘贴 Alpha Vantage API key",
        },
        "gdelt": {
            "enabled": bool(gdelt and gdelt.enabled),
            "max_results": gdelt.max_results if gdelt else 80,
            "keywords": "\n".join(gdelt.keywords if gdelt else []),
        },
        "sec_edgar": {
            "enabled": bool(sec_edgar and sec_edgar.enabled),
            "max_results": sec_edgar.max_results if sec_edgar else 60,
            "forms": ", ".join(sec_edgar.forms if sec_edgar else []),
        },
        "rss": {
            "enabled": bool(rss and rss.enabled),
            "max_results": rss.max_results if rss else 120,
            "feeds": _rss_feed_options(rss),
        },
        "china_sites": {
            "enabled": bool(china_sites and china_sites.enabled),
            "max_results": china_sites.max_results if china_sites else 100,
            "sites": _china_site_options(china_sites),
        },
        "sources": {
            "enabled_count": len([source for source in app_config.sources.values() if source.enabled]),
            "cards": [_source_card(name, source) for name, source in app_config.sources.items()],
        },
    }


def _rss_feed_options(rss_source: SourceBlockConfig | None) -> list[dict[str, Any]]:
    if not rss_source:
        return []
    options = []
    for index, feed in enumerate(rss_source.feeds):
        is_china = feed.language == "zh" or feed.source_type in {"china_finance", "china_regulator", "china_exchange"}
        options.append(
            {
                "index": index,
                "name": feed.name,
                "url": feed.url,
                "source_type": feed.source_type,
                "language": feed.language or "",
                "enabled": feed.enabled,
                "is_china": is_china,
            }
        )
    return sorted(options, key=lambda item: (not item["is_china"], item["name"]))


def _china_site_options(source: SourceBlockConfig | None) -> list[dict[str, Any]]:
    sites = ((source.extra or {}).get("sites") if source else []) or []
    options = []
    for index, site in enumerate(sites):
        if not isinstance(site, dict):
            continue
        options.append(
            {
                "index": index,
                "name": site.get("name", "国内新闻网站"),
                "url": site.get("url", ""),
                "enabled": bool(site.get("enabled", True)),
            }
        )
    return options


def _source_card(name: str, source) -> dict:
    readable = {
        "gdelt": ("GDELT", "全球新闻广覆盖，默认关键词已偏向 A 股、人民币、政策、地产、半导体和新能源。"),
        "rss": ("RSS", "中文财经媒体、全球宏观媒体、央行和交易所订阅。"),
        "china_sites": ("国内新闻网站", "证券时报、中国证券报、财联社、第一财经、东方财富等国内财经与监管网站。"),
        "sec_edgar": ("SEC EDGAR", "美股公司公告，例如 8-K、10-Q、10-K、6-K、20-F。"),
        "newsapi": ("NewsAPI", "关键词和媒体检索，需要 API key，可在本页保存并测试。"),
        "alphavantage": ("Alpha Vantage", "Ticker 相关新闻和情绪数据，需要 API key，可在本页保存并测试。"),
    }
    title, description = readable.get(name.lower(), (name, "自定义数据源。"))
    details = []
    if source.keywords:
        details.append(f"{len(source.keywords)} 个关键词")
    if source.feeds:
        details.append(f"{len([feed for feed in source.feeds if feed.enabled])} 个 RSS")
    if source.forms:
        details.append(f"{len(source.forms)} 类公告")
    if source.tickers:
        details.append(f"{len(source.tickers)} 个 ticker")
    sites = (source.extra or {}).get("sites") or []
    if sites:
        details.append(f"{len([site for site in sites if site.get('enabled', True)])} 个网站")
    details.append(f"最多 {source.max_results} 条")
    return {
        "key": name,
        "title": title,
        "description": description,
        "enabled": source.enabled,
        "details": " · ".join(details),
    }


def _time_window_label(value: str) -> str:
    return {
        "6h": "最近 6 小时",
        "24h": "最近 24 小时",
        "3d": "最近 3 天",
        "7d": "最近 7 天",
    }.get(value, value)


def _database_label(database_url: str) -> str:
    if database_url.startswith("sqlite"):
        return "SQLite 本地数据库"
    if "@" in database_url:
        return "外部数据库（连接信息已隐藏）"
    return database_url


def _form_text(form: Any, key: str) -> str | None:
    value = form.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


async def _read_urlencoded_form(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8")
    parsed = parse_qs(body, keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items()}


def _form_bool(form: Any, key: str) -> bool:
    return str(form.get(key, "")).lower() in {"1", "true", "on", "yes"}


def _form_int(form: Any, key: str, default: int, minimum: int = 0, maximum: int | None = None) -> int:
    try:
        value = max(int(str(form.get(key, default)).strip()), minimum)
        return min(value, maximum) if maximum is not None else value
    except (TypeError, ValueError):
        return default


def _form_float(form: Any, key: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(str(form.get(key, default)).strip())
    except (TypeError, ValueError):
        value = default
    return min(max(value, minimum), maximum)


def _form_time_parts(form: Any, key: str, default_hour: Any, default_minute: Any) -> tuple[int, int]:
    value = _form_text(form, key)
    try:
        fallback_hour = min(max(int(default_hour), 0), 23)
        fallback_minute = min(max(int(default_minute), 0), 59)
    except (TypeError, ValueError):
        fallback_hour, fallback_minute = 0, 0
    if not value or ":" not in value:
        return fallback_hour, fallback_minute
    hour_text, minute_text = value.split(":", 1)
    try:
        hour = min(max(int(hour_text), 0), 23)
        minute = min(max(int(minute_text), 0), 59)
    except ValueError:
        return fallback_hour, fallback_minute
    return hour, minute


def _time_input_value(hour: Any, minute: Any) -> str:
    try:
        return f"{min(max(int(hour), 0), 23):02d}:{min(max(int(minute), 0), 59):02d}"
    except (TypeError, ValueError):
        return "00:00"


def _split_lines(value: str) -> list[str]:
    return [item.strip() for item in value.replace(",", "\n").splitlines() if item.strip()]


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _load_config_document() -> dict[str, Any]:
    path = CONFIG_PATH if CONFIG_PATH.exists() else CONFIG_EXAMPLE_PATH
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}
    if path != CONFIG_EXAMPLE_PATH and CONFIG_EXAMPLE_PATH.exists():
        with CONFIG_EXAMPLE_PATH.open("r", encoding="utf-8") as file:
            defaults = yaml.safe_load(file) or {}
        return _deep_merge(defaults, data)
    return data


def _example_source_config(source_name: str) -> dict[str, Any]:
    if not CONFIG_EXAMPLE_PATH.exists():
        return {}
    with CONFIG_EXAMPLE_PATH.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}
    source = ((data.get("sources") or {}).get(source_name)) or {}
    return deepcopy(source)


def _write_config_document(data: dict[str, Any]) -> None:
    with CONFIG_PATH.open("w", encoding="utf-8") as file:
        yaml.safe_dump(data, file, allow_unicode=True, sort_keys=False)


def _update_env_file(updates: dict[str, str]) -> None:
    existing_lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    seen: set[str] = set()
    output: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            output.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in updates:
            output.append(f"{key}={_format_env_value(updates[key])}")
            seen.add(key)
        else:
            output.append(line)
    for key, value in updates.items():
        if key not in seen:
            output.append(f"{key}={_format_env_value(value)}")
    ENV_PATH.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


def _format_env_value(value: str) -> str:
    text = str(value)
    if not text:
        return ""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _friendly_runtime_error(error: str) -> str:
    lowered = (error or "").lower()
    if "401" in lowered or "unauthorized" in lowered:
        return "鉴权失败，请检查 API Key、Base URL 和模型权限。"
    if "403" in lowered or "forbidden" in lowered:
        return "无访问权限，请检查 API Key 或模型权限。"
    if "404" in lowered or "not found" in lowered:
        return "接口地址或模型名称可能不正确。"
    if "429" in lowered or "quota" in lowered or "rate" in lowered:
        return "接口限流或额度不足。"
    if "timeout" in lowered or "connect" in lowered:
        return "接口连接失败或超时。"
    return "接口返回异常，请重新测试配置。"


def _clear_config_caches() -> None:
    get_settings.cache_clear()
    get_app_config.cache_clear()


async def _test_newsapi(api_key: str | None) -> dict[str, Any]:
    if not api_key:
        return {"ok": False, "message": "请先填写 NewsAPI key。"}
    params = {"q": "stock market", "pageSize": 1, "sortBy": "publishedAt", "apiKey": api_key}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get("https://newsapi.org/v2/everything", params=params)
    except httpx.HTTPError as exc:
        return {"ok": False, "message": f"网络请求失败：{exc.__class__.__name__}"}
    payload = _json_or_empty(response)
    if response.status_code >= 400 or payload.get("status") == "error":
        return {"ok": False, "message": f"NewsAPI 测试失败：{payload.get('message') or response.status_code}"}
    return {"ok": True, "message": f"NewsAPI 可用，返回 totalResults={payload.get('totalResults', 0)}。"}


async def _test_alphavantage(api_key: str | None) -> dict[str, Any]:
    if not api_key:
        return {"ok": False, "message": "请先填写 Alpha Vantage API key。"}
    params = {"function": "NEWS_SENTIMENT", "tickers": "SPY", "limit": 1, "apikey": api_key}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get("https://www.alphavantage.co/query", params=params)
    except httpx.HTTPError as exc:
        return {"ok": False, "message": f"网络请求失败：{exc.__class__.__name__}"}
    payload = _json_or_empty(response)
    error = payload.get("Error Message") or payload.get("Information") or payload.get("Note")
    if response.status_code >= 400 or error:
        return {"ok": False, "message": f"Alpha Vantage 测试失败：{error or response.status_code}"}
    return {"ok": True, "message": f"Alpha Vantage 可用，返回 {len(payload.get('feed', []))} 条测试新闻。"}


async def _test_llm(base_url: str | None, api_key: str | None, model: str | None) -> dict[str, Any]:
    if not base_url or not api_key or not model:
        return {"ok": False, "message": "请填写 LLM_BASE_URL、LLM_API_KEY 和 LLM_MODEL。"}
    url = f"{base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with OK only."}],
        "max_tokens": 8,
        "temperature": 0,
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(url, headers={"Authorization": f"Bearer {api_key}"}, json=payload)
    except httpx.HTTPError as exc:
        return {"ok": False, "message": f"网络请求失败：{exc.__class__.__name__}"}
    if response.status_code >= 400:
        return {"ok": False, "message": f"LLM 测试失败：HTTP {response.status_code}"}
    return {"ok": True, "message": f"LLM 接口可用，模型 {model} 响应成功。"}


async def _test_wecom(webhook_url: str | None) -> dict[str, Any]:
    if not webhook_url:
        return {"ok": False, "message": "请先填写 WECOM_WEBHOOK_URL。"}
    content = (
        "# 全球市场新闻雷达\n\n"
        "> 连接状态：<font color=\"info\">企业微信机器人可用</font>\n"
        "> 消息类型：<font color=\"comment\">markdown</font>\n\n"
        "**测试消息**\n"
        "这是一条连接测试，用于确认后续高影响市场事件可以推送到当前群。\n\n"
        "> 免责声明：本消息由 AI 自动整理，仅用于新闻监测和研究辅助，不构成任何投资建议、买卖建议或收益承诺。"
    )
    payload = {
        "msgtype": "markdown",
        "markdown": {"content": content},
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(webhook_url, json=payload)
    except httpx.HTTPError as exc:
        return {"ok": False, "message": f"网络请求失败：{exc.__class__.__name__}"}
    data = _json_or_empty(response)
    if response.status_code >= 400 or data.get("errcode") not in (None, 0):
        return {"ok": False, "message": f"企业微信测试失败：{data.get('errmsg') or response.status_code}"}
    return {"ok": True, "message": "企业微信机器人可用，测试消息已发送。"}


async def _test_translation(provider: str | None, base_url: str | None, api_key: str | None) -> dict[str, Any]:
    provider = (provider or "llm").strip()
    if provider.lower() in {"disabled", "none", "off", ""}:
        return {"ok": False, "message": "标题翻译已关闭。"}
    settings = get_settings().model_copy(
        update={
            "translation_provider": provider,
            "translation_base_url": base_url,
            "translation_api_key": api_key,
        }
    )
    result = await translate_title_online("US stocks rise as Fed rate-cut expectations improve", settings=settings)
    if result is None:
        return {"ok": False, "message": "标题翻译测试失败，请检查翻译服务配置或网络连接。"}
    return {"ok": True, "message": f"标题翻译可用：{result.text}"}


def _json_or_empty(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _safe_error(exc: Exception) -> str:
    message = str(exc)
    settings = get_settings()
    for secret in [
        settings.llm_api_key,
        settings.newsapi_api_key,
        settings.alpha_vantage_api_key,
        settings.wecom_webhook_url,
        settings.translation_api_key,
    ]:
        if secret:
            message = message.replace(secret, "[redacted]")
    return message[:300] or exc.__class__.__name__
