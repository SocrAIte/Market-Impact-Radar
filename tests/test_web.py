from fastapi import FastAPI
from fastapi.testclient import TestClient
import yaml
from datetime import UTC, datetime
from types import SimpleNamespace

from app.db import get_db
from app.config import Settings
from app.models import EventCluster, MarketImpactAnalysis
from app.pipeline.ingest import IngestResult
from app.web import routes as web_routes


def test_dashboard_renders_with_empty_rows(monkeypatch):
    app = FastAPI()
    app.include_router(web_routes.router)
    app.dependency_overrides[get_db] = lambda: object()
    monkeypatch.setattr(web_routes, "_event_rows", lambda db, window, limit, sort="impact_score": [])

    response = TestClient(app).get("/")

    assert response.status_code == 200
    assert "Market Impact Radar" in response.text
    assert "立即扫描并分析" in response.text
    assert "/api/ingest/run" in response.text
    assert "扫描结果会显示在这里" in response.text
    assert "点击后会真正调用" not in response.text
    assert "事件列表排序" in response.text
    assert "切换后立即重排当前列表，不刷新页面，也不触发扫描分析" not in response.text
    assert "提供翻译 API 后，英文新闻标题会自动转为中文，阅读更准确" in response.text
    assert "/events/fragment" in response.text
    assert "转发来源链接" in response.text
    assert "暂时没有获取到新闻，请稍后重试或检查网络、数据源配置" in response.text
    assert "应用排序" not in response.text
    assert "系统设置" in response.text
    assert "按影响分数排序" in response.text
    assert "按最新时间排序" in response.text


def test_settings_page_renders():
    app = FastAPI()
    app.include_router(web_routes.router)

    response = TestClient(app).get("/settings")

    assert response.status_code == 200
    assert "系统设置 - Market Impact Radar" in response.text
    assert "Configuration，配置" not in response.text
    assert "外部接口配置" in response.text
    assert "标题翻译" not in response.text
    assert "MyMemory" not in response.text
    assert "LibreTranslate" not in response.text
    assert "默认使用 LLM API 翻译英文标题" not in response.text
    assert "translation_provider" not in response.text
    assert "国内新闻源" in response.text
    assert "启用国内新闻网站抓取" in response.text
    assert "证券时报" in response.text
    assert "中国证券报" in response.text
    assert "新闻抓取配置" in response.text
    assert "评分与推送配置" in response.text
    assert "一键恢复默认" in response.text
    assert "运行配置" not in response.text
    assert "默认时间范围" not in response.text
    assert "数据源超时（秒）" not in response.text
    assert "单次最多 API 分析" not in response.text
    assert "API 分析并发数" not in response.text
    assert "请求超时（秒）" not in response.text
    assert "单次最多分析事件" not in response.text
    assert "补分析历史事件" not in response.text
    assert "自动扫描" not in response.text
    assert "数据库" not in response.text
    assert "基础数据源" in response.text
    assert "企业微信机器人" in response.text
    assert "NewsAPI" in response.text
    assert "用于中文标题、摘要、影响解释和评分校准" in response.text
    assert "当前状态" in response.text
    assert "最近一次分析没有调用 LLM" not in response.text
    assert "尚未成功" not in response.text
    assert "data-llm-status" in response.text
    assert "updateLlmStatus" in response.text
    assert "我们用 API 做什么" not in response.text
    assert "API Key 和企业微信 Webhook" not in response.text
    assert "衡量事件本身的冲击强度" in response.text
    assert "衡量来源可靠性" in response.text
    assert "发送给 LLM API 的内容" not in response.text
    assert "/api/config/test/" in response.text
    assert "给普通用户的配置建议" not in response.text


def test_run_ingest_endpoint_returns_slots_dataclass(monkeypatch):
    app = FastAPI()
    app.include_router(web_routes.router)

    async def fake_run_ingest_once(time_window: str, *, defer_llm: bool = False):
        assert time_window == "6h"
        assert defer_llm is True
        return IngestResult(fetched_count=3, inserted_count=2, duplicate_count=1, cluster_count=1, analysis_count=1)

    monkeypatch.setattr(web_routes, "run_ingest_once", fake_run_ingest_once)

    response = TestClient(app).post("/api/ingest/run?window=6h")

    assert response.status_code == 200
    assert response.json()["fetched_count"] == 3
    assert response.json()["cluster_count"] == 1
    assert "llm_status_note" in response.json()


