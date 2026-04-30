# 市场来源验证 Agent — 项目索引

输入：用户整理的「事实资料」表格（PDF / Word / 粘贴文本），每行包含 指标 / 数值 / 年份 / 来源 / 原文摘要。

输出：在原表格基础上**追加两列**：
- `来源是否真实`（✅ 支持 / ⚠️ 部分支持 / ❌ 未找到 / ❗ 矛盾 / ❓ 来源失效）
- `来源类别`（A 官方权威 / B 主流可信 / C 不知名/无法验证）

核心核验逻辑：优先从每行来源中抽取可打开的 URL，然后**打开 URL、读取对应文章正文、判断正文是否支持该指标 claim**。来源类别 A/B/C 是独立维度，不替代正文核验。

## 数据流

```
输入 ─▶ Ingestor ─▶ Extractor ─▶ Resolver ─▶ Verifier ─▶ Classifier ─▶ Reporter ─▶ 输出
```

## Web 服务架构（10+ 并发访问）

前端 `web/demo.html` 的视觉与交互方案保持不变；生产化时只把上传、进度、下载按钮接到后端 API。

```
浏览器 ─▶ FastAPI ─▶ MongoDB Atlas（runs / claims / events / artifacts 元数据）
             │
             ├─▶ Redis + RQ 队列 ─▶ Worker ─▶ Agent 数据流
             │
             └─▶ data/uploads + data/reports + cache/sources（本地或对象存储）
```

原则：
- HTTP 请求只创建任务并返回 `run_id`，不在请求线程内同步跑完整核验。
- MongoDB Atlas 保存任务状态、进度、claim 结果、报告元数据；大文件仍放文件系统或对象存储。
- Worker 数量和 LLM 并发独立限流，避免 10+ 用户同时上传时打满 OpenAI / 来源站点。

## 模块文档

- [01 Ingestor](docs/01_ingestor.md) — PDF/Word/纯文本 → 中间结构（文本块 + 表格）
- [02 Extractor](docs/02_extractor.md) — 中间结构 → 行级 Claim 列表（重点抽出可直接打开的来源 URL）
- [03 Resolver](docs/03_resolver.md) — 来源名称 / URL 提示 → 真实 URL / 本地文件
- [04 Verifier](docs/04_verifier.md) — 打开来源 URL、读取正文、判断 claim 是否被支持
- [05 Classifier](docs/05_classifier.md) — 来源 → A/B/C 类别（白名单 + LLM 兜底）
- [06 Reporter](docs/06_reporter.md) — 合并结果 → xlsx / md / html
- [07 Orchestrator](docs/07_orchestrator.md) — 主流程、并发、缓存、失败重试
- [08 Config & Prompts](docs/08_config_prompts.md) — 配置项与所有 LLM Prompt 模板
- [09 Schema](docs/09_schema.md) — 各阶段中间数据 JSON schema（模块解耦合同）

## 配置 / 入口

- [README](README.md) — 用户安装与使用说明
- [config/source_tiers.yaml](config/source_tiers.yaml) — A/B/C 域名白名单
- [config/settings.yaml](config/settings.yaml) — 模型、并发、缓存、Web、队列、MongoDB Atlas 配置
- [pyproject.toml](pyproject.toml) — Python 依赖

## 当前阶段

**阶段一（本次）**：仅产出架构文档（本目录所有 `.md` + `config/*.yaml` + `pyproject.toml`），不写 `src/` 实现。

**阶段二**：按 `docs/` 顺序实现 `src/` 核心模块，并保留 CLI 入口；用 `examples/低空经济_input.pdf` 做端到端回归。

**阶段三**：实现 FastAPI + Redis/RQ + MongoDB Atlas 的任务式 Web 服务；保持 `web/demo.html` 设计不变，仅接入真实 API。当前已完成 FastAPI 任务接口、MongoDB Atlas 任务存储、Redis/RQ Worker 入口与本地文件降级后端。

## 技术栈

Python 3.11+ · FastAPI · Redis/RQ · MongoDB Atlas（Motor/PyMongo）· OpenAI SDK（gpt-5-mini 抽取 / 核验 / 分类）· pdfplumber · python-docx · httpx · BeautifulSoup · pandas · openpyxl
