# 04 · Verifier — 事实核验

## 职责

判断一条 Claim 的「数值 / 表述」是否真的能在它所标注的来源中找到。**这是 Agent 的核心价值环节。**

这里的“来源”默认指向一篇可打开的文章或文档 URL。Verifier 的核心任务不是判断“来源名看起来是否靠谱”，而是：

- 打开 Claim 绑定的 URL
- 读取该 URL 对应的正文内容
- 判断正文里是否真的出现了这条指标，或者 LLM 能否从正文中稳定读出与 claim 一致的事实

## 输入

`Claim` + `ResolvedSource`（含本地缓存的来源全文）。

## 输出（VerifyResult）

```json
{
  "claim_id": "...",
  "verdict": "supported|partially_supported|not_found|contradicted|not_verifiable",
  "confidence": 0.0_to_1.0,
  "evidence_quote": "原文：『2023 年中国低空经济规模达到 5059.5 亿元』",
  "evidence_locator": "page=3, paragraph=2",
  "discrepancy": null,            // verdict=contradicted 时填："来源说 5059.5，claim 写成 5095.5"
  "reasoning": "数值/年份/口径三项均匹配"
}
```

## 流程

```
Claim.source_url_hint / ResolvedSource.url
   │
   ├─▶ 打开 URL，抓取正文（HTML/PDF/纯文本）
   │
   ├─▶ 清洗 (boilerplate 移除、正文抽取 trafilatura)
   ├─▶ 分块 (按段落/小标题切，每块 ≤ 800 字)
   ├─▶ 检索 top-k 块 (BM25 → 阈值低再 fallback embedding)
   │       检索 query = metric + value + year + 关键名词
   └─▶ LLM 判定
          model: gpt-4o-mini
          system: 见 08_config_prompts.md#verifier-system
          input: claim 结构化字段 + top-k 块原文
          output: VerifyResult JSON
```

一句话概括：**先去 URL 里读文章，再判断文章是否支持这条 claim。**

## verdict 判定规则（写在 prompt 里）

| verdict | 条件 |
|---|---|
| `supported` | 从 URL 对应文章正文中能直接找到该指标，且数值、年份、口径**全部**精确匹配 |
| `partially_supported` | 文章正文表达了同一事实，但年份/口径/单位有小差异（如「2023」vs「截至 2023 年底」） |
| `not_found` | 已打开 URL 并检索正文，但 top-k 块均未涉及该 claim 的核心要素 |
| `contradicted` | 来源中有同一指标但**数值/方向矛盾** |
| `not_verifiable` | URL 打不开、正文抓取失败、paywalled、或只能拿到标题而拿不到正文 |

## 降级模式

- 来源仅有标题/摘要可拿（paywalled）：只比对「机构名 + 标题主题」是否一致 → 最高给到 `partially_supported`。
- 来源是 PDF 但 OCR 失败：标 `not_verifiable`。
- 来源是「XX 报告（无链接）」：不再尝试 web search；直接标 `not_verifiable`。

## 成本控制

- 同一 (source_url, claim_metric_normalized) 命中过则跳过 LLM。

## 关键函数

```python
def verify(claim: Claim, source: ResolvedSource) -> VerifyResult
def _retrieve_passages(claim, source_text, k=5) -> list[Passage]
def _llm_judge(claim, passages) -> VerifyResult
```

## 依赖

`openai`, `trafilatura`（正文抽取）, `rank-bm25`, 可选 `voyageai`（embedding 检索）。
