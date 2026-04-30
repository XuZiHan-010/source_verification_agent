# 07 · Orchestrator — 主流程与任务服务

## 职责

串联 1→6，处理并发、缓存命中、错误恢复、进度展示。Orchestrator 同时支持两种入口：

| 入口 | 用途 | 行为 |
|---|---|---|
| CLI 同步入口 | 本地调试、单文件回归 | 当前进程直接跑完整流水线 |
| Web 任务入口 | 10+ 用户并发访问 | HTTP 只创建任务；Worker 后台执行流水线 |

## 主流程伪代码

```python
def run(input_path: str, out_path: str, fmt: str="xlsx", config: Settings) -> Report:
    ir       = ingestor.ingest(input_path)
    claims   = extractor.extract_claims(ir)              # batched LLM
    sources  = parallel(resolver.resolve, claims, n=8)   # IO bound
    verifies = parallel(verifier.verify,  zip(claims, sources), n=4)   # LLM bound
    classes  = parallel(classifier.classify, sources, n=8)
    return reporter.render(ir, verifies, classes, fmt=fmt)
```

## Web 任务模式

前端 `web/demo.html` 保持现有设计，不改视觉方案；生产化时把上传、进度、下载动作接到 API：

```text
POST /api/runs
GET  /api/runs/{run_id}
GET  /api/runs/{run_id}/result
GET  /api/runs/{run_id}/download?fmt=xlsx
```

### 鉴权与多用户隔离

10+ 用户共享同一部署，必须做最小化身份隔离：

- 所有 `/api/runs*` 路由要求请求头携带 `X-API-Key`（或匿名 session token，由前端首次访问时由后端发放并写 cookie）。
- 每个 `RunTask` / `Artifact` 写入时记录 `owner_id`（即 API key / session token 的 hash）。
- `GET /api/runs/{run_id}*` 必须按 `owner_id` 过滤；`run_id` 一律用 UUIDv4，禁止可枚举 ID。
- `auth` 段在 `settings.yaml` 配置：API key 列表来源（环境变量 / Atlas 集合）、session token TTL。

请求生命周期：

1. FastAPI 接收文件或粘贴文本，写入 `data/uploads/<run_id>/`。
2. FastAPI 在 MongoDB Atlas `runs` 集合创建任务文档，状态为 `queued`。
3. FastAPI 将 `run_id` 投递到 Redis/RQ 队列，并立即返回。
4. Worker 领取任务，状态改为 `running`，逐阶段更新进度。
5. Worker 生成报告到 `data/reports/<run_id>/`，把 artifact 元数据写入 MongoDB。
6. 前端轮询 `GET /api/runs/{run_id}`，完成后展示结果与下载按钮。

### MongoDB Atlas 集合

| 集合 | 作用 |
|---|---|
| `runs` | 任务状态、进度、输入文件、输出格式、开始/结束时间、错误信息 |
| `claims` | 行级 claim、resolved source、verify/classify 结果，用 `run_id + claim_id` 查询 |
| `events` | 阶段日志与进度事件，便于前端展示 timeline |
| `artifacts` | 生成的 xlsx/md/html/json 报告元数据与下载路径 |

大文件不直接存 MongoDB。上传文件、抓取缓存、报告文件默认存本地目录；部署到多机时替换为 S3/R2/OSS 等对象存储，MongoDB 只保存路径和 hash。

## 并发与限流

- IO（fetch）：`asyncio` + `httpx.AsyncClient`，默认并发 8（进程内）。
- LLM：进程内信号量（默认 4） + **Redis 全局信号量**（`llm_global_max`，默认 8）。多 Worker 部署时全局上限以 Redis 为准；进程内信号量仅是 fallback，避免单 Worker 起爆。
- 同一 domain 全局并发 ≤ 2，使用 Redis key `domain_lock:<domain>` 协调，跨 Worker 生效，避免被 ban。
- Web 模式下任务级并发由 Worker 数量控制；建议起步 `worker_concurrency=2~4`，`llm_global_max=8` 左右（按 OpenAI tier 调整）。
- API 层不执行重任务，只做上传、创建任务、状态查询和报告下载。

## 缓存层

| 阶段 | 缓存 key | TTL |
|---|---|---|
| Resolver 抓取 | sha1(url) | 30d |
| Verifier 判定 | sha1(claim_normalized + source_url) | 7d |
| Classifier | sha1(domain + raw_name) | 90d |

缓存目录默认 `cache/`，可在 `settings.yaml` 改。缓存索引与任务状态优先写 MongoDB Atlas；本地 cache 文件只保存正文/PDF 等大内容。`--no-cache` CLI 参数强制刷新。

## 当前实现状态

- `market_source_verification_agent.api` 提供 `/api/runs*` HTTP 合同。
- `MongoTaskStore` 使用 PyMongo 写入 `runs / events / artifacts`，并创建基础索引。
- `source-verification-worker` 启动 Redis/RQ Worker，执行 `market_source_verification_agent.worker.run_worker`。
- `runtime.task_store_backend=auto`：检测到 `MONGODB_URI` 或 `MONGODB_URL` 时使用 MongoDB，否则使用本地 JSON 文件存储。
- `queue.backend=auto`：检测到 `REDIS_URL` 且 Redis/RQ 可用时投递队列，否则回退 FastAPI 后台任务。

## CLI

```
verify-sources INPUT [--fmt xlsx|md|html|json] [-o OUT] [--detailed] [--no-cache]
                     [--config config/settings.yaml] [--limit N]
```

`--limit N`：只处理前 N 条 claim，调试用。

## 进度展示

`rich.progress` 多轨进度条：Extract / Resolve / Verify / Classify 各一条，实时显示「当前条数 / 总数 / 缓存命中率 / API 调用数 / 累计成本」。

## 错误恢复

- 单条 claim 失败不中断全局：`VerifyResult(verdict=not_verifiable, reasoning=<exc>)`，最终在报告中标 ❓。
- 全局失败（API key 失效、磁盘满）：抛 `OrchestratorError`，CLI 退出码非 0。
- Web 任务失败：`runs.status=failed`，`runs.error` 记录用户可读错误，`events` 保留阶段日志。
- `--resume` / Web 重试模式：基于 MongoDB Atlas 中的 `runs` / `claims` / `artifacts` 续跑，已完成 claim 跳过。
- **RQ 超时**：`queue.job_timeout`（默认 1800s）限制单任务最长执行时间，超时由 RQ 自动 kill 并标 `failed`。
- **僵尸任务回收**：FastAPI 启动 + 每 5 分钟扫一次 `runs.status in ['queued','running']` 且 `updated_at` 早于 `now - 2 × job_timeout` 的记录，标 `failed` 并写 `events`，防止 Worker OOM 后前端永远转圈。
- **部署假设**：阶段三只支持单机部署，FastAPI 与 Worker 共享同一 `data/` 与 `cache/` 卷；多机部署需先把 storage 抽成接口（`LocalStorage` / `S3Storage`），`Artifact.storage_uri` 取代本地路径。

## 关键函数

```python
def run(input_path, out_path, fmt, config) -> Report
def enqueue_run(input_payload, fmt, config) -> str  # returns run_id
def run_worker(run_id, config) -> Report
async def _phase_resolve(claims, config) -> list[ResolvedSource]
async def _phase_verify(pairs, config) -> list[VerifyResult]
```

## 依赖

`asyncio`, `rich`, `tenacity`, `fastapi`, `redis`, `rq`, `motor` / `pymongo`。
