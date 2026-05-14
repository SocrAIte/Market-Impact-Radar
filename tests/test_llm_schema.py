import pytest
from pydantic import ValidationError

from app.schemas import MarketImpactLLMOutput


def _valid_payload():
    return {
        "event_type": "宏观经济",
        "one_sentence_summary_zh": "美国通胀数据超预期，可能影响全球风险资产定价。",
        "facts": ["新闻确认美国通胀数据高于市场预期。"],
        "assumptions": ["分析假设通胀数据会影响利率预期。"],
        "affected_assets": [
            {
                "asset_type": "index",
                "name": "S&P 500",
                "ticker": "SPX",
                "impact_direction": "mixed",
                "impact_horizon": "short_term",
                "reason_zh": "利率预期变化可能影响股指估值和风险偏好。",
            }
        ],
        "impact_direction": "mixed",
        "impact_horizon": "short_term",
        "event_severity_score": 75,
        "market_scope_score": 80,
        "asset_sensitivity_score": 85,
        "credibility_score": 88,
        "novelty_score": 70,
        "timeliness_score": 95,
        "confidence_score": 80,
        "market_impact_score": 81,
        "confidence_level": "high",
        "impact_explanation_zh": "该事件可能通过利率预期、美元流动性和风险偏好影响全球股指，仍需结合后续数据确认。",
        "uncertainties": ["后续央行表态仍不确定。"],
        "is_major_update": False,
        "should_push": True,
        "push_reason": "影响分较高且影响路径清晰，适合推送给研究人员关注。",
    }


def test_llm_schema_accepts_valid_payload():
    output = MarketImpactLLMOutput.model_validate(_valid_payload())
    assert output.market_impact_score == 81
    assert output.affected_assets[0].name == "S&P 500"


def test_llm_schema_coerces_legacy_asset_strings():
    payload = _valid_payload()
    payload["affected_assets"] = ["美元"]
    output = MarketImpactLLMOutput.model_validate(payload)
    assert output.affected_assets[0].name == "美元"


def test_llm_schema_rejects_trade_directives():
    payload = _valid_payload()
    payload["impact_explanation_zh"] = "建议买入相关股票。"
    with pytest.raises(ValidationError):
        MarketImpactLLMOutput.model_validate(payload)
