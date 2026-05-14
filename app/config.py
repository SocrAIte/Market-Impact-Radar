from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


TimeWindow = Literal["6h", "24h", "3d", "7d"]
SchedulerMode = Literal["interval", "noon", "pre_open"]
PushSourceScope = Literal["all", "domestic", "foreign"]
PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FeedConfig(BaseModel):
    name: str
    url: str
    source_type: str = "rss"
    language: str | None = None
    enabled: bool = True


class SourceBlockConfig(BaseModel):
    enabled: bool = True
    keywords: list[str] = Field(default_factory=list)
    feeds: list[FeedConfig] = Field(default_factory=list)
    tickers: list[str] = Field(default_factory=list)
    forms: list[str] = Field(default_factory=list)
    max_results: int = 50
    extra: dict[str, Any] = Field(default_factory=dict)


class RuntimeConfig(BaseModel):
    default_time_window: TimeWindow = "24h"
    allowed_time_windows: list[TimeWindow] = Field(default_factory=lambda: ["6h", "24h", "3d", "7d"])
    request_timeout_seconds: float = 20.0
    source_fetch_timeout_seconds: float = 6.0
    user_agent: str = "market-impact-radar/0.1 (+https://github.com/your-org/market-impact-radar)"
    max_clusters_per_run: int = 40
    max_llm_analysis_per_run: int = 20
    max_concurrent_llm_analysis: int = 10
    backfill_unanalyzed_clusters: bool = False


class SchedulerConfig(BaseModel):
    enabled: bool = True
    mode: SchedulerMode = "interval"
    interval_minutes: int = 30
    timezone: str = "Asia/Shanghai"
    noon_hour: int = 12
    noon_minute: int = 0
    pre_open_hour: int = 9
    pre_open_minute: int = 0


class ScoringConfig(BaseModel):
    push_score_threshold: float = 70.0
    duplicate_push_window_hours: int = 12
    score_delta_for_repush: float = 15.0
    push_source_scope: PushSourceScope = "all"
    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "event_severity_score": 0.24,
            "market_scope_score": 0.18,
            "asset_sensitivity_score": 0.18,
            "credibility_score": 0.16,
            "novelty_score": 0.14,
            "timeliness_score": 0.10,
        }
    )


class AppYamlConfig(BaseModel):
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    sources: dict[str, SourceBlockConfig] = Field(default_factory=dict)


class Settings(BaseSettings):
    app_name: str = "market-impact-radar"
    environment: str = "development"
    database_url: str = "sqlite:///./market_impact_radar.db"
    config_yaml: str = "config.yaml"

    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 60.0

    translation_provider: str = "llm"
    translation_base_url: str | None = None
    translation_api_key: str | None = None
    translation_timeout_seconds: float = 8.0

    newsapi_api_key: str | None = None
    alpha_vantage_api_key: str | None = None
    sec_user_agent: str = "market-impact-radar contact@example.com"
    wecom_webhook_url: str | None = None

    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    @field_validator("database_url")
    @classmethod
    def normalize_sqlite_path(cls, value: str) -> str:
        if value.startswith("sqlite:///"):
            sqlite_path = value.removeprefix("sqlite:///")
            if sqlite_path.startswith("/") or re.match(r"^[A-Za-z]:", sqlite_path):
                return value
            return f"sqlite:///{(PROJECT_ROOT / sqlite_path).as_posix()}"
        if "://" in value:
            return value
        return f"sqlite:///{(PROJECT_ROOT / value).as_posix()}"

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)

    @property
    def title_translation_enabled(self) -> bool:
        provider = self.translation_provider.strip().lower()
        if provider in {"", "none", "disabled", "off"}:
            return False
        if provider in {"llm", "model"}:
            return self.llm_enabled
        if provider in {"libretranslate", "libre"} and not self.translation_base_url:
            return self.llm_enabled
        return True


@lru_cache
def get_settings() -> Settings:
    return Settings()


def load_yaml_config(path: str | Path | None = None) -> AppYamlConfig:
    settings = get_settings()
    config_path = _resolve_config_path(path or settings.config_yaml)
    if not config_path.exists():
        return AppYamlConfig()

    with config_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    example_path = PROJECT_ROOT / "config.example.yaml"
    if config_path != example_path and example_path.exists():
        with example_path.open("r", encoding="utf-8") as f:
            defaults = yaml.safe_load(f) or {}
        data = _deep_merge(defaults, data)
    return AppYamlConfig.model_validate(data)


@lru_cache
def get_app_config() -> AppYamlConfig:
    return load_yaml_config()


def _resolve_config_path(path: str | Path) -> Path:
    requested = Path(path)
    if requested.is_absolute() and requested.exists():
        return requested

    candidates = []
    if not requested.is_absolute():
        candidates.extend([Path.cwd() / requested, PROJECT_ROOT / requested])
    candidates.append(requested)
    candidates.extend([Path.cwd() / "config.example.yaml", PROJECT_ROOT / "config.example.yaml"])

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return requested


def _deep_merge(defaults: dict, overrides: dict) -> dict:
    merged = dict(defaults)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        elif key in {"feeds", "sites"} and isinstance(value, list) and isinstance(merged.get(key), list):
            merged[key] = _merge_named_dict_list(merged[key], value)
        else:
            merged[key] = value
    return merged


def _merge_named_dict_list(defaults: list[Any], overrides: list[Any]) -> list[Any]:
    """Merge configurable source lists without hiding newly added defaults."""
    output: list[Any] = []
    positions: dict[str, int] = {}
    for item in defaults:
        output.append(item)
        key = _list_item_key(item)
        if key:
            positions[key] = len(output) - 1

    for item in overrides:
        key = _list_item_key(item)
        if key and key in positions and isinstance(output[positions[key]], dict) and isinstance(item, dict):
            output[positions[key]] = _deep_merge(output[positions[key]], item)
        else:
            output.append(item)
            if key:
                positions[key] = len(output) - 1
    return output


def _list_item_key(item: Any) -> str | None:
    if not isinstance(item, dict):
        return None
    url = item.get("url")
    name = item.get("name")
    if url:
        return f"url:{url}"
    if name:
        return f"name:{name}"
    return None
