from datetime import UTC, datetime, timedelta

from app.config import AppYamlConfig
from app.models import Article, EventCluster
from app.pipeline.scoring import score_event


def test_high_severity_macro_event_scores_above_low_relevance_news():
    now = datetime.now(UTC)
    high_cluster = EventCluster(
        id=1,
        cluster_key="high",
        title="Federal Reserve emergency rate cut after banking stress hits markets",
        summary="The Fed announces an emergency rate cut as banking stress spreads.",
        first_seen_at=now - timedelta(hours=2),
        last_seen_at=now,
        source_count=4,
        article_count=6,
        event_type="macro_policy",
        status="new",
        is_major_update=True,
    )
    high_articles = [
        Article(
            source="Reuters",
            source_type="rss",
            title=high_cluster.title,
            url="https://example.com/high",
            content_snippet=high_cluster.summary,
            title_hash="a",
            content_hash="b",
            fetched_at=now,
        )
    ]

    low_cluster = EventCluster(
        id=2,
        cluster_key="low",
        title="Small software company launches a minor product update",
        summary="A product update was announced.",
        first_seen_at=now - timedelta(days=2),
        last_seen_at=now - timedelta(days=2),
        source_count=1,
        article_count=1,
        event_type="market_news",
        status="new",
        is_major_update=False,
    )
    low_articles = [
        Article(
            source="Company Blog",
            source_type="rss",
            title=low_cluster.title,
            url="https://example.com/low",
            content_snippet=low_cluster.summary,
            title_hash="c",
            content_hash="d",
            fetched_at=now,
        )
    ]

    high = score_event(high_cluster, high_articles, AppYamlConfig(), now=now)
    low = score_event(low_cluster, low_articles, AppYamlConfig(), now=now)

    assert high.market_impact_score > low.market_impact_score
    assert high.market_impact_score >= 75
    assert high.impact_horizon in {"intraday", "short_term", "medium_term"}
    assert "事件聚合" not in high.one_sentence_summary_zh
