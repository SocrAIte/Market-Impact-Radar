from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


ImpactDirection = Literal["positive", "negative", "mixed", "uncertain"]
ImpactHorizon = Literal["intraday", "short_term", "medium_term", "long_term"]
ConfidenceLevel = Literal["high", "medium", "low"]
AssetType = Literal["stock", "index", "sector", "commodity", "currency", "bond", "country"]

FORBIDDEN_ADVICE_TERMS = (
    "买入",
    "卖出",
    "持有",
    "目标价",
    "收益承诺",
    "buy recommendation",
    "sell recommendation",
    "hold recommendation",
    "target price",
)


class ArticleCreate(BaseModel):
    source: str
    source_type: str
    title: str
    url: str
    published_at: datetime | None = None
    fetched_at: datetime
    language: str | None = None
    content_snippet: str | None = None
    raw_json: dict[str, Any] = Field(default_factory=dict)
    canonical_url: str | None = None
    content_hash: str
    title_hash: str


class ArticleRead(ArticleCreate):
    id: int
    event_cluster_id: int | None = None

    model_config = ConfigDict(from_attributes=True)


class EventClusterRead(BaseModel):
    id: int
    cluster_key: str
    title: str
    summary: str | None = None
    first_seen_at: datetime
    last_seen_at: datetime
    main_source: str | None = None
    source_count: int
    article_count: int
    event_type: str | None = None
    status: str
    is_major_update: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AffectedAsset(BaseModel):
    asset_type: AssetType
    name: str = Field(..., min_length=1, max_length=120)
    ticker: str = Field(default="", max_length=32)
    impact_direction: ImpactDirection
    impact_horizon: ImpactHorizon
    reason_zh: str = Field(..., min_length=4, max_length=600)

    @field_validator("reason_zh")
    @classmethod
    def reject_asset_advice(cls, value: str) -> str:
        return _reject_investment_advice(value)


class MarketImpactLLMOutput(BaseModel):
    event_type: str = Field(..., max_length=80)
    event_title_zh: str = Field(default="", max_length=220)
    one_sentence_summary_zh: str = Field(..., min_length=6, max_length=220)
    facts: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    affected_assets: list[AffectedAsset] = Field(default_factory=list)
    impact_explanation_zh: str = Field(..., min_length=20, max_length=3000)
    affected_countries: list[str] = Field(default_factory=list)
    affected_sectors: list[str] = Field(default_factory=list)
    impact_direction: ImpactDirection
    impact_horizon: ImpactHorizon
    event_severity_score: float = Field(..., ge=0, le=100)
    market_scope_score: float = Field(..., ge=0, le=100)
    asset_sensitivity_score: float = Field(..., ge=0, le=100)
    credibility_score: float = Field(..., ge=0, le=100)
    novelty_score: float = Field(..., ge=0, le=100)
    timeliness_score: float = Field(..., ge=0, le=100)
    confidence_score: float = Field(..., ge=0, le=100)
    market_impact_score: float = Field(..., ge=0, le=100)
    confidence_level: ConfidenceLevel
    uncertainties: list[str] = Field(default_factory=list)
    is_major_update: bool = False
    should_push: bool = False
    push_reason: str = Field(default="", max_length=800)

    @field_validator("affected_assets", mode="before")
    @classmethod
    def coerce_legacy_assets(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        coerced = []
        for item in value:
            if isinstance(item, str):
                coerced.append(
                    {
                        "asset_type": "index",
                        "name": item,
                        "ticker": "",
                        "impact_direction": "uncertain",
                        "impact_horizon": "short_term",
                        "reason_zh": "规则回退识别为可能相关资产，具体影响路径需进一步核实。",
                    }
                )
            else:
                coerced.append(item)
        return coerced

    @field_validator(
        "event_title_zh",
        "one_sentence_summary_zh",
        "impact_explanation_zh",
        "push_reason",
    )
    @classmethod
    def reject_investment_advice(cls, value: str) -> str:
        return _reject_investment_advice(value)

    @field_validator("facts", "assumptions", "uncertainties")
    @classmethod
    def reject_list_advice(cls, value: list[str]) -> list[str]:
        return [_reject_investment_advice(item) for item in value]


class MarketImpactAnalysisRead(BaseModel):
    id: int
    event_cluster_id: int
    one_sentence_summary_zh: str
    impact_explanation_zh: str
    event_type: str
    affected_assets_json: list[Any]
    affected_countries_json: list[Any]
    affected_sectors_json: list[Any]
    impact_direction: str
    impact_horizon: str
    event_severity_score: float
    market_scope_score: float
    asset_sensitivity_score: float
    credibility_score: float
    novelty_score: float
    timeliness_score: float
    confidence_score: float
    market_impact_score: float
    confidence_level: str
    uncertainties_json: list[Any]
    should_push: bool
    push_reason: str | None = None
    model_name: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


def _reject_investment_advice(value: str) -> str:
    lowered = value.lower()
    for term in FORBIDDEN_ADVICE_TERMS:
        if term in lowered:
            raise ValueError(f"LLM output contains forbidden investment-advice term: {term}")
    return value
