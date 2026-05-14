# Scoring

`market_impact_score` 衡量新闻事件对全球或区域股市的潜在影响大小，而不是新闻热度或发布时间。

## Dimensions

- `event_severity_score`：事件严重性，例如战争、制裁、违约、央行政策、通胀、能源供应冲击。
- `market_scope_score`：影响范围，考虑来源数、文章数、涉及国家、指数和行业数量。
- `asset_sensitivity_score`：资产敏感度，考虑是否涉及指数、利率、汇率、债券、商品、ticker 或公司业绩。
- `credibility_score`：来源可信度，监管公告、央行、交易所、Reuters、AP、FT 等权重更高。
- `novelty_score`：新事件或重大进展得分更高；重复转载会降低新颖度。
- `timeliness_score`：按 `last_seen_at` 衰减，但不会替代影响分排序；旧新闻如果仍在发酵，仍可获得较高总分。
- `confidence_score`：来源可信度、影响范围、时效性和影响路径清晰度共同决定。

## LLM Analysis Output

LLM 分析输出必须是严格 JSON，并通过 Pydantic schema 校验。新版 schema 要求显式区分：

- `facts`：新闻中可以确认的事实。
- `assumptions`：影响分析中用到的推测或假设。
- `uncertainties`：仍需验证的主要不确定性。
- `affected_assets`：结构化资产列表，包含 `asset_type`、`name`、`ticker`、`impact_direction`、`impact_horizon` 和 `reason_zh`。
- `should_push` 与 `push_reason`：LLM 对是否推送的分析意见。

最终推送仍由本地阈值、12 小时去重和重大更新规则裁决；如果 LLM 明确不建议推送，本地推送会被 veto。

## Fallback Strategy

LLM 只是修正器，不是单点依赖。LLM 请求失败、输出非 JSON、schema 校验失败或包含荐股话术时，系统保留规则评分。

## Sorting

Dashboard 和 API 优先按 `market_impact_score` 降序排序。同分时再按 `last_seen_at` 降序展示。
