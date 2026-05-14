from __future__ import annotations

import json
from dataclasses import dataclass
from html import unescape
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.schemas import MarketImpactLLMOutput
from app.utils.logging import logger


MYMEMORY_ENDPOINT = "https://api.mymemory.translated.net/get"


@dataclass(slots=True)
class TitleTranslationResult:
    text: str
    provider: str
    raw_json: dict[str, Any]


async def ensure_online_title_translation(
    original_title: str,
    output: MarketImpactLLMOutput,
    raw_json: dict[str, Any],
    settings: Settings | None = None,
) -> tuple[MarketImpactLLMOutput, dict[str, Any]]:
    """Fill event_title_zh with an online translation when the LLM did not produce one."""
    parsed = raw_json.get("parsed_json") if isinstance(raw_json, dict) else None
    llm_title = str(parsed.get("event_title_zh") or "") if isinstance(parsed, dict) else ""
    if _is_good_chinese_title(llm_title):
        return output, raw_json
    if not _needs_translation(original_title):
        return output, raw_json

    translated = await translate_title_online(original_title, settings=settings)
    if translated is None:
        return output, raw_json

    output = output.model_copy(update={"event_title_zh": translated.text})
    updated_raw = dict(raw_json or {})
    parsed_json = updated_raw.get("parsed_json")
    if not isinstance(parsed_json, dict):
        parsed_json = output.model_dump(mode="json")
    else:
        parsed_json = dict(parsed_json)
        parsed_json["event_title_zh"] = translated.text
    updated_raw["parsed_json"] = parsed_json
    updated_raw["title_translation"] = {
        "provider": translated.provider,
        "source": "online_translation",
    }
    return output, updated_raw


async def translate_title_online(
    title: str,
    settings: Settings | None = None,
    client: httpx.AsyncClient | None = None,
) -> TitleTranslationResult | None:
    settings = settings or get_settings()
    provider = settings.translation_provider.strip().lower()
    if not settings.title_translation_enabled or not _needs_translation(title):
        return None

    close_client = client is None
    client = client or httpx.AsyncClient(timeout=settings.translation_timeout_seconds)
    try:
        if provider in {"libretranslate", "libre"} and not settings.translation_base_url and settings.llm_enabled:
            return await _translate_with_llm(title, settings, client)
        if provider in {"llm", "model"}:
            return await _translate_with_llm(title, settings, client)
        if provider in {"mymemory", "my_memory"}:
            return await _translate_with_mymemory(title, settings, client)
        if provider in {"libretranslate", "libre"}:
            return await _translate_with_libretranslate(title, settings, client)
        logger.warning("Unsupported translation provider: {}", settings.translation_provider)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Title translation failed: {}", exc)
        return None
    finally:
        if close_client:
            await client.aclose()


async def _translate_with_llm(
    title: str,
    settings: Settings,
    client: httpx.AsyncClient,
) -> TitleTranslationResult | None:
    if not settings.llm_enabled:
        return None
    payload = {
        "model": settings.llm_model,
        "messages": [
            {
                "role": "system",
                "content": "你是财经新闻标题翻译助手。只把英文新闻标题翻译成适合中文阅读的标题，不添加影响分析，不输出 Markdown。",
            },
            {
                "role": "user",
                "content": f"请翻译这个新闻标题，直接输出中文标题：{title}",
            },
        ],
        "temperature": 0,
        "max_tokens": 120,
    }
    response = await client.post(
        f"{settings.llm_base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        json=payload,
    )
    response.raise_for_status()
    data = response.json()
    content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    text = _clean_translation(_strip_json_or_quotes(content))
    if not _is_good_chinese_title(text):
        return None
    return TitleTranslationResult(text=text, provider="llm", raw_json=data)


async def _translate_with_mymemory(
    title: str,
    settings: Settings,
    client: httpx.AsyncClient,
) -> TitleTranslationResult | None:
    endpoint = settings.translation_base_url or MYMEMORY_ENDPOINT
    params = {"q": title, "langpair": "en|zh-CN"}
    if settings.translation_api_key:
        params["key"] = settings.translation_api_key
    response = await client.get(endpoint, params=params)
    response.raise_for_status()
    payload = response.json()
    text = _clean_translation((payload.get("responseData") or {}).get("translatedText"))
    if not _is_good_chinese_title(text):
        return None
    return TitleTranslationResult(text=text, provider="mymemory", raw_json=payload)


async def _translate_with_libretranslate(
    title: str,
    settings: Settings,
    client: httpx.AsyncClient,
) -> TitleTranslationResult | None:
    if not settings.translation_base_url:
        logger.warning("LibreTranslate provider requires TRANSLATION_BASE_URL.")
        return None
    url = f"{settings.translation_base_url.rstrip('/')}/translate"
    payload: dict[str, Any] = {
        "q": title,
        "source": "en",
        "target": "zh",
        "format": "text",
    }
    if settings.translation_api_key:
        payload["api_key"] = settings.translation_api_key
    response = await client.post(url, json=payload)
    response.raise_for_status()
    data = response.json()
    text = _clean_translation(data.get("translatedText"))
    if not _is_good_chinese_title(text):
        return None
    return TitleTranslationResult(text=text, provider="libretranslate", raw_json=data)


def _clean_translation(value: Any) -> str:
    return " ".join(unescape(str(value or "")).split())


def _strip_json_or_quotes(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            for key in ("title_zh", "event_title_zh", "translation", "text"):
                if parsed.get(key):
                    return str(parsed[key])
        if isinstance(parsed, str):
            return parsed
    except ValueError:
        pass
    return text.strip().strip("`").strip().strip('"').strip("'")


def _needs_translation(title: str) -> bool:
    title = (title or "").strip()
    return bool(title) and _mostly_ascii(title) and not _is_good_chinese_title(title)


def _is_good_chinese_title(value: str) -> bool:
    value = (value or "").strip()
    cjk_count = sum(1 for char in value if "\u4e00" <= char <= "\u9fff")
    return cjk_count >= 3 and not _mostly_ascii(value)


def _mostly_ascii(value: str) -> bool:
    if not value:
        return False
    ascii_count = sum(1 for char in value if ord(char) < 128)
    return ascii_count / max(len(value), 1) > 0.55
