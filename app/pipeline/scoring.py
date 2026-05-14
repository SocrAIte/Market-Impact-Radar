from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime

from app.config import AppYamlConfig, get_app_config
from app.models import Article, EventCluster
from app.pipeline.entity_extract import extract_entities
from app.schemas import AffectedAsset, AssetType, MarketImpactLLMOutput
from app.utils.time import ensure_aware, utcnow


HIGH_SEVERITY_TERMS = {
    "war": 88,
    "attack": 85,
    "sanction": 78,
    "default": 90,
    "bankruptcy": 84,
    "rate hike": 78,
    "rate cut": 76,
    "inflation": 70,
    "cpi": 68,
    "recession": 82,
    "opec": 72,
    "export ban": 78,
    "guidance cut": 74,
    "战争": 88,
    "袭击": 85,
    "制裁": 78,
    "违约": 90,
    "破产": 84,
    "加息": 78,
    "降息": 76,
    "通胀": 70,
    "衰退": 82,
}

TRUSTED_SOURCE_TERMS = {
    "sec": 92,
    "edgar": 92,
    "federal reserve": 90,
    "ecb": 90,
    "reuters": 88,
    "associated press": 84,
    "ap": 84,
    "bbc": 80,
    "cnbc": 78,
    "financial times": 86,
    "交易所": 88,
    "央行": 90,
}


def score_event(
    cluster: EventCluster,
    articles: Sequence[Article],
    config: AppYamlConfig | None = None,
    now: datetime | None = None,
) -> MarketImpactLLMOutput:
    config = config or get_app_config()
    now = ensure_aware(now) or utcnow()
    text = _cluster_text(cluster, articles)
    lowered = text.casefold()
    entities = extract_entities(text)

    severity = _keyword_score(lowered, HIGH_SEVERITY_TERMS, default=35)
    scope = _scope_score(cluster, entities)
    sensitivity = _asset_sensitivity_score(lowered, entities)
    credibility = _credibility_score(cluster, articles)
    novelty = 82 if cluster.status == "new" else 66
    if cluster.is_major_update:
        novelty = max(novelty, 88)
    timeliness = _timeliness_score(cluster.last_seen_at, now)
    confidence = min(95.0, (credibility * 0.48) + (scope * 0.22) + (timeliness * 0.16) + 12)
    impact = _weighted_score(
        {
            "event_severity_score": severity,
            "market_scope_score": scope,
            "asset_sensitivity_score": sensitivity,
            "credibility_score": credibility,
            "novelty_score": novelty,
            "timeliness_score": timeliness,
        },
        config.scoring.weights,
    )

    affected_assets = _affected_assets(entities)
    internal_event_type = cluster.event_type or _infer_event_type(lowered)
    event_type = _display_event_type(internal_event_type)
    direction = _impact_direction(lowered)
    horizon = "intraday" if timeliness >= 86 and severity >= 70 else "short_term"
    if internal_event_type in {"macro_policy", "geopolitics", "commodity_supply"} and severity >= 75:
        horizon = "medium_term"

    should_push = impact >= config.scoring.push_score_threshold
    return MarketImpactLLMOutput(
        event_type=event_type,
        event_title_zh=_summary_zh(cluster),
        one_sentence_summary_zh=_summary_zh(cluster),
        facts=_facts(cluster, articles),
        assumptions=_assumptions(event_type, affected_assets),
        affected_assets=affected_assets,
        affected_countries=entities.countries,
        affected_sectors=entities.sectors,
        impact_direction=direction,
        impact_horizon=horizon,
        event_severity_score=round(severity, 2),
        market_scope_score=round(scope, 2),
        asset_sensitivity_score=round(sensitivity, 2),
        credibility_score=round(credibility, 2),
        novelty_score=round(novelty, 2),
        timeliness_score=round(timeliness, 2),
        confidence_score=round(confidence, 2),
        market_impact_score=round(impact, 2),
        confidence_level=_confidence_level(confidence),
        impact_explanation_zh=_explanation_zh(event_type, direction, affected_assets),
        uncertainties=_uncertainties(cluster, articles),
        is_major_update=cluster.is_major_update,
        should_push=should_push,
        push_reason=(
            f"规则评分 {impact:.1f} 达到推送阈值 {config.scoring.push_score_threshold:.1f}。"
            if should_push
            else f"规则评分 {impact:.1f} 低于推送阈值 {config.scoring.push_score_threshold:.1f}。"
        ),
    )


def _cluster_text(cluster: EventCluster, articles: Sequence[Article]) -> str:
    parts = [cluster.title, cluster.summary or ""]
    for article in articles[:10]:
        parts.extend([article.title, article.content_snippet or "", article.source])
    return " ".join(parts)