def test_event_fragment_uses_backend_sort(monkeypatch):
    app = FastAPI()
    app.include_router(web_routes.router)
    app.dependency_overrides[get_db] = lambda: object()

    def fake_event_rows(db, window, limit, sort="impact_score"):
        assert window == "24h"
        assert sort == "time"
        return []

    monkeypatch.setattr(web_routes, "_event_rows", fake_event_rows)

    response = TestClient(app).get("/events/fragment?window=24h&sort=time")

    assert response.status_code == 200


def test_dashboard_filters_low_impact_events(monkeypatch):
    app = FastAPI()
    app.include_router(web_routes.router)
    app.dependency_overrides[get_db] = lambda: object()
    now = datetime.now(UTC)
    cluster = EventCluster(
        id=10,
        cluster_key="low",
        title="10-Q - Zenas BioPharma, Inc. (0001953926) (Filer)",
        first_seen_at=now,
        last_seen_at=now,
        source_count=1,
        article_count=1,
        event_type="company_filing",
    )
    analysis = MarketImpactAnalysis(
        event_cluster_id=10,
        one_sentence_summary_zh="Zenas BioPharma, Inc. 提交 10-Q 季度报告",
        impact_explanation_zh="常规季度报告披露，缺乏可直接判断市场影响的摘要。",
        event_type="公司公告",
        affected_assets_json=[],
        affected_countries_json=[],
        affected_sectors_json=[],
        impact_direction="uncertain",
        impact_horizon="short_term",
        event_severity_score=10,
        market_scope_score=10,
        asset_sensitivity_score=10,
        credibility_score=90,
        novelty_score=10,
        timeliness_score=40,
        confidence_score=50,
        market_impact_score=10,
        confidence_level="low",
        uncertainties_json=[],
        should_push=False,
        push_reason="",
        model_name="llm",
        llm_raw_json={},
    )

    class FakeDb:
        def execute(self, statement):
            return FakeResult()

    class FakeResult:
        def all(self):
            return [(cluster, analysis)]

    rows = web_routes._event_rows(FakeDb(), window="24h", limit=100, sort="impact_score")

    assert rows == []


