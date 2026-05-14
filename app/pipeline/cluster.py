from __future__ import annotations

import re
from difflib import SequenceMatcher

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Article, EventCluster
from app.pipeline.entity_extract import extract_entities
from app.utils.hashing import normalize_text_for_hash, stable_hash
from app.utils.time import utcnow


STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "that",
    "this",
    "into",
    "over",
    "after",
    "before",
    "says",
    "said",
    "market",
    "stock",
    "stocks",
    "news",
}

MAJOR_UPDATE_TERMS = {
    "emergency",
    "default",
    "bankruptcy",
    "war",
    "attack",
    "sanction",
    "rate cut",
    "rate hike",
    "重大",
    "紧急",
    "违约",
    "破产",
    "战争",
    "袭击",
    "制裁",
    "降息",
    "加息",
}


def build_cluster_key(title: str, content_snippet: str | None = None) -> str:
    title_entities = extract_entities(title)
    snippet_entities = extract_entities(content_snippet or "")
    entities = title_entities if _entity_count(title_entities) else snippet_entities
    entity_tokens = (
        entities.tickers
        + entities.companies[:4]
        + entities.countries
        + entities.sectors
        + entities.indices
        + entities.commodities
    )
    title_tokens = _meaningful_tokens(title)
    snippet_tokens = _meaningful_tokens(content_snippet or "")
    signature_parts = entity_tokens[:8] + title_tokens[:12]
    if len(signature_parts) < 6:
        signature_parts.extend(snippet_tokens[: 6 - len(signature_parts)])
    if not signature_parts:
        signature_parts = [normalize_text_for_hash(title)[:80]]
    return stable_hash("|".join(part.casefold() for part in signature_parts))[:32]


def _entity_count(entities) -> int:
    return sum(
        len(items)
        for items in (
            entities.tickers,
            entities.companies,
            entities.countries,
            entities.sectors,
            entities.indices,
            entities.commodities,
        )
    )


def cluster_article(db: Session, article: Article) -> EventCluster:
    key = build_cluster_key(article.title, article.content_snippet)
    cluster = db.execute(select(EventCluster).where(EventCluster.cluster_key == key)).scalar_one_or_none()
    seen_at = article.published_at or article.fetched_at or utcnow()
    is_major_update = _contains_major_update(article.title, article.content_snippet)

    if cluster is None:
        cluster = EventCluster(
            cluster_key=key,
            title=article.title,
            summary=article.content_snippet or article.title,
            first_seen_at=seen_at,
            last_seen_at=seen_at,
            main_source=article.source,
            event_type=_rough_event_type(article.title, article.content_snippet),
            status="new",
            is_major_update=is_major_update,
        )
        db.add(cluster)
        db.flush()
    else:
        cluster.status = "updated"
        cluster.last_seen_at = max(cluster.last_seen_at, seen_at)
        cluster.is_major_update = cluster.is_major_update or is_major_update
        if _title_similarity(article.title, cluster.title) < 0.55 and len(article.title) > len(cluster.title):
            cluster.title = article.title
        if not cluster.event_type:
            cluster.event_type = _rough_event_type(article.title, article.content_snippet)

    article.event_cluster_id = cluster.id
    db.flush()
    refresh_cluster_counts(db, cluster)
    return cluster


def refresh_cluster_counts(db: Session, cluster: EventCluster) -> None:
    article_count = db.execute(
        select(func.count(Article.id)).where(Article.event_cluster_id == cluster.id)
    ).scalar_one()
    source_count = db.execute(
        select(func.count(func.distinct(Article.source))).where(Article.event_cluster_id == cluster.id)
    ).scalar_one()
    cluster.article_count = int(article_count or 0)
    cluster.source_count = int(source_count or 0)


def _meaningful_tokens(text: str) -> list[str]:
    normalized = normalize_text_for_hash(text)
    tokens = re.findall(r"[\w\u4e00-\u9fff]+", normalized)
    filtered = [token for token in tokens if len(token) > 2 and token not in STOPWORDS]
    return sorted(set(filtered), key=lambda token: (-len(token), token))


def _contains_major_update(title: str, snippet: str | None) -> bool:
    text = f"{title} {snippet or ''}".casefold()
    return any(term in text for term in MAJOR_UPDATE_TERMS)


def _rough_event_type(title: str, snippet: str | None) -> str:
    text = f"{title} {snippet or ''}".casefold()
    if any(term in text for term in ["rate", "fed", "ecb", "cpi", "inflation", "央行", "通胀", "利率"]):
        return "macro_policy"
    if any(term in text for term in ["war", "attack", "sanction", "geopolitical", "战争", "袭击", "制裁"]):
        return "geopolitics"
    if any(term in text for term in ["8-k", "10-q", "10-k", "6-k", "20-f", "earnings", "sec"]):
        return "company_filing"
    if any(term in text for term in ["oil", "gas", "supply chain", "原油", "天然气", "供应链"]):
        return "commodity_supply"
    return "market_news"


def _title_similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, normalize_text_for_hash(left), normalize_text_for_hash(right)).ratio()