def _keyword_score(text: str, mapping: dict[str, int], default: float) -> float:
    score = default
    for term, value in mapping.items():
        if term in text:
            score = max(score, float(value))
    return min(100.0, score)


def _scope_score(cluster: EventCluster, entities) -> float:
    base = 35 + min(cluster.source_count * 8, 25) + min(cluster.article_count * 4, 18)
    base += min(len(entities.countries) * 8, 20)
    if entities.indices or len(entities.sectors) >= 2:
        base += 8
    return min(100.0, base)


def _asset_sensitivity_score(text: str, entities) -> float:
    base = 32
    if entities.indices:
        base += 18
    if entities.commodities:
        base += 18
    if entities.fx or entities.bonds:
        base += 14
    if entities.tickers:
        base += 12
    if any(term in text for term in ["earnings", "guidance", "8-k", "10-q", "10-k", "业绩", "公告"]):
        base += 12
    if any(term in text for term in ["rate", "inflation", "cpi", "fed", "央行", "通胀", "利率"]):
        base += 16
    return min(100.0, base)


def _credibility_score(cluster: EventCluster, articles: Sequence[Article]) -> float:
    score = 42 + min(cluster.source_count * 9, 28)
    text = " ".join(article.source for article in articles).casefold()
    for term, value in TRUSTED_SOURCE_TERMS.items():
        if term in text:
            score = max(score, float(value))
    return min(100.0, score)


def _timeliness_score(last_seen_at: datetime, now: datetime) -> float:
    last_seen_at = ensure_aware(last_seen_at) or now
    age_hours = max((now - last_seen_at).total_seconds() / 3600, 0)
    if age_hours <= 1:
        return 100
    if age_hours <= 6:
        return 90
    if age_hours <= 24:
        return 76
    if age_hours <= 72:
        return 58
    return 40


def _weighted_score(scores: dict[str, float], weights: dict[str, float]) -> float:
    total_weight = sum(weights.values()) or 1
    return sum(scores[key] * weights.get(key, 0) for key in scores) / total_weight


def _affected_assets(entities) -> list[AffectedAsset]:
    assets: list[AffectedAsset] = []
    assets.extend(_asset("index", name, "", "主要指数可能受风险偏好、资金流或宏观预期变化影响。") for name in entities.indices)
    assets.extend(_asset("commodity", name, "", "大宗商品价格可能受供需、地缘或政策预期影响。") for name in entities.commodities)
    assets.extend(_asset("currency", name, "", "汇率可能受利率预期、避险需求或资本流动影响。") for name in entities.fx)
    assets.extend(_asset("bond", name, "", "债券可能受通胀、利率路径和避险需求影响。") for name in entities.bonds)
    assets.extend(_asset("stock", ticker, ticker, "相关股票可能受盈利预期、监管或行业景气变化影响。") for ticker in entities.tickers)
    assets.extend(_asset("sector", sector, "", "相关行业可能受事件传导路径影响。") for sector in entities.sectors)
    assets.extend(_asset("country", country, "", "相关国家或地区股市可能受宏观预期和风险偏好影响。") for country in entities.countries)
    if not assets:
        assets.append(_asset("index", "全球股指", "", "未识别到具体资产，默认作为全球股市风险事件观察。"))

    deduped = {}
    for asset in assets:
        deduped[(asset.asset_type, asset.name, asset.ticker)] = asset
    return sorted(deduped.values(), key=lambda item: (item.asset_type, item.name))


def _asset(asset_type: AssetType, name: str, ticker: str, reason: str) -> AffectedAsset:
    return AffectedAsset(
        asset_type=asset_type,
        name=name,
        ticker=ticker,
        impact_direction="uncertain",
        impact_horizon="short_term",
        reason_zh=reason,
    )


def _infer_event_type(text: str) -> str:
    if any(term in text for term in ["rate", "fed", "cpi", "inflation", "央行", "利率", "通胀"]):
        return "macro_policy"
    if any(term in text for term in ["war", "attack", "sanction", "战争", "袭击", "制裁"]):
        return "geopolitics"
    if any(term in text for term in ["sec", "8-k", "10-q", "earnings", "公告", "业绩"]):
        return "company_filing"
    if any(term in text for term in ["oil", "gas", "opec", "energy", "原油", "天然气", "能源"]):
        return "commodity_supply"
    return "market_news"


def _display_event_type(event_type: str) -> str:
    return {
        "macro_policy": "宏观经济",
        "geopolitics": "地缘政治",
        "company_filing": "财报",
        "commodity_supply": "能源",
        "market_news": "其他",
    }.get(event_type, event_type)


