from __future__ import annotations

import re
from typing import Any

from app.models import EventCluster, MarketImpactAnalysis


SUMMARY_PREFIX_RE = re.compile(r"^\s*(事件聚合|Event\s+Cluster|Cluster\s+Summary)\s*[:：]\s*", re.IGNORECASE)


DIRECTION_LABELS = {
    "positive": "正面",
    "negative": "负面",
    "mixed": "多空交织",
    "uncertain": "不确定",
}

HORIZON_LABELS = {
    "intraday": "日内",
    "short_term": "短期",
    "medium_term": "中期",
    "long_term": "长期",
}

CONFIDENCE_LABELS = {
    "high": "高",
    "medium": "中",
    "low": "低",
}

STATUS_LABELS = {
    "new": "新事件",
    "updated": "已更新",
    "stale": "待复核",
}

PUSH_STATUS_LABELS = {
    "pending": "待处理",
    "success": "已推送",
    "skipped": "已跳过",
    "failed": "失败",
}

EVENT_TYPE_LABELS = {
    "macro_policy": "宏观经济",
    "geopolitics": "地缘政治",
    "company_filing": "公司公告",
    "commodity_supply": "能源 / 大宗商品",
    "market_news": "市场新闻",
    "other": "其他",
}


def event_view(
    cluster: EventCluster,
    analysis: MarketImpactAnalysis | None,
    *,
    llm_configured: bool = False,
) -> dict[str, Any]:
    event_type = label_event_type(_analysis_attr(analysis, "event_type", cluster.event_type or "其他"))
    direction = label_impact_direction(_analysis_attr(analysis, "impact_direction", "uncertain"))
    headline_zh = chinese_headline(
        title=cluster.title,
        summary=_analysis_attr(analysis, "one_sentence_summary_zh", cluster.summary or ""),
        event_type=event_type,
        llm_title=_llm_event_title(analysis),
    )
    impact_explanation = readable_text(
        _analysis_attr(analysis, "impact_explanation_zh", ""),
        fallback=f"该事件已归类为{event_type}，具体影响路径仍需结合原文和后续进展验证。",
    )
    if _is_rule_based_analysis(analysis):
        impact_explanation = _rule_based_explanation(event_type, direction)
    return {
        "headline_zh": headline_zh,
        "headline_en": english_title(cluster.title, headline_zh),
        "event_type": event_type,
        "direction": direction,
        "horizon": label_impact_horizon(_analysis_attr(analysis, "impact_horizon", "short_term")),
        "confidence": label_confidence(_analysis_attr(analysis, "confidence_level", "low")),
        "status": STATUS_LABELS.get(cluster.status, cluster.status),
        "impact_explanation": impact_explanation,
        "push_reason": readable_text(_analysis_attr(analysis, "push_reason", ""), fallback="暂无推送判断说明。"),
        "uncertainties": readable_list(
            _analysis_attr(analysis, "uncertainties_json", []),
            fallback=["后续事实更新和实际市场反应仍需继续验证。"],
        ),
        "assets": readable_assets(_analysis_attr(analysis, "affected_assets_json", [])),
        "analysis_status": analysis_status_view(analysis, llm_configured=llm_configured),
    }


def chinese_headline(
    title: str,
    summary: str | None,
    event_type: str = "市场新闻",
    llm_title: str | None = None,
) -> str:
    """Return a title-style Chinese headline, not an impact-analysis sentence."""
    llm_title = strip_internal_summary_prefix(llm_title or "")
    if _is_readable_chinese(llm_title) and not _mostly_ascii(llm_title):
        return llm_title

    cleaned_title = strip_internal_summary_prefix(title or "")
    if _is_readable_chinese(cleaned_title) and not _mostly_ascii(cleaned_title):
        return cleaned_title

    translated = title_to_chinese_headline(cleaned_title, event_type)
    if translated:
        return translated

    cleaned_summary = strip_internal_summary_prefix(summary or "")
    if _is_readable_chinese(cleaned_summary) and not _mostly_ascii(cleaned_summary):
        return cleaned_summary
    return cleaned_title or "未命名事件"


