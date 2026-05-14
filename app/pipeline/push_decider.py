from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import ScoringConfig
from app.models import EventCluster, MarketImpactAnalysis, PushRecord
from app.schemas import MarketImpactLLMOutput
from app.utils.time import utcnow


@dataclass(slots=True)
class PushDecision:
    should_push: bool
    reason: str


def evaluate_push(
    db: Session,
    cluster: EventCluster,
    analysis: MarketImpactLLMOutput,
    scoring_config: ScoringConfig,
) -> PushDecision:
    threshold = scoring_config.push_score_threshold
    if analysis.market_impact_score < threshold:
        return PushDecision(False, f"评分 {analysis.market_impact_score:.1f} 低于推送阈值 {threshold:.1f}。")

    latest_push = db.execute(
        select(PushRecord)
        .where(PushRecord.event_cluster_id == cluster.id, PushRecord.status == "success")
        .order_by(desc(PushRecord.pushed_at))
        .limit(1)
    ).scalar_one_or_none()
    if latest_push is None:
        return PushDecision(True, "评分超过阈值，且该事件尚未推送。")

    duplicate_window = timedelta(hours=scoring_config.duplicate_push_window_hours)
    if utcnow() - latest_push.pushed_at > duplicate_window:
        return PushDecision(True, "距离上次推送已超过去重窗口。")

    previous_score = _latest_analysis_score(db, cluster.id)
    if analysis.market_impact_score - previous_score >= scoring_config.score_delta_for_repush:
        return PushDecision(True, "评分较上次显著提高，符合再次推送条件。")

    if analysis.is_major_update or cluster.is_major_update:
        return PushDecision(True, "事件被识别为重大进展，允许去重窗口内再次推送。")

    if cluster.source_count >= 3 and latest_push.score_at_push < threshold + 5:
        return PushDecision(True, "新增多个来源交叉验证，允许再次推送。")

    return PushDecision(False, "同一事件仍在 12 小时去重窗口内，且无重大更新。")


def _latest_analysis_score(db: Session, cluster_id: int) -> float:
    latest = db.execute(
        select(MarketImpactAnalysis)
        .where(MarketImpactAnalysis.event_cluster_id == cluster_id)
        .order_by(desc(MarketImpactAnalysis.created_at))
        .limit(1)
    ).scalar_one_or_none()
    return latest.market_impact_score if latest else 0.0
