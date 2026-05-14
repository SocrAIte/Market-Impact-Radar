import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import AppYamlConfig, RuntimeConfig, Settings
from app.db import Base
from app.models import EventCluster, MarketImpactAnalysis
from app.pipeline.ingest import ClusterAnalysisRunResult, _analyze_cluster_ids_concurrently, _clusters_needing_api_analysis


def _cluster(cluster_id: int, key: str, last_seen_at: datetime) -> EventCluster:
    return EventCluster(
        id=cluster_id,
        cluster_key=key,
        title=f"{key} title",
        first_seen_at=last_seen_at,
        last_seen_at=last_seen_at,
        source_count=1,
        article_count=1,
        status="new",
        is_major_update=False,
    )


def _analysis(cluster_id: int, model_name: str, created_at: datetime) -> MarketImpactAnalysis:
    return MarketImpactAnalysis(
        event_cluster_id=cluster_id,
        one_sentence_summary_zh="测试摘要",
        impact_explanation_zh="测试影响解释，用于判断是否需要重新调用 API 分析。",
        event_type="其他",
        affected_assets_json=[],
        affected_countries_json=[],
        affected_sectors_json=[],
        impact_direction="uncertain",
        impact_horizon="short_term",
        event_severity_score=40,
        market_scope_score=40,
        asset_sensitivity_score=40,
        credibility_score=40,
        novelty_score=40,
        timeliness_score=40,
        confidence_score=40,
        market_impact_score=40,
        confidence_level="low",
        uncertainties_json=[],
        should_push=False,
        push_reason="",
        model_name=model_name,
        llm_raw_json={},
        created_at=created_at,
    )


def test_clusters_needing_api_analysis_includes_rules_only_and_unanalyzed_clusters():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now = datetime(2026, 5, 13, 12, 0, 0)

    with Session(engine) as db:
        db.add_all(
            [
                _cluster(1, "rules", now),
                _cluster(2, "llm", now),
                _cluster(3, "unanalyzed", now),
                _cluster(4, "old", now - timedelta(days=8)),
            ]
        )
        db.flush()
        db.add_all(
            [
                _analysis(1, "rules-only", now),
                _analysis(2, "mimo-v2.5-pro", now),
                _analysis(4, "rules-only", now),
            ]
        )
        db.commit()

        cluster_ids = _clusters_needing_api_analysis(db, now - timedelta(days=1), limit=10)

    assert cluster_ids == {1, 3}


def test_clusters_needing_api_analysis_respects_exclude_list():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now = datetime(2026, 5, 13, 12, 0, 0)

    with Session(engine) as db:
        db.add(_cluster(1, "rules", now))
        db.flush()
        db.add(_analysis(1, "rules-fallback", now))
        db.commit()

        cluster_ids = _clusters_needing_api_analysis(db, now - timedelta(days=1), limit=10, exclude={1})

    assert cluster_ids == set()


@pytest.mark.asyncio
async def test_concurrent_api_analysis_is_limited_to_configured_queue(monkeypatch):
    active = 0
    peak = 0

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, model, cluster_id):
            return object()

        def commit(self):
            return None

    async def fake_analyze_cluster(db, cluster, app_config, settings, allow_push=True):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return ClusterAnalysisRunResult(llm_attempted=True, llm_succeeded=True)

    monkeypatch.setattr("app.pipeline.ingest.SessionLocal", FakeSession)
    monkeypatch.setattr("app.pipeline.ingest._analyze_cluster", fake_analyze_cluster)
    app_config = AppYamlConfig(runtime=RuntimeConfig(max_concurrent_llm_analysis=3, max_llm_analysis_per_run=20))
    settings = Settings(
        database_url="sqlite:///./market_impact_radar.db",
        llm_base_url="https://llm.example/v1",
        llm_api_key="key",
        llm_model="model",
    )

    results = await _analyze_cluster_ids_concurrently(
        cluster_ids=list(range(12)),
        app_config=app_config,
        settings=settings,
        llm_budget=12,
    )

    assert len(results) == 12
    assert peak == 3
    assert all(result and result.llm_succeeded for _, result, exc in results if exc is None)
