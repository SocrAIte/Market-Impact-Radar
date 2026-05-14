from __future__ import annotations

from bs4 import BeautifulSoup

from app.schemas import ArticleCreate
from app.sources.base import RawArticle
from app.utils.hashing import canonicalize_url, normalize_text_for_hash, stable_hash
from app.utils.time import ensure_aware, utcnow


def html_to_text(value: str | None) -> str | None:
    if not value:
        return None
    text = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    return " ".join(text.split())[:2000]


def normalize_article(raw: RawArticle) -> ArticleCreate:
    fetched_at = utcnow()
    title = " ".join(raw.title.split())
    snippet = html_to_text(raw.content_snippet)
    canonical_url = canonicalize_url(raw.url)
    title_fingerprint = normalize_text_for_hash(title)
    content_fingerprint = normalize_text_for_hash(f"{title} {snippet or ''}")
    return ArticleCreate(
        source=raw.source[:120],
        source_type=raw.source_type[:60],
        title=title,
        url=raw.url,
        published_at=ensure_aware(raw.published_at),
        fetched_at=fetched_at,
        language=raw.language,
        content_snippet=snippet,
        raw_json=raw.raw_json or {},
        canonical_url=canonical_url,
        title_hash=stable_hash(title_fingerprint),
        content_hash=stable_hash(content_fingerprint),
    )
