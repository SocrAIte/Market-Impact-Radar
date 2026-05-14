# Architecture

`market-impact-radar` 采用可替换的数据源层、统一 Article 模型、事件聚类流水线和可解释评分层。

## Components

- `app/sources/*`：异步数据源适配器，统一输出 `RawArticle`。
- `app/pipeline/normalize.py`：清理 HTML、canonical URL、标题和内容 hash。
- `app/pipeline/dedup.py`：基于 `canonical_url`、`title_hash`、`content_hash` 去重。
- `app/pipeline/cluster.py`：基于标题、实体、关键词签名生成 `cluster_key`。
- `app/pipeline/entity_extract.py`：规则实体识别，覆盖公司、ticker、国家、行业、指数、商品、外汇和债券。
- `app/pipeline/scoring.py`：无 LLM 也可运行的规则评分。
- `app/pipeline/llm_analyzer.py`：OpenAI-compatible LLM 修正，严格 JSON schema 校验。
- `app/pipeline/push_decider.py`：阈值、12 小时去重、重大更新和评分提升判断。
- `app/notifier/wecom.py`：企业微信机器人 Markdown 推送。
- `app/web/routes.py`：Dashboard 和 JSON API。

## Data Flow

1. APScheduler 定时触发 `run_ingest_once`。
2. 数据源按配置时间窗口抓取新闻。
3. 统一标准化为 `Article`。
4. URL 和内容指纹去重。
5. 事件聚类并更新 `EventCluster`。
6. 规则系统生成初始评分。
7. LLM 输出结构化 JSON，失败则回退规则结果。
8. 保存 `MarketImpactAnalysis`。
9. 推送决策器判断是否写入 `PushRecord` 并发送企业微信。
10. Dashboard 按 `market_impact_score` 降序展示事件。

## Database Notes

当前使用 SQLite 作为默认数据库。SQLAlchemy 模型避免使用 SQLite 独有能力，JSON 字段可迁移到 PostgreSQL JSONB，后续可用 Alembic 管理迁移。
