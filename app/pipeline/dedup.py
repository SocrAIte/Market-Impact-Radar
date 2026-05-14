from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Article
from app.schemas import ArticleCreate


def find_duplicate_article(db: Session, article: ArticleCreate) -> Article | None:
    conditions = [Article.title_hash == article.title_hash, Article.content_hash == article.content_hash]
    if article.canonical_url:
        conditions.insert(0, Article.canonical_url == article.canonical_url)
    return db.execute(select(Article).where(or_(*conditions)).limit(1)).scalar_one_or_none()


def upsert_article(db: Session, article: ArticleCreate) -> tuple[Article, bool]:
    existing = find_duplicate_article(db, article)
    if existing:
        return existing, False

    db_article = Article(**article.model_dump())
    db.add(db_article)
    db.flush()
    return db_article, True
