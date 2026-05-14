from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import AppYamlConfig
from app.db import Base
from app.models import Article, EventCluster, PushRecord
from app.models import MarketImpactAnalysis
from app.notifier.wecom import (
    WeComNotifier,
    _fit_wecom_markdown,
    _format_message_time,
    format_wecom_message,
    has_new_official_or_tier1_source,
    message_hash,
)
from app.schemas import MarketImpactLLMOutput


def _analysis(score: float = 84, should_push: bool = True) -> MarketImpactLLMOutput:
    return MarketImpactLLMOutput(
        event_type="半导体 / 出口管制",
        one_sentence_summary_zh="美国扩大半导体出口管制，相关供应链可能重新定价。",
        facts=["新闻确认监管部门发布新的出口管制措施。"],
        assumptions=["分析假设限制措施会影响部分半导体供应链预期。"],
        affected_assets=[
            {
                "asset_type": "sector",
                "name": "半导体",
                "ticker": "",
                "impact_direction": "mixed",
                "impact_horizon": "short_term",
                "reason_zh": "出口限制可能影响供应链、订单节奏和风险偏好。",
            },
            {
                "asset_type": "index",
                "name": "Nasdaq",
                "ticker": "IXIC",
                "impact_direction": "mixed",
                "impact_horizon": "short_term",
                "reason_zh": "科技权重板块预期变化可能传导至指数。",
            },
        ],
        affected_countries=["美国", "中国"],
        affected_sectors=["半导体"],
        impact_direction="mixed",
        impact_horizon="short_term",
        event_severity_score=80,
        market_scope_score=85,
        asset_sensitivity_score=90,
        credibility_score=88,
        novelty_score=80,
        timeliness_score=95,
        confidence_score=82,
        market_impact_score=score,
        confidence_level="high",
        impact_explanation_zh="该事件可能通过供应链预期、科技板块估值和风险偏好影响相关股票与指数。",
        uncertainties=["执行细则和企业实际受影响程度仍需核实。"],
        should_push=should_push,
        push_reason="影响路径较清晰，适合推送。",
    )


def _cluster(now: datetime) -> EventCluster:
    return EventCluster(
        cluster_key="x",
        title="US expands semiconductor export controls",
        summary="Semiconductor export control update",
        first_seen_at=now - timedelta(hours=2),
        last_seen_at=now,
        main_source="Reuters",
        source_count=2,
        article_count=2,
        event_type="半导体 / 出口管制",
    )


def _article(cluster_id: int | None, now: datetime, source: str = "Reuters", source_type: str = "rss") -> Article:
    return Article(
        source=source,
        source_type=source_type,
        title="US expands semiconductor export controls",
        url="https://example.com/export-controls",
        published_at=now,
        fetched_at=now,
        content_snippet="Export controls were expanded.",
        raw_json={},
        canonical_url=f"https://example.com/export-controls/{source}",
        title_hash=f"title-{source}",
        content_hash=f"content-{source}",
        event_cluster_id=cluster_id,
    )


def _orm_analysis(cluster_id: int, score: float) -> MarketImpactAnalysis:
    return MarketImpactAnalysis(
        event_cluster_id=cluster_id,
        one_sentence_summary_zh="摘要",
        impact_explanation_zh="影响路径",
        event_type="宏观经济",
        affected_assets_json=[],
        affected_countries_json=[],
        affected_sectors_json=[],
        impact_direction="mixed",
        impact_horizon="short_term",
        event_severity_score=score,
        market_scope_score=score,
        asset_sensitivity_score=score,
        credibility_score=score,
        novelty_score=score,
        timeliness_score=score,
        confidence_score=score,
        market_impact_score=score,
        confidence_level="high",
        uncertainties_json=[],
        should_push=True,
        push_reason="达到推送条件",
        model_name="test-model",
        llm_raw_json={},
    )


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    db = Session()
    try:
        yield db
    finally:
        db.close()


def test_wecom_message_matches_required_format_and_avoids_secret_leakage():
    now = datetime.now(UTC)
    cluster = _cluster(now)
    analysis = _analysis()
    message = format_wecom_message(cluster, analysis, [("Reuters", "https://example.com/export-controls")])

    assert "# 全球市场新闻雷达" in message
    assert '<font color="info">84/100</font>' in message
    assert "半导体 / 出口管制" in message
    assert "**标题**" in message
    assert "**一句话摘要**" in message
    assert "免责声明" in message
    assert "不构成任何投资建议、买卖建议或收益承诺" in message
    assert "[查看原文](https://example.com/export-controls)" in message
    assert len(message.encode("utf-8")) <= 4096
    assert "WECOM_WEBHOOK_URL" not in message
    for forbidden in ["目标价", "收益承诺。预计", "建议买入"]:
        assert forbidden not in message
    assert message_hash(message) == message_hash(message)


