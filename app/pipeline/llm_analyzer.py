from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.models import Article, EventCluster
from app.schemas import MarketImpactLLMOutput
from app.utils.json_repair import extract_json_object
from app.utils.logging import logger


PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "market_impact_prompt.txt"


async def analyze_with_llm(
    cluster: EventCluster,
    articles: list[Article],
    rule_output: MarketImpactLLMOutput,
    settings: Settings | None = None,
) -> tuple[MarketImpactLLMOutput, dict[str, Any], str]:
    settings = settings or get_settings()
    if not settings.llm_enabled:
        return rule_output, {"skipped": "LLM is not configured."}, "rules-only"

    prompt = PROMPT_PATH.read_text(encoding="utf-8") if PROMPT_PATH.exists() else _fallback_prompt()
    payload = _build_payload(cluster, articles, rule_output)
    messages = [
        {"role": "system", "content": prompt},
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False, default=str),
        },
    ]
    request_body = {
        "model": settings.llm_model,
        "messages": messages,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }

    try:
        async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
            raw = await _post_chat_completion(client, settings, request_body)
        content = raw["choices"][0]["message"]["content"]
        parsed = extract_json_object(content)
        output = MarketImpactLLMOutput.model_validate(parsed)
        raw["parsed_json"] = output.model_dump(mode="json")
        return output, raw, settings.llm_model
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM analysis failed; falling back to rules: {}", exc)
        return rule_output, {"error": str(exc), "fallback": rule_output.model_dump()}, "rules-fallback"


def _build_payload(
    cluster: EventCluster,
    articles: list[Article],
    rule_output: MarketImpactLLMOutput,
) -> dict[str, Any]:
    return {
        "event_title": cluster.title,
        "main_source": cluster.main_source,
        "source_count": cluster.source_count,
        "article_count": cluster.article_count,
        "first_seen_at": cluster.first_seen_at,
        "last_seen_at": cluster.last_seen_at,
        "articles": [
            {
                "source": article.source,
                "source_type": article.source_type,
                "title": article.title,
                "url": article.url,
                "published_at": article.published_at,
                "content_snippet": article.content_snippet,
            }
            for article in articles[:12]
        ],
        "rule_score_json": rule_output.model_dump(mode="json"),
        "required_schema": MarketImpactLLMOutput.model_json_schema(),
    }


def _fallback_prompt() -> str:
    return (
        "You are a market news research assistant. Return strict JSON matching the provided schema. "
        "Do not provide buy/sell/hold recommendations, target prices, or return promises."
    )


async def _post_chat_completion(
    client: httpx.AsyncClient,
    settings: Settings,
    request_body: dict[str, Any],
) -> dict[str, Any]:
    """Call OpenAI-compatible chat completions, retrying for APIs that do not support response_format."""
    url = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
    response = await client.post(url, headers=headers, json=request_body)
    if response.status_code in {400, 422} and "response_format" in request_body:
        error_text = response.text.lower()
        if "response_format" in error_text or "json_object" in error_text or "unsupported" in error_text:
            fallback_body = dict(request_body)
            fallback_body.pop("response_format", None)
            retry_response = await client.post(url, headers=headers, json=fallback_body)
            retry_response.raise_for_status()
            raw = retry_response.json()
            if isinstance(raw, dict):
                raw["_request_mode"] = "json_object_retry_without_response_format"
            return raw
    response.raise_for_status()
    raw = response.json()
    if isinstance(raw, dict):
        raw["_request_mode"] = "json_object_response_format"
    return raw
