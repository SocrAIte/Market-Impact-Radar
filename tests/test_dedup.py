from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.pipeline.dedup import upsert_article
from app.pipeline.normalize import normalize_article
from app.sources.base import RawArticle
from app.utils.hashing import canonicalize_url


def test_canonicalize_url_removes_tracking_params():
    left = canonicalize_url("https://example.com/news/1?utm_source=x&b=2&a=1#section")
    right = canonicalize_url("https://EXAMPLE.com/news/1?a=1&b=2")
    assert left == right


def test_upsert_article_deduplicates_by_canonical_url():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    db = Session()

    raw_one = RawArticle(
        source="Reuters",
        source_type="rss",
        title="Fed cuts rates",
        url="https://example.com/news/1?utm_source=newsletter",
        published_at=datetime.now(UTC),
        content_snippet="The Fed cut interest rates.",
    )
    raw_two = RawArticle(
        source="Mirror",
        source_type="rss",
        title="Fed cuts rates",
        url="https://example.com/news/1",
        published_at=datetime.now(UTC),
        content_snippet="The Fed cut interest rates.",
    )

    first, first_is_new = upsert_article(db, normalize_article(raw_one))
    second, second_is_new = upsert_article(db, normalize_article(raw_two))

    assert first_is_new is True
    assert second_is_new is False
    assert first.id == second.id
