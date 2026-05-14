from __future__ import annotations

import re
from dataclasses import dataclass, field


COUNTRY_TERMS = {
    "美国": ["us", "u.s.", "united states", "america", "美国", "美联储"],
    "中国": ["china", "chinese", "中国", "央行", "人民银行"],
    "日本": ["japan", "boj", "日本"],
    "欧元区": ["eurozone", "ecb", "european central bank", "欧央行", "欧洲央行", "欧元区"],
    "英国": ["uk", "britain", "boe", "英国"],
    "俄罗斯": ["russia", "russian", "俄罗斯"],
    "中东": ["middle east", "israel", "iran", "gaza", "red sea", "中东", "以色列", "伊朗"],
}

SECTOR_TERMS = {
    "能源": ["oil", "gas", "opec", "brent", "wti", "energy", "原油", "天然气", "欧佩克"],
    "金融": ["bank", "banks", "fed", "rates", "yield", "金融", "银行", "利率", "收益率"],
    "科技": ["ai", "chip", "semiconductor", "software", "technology", "科技", "芯片", "半导体", "人工智能"],
    "消费": ["retail", "consumer", "消费", "零售"],
    "医药": ["pharma", "biotech", "fda", "医药", "生物科技"],
    "工业": ["supply chain", "manufacturing", "industrial", "供应链", "制造"],
}

ASSET_TERMS = {
    "美股": ["s&p 500", "nasdaq", "dow jones", "美股", "纳斯达克", "标普"],
    "港股": ["hang seng", "hong kong stocks", "恒生", "港股"],
    "A股": ["csi 300", "shanghai composite", "a-share", "沪深", "a股"],
    "美元": ["dollar", "usd", "美元"],
    "美债": ["treasury", "yield", "bond", "美债", "国债"],
    "原油": ["oil", "brent", "wti", "crude", "原油"],
    "黄金": ["gold", "黄金"],
    "比特币": ["bitcoin", "btc", "crypto", "比特币", "加密"],
}

INDEX_TERMS = ["S&P 500", "Nasdaq", "Dow Jones", "Nikkei", "Hang Seng", "CSI 300", "DAX", "FTSE"]
TICKER_RE = re.compile(r"(?<![A-Za-z])\$?([A-Z]{2,5})(?:\.[A-Z]{1,2})?(?![A-Za-z])")


@dataclass
class ExtractedEntities:
    companies: list[str] = field(default_factory=list)
    tickers: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    sectors: list[str] = field(default_factory=list)
    indices: list[str] = field(default_factory=list)
    commodities: list[str] = field(default_factory=list)
    fx: list[str] = field(default_factory=list)
    bonds: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "companies": self.companies,
            "tickers": self.tickers,
            "countries": self.countries,
            "sectors": self.sectors,
            "indices": self.indices,
            "commodities": self.commodities,
            "fx": self.fx,
            "bonds": self.bonds,
        }


def extract_entities(text: str) -> ExtractedEntities:
    lowered = text.casefold()
    countries = _match_terms(lowered, COUNTRY_TERMS)
    sectors = _match_terms(lowered, SECTOR_TERMS)
    assets = _match_terms(lowered, ASSET_TERMS)
    tickers = _extract_tickers(text)
    indices = [idx for idx in INDEX_TERMS if idx.casefold() in lowered]
    commodities = [asset for asset in assets if asset in {"原油", "黄金", "比特币"}]
    fx = [asset for asset in assets if asset in {"美元"}]
    bonds = [asset for asset in assets if asset in {"美债"}]
    companies = _extract_company_like_phrases(text)
    return ExtractedEntities(
        companies=companies[:10],
        tickers=tickers[:20],
        countries=countries,
        sectors=sectors,
        indices=indices,
        commodities=commodities,
        fx=fx,
        bonds=bonds,
    )


def _match_terms(lowered: str, mapping: dict[str, list[str]]) -> list[str]:
    matches = []
    for label, terms in mapping.items():
        if any(term.casefold() in lowered for term in terms):
            matches.append(label)
    return matches


def _extract_tickers(text: str) -> list[str]:
    stop = {
        "THE",
        "AND",
        "FOR",
        "CEO",
        "CFO",
        "SEC",
        "FED",
        "USA",
        "GDP",
        "API",
        "AI",
        "UK",
        "US",
        "BBC",
        "CNN",
        "CNBC",
        "AP",
        "RSS",
    }
    tickers = []
    for match in TICKER_RE.findall(text):
        if match not in stop and (f"${match}" in text or len(match) >= 2):
            tickers.append(match)
    return sorted(set(tickers))


def _extract_company_like_phrases(text: str) -> list[str]:
    pattern = re.compile(r"\b([A-Z][A-Za-z&.-]+(?:\s+[A-Z][A-Za-z&.-]+){0,3})\b")
    stop = {"United States", "Federal Reserve", "Wall Street", "New York", "White House"}
    phrases = []
    for phrase in pattern.findall(text):
        if phrase not in stop and len(phrase) > 2:
            phrases.append(phrase)
    return sorted(set(phrases))
