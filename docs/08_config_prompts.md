# 08 · Config & Prompts

## settings.yaml 字段

```yaml
models:
  extractor:  gpt-4o-mini
  verifier:   gpt-4o-mini
  classifier: gpt-4o-mini

concurrency:
  fetch_workers:      8
  llm_workers:        4        # 进程内 fallback
  llm_global_max:     8        # Redis 全局信号量，跨 Worker 生效
  llm_global_backend: redis
  per_domain_max:     2        # 同 domain 跨 Worker 协调

cache:
  dir:              cache/
  fetch_ttl_days:   30
  verify_ttl_days:  7
  classify_ttl_days: 90

storage:
  uploads_dir:       data/uploads
  reports_dir:       data/reports

mongodb:
  uri_env:           MONGODB_URI
  database:          market_source_verification

queue:
  backend:              auto       # auto | local | redis-rq
  redis_url_env:        REDIS_URL
  default_queue:        source-verification
  worker_concurrency:   2
  job_timeout:          1800
  result_ttl:           86400
  failure_ttl:          604800
  zombie_scan_interval: 300

auth:
  api_keys_env:      API_KEYS
  session_token_ttl: 2592000
  require_auth:      true

runtime:
  task_store_backend: auto          # auto | local | mongodb

web:
  host:              0.0.0.0
  port:              8000
  cors_origins:      ["http://localhost:8000", "http://localhost:5173"]

search:
  provider: none        # no web search fallback; require source URL/hyperlink/whitelist
  api_key_env: null

output:
  default_format: xlsx
  include_detail_column: false

limits:
  max_claims_per_run: 2000
  per_claim_max_tokens: 8000
```

## 环境变量（`.env`）

```
OPENAI_API_KEY=sk-...
MONGODB_URI=mongodb+srv://<user>:<password>@<cluster>/<db>?retryWrites=true&w=majority
REDIS_URL=redis://localhost:6379/0
API_KEYS=key1,key2,key3                # 逗号分隔；留空则使用匿名 session token
```

MongoDB Atlas 保存任务状态、claim 结果、进度事件和报告元数据；上传文件、抓取缓存、导出报告默认保存到 `storage.*_dir` 与 `cache.dir`。

## Prompt 模板

所有 prompt 放 `src/prompts/*.j2`（jinja2），便于版本管理。

### Extractor Prompt（02 引用）

System：
> 你是一个表格结构化助手。把给定的中文研究表格按目标 schema 输出为 JSON 数组。每行一个对象，字段：metric / value / year / region / statement / source_name_raw / source_url_hint / publish_time / notes / is_forecast。无法对应的列塞 notes。仅输出 JSON，不要解释。

User：
> 表格章节路径：{{section_path}}
> 表头：{{headers}}
> 行数据（最多 50 行）：{{rows}}

### Verifier System Prompt（04 引用）

> 你是事实核验助手。给定 claim 与候选段落，判定 claim 是否被段落支持。严格按以下规则给 verdict：
> - supported：数值/年份/口径全部精确匹配
> - partially_supported：主要事实匹配，但年份/口径/单位略有差异
> - not_found：候选段落未涉及 claim 核心要素
> - contradicted：来源中出现同一指标但数值或方向矛盾
> - not_verifiable：候选为空或仅含元数据
>
> 输出 JSON：{verdict, confidence (0-1), evidence_quote, evidence_locator, discrepancy, reasoning}。引用原文须**逐字**抄录，不可改写。

User：
> Claim：{{claim_json}}
> 候选段落（top-{{k}}）：
> {% for p in passages %}
> [#{{loop.index}} | {{p.locator}}] {{p.text}}
> {% endfor %}

### Classifier Fallback Prompt（05 引用）

System：
> 你是来源可信度评估助手。仅基于提供的域名 / 标题 / 来源原始名称，给出 A/B/C 类别。规则：A=政府/监管/统计局/经审计上市公司财报；B=主流媒体/头部券商/上市公司官网披露/知名行业白皮书；C=个人博客/自媒体/聚合站/无法判断。**不确定时降一档**。

User：
> domain: {{domain}}
> title: {{title}}
> raw_name: {{raw_name}}
> 输出 JSON：{tier: "A"|"B"|"C", tier_reason: "..."}。

## OpenAI Client

- LLM 调用使用 OpenAI SDK，并读取 `settings.yaml` 中的 `gpt-4o-mini` 模型名。
- Orchestrator 继续用信号量控制 LLM 并发。
- 所有 LLM 输出仍必须通过 JSON schema 校验后才能进入下游模块。
- Web 任务模式下，LLM 并发由 Worker 内信号量和全局配置共同控制，避免多用户同时访问时超出 API 限流。
