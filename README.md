# Market Source Verification Agent

输入一份你整理的「事实资料」表格（PDF / Word / 粘贴文本），自动逐行核验**所标注 URL 对应的原文是否真实包含相应内容**，并对来源做**A/B/C 可信度分级**，最终在原表基础上追加两列输出。

Agent 的核心工作方式是：

- 从每一行里抽取 `指标 / 数值 / 年份 / 来源URL`
- 打开这个 URL，读取文章或文档正文
- 判断正文里是否真的出现了这条指标，或者 LLM 能否从正文中读出与 claim 一致的事实

## 用例

研究员/分析师整理出类似下面的表格（节选自 `examples/低空经济_input.pdf`）：

| 指标 | 数值 | 年份 | 来源名称 |
|---|---|---|---|
| 低空经济规模 | 5059.5 亿元 | 2023 | 赛迪研究院/赛迪智库（含文章 URL） |
| 注册无人机数量 | 217.7 万架 | 2024 年底 | 《2024 年民航行业发展统计公报》 |
| ... | ... | ... | ... |

跑完 Agent，会得到：

| 指标 | 数值 | 年份 | 来源名称 | **来源是否真实** | **来源类别** |
|---|---|---|---|---|---|
| 低空经济规模 | 5059.5 亿元 | 2023 | 赛迪研究院/赛迪智库 | ✅ 支持 | B |
| 注册无人机数量 | 217.7 万架 | 2024 年底 | 《2024 年民航行业发展统计公报》 | ✅ 支持 | A |

## 安装

```bash
git clone <repo>
cd market_soruce_verfication_agent
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e .
cp .env.example .env       # 填入 OPENAI_API_KEY / MONGODB_URI / REDIS_URL
```

## 使用

### Web 任务模式（多人访问推荐）

未来生产环境使用任务式 API，前端 `web/demo.html` 保持现有设计，只接入以下接口：

```text
POST /api/runs                    上传文件或文本，创建任务，返回 run_id
GET  /api/runs/{run_id}           查询状态、进度、汇总
GET  /api/runs/{run_id}/result    获取结构化结果
GET  /api/runs/{run_id}/download?fmt=xlsx
```

本地开发可直接启动 API：

```bash
source-verification-api
# 或
uvicorn market_source_verification_agent.api:app --reload
```

当前实现已经提供同样的 HTTP 合同。默认 `runtime.task_store_backend=auto`，检测到 `MONGODB_URI` 或 `MONGODB_URL` 时会使用 MongoDB Atlas 保存 `runs / events / artifacts`，否则使用本地文件系统作为开发降级。

Redis/RQ Worker 可单独启动：

```bash
source-verification-worker
```

`queue.backend=auto` 时，API 会优先把任务投递到 `REDIS_URL` 指向的 RQ 队列；Redis 不可用时回退到 FastAPI 后台任务，便于本地调试。生产环境建议显式配置 `runtime.task_store_backend=mongodb` 和 `queue.backend=redis-rq`。

架构：

```text
FastAPI → MongoDB Atlas 记录任务状态 / 结果元数据
        → Redis/RQ 投递后台任务
        → Worker 执行 Ingestor → Extractor → Resolver → Verifier → Classifier → Reporter
```

MongoDB Atlas 用于 `runs`、`claims`、`events`、`artifacts` 等集合；上传文件、缓存网页和导出报告仍放 `data/` 与 `cache/`，后续可平滑替换为对象存储。

**部署假设**：阶段三只支持单机部署（FastAPI + Worker 共享同一 `data/` / `cache/` 卷）。多机/容器化部署需先把 `storage` 切成对象存储后端，详见 [config/settings.yaml](config/settings.yaml) 的 `storage.backend`。所有 `/api/runs*` 路由要求 `X-API-Key` 头（或匿名 session token），并按 `owner_id` 隔离。

### CLI 调试模式

```bash
verify-sources examples/低空经济_input.pdf -o report.xlsx
verify-sources my_table.docx --fmt md -o report.md
verify-sources my_text.txt   --fmt html --detailed -o report.html
```

CLI 参数详见 [docs/07_orchestrator.md](docs/07_orchestrator.md#cli)。

## 项目结构

见 [CLAUDE.md](CLAUDE.md)。

## 当前状态

✅ 阶段一：架构文档完成
✅ 阶段二：`src/` 同步流水线与 CLI 实现
✅ 阶段三：FastAPI 任务接口、MongoDB Atlas 任务存储、Redis/RQ Worker 入口与本地降级后端已实现