def test_dashboard_event_cards_expose_api_analysis_action(monkeypatch):
    app = FastAPI()
    app.include_router(web_routes.router)
    app.dependency_overrides[get_db] = lambda: object()
    now = datetime.now(UTC)
    cluster = EventCluster(
        id=125,
        cluster_key="fed",
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
        event_type="市场新闻",
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

    async def no_translate(db, rows):
        return None

    monkeypatch.setattr(web_routes, "_event_rows", lambda db, window, limit, sort="impact_score": [{"cluster": cluster, "analysis": analysis}])
    monkeypatch.setattr(web_routes, "_ensure_title_translations", no_translate)

    response = TestClient(app).get("/")

    assert response.status_code == 200
    assert "js-reanalyze-event" in response.text
    assert 'data-cluster-id="125"' in response.text
    assert "API 分析" in response.text
    assert "/api/events/${clusterId}/reanalyze" in response.text
    assert "updateDashboardStats(data.stats)" in response.text
    assert 'data-stat="top_score"' in response.text
    assert "data.card_html" in response.text
    assert "replaceEventCardHtml(clusterId, data.card_html)" in response.text
    assert "refreshEventCard(clusterId)" in response.text
    assert "window.location.replace(url)" not in response.text
    assert "/events/${encodeURIComponent(clusterId)}/card" in response.text
    assert '{ cache: "no-store" }' in response.text


def test_single_event_card_fragment_renders_one_card(monkeypatch):
    app = FastAPI()
    app.include_router(web_routes.router)
    app.dependency_overrides[get_db] = lambda: object()
    now = datetime.now(UTC)
    cluster = EventCluster(
        id=125,
        cluster_key="fed",
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
        event_type="市场新闻",
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

    monkeypatch.setattr(
        web_routes,
        "_event_row_by_cluster_id",
        lambda db, cluster_id: {"cluster": cluster, "analysis": analysis},
    )

    response = TestClient(app).get("/events/125/card")

    assert response.status_code == 200
    assert 'data-cluster-id="125"' in response.text
    assert response.headers["cache-control"] == "no-store"


def test_reanalyze_api_returns_updated_card_html(monkeypatch):
    app = FastAPI()
    app.include_router(web_routes.router)

    class FakeDb:
        def get(self, model, cluster_id):
            return object()

        def commit(self):
            return None

        def rollback(self):
            return None

    async def fake_analyze(db, cluster, allow_push=False):
        return SimpleNamespace(llm_succeeded=True)

    monkeypatch.setattr(
        web_routes,
        "get_settings",
        lambda: Settings(llm_base_url="https://llm.example/v1", llm_api_key="test-key", llm_model="test-model"),
    )
    monkeypatch.setattr(web_routes, "analyze_existing_cluster_once", fake_analyze)
    monkeypatch.setattr(web_routes, "_render_event_card_html", lambda request, db, cluster_id: '<article class="event-card" data-cluster-id="125">updated</article>')
    monkeypatch.setattr(web_routes, "_dashboard_stats_for_window", lambda db, window, sort="impact_score": {"event_count": 1, "high_impact_count": 1, "top_score": 88.0, "push_threshold": 70.0})
    app.dependency_overrides[get_db] = lambda: FakeDb()

    response = TestClient(app).post("/api/events/125/reanalyze")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert 'data-cluster-id="125"' in response.json()["card_html"]
    assert response.json()["stats"]["top_score"] == 88.0


def test_run_ingest_endpoint_returns_structured_error(monkeypatch):
    app = FastAPI()
    app.include_router(web_routes.router)

    async def fake_run_ingest_once(time_window: str, *, defer_llm: bool = False):
        raise RuntimeError("boom")

    monkeypatch.setattr(web_routes, "run_ingest_once", fake_run_ingest_once)

    response = TestClient(app).post("/api/ingest/run?window=24h")

    assert response.status_code == 200
    assert response.json()["fetched_count"] == 0
    assert "扫描流程异常" in response.json()["errors"][0]


def test_base_source_toggles_are_saved(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(web_routes, "CONFIG_PATH", config_path)
    app = FastAPI()
    app.include_router(web_routes.router)
    client = TestClient(app)

    response = client.post(
        "/api/config/source/gdelt",
        data={"max_results": "12", "keywords": "A shares\nPBOC"},
    )
    assert response.status_code == 200
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert data["sources"]["gdelt"]["enabled"] is False
    assert data["sources"]["gdelt"]["max_results"] == 12
    assert data["sources"]["gdelt"]["keywords"] == ["A shares", "PBOC"]

    response = client.post(
        "/api/config/source/sec_edgar",
        data={"enabled": "on", "max_results": "7", "forms": "8-K, 10-Q"},
    )
    assert response.status_code == 200
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert data["sources"]["sec_edgar"]["enabled"] is True
    assert data["sources"]["sec_edgar"]["max_results"] == 7
    assert data["sources"]["sec_edgar"]["forms"] == ["8-K", "10-Q"]

    response = client.post(
        "/api/config/source/rss",
        data={"enabled": "on", "max_results": "9", "feed_0": "on"},
    )
    assert response.status_code == 200
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert data["sources"]["rss"]["enabled"] is True
    assert data["sources"]["rss"]["max_results"] == 9
    assert data["sources"]["rss"]["feeds"][0]["enabled"] is True
    assert data["sources"]["rss"]["feeds"][1]["enabled"] is False


def test_runtime_api_analysis_limit_is_saved(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(web_routes, "CONFIG_PATH", config_path)
    app = FastAPI()
    app.include_router(web_routes.router)

    response = TestClient(app).post(
        "/api/config/runtime",
        data={
            "default_time_window": "24h",
            "request_timeout_seconds": "12",
            "max_clusters_per_run": "30",
            "max_llm_analysis_per_run": "3",
            "max_concurrent_llm_analysis": "10",
            "scheduler_enabled": "on",
            "interval_minutes": "20",
        },
    )

    assert response.status_code == 200
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert data["runtime"]["max_llm_analysis_per_run"] == 3
    assert data["runtime"]["max_concurrent_llm_analysis"] == 10


def test_env_update_quotes_api_keys_with_special_characters(monkeypatch, tmp_path):
    env_path = tmp_path / ".env"
    monkeypatch.setattr(web_routes, "ENV_PATH", env_path)

    web_routes._update_env_file({"LLM_API_KEY": "abc#def", "LLM_BASE_URL": "https://llm.example/v1"})

    text = env_path.read_text(encoding="utf-8")
    assert 'LLM_API_KEY="abc#def"' in text
    assert Settings(_env_file=env_path).llm_api_key == "abc#def"


def test_successful_wecom_test_persists_webhook(monkeypatch, tmp_path):
    env_path = tmp_path / ".env"
    monkeypatch.setattr(web_routes, "ENV_PATH", env_path)

    async def fake_test_wecom(webhook_url: str | None):
        assert webhook_url == "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
        return {"ok": True, "message": "ok"}

    monkeypatch.setattr(web_routes, "_test_wecom", fake_test_wecom)
    app = FastAPI()
    app.include_router(web_routes.router)

    response = TestClient(app).post(
        "/api/config/test/wecom",
        data={"webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"},
    )

    assert response.status_code == 200
    assert response.json()["saved"] is True
    assert 'WECOM_WEBHOOK_URL="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"' in env_path.read_text(
        encoding="utf-8"
    )
