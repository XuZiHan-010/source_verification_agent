# 03 · Resolver — 来源 → 可访问资源

## 职责

把 Claim 的 `source_name_raw` / `source_url_hint` 解析成**可被 Verifier 打开和阅读的真实资源**（URL 或本地路径）。

它服务的核心目标很直接：尽量把每条 claim 都落到一个可访问的文章 URL 或文档文件上，让 Verifier 能继续做“打开原文、读取正文、判断是否支持 claim”。

## 输入

`Claim`（来自 [02 Extractor](02_extractor.md)）。

## 输出（ResolvedSource）

```json
{
  "claim_id": "...",
  "resolution_method": "hyperlink|whitelist|user_provided|failed",
  "url": "https://www.mot.gov.cn/...",
  "domain": "mot.gov.cn",
  "title": "2024 年民航行业发展统计公报",
  "fetch_status": "ok|404|forbidden|timeout|paywalled",
  "local_cache_path": "cache/sources/<sha1>.html"
}
```

## 解析顺序（由优到劣）

1. **原文超链接**：IR 里 `hyperlinks` 字段直接拿到 URL。
2. **域名提示**：`source_url_hint` 已有完整 URL 时直接抓取；若只有域名（如 `pdf.dfcfw.com`）则只作为来源域名线索，不发起搜索。
3. **白名单匹配**：来源名匹配 `config/source_tiers.yaml` 中的「机构 → 官网」映射（如「中国民航局」→ `mot.gov.cn`）。
4. **失败**：没有可抓取 URL / 本地文件时，`resolution_method=failed`；下游 Verifier 无法进入“打开原文核验”阶段，因此标 `verdict=not_verifiable`，Classifier 倾向 C。

## 关键函数

```python
def resolve(claim: Claim, ir_hyperlinks: dict) -> ResolvedSource
def _try_hyperlink(claim, ir_hyperlinks) -> ResolvedSource | None
def _try_whitelist(claim) -> ResolvedSource | None
def _fetch_and_cache(url) -> CachedFetch
```

## 抓取策略

- httpx + 自定义 UA；遵守 robots.txt。
- 中文站点 GBK 编码兜底。
- PDF 链接：下载到 cache 后用 [Ingestor](01_ingestor.md) 复用解析。
- 反爬强的站（部分券商研报）：标 `paywalled`，Verifier 走「仅元数据匹配」降级模式。

## 缓存

`cache/sources/<sha1(url)>.{html,pdf,json}` 保存正文/PDF 文件；缓存索引写入 MongoDB Atlas（url, fetched_at, status, content_hash, local_cache_path）。本地开发可降级为轻量 JSON 索引，但生产默认使用 MongoDB。
TTL 默认 30 天，可在 `settings.yaml` 调。

## 依赖

`httpx`, `tenacity`（重试）, `charset-normalizer`。