def _impact_direction(text: str) -> str:
    negative_terms = ["war", "attack", "sanction", "default", "bankruptcy", "miss", "cut", "战争", "袭击", "制裁", "违约"]
    positive_terms = ["beat", "approval", "stimulus", "deal", "降息", "刺激", "批准", "协议"]
    neg = any(term in text for term in negative_terms)
    pos = any(term in text for term in positive_terms)
    if neg and pos:
        return "mixed"
    if neg:
        return "negative"
    if pos:
        return "positive"
    return "uncertain"


def _summary_zh(cluster: EventCluster) -> str:
    title = " ".join((cluster.title or "").split())
    lower = title.casefold()
    event_type = _display_event_type(cluster.event_type or _infer_event_type(lower))

    filing_summary = _filing_summary_zh(title)
    if filing_summary:
        return filing_summary

    if any(term in lower for term in ("inflation", "cpi", "consumer price")):
        country = "美国" if any(term in lower for term in ("us", "u.s.", "fed", "america")) else "相关经济体"
        percent_match = re.search(r"\b\d+(?:\.\d+)?%", title)
        level = f"跃升至 {percent_match.group(0)}" if percent_match else "出现变化"
        if "iran" in lower and ("energy" in lower or "oil" in lower):
            return f"{country}通胀因伊朗战争推升能源成本{level}"
        if "energy" in lower or "oil" in lower:
            return f"{country}通胀因能源成本上升{level}"
        return f"{country}通胀{level}"

    if any(term in lower for term in ("rate cut", "cuts rates")):
        return "降息相关消息可能影响市场利率预期"
    if any(term in lower for term in ("rate hike", "raises rates")):
        return "加息相关消息可能影响市场利率预期"
    if any(term in lower for term in ("war", "attack", "sanction", "conflict")):
        return "地缘政治冲突或制裁消息出现新进展"
    if any(term in lower for term in ("earnings", "revenue", "profit", "guidance")):
        return "公司业绩或经营指引消息出现新进展"
    if any(term in lower for term in ("oil", "gas", "opec", "energy")):
        return "能源市场供需或价格相关消息出现新进展"
    if any(term in lower for term in ("ai", "chip", "semiconductor", "export control")):
        return "科技或半导体产业相关消息出现新进展"

    if _contains_cjk(title) and len(title) <= 80:
        return title
    return f"{event_type}相关新闻出现新的市场关注点"


def _filing_summary_zh(title: str) -> str:
    match = re.match(
        r"^\s*(?P<form>10-Q|10-K|8-K|6-K|20-F)\s*[-:]\s*(?P<company>.+?)(?:\s*\(\d+\))?(?:\s*\(Filer\))?\s*$",
        title or "",
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


def _contains_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def _facts(cluster: EventCluster, articles: Sequence[Article]) -> list[str]:
    facts = [f"事件标题：{cluster.title}"]
    if cluster.main_source:
        facts.append(f"主要来源：{cluster.main_source}")
    if articles:
        facts.append(f"已聚合 {len(articles)} 篇相关新闻或公告。")
    return facts


def _assumptions(event_type: str, affected_assets: list[AffectedAsset]) -> list[str]:
    assets = "、".join(asset.name for asset in affected_assets[:5])
    return [
        f"规则系统假设该事件类型为 {event_type}，具体分类可由 LLM 根据原文修正。",
        f"规则系统假设相关资产包括 {assets}，实际影响需结合后续新闻和市场反应核实。",
    ]


def _explanation_zh(event_type: str, direction: str, affected_assets: list[AffectedAsset]) -> str:
    assets = "、".join(asset.name for asset in affected_assets[:8])
    direction_zh = {
        "positive": "正面",
        "negative": "负面",
        "mixed": "多空交织",
        "uncertain": "不确定",
    }.get(direction, "不确定")
    return (
        f"该事件被识别为{event_type}，可能通过风险偏好、资金流、行业预期或宏观变量变化影响{assets}。"
        f"当前方向暂判为{direction_zh}。这是规则评分的临时结果，建议结合可用 API 分析和后续来源继续校验。"
    )


def _confidence_level(score: float) -> str:
    if score >= 75:
        return "high"
    if score >= 55:
        return "medium"
    return "low"


def _uncertainties(cluster: EventCluster, articles: Sequence[Article]) -> list[str]:
    uncertainties = []
    if cluster.source_count <= 1:
        uncertainties.append("当前仅有单一来源，需等待更多可信来源交叉验证。")
    if not articles:
        uncertainties.append("事件缺少关联原文，解释可能不完整。")
    if cluster.event_type == "market_news":
        uncertainties.append("事件类型较泛，需要进一步核实其与具体资产的传导路径。")
    return uncertainties