def english_title(title: str, headline_zh: str) -> str:
    cleaned = strip_internal_summary_prefix(title)
    if cleaned and cleaned != headline_zh:
        return cleaned
    return ""


def strip_internal_summary_prefix(value: str) -> str:
    return SUMMARY_PREFIX_RE.sub("", value or "").strip()


def label_impact_direction(value: str) -> str:
    return DIRECTION_LABELS.get((value or "").strip().lower(), value or "不确定")


def label_impact_horizon(value: str) -> str:
    return HORIZON_LABELS.get((value or "").strip().lower(), value or "短期")


def label_confidence(value: str) -> str:
    return CONFIDENCE_LABELS.get((value or "").strip().lower(), value or "低")


def label_event_type(value: str | None) -> str:
    if not value:
        return "其他"
    value = value.strip()
    return EVENT_TYPE_LABELS.get(value.lower(), value)


def label_push_status(value: str | None) -> str:
    if not value:
        return "未知"
    return PUSH_STATUS_LABELS.get(value.lower(), value)


def readable_text(value: str | None, fallback: str) -> str:
    value = strip_internal_summary_prefix(value or "")
    if not value or _looks_garbled(value):
        return fallback
    return value


def readable_list(values: Any, fallback: list[str] | None = None) -> list[str]:
    fallback = fallback or []
    if not isinstance(values, list):
        return fallback
    cleaned = [readable_text(str(item), fallback="") for item in values]
    cleaned = [item for item in cleaned if item]
    return cleaned or fallback


def readable_assets(values: Any) -> list[dict[str, str]]:
    if not isinstance(values, list):
        return []
    assets: list[dict[str, str]] = []
    for item in values[:6]:
        if isinstance(item, dict):
            name = str(item.get("name") or "相关资产")
            if not _is_readable_asset_name(name):
                continue
            reason = readable_text(
                str(item.get("reason_zh") or ""),
                fallback="可能受事件传导影响，具体方向仍需结合后续信息验证。",
            )
            direction = label_impact_direction(str(item.get("impact_direction") or "uncertain"))
            horizon = label_impact_horizon(str(item.get("impact_horizon") or "short_term"))
        else:
            name = str(item)
            if not _is_readable_asset_name(name):
                continue
            reason = "可能受事件传导影响，具体方向仍需结合后续信息验证。"
            direction = "不确定"
            horizon = "短期"
        assets.append({"name": name, "reason": reason, "direction": direction, "horizon": horizon})
    return assets


def analysis_status_view(
    analysis: MarketImpactAnalysis | None,
    *,
    llm_configured: bool = False,
) -> dict[str, Any] | None:
    if analysis is None:
        return None
    model_name = str(getattr(analysis, "model_name", "") or "")
    if model_name == "rules-only":
        if llm_configured:
            return {
                "level": "warning",
                "title": "尚未用 API 分析",
                "message": "这条事件仍是历史规则评分结果。可以立即重新调用 LLM API 生成标题、摘要、影响路径、资产和评分。",
                "can_reanalyze": True,
            }
        return {
            "level": "warning",
            "title": "当前为规则评分",
            "message": "AI 分析 API 未配置；配置并测试通过后，可以重新生成中文标题、摘要、影响路径和评分。",
            "can_reanalyze": False,
        }
    if model_name == "rules-fallback":
        raw = analysis.llm_raw_json if isinstance(analysis.llm_raw_json, dict) else {}
        return {
            "level": "danger",
            "title": "API 分析未完成",
            "message": f"{_friendly_api_error(str(raw.get('error') or ''))} 当前暂用规则评分展示。",
            "can_reanalyze": llm_configured,
        }
    return {
        "level": "success",
        "title": "API 分析完成",
        "message": "已生成中文标题、摘要、影响路径和评分。",
        "can_reanalyze": False,
    }


def title_to_chinese_headline(title: str, event_type: str = "市场新闻") -> str:
    filing_title = _filing_title_to_chinese(title)
    if filing_title:
        return filing_title

    # Do not fake general machine translation with brittle keyword replacement.
    # Real general title translation is produced by the configured LLM API as `event_title_zh`.
    return ""


