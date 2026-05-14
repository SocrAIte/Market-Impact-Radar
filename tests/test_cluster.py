from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Article
from app.pipeline.cluster import build_cluster_key, cluster_article


def test_build_cluster_key_is_stable_for_same_event_text():
    key_one = build_cluster_key(
        "Oil jumps after Red Sea attack disrupts shipping",
        "Energy supply risk rises after an attack in the Red Sea.",
    )
    key_two = build_cluster_key(
        "Oil jumps after Red Sea attack disrupts shipping",
        "Additional reports cite the same Red Sea shipping disruption.",
    )
    assert key_one == key_two


def test_cluster_article_reuses_existing_cluster():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    db = Session()
    now = datetime.now(UTC)

    article_one = Article(
        source="Reuters",
        source_type="rss",
        title="Oil jumps after Red Sea attack disrupts shipping",
        url="https://example.com/1",
        published_at=now,
        fetched_at=now,
        content_snippet="Energy supply risk rises.",
        raw_json={},
        canonical_url="https://example.com/1",
        title_hash="1",
        content_hash="1",
    )
    article_two = Article(
        source="AP",
        source_type="rss",
        title="Oil jumps after Red Sea attack disrupts shipping",
        url="https://example.com/2",
        published_at=now,
        fetched_at=now,
        content_snippet="Energy supply risk rises.",
        raw_json={},
        canonical_url="https://example.com/2",
        title_hash="2",
        content_hash="2",
    )
    db.add_all([article_one, article_two])
    db.flush()

    cluster_one = cluster_article(db, article_one)
    cluster_two = cluster_article(db, article_two)

    assert cluster_one.id == cluster_two.id
    assert cluster_two.article_count == 2
    assert cluster_two.source_count == 2
