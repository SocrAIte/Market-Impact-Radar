from datetime import UTC, datetime

from app.models import EventCluster, MarketImpactAnalysis
from app.web.presentation import chinese_headline, event_view, label_impact_direction, label_impact_horizon


def test_chinese_headline_preserves_english_title_when_llm_translation_is_missing():
    headline = chinese_headline(
        title="US inflation jumps to 3.8% as energy costs surge from Iran war",
        summary="事件聚合：US inflation jumps to 3.8% as energy costs surge from Iran war",
        event_type="宏观经济",
    )

    assert "事件聚合" not in headline
    assert headline == "US inflation jumps to 3.8% as energy costs surge from Iran war"
    assert "可能影响" not in headline
    assert "风险偏好" not in headline


def test_chinese_headline_uses_llm_api_translation_when_available():
    headline = chinese_headline(
        title="US inflation jumps to 3.8% as energy costs surge from Iran war",
        summary="事件聚合：US inflation jumps to 3.8% as energy costs surge from Iran war",
        event_type="宏观经济",
        llm_title="美国通胀因伊朗战争推升能源成本跃升至 3.8%",
    )

    assert headline == "美国通胀因伊朗战争推升能源成本跃升至 3.8%"


def test_labels_translate_market_enums_to_chinese():
    assert label_impact_direction("negative") == "负面"
    assert label_impact_horizon("short_term") == "短期"


def test_english_title_is_not_hardcoded_translated_without_llm_api():
    headline = chinese_headline(
        title="Trump doesn't need Congress to restart Iran strikes: Hegseth",
        summary="",
        event_type="地缘政治",
    )

    assert headline == "Trump doesn't need Congress to restart Iran strikes: Hegseth"
    assert "赫格塞思" not in headline


def test_sec_filing_title_has_chinese_fallback():
    headline = chinese_headline(
        title="10-Q - Zenas BioPharma, Inc. (0001953926) (Filer)",
        summary="",
        event_type="公司公告",
    )

    assert headline == "Zenas BioPharma, Inc. 提交 10-Q 季度报告"


def test_event_view_builds_bilingual_event_title():
    now = datetime.now(UTC)
    cluster = EventCluster(
        id=64,
        cluster_key="x",
        title="US inflation jumps to 3.8% as energy costs surge from Iran war",
        first_seen_at=now,
        last_seen_at=now,
        source_count=1,
        article_count=1,
        event_type="macro_policy",
    )
    analysis = MarketImpactAnalysis(
        event_cluster_id=64,
        one_sentence_summary_zh="事件聚合：US inflation jumps to 3.8% as energy costs surge from Iran war",
        impact_explanation_zh="The event may affect markets.",
        event_type="宏观经济",
        affected_assets_json=[],
        affected_countries_json=[],
        affected_sectors_json=[],
        impact_direction="negative",
        impact_horizon="short_term",
        event_severity_score=70,
        market_scope_score=70,
        asset_sensitivity_score=70,
        credibility_score=70,
        novelty_score=70,
        timeliness_score=70,
        confidence_score=70,
        market_impact_score=70,
        confidence_level="medium",
        uncertainties_json=[],
        should_push=False,
        push_reason="",
        model_name="llm",
        llm_raw_json={"parsed_json": {"event_title_zh": "美国通胀因伊朗战争推升能源成本跃升至 3.8%"}},
    )

    view = event_view(cluster, analysis)

    assert view["headline_zh"] == "美国通胀因伊朗战争推升能源成本跃升至 3.8%"
    assert view["headline_en"] == cluster.title
    assert view["direction"] == "负面"
    assert view["horizon"] == "短期"
    assert view["confidence"] == "中"
    assert view["analysis_status"]["message"] == "已生成中文标题、摘要、影响路径和评分。"


def test_event_view_surfaces_api_failure_without_leaking_rule_noise():
    now = datetime.now(UTC)
    cluster = EventCluster(
        id=331,
        cluster_key="uk-prices",
        title="Why are UK prices rising more quickly?",
        first_seen_at=now,
        last_seen_at=now,
        source_count=1,
        article_count=1,
        event_type="macro_policy",
    )
    analysis = MarketImpactAnalysis(
        event_cluster_id=331,
        one_sentence_summary_zh="宏观经济相关新闻出现新的市场关注点",
        impact_explanation_zh="该事件被识别为 宏观经济，可能通过风险偏好、资金流、行业预期或宏观变量变化影响 中东、美国、英国、金融、B、BBC、E、I。当前方向判断为 negative，仅用于新闻监测和研究辅助，不构成任何投资建议。",
        event_type="宏观经济",
        affected_assets_json=[
            {"asset_type": "stock", "name": "B", "ticker": "B", "impact_direction": "uncertain", "impact_horizon": "short_term", "reason_zh": "噪声"},
            {"asset_type": "stock", "name": "BBC", "ticker": "BBC", "impact_direction": "uncertain", "impact_horizon": "short_term", "reason_zh": "噪声"},
            {"asset_type": "country", "name": "英国", "ticker": "", "impact_direction": "uncertain", "impact_horizon": "short_term", "reason_zh": "相关国家或地区股市可能受宏观预期和风险偏好影响。"},
        ],
        affected_countries_json=["英国"],
        affected_sectors_json=["金融"],
        impact_direction="negative",
        impact_horizon="intraday",
        event_severity_score=70,
        market_scope_score=70,
        asset_sensitivity_score=70,
        credibility_score=70,
        novelty_score=70,
        timeliness_score=70,
        confidence_score=70,
        market_impact_score=70,
        confidence_level="medium",
        uncertainties_json=[],
        should_push=False,
        push_reason="",
        model_name="rules-fallback",
        llm_raw_json={"error": "401 Unauthorized"},
    )

    view = event_view(cluster, analysis)

    assert view["analysis_status"]["title"] == "API 分析未完成"
    assert "接口鉴权失败" in view["analysis_status"]["message"]
    assert "negative" not in view["impact_explanation"]
    assert "BBC" not in view["impact_explanation"]
    assert [asset["name"] for asset in view["assets"]] == ["英国"]


def test_rules_only_event_with_configured_llm_can_be_reanalyzed():
    now = datetime.now(UTC)
    cluster = EventCluster(
        id=125,
        cluster_key="old-rules",
        title="US stocks rise as Fed rate-cut expectations improve",
        first_seen_at=now,
        last_seen_at=now,
        source_count=1,
        article_count=1,
        event_type="market_news",
    )
    analysis = MarketImpactAnalysis(
        event_cluster_id=125,
        one_sentence_summary_zh="市场新闻出现新的关注点",
        impact_explanation_zh="规则解释",
        event_type="其他",
        affected_assets_json=[],
        affected_countries_json=[],
        affected_sectors_json=[],
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
        uncertainties_json=[],
        should_push=False,
        push_reason="",
        model_name="rules-only",
        llm_raw_json={},
    )

    view = event_view(cluster, analysis, llm_configured=True)

    assert view["analysis_status"]["title"] == "尚未用 API 分析"
    assert view["analysis_status"]["can_reanalyze"] is True
    assert "API 未配置" not in view["analysis_status"]["message"]
