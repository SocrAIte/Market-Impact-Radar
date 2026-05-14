# Data Sources

## GDELT

GDELT 用于全球新闻广覆盖，适合宏观、政策、地缘政治、战争、能源、供应链和科技事件。配置项位于 `sources.gdelt`。

## NewsAPI

NewsAPI 是可选源，需要 `NEWSAPI_API_KEY`。适合指定关键词、媒体源、语言和时间范围检索。

## Alpha Vantage News Sentiment

Alpha Vantage 是可选源，需要 `ALPHA_VANTAGE_API_KEY`。适合 ticker 相关新闻和情绪信息。

## SEC EDGAR

SEC EDGAR 默认抓取近期 8-K、10-Q、10-K、6-K、20-F。请配置合规的 `SEC_USER_AGENT`，建议包含项目名和联系邮箱。

## RSS

RSS 是最灵活的扩展点。可以在 `config.yaml` 中加入央行、交易所、公司 IR、监管机构和主流财经媒体 RSS。

示例：

```yaml
sources:
  rss:
    enabled: true
    feeds:
      - name: Example IR
        url: https://example.com/investors/rss.xml
        source_type: company_ir
        language: en
```
