import json
from datetime import datetime

import httpx
import pytest

from app.config import Settings
from app.models import EventCluster
from app.pipeline.llm_analyzer import analyze_with_llm
from app.schemas import MarketImpactLLMOutput


def _rule_output() -> MarketImpactLLMOutput:
    return MarketImpactLLMOutput(
        event_type="其他",
        event_title_zh="",
        one_sentence_summary_zh="规则摘要占位",
        facts=[],
        assumptions=[],
        affected_assets=[],
        affected_countries=[],
        affected_sectors=[],
        impact_explanation_zh="规则分析占位，用于测试 LLM 失败时的回退路径。",
        impact_direction="uncertain",
        impact_horizon="short_term",
        event_severity_score=40,
        market_scope_score=40,
        asset_sensitivity_score=40,
        credibility_score=40,
        novelty_score=40,
        timeliness_score=40,
        confidence_score=40,
        market_impact_score=40,
        confidence_level="low",
        uncertainties=[],
        should_push=False,
        push_reason="",
    )


def _llm_payload() -> dict:
    return {
        "event_type": "地缘政治",
        "event_title_zh": "美股因地缘政治风险升温而承压",
        "one_sentence_summary_zh": "多家媒体报道地缘政治紧张局势升温，市场风险偏好受到压制。",
        "facts": ["新闻报道地缘政治紧张局势升温。"],
        "assumptions": ["市场可能重新评估风险资产定价。"],
        "affected_assets": [],
        "affected_countries": ["美国"],
        "affected_sectors": [],
        "impact_explanation_zh": "该事件可能通过避险情绪、能源价格和风险资产估值预期影响股票、债券和外汇市场。",
        "impact_direction": "mixed",
        "impact_horizon": "short_term",
        "event_severity_score": 72,
        "market_scope_score": 68,
        "asset_sensitivity_score": 65,
        "credibility_score": 76,
        "novelty_score": 62,
        "timeliness_score": 70,
        "confidence_score": 66,
        "market_impact_score": 70,
        "confidence_level": "medium",
        "uncertainties": ["后续政策和市场反应仍不确定。"],
        "is_major_update": False,
        "should_push": True,
        "push_reason": "影响范围较广，值得监测。",
    }


@pytest.mark.asyncio
async def test_llm_analysis_retries_without_response_format(respx_mock):
    route = respx_mock.post("https://llm.example/v1/chat/completions")
    route.side_effect = [
        httpx.Response(400, json={"error": {"message": "response_format is unsupported"}}),
        httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(_llm_payload(), ensure_ascii=False)}}]},
        ),
    ]
    cluster = EventCluster(
        id=1,
        cluster_key="geo",
        title="US stocks fall as geopolitical tensions rise",
        first_seen_at=datetime(2026, 1, 1, 9, 0, 0),
        last_seen_at=datetime(2026, 1, 1, 10, 0, 0),
        main_source="Reuters",
        source_count=2,
        article_count=2,
    )
    settings = Settings(
        llm_base_url="https://llm.example/v1",
        llm_api_key="test-key",
        llm_model="test-model",
    )

    output, raw, model_name = await analyze_with_llm(cluster, [], _rule_output(), settings=settings)

    assert route.call_count == 2
    first_body = json.loads(route.calls[0].request.content.decode("utf-8"))
    second_body = json.loads(route.calls[1].request.content.decode("utf-8"))
    assert "response_format" in first_body
    assert "response_format" not in second_body
    assert output.event_title_zh == "美股因地缘政治风险升温而承压"
    assert output.market_impact_score == 70
    assert raw["_request_mode"] == "json_object_retry_without_response_format"
    assert model_name == "test-model"