def test_wecom_message_is_truncated_to_enterprise_wechat_limit():
    message = _fit_wecom_markdown("# 全球市场新闻雷达\n\n" + "供应链、流动性和风险偏好变化。" * 600)

    assert len(message.encode("utf-8")) <= 4096
    assert "内容较长，已自动精简" in message


def test_wecom_time_format_removes_microseconds():
    value = datetime(2026, 5, 14, 3, 9, 53, 551747, tzinfo=UTC)

    assert _format_message_time(value) == "2026-05-14 03:09:53"


def test_default_threshold_is_70(monkeypatch):
    monkeypatch.setattr("app.notifier.wecom.get_app_config", lambda: AppYamlConfig())
    assert AppYamlConfig().scoring.push_score_threshold == 70
    assert WeComNotifier(webhook_url=None).threshold == 70


def test_can_push_blocks_duplicate_inside_window(db_session):
    now = datetime.now(UTC)
    cluster = _cluster(now)
    db_session.add(cluster)
    db_session.flush()
    article = _article(cluster.id, now - timedelta(hours=2), source="Small Blog")
    db_session.add(article)
    db_session.add(
        PushRecord(
            event_cluster_id=cluster.id,
            channel="wecom",
            pushed_at=now - timedelta(hours=1),
            score_at_push=82,
            message_hash="previous",
            status="success",
        )
    )
    db_session.flush()

    allowed, reason = WeComNotifier(webhook_url=None, threshold=70).can_push(db_session, cluster, _analysis(84), [article])

    assert allowed is False
    assert "Duplicate event" in reason


def test_can_push_allows_score_jump_major_update_or_new_tier1_source(db_session):
    now = datetime.now(UTC)
    cluster = _cluster(now)
    db_session.add(cluster)
    db_session.flush()
    db_session.add(
        PushRecord(
            event_cluster_id=cluster.id,
            channel="wecom",
            pushed_at=now - timedelta(hours=1),
            score_at_push=80,
            message_hash="previous",
            status="success",
        )
    )
    db_session.flush()
    notifier = WeComNotifier(webhook_url=None, threshold=70)

    old_article = _article(cluster.id, now - timedelta(hours=2), source="Small Blog")
    assert notifier.can_push(db_session, cluster, _analysis(96), [old_article])[0] is True

    cluster.is_major_update = True
    assert notifier.can_push(db_session, cluster, _analysis(82), [old_article])[0] is True

    cluster.is_major_update = False
    new_reuters = _article(cluster.id, now, source="Reuters", source_type="rss")
    assert notifier.can_push(db_session, cluster, _analysis(82), [new_reuters])[0] is True
    assert has_new_official_or_tier1_source([new_reuters], since=now - timedelta(minutes=5)) is True


def test_new_tier1_article_from_same_source_does_not_repush(db_session):
    now = datetime.now(UTC)
    old_reuters = _article(None, now - timedelta(hours=2), source="Reuters", source_type="rss")
    new_reuters = _article(None, now, source="Reuters", source_type="rss")

    assert has_new_official_or_tier1_source([old_reuters, new_reuters], since=now - timedelta(hours=1)) is False


@pytest.mark.asyncio
async def test_push_without_webhook_does_not_record_push(db_session):
    now = datetime.now(UTC)
    cluster = _cluster(now)
    db_session.add(cluster)
    db_session.flush()
    article = _article(cluster.id, now)
    db_session.add(article)
    db_session.flush()

    record = await WeComNotifier(webhook_url="", threshold=70).push_event(db_session, cluster, _analysis(84), [article])

    assert record is None
    assert db_session.query(PushRecord).count() == 0


@pytest.mark.asyncio
async def test_push_high_impact_events_sends_highest_score_first(monkeypatch, db_session):
    now = datetime.now(UTC)
    low_cluster = _cluster(now)
    low_cluster.cluster_key = "low"
    low_cluster.title = "Lower impact event"
    high_cluster = _cluster(now)
    high_cluster.cluster_key = "high"
    high_cluster.title = "Higher impact event"
    db_session.add_all([low_cluster, high_cluster])
    db_session.flush()
    db_session.add_all(
        [
            _article(low_cluster.id, now, source="Low Source"),
            _article(high_cluster.id, now, source="High Source"),
            _orm_analysis(low_cluster.id, 75),
            _orm_analysis(high_cluster.id, 95),
        ]
    )
    db_session.flush()
    sent_messages: list[str] = []

    async def fake_send_markdown(self, content: str):
        sent_messages.append(content)

    monkeypatch.setattr(WeComNotifier, "send_markdown", fake_send_markdown)

    result = await WeComNotifier(webhook_url="https://example.com/webhook", threshold=70).push_high_impact_events(db_session)

    assert result.sent == 2
    assert "95/100" in sent_messages[0]
    assert "75/100" in sent_messages[1]
