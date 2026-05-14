# Compliance

本项目定位为新闻监测和研究辅助工具，不是荐股工具。

## Output Guardrails

- 不输出买入、卖出、持有、目标价或收益承诺。
- 不承诺收益，不生成交易指令。
- 所有事件分析必须保留来源链接。
- 所有影响解释必须包含不确定性或置信度。
- LLM 输出必须通过 JSON schema 校验。
- LLM 输出若包含禁止话术，应被拒绝并回退规则评分。

## User Responsibility

新闻源可能延迟、缺失或误报；LLM 和规则评分也可能出错。使用者应自行核验来源，不应将本工具输出作为投资决策依据。

## Data Source Terms

不同数据源有不同的 API 限制、署名要求和商用条款。部署者必须自行确认 GDELT、NewsAPI、Alpha Vantage、SEC EDGAR、RSS 提供方和企业微信机器人的使用条款。
