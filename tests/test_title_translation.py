import httpx
import pytest
import respx

from app.config import Settings
from app.pipeline.title_translation import ensure_online_title_translation, translate_title_online
from app.schemas import MarketImpactLLMOutput


def _settings(**updates) -> Settings:
    data = {
        "database_url": "sqlite:///./market_impact_radar.db",
        "translation_provider": "mymemory",
        "translation_base_url": "https://translate.example/get",
    }
    data.update(updates)
    return Settings(**data)


def _analysis() -> MarketImpactLLMOutput:
    return MarketImpactLLMOutput(
        event_type="其他",
        event_title_zh="",
        one_sentence_summary_zh="规则摘要占位",
        facts=[],
        assumptions=[],
        affected_assets=[],
        affected_countries=[],
        affected_sectors=[],
        impact_explanation_zh="规则分析占位，用于测试标题翻译流程是否正确写入结构化结果。",
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


@pytest.mark.asyncio
async def test_llm_title_translation_success():
    settings = _settings(
        translation_provider="llm",
        llm_base_url="https://llm.example/v1",
        llm_api_key="key",
        llm_model="test-model",
    )
    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://llm.example/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={"choices": [{"message": {"content": "美股因美联储降息预期改善而上涨"}}]},
            )
        )
        result = await translate_title_online("US stocks rise as Fed rate-cut expectations improve", settings=settings)

    assert result is not None
    assert result.provider == "llm"
    assert result.text == "美股因美联储降息预期改善而上涨"


@pytest.mark.asyncio
async def test_mymemory_title_translation_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["q"] == "US stocks rise as Fed rate-cut expectations improve"
        assert request.url.params["langpair"] == "en|zh-CN"
        return httpx.Response(
            200,
            json={"responseData": {"translatedText": "美股上涨，因美联储降息预期改善"}},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await translate_title_online(
            "US stocks rise as Fed rate-cut expectations improve",
            settings=_settings(),
            client=client,
        )

    assert result is not None
    assert result.provider == "mymemory"
    assert result.text == "美股上涨，因美联储降息预期改善"


@pytest.mark.asyncio
async def test_enrich_title_translation_adds_parsed_json_for_rules_only(monkeypatch):
    async def fake_translate(title, settings=None, client=None):
        from app.pipeline.title_translation import TitleTranslationResult

        return TitleTranslationResult(text="赫格塞思称特朗普重启对伊朗打击无需国会批准", provider="mymemory", raw_json={})

    monkeypatch.setattr("app.pipeline.title_translation.translate_title_online", fake_translate)

    output, raw = await ensure_online_title_translation(
        "Trump doesn't need Congress to restart Iran strikes: Hegseth",
        _analysis(),
        {"skipped": "LLM is not configured."},
        settings=_settings(),
    )

    assert output.event_title_zh == "赫格塞思称特朗普重启对伊朗打击无需国会批准"
    assert raw["parsed_json"]["event_title_zh"] == "赫格塞思称特朗普重启对伊朗打击无需国会批准"
    assert raw["title_translation"]["source"] == "online_translation"


@pytest.mark.asyncio
async def test_existing_llm_title_is_not_overwritten(monkeypatch):
    async def fail_if_called(title, settings=None, client=None):
        raise AssertionError("translator should not be called when LLM title exists")

    monkeypatch.setattr("app.pipeline.title_translation.translate_title_online", fail_if_called)

    output, raw = await ensure_online_title_translation(
        "US stocks rise as Fed rate-cut expectations improve",
        _analysis(),
        {"parsed_json": {"event_title_zh": "美股因降息预期改善而上涨"}},
        settings=_settings(),
    )

    assert output.event_title_zh == ""
    assert raw["parsed_json"]["event_title_zh"] == "美股因降息预期改善而上涨"
