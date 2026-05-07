# 02 · Extractor — 行级 Claim 结构化

## 职责

把 [Ingestor](01_ingestor.md) 输出的 IR 转成**行级 Claim 列表**。每条 Claim 是一个可以独立核验的「事实声明 + 来源」单元。

## 为什么用 LLM 而不是纯规则

输入表格的「列名」千变万化（指标/数值/年份 vs 时间/事件/企业/事件类型）。同一份文档不同 section 列名都不同（见低空经济 PDF：1.1 节 vs 4.2 节 vs 7.1 节）。LLM 做语义级列对齐比硬编码 schema 鲁棒。

## 输入

[01 Ingestor](01_ingestor.md) 的 IR。

## 输出（Claim Schema）

详见 [09 Schema](09_schema.md#claim)。核心字段：

```json


  {
    "章节": "低空经济样例",
    "指标": "低空经济规模",
    "数值": "5059.5 亿元",
    "年份": "2023",
    "地区/口径": "中国",
    "事实声明": "2023 中国 低空经济规模 5059.5 亿元",
    "来源名称": "赛迪研究院/赛迪智库 (https://www.news.cn/tech/20240402/1854417fb84a4b6d8322410c2eca31db/c.html)",
    "来源URL提示": "https://www.news.cn/tech/20240402/1854417fb84a4b6d8322410c2eca31db/c.html",
    "来源是否真实": "✅ 支持",
    "来源类别": "B"
  }



```

非表格段落（如「驱动因素」描述段）也转 Claim：`metric=null, statement=<段落原文>, source_name_raw=<紧邻引用>`。

## 关键函数

```python
def extract_claims(ir: IR) -> list[Claim]
def _table_to_claims(table_block, section_path, llm) -> list[Claim]
def _paragraph_to_claims(text, section_path, llm) -> list[Claim]
```

## LLM Prompt 概要

详见 [08 Config & Prompts](08_config_prompts.md#extractor-prompt)。要点：
- 输入：表头 + 一批行（≤ 50 行/批，控成本）；
- 任务：列名对齐到目标 schema，多余列塞 `notes`；
- 严格 JSON 输出，schema 校验失败重试 1 次。

## 已知坑

- 「来源」列里同时含中文机构名 + 英文域名 + Markdown 链接：保留原文到 `source_name_raw`，把可识别 URL 抽到 `source_url_hint`。
- 「来源」列末尾的脚注/上标编号会先查 `Block.footnotes`：第一条 URL 写入 `source_url_hint`，其余写入 `extra_source_urls`，去掉编号前的原文保存在 `source_name_raw`，带编号原文保存在 `source_name_with_marks`。
- 一行多个数值（如 "2024 年累计完成 491 万单和 45 万单"）：拆成多条 Claim。
- 表格中的「预测值」标记：转移到 `notes`，并设 `is_forecast=true`。

## 依赖

`openai` (`gpt-4o-mini`，单次 ≤ 50 行成本可控)，`pydantic`（schema 校验）。