# Backward-compatible alias for earlier public imports.
title_to_chinese_sentence = title_to_chinese_headline


def _analysis_attr(analysis: MarketImpactAnalysis | None, name: str, default: Any) -> Any:
    if analysis is None:
        return default
    return getattr(analysis, name, None) or default


def _llm_event_title(analysis: MarketImpactAnalysis | None) -> str:
    if analysis is None or not isinstance(analysis.llm_raw_json, dict):
        return ""
    parsed = analysis.llm_raw_json.get("parsed_json")
    if isinstance(parsed, dict):
        return str(parsed.get("event_title_zh") or "")
    return ""


def _is_rule_based_analysis(analysis: MarketImpactAnalysis | None) -> bool:
    model_name = str(getattr(analysis, "model_name", "") or "")
    return model_name in {"rules-only", "rules-fallback"}


def _rule_based_explanation(event_type: str, direction: str) -> str:
    return (
        f"当前展示的是规则评分的临时结果。系统暂将事件归类为{event_type}，影响方向暂判为{direction}；"
        "需要可用的 AI 分析 API 结合原文重新生成更完整的影响路径、资产映射和置信度。"
    )


def _friendly_api_error(error: str) -> str:
    lowered = error.lower()
    if "401" in lowered or "unauthorized" in lowered:
        return "接口鉴权失败，请检查 Base URL、API Key 和模型权限。"
    if "403" in lowered or "forbidden" in lowered:
        return "接口没有访问权限，请检查 API Key 权限或模型权限。"
    if "404" in lowered or "not found" in lowered:
        return "接口地址或模型名称可能不正确。"
    if "429" in lowered or "rate" in lowered or "quota" in lowered:
        return "接口限流或额度不足，请稍后重试或检查账户额度。"
    if "timeout" in lowered or "connect" in lowered:
        return "接口网络连接失败或超时，请检查网络和 Base URL。"
    return "接口返回异常，请在设置页测试 API 配置。"


def _filing_title_to_chinese(title: str) -> str:
    cleaned = strip_internal_summary_prefix(title or "")
    match = re.match(
        r"^\s*(?P<form>10-Q|10-K|8-K|6-K|20-F)\s*[-:]\s*(?P<company>.+?)(?:\s*\(\d+\))?(?:\s*\(Filer\))?\s*$",
        cleaned,
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    form = match.group("form").upper()
    company = re.sub(r"\s*\([^)]*\)\s*", " ", match.group("company")).strip(" -:")
    company = " ".join(company.split()) or "相关公司"
    form_labels = {
        "10-Q": "季度报告",
        "10-K": "年度报告",
        "8-K": "重大事项报告",
        "6-K": "境外发行人报告",
        "20-F": "年度报告",
    }
    return f"{company} 提交 {form} {form_labels.get(form, '监管文件')}"


def _is_readable_asset_name(name: str) -> bool:
    value = (name or "").strip()
    if not value:
        return False
    noisy_tokens = {"B", "E", "I", "BBC", "CNN", "CNBC", "AP", "RSS", "UK", "US", "THE", "AND", "FOR"}
    if value.upper() in noisy_tokens:
        return False
    if re.fullmatch(r"[A-Z]", value):
        return False
    return not _looks_garbled(value)


def _is_readable_chinese(value: str) -> bool:
    cjk_count = sum(1 for char in value if "\u4e00" <= char <= "\u9fff")
    return cjk_count >= 4 and not _looks_garbled(value)


def _mostly_ascii(value: str) -> bool:
    if not value:
        return False
    ascii_count = sum(1 for char in value if ord(char) < 128)
    return ascii_count / max(len(value), 1) > 0.55


def _looks_garbled(value: str) -> bool:
    if not value:
        return True
    markers = ("�", "锛", "涓", "鏉", "褰", "鍙", "绛", "€", "浠", "搷", "煎")
    marker_count = sum(value.count(marker) for marker in markers)
    return marker_count >= 3
