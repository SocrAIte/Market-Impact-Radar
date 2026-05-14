from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.utils.time import utcnow


class Article(Base):
    __tablename__ = "articles"
    __table_args__ = (
        UniqueConstraint("canonical_url", name="uq_articles_canonical_url"),
        Index("ix_articles_title_hash", "title_hash"),
        Index("ix_articles_content_hash", "content_hash"),
        Index("ix_articles_published_at", "published_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    language: Mapped[str | None] = mapped_column(String(24), nullable=True)
    content_snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    canonical_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    title_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    event_cluster_id: Mapped[int | None] = mapped_column(ForeignKey("event_clusters.id"), nullable=True, index=True)
    event_cluster: Mapped["EventCluster | None"] = relationship(back_populates="articles")


class EventCluster(Base):
    __tablename__ = "event_clusters"
    __table_args__ = (
        UniqueConstraint("cluster_key", name="uq_event_clusters_cluster_key"),
        Index("ix_event_clusters_last_seen_at", "last_seen_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cluster_key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_seen_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    main_source: Mapped[str | None] = mapped_column(String(120), nullable=True)
    source_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    article_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    event_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="new", nullable=False)
    is_major_update: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    articles: Mapped[list[Article]] = relationship(back_populates="event_cluster")
    analyses: Mapped[list["MarketImpactAnalysis"]] = relationship(back_populates="event_cluster")
    push_records: Mapped[list["PushRecord"]] = relationship(back_populates="event_cluster")


class MarketImpactAnalysis(Base):
    __tablename__ = "market_impact_analyses"
    __table_args__ = (
        Index("ix_market_impact_score_created", "market_impact_score", "created_at"),
        Index("ix_market_impact_cluster_created", "event_cluster_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_cluster_id: Mapped[int] = mapped_column(ForeignKey("event_clusters.id"), nullable=False, index=True)
    one_sentence_summary_zh: Mapped[str] = mapped_column(Text, nullable=False)
    impact_explanation_zh: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    affected_assets_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    affected_countries_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    affected_sectors_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    impact_direction: Mapped[str] = mapped_column(String(24), nullable=False)
    impact_horizon: Mapped[str] = mapped_column(String(24), nullable=False)
    event_severity_score: Mapped[float] = mapped_column(Float, nullable=False)
    market_scope_score: Mapped[float] = mapped_column(Float, nullable=False)
    asset_sensitivity_score: Mapped[float] = mapped_column(Float, nullable=False)
    credibility_score: Mapped[float] = mapped_column(Float, nullable=False)
    novelty_score: Mapped[float] = mapped_column(Float, nullable=False)
    timeliness_score: Mapped[float] = mapped_column(Float, nullable=False)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    market_impact_score: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    confidence_level: Mapped[str] = mapped_column(String(24), nullable=False)
    uncertainties_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    should_push: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    push_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_name: Mapped[str] = mapped_column(String(120), nullable=False)
    llm_raw_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    event_cluster: Mapped[EventCluster] = relationship(back_populates="analyses")


class PushRecord(Base):
    __tablename__ = "push_records"
    __table_args__ = (
        Index("ix_push_records_cluster_pushed", "event_cluster_id", "pushed_at"),
        UniqueConstraint("message_hash", name="uq_push_records_message_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_cluster_id: Mapped[int] = mapped_column(ForeignKey("event_clusters.id"), nullable=False, index=True)
    pushed_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    channel: Mapped[str] = mapped_column(String(60), default="wecom", nullable=False)
    score_at_push: Mapped[float] = mapped_column(Float, nullable=False)
    message_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    event_cluster: Mapped[EventCluster] = relationship(back_populates="push_records")
