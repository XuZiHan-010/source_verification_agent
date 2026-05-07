# 05 · Classifier — 来源类别 A/B/C

## 职责

给每个 `ResolvedSource` 打 A / B / C 标签。

## 类别定义

| 类别 | 说明 | 典型 |
|---|---|---|
| **A 官方/权威** | 政府、监管、央行、统计局、行业协会公报、上市公司经审计财报、SCI 期刊 | 民航局《行业发展统计公报》、国务院政策原文、央行公告、上市公司年报 |
| **B 主流/可信** | 主流通讯社/财经媒体、头部券商研报、知名咨询机构、上市公司官网披露、知名行业白皮书 | 新华社、财新、赛迪研报、IDC/Gartner、美团/小鹏官网新闻稿、大疆白皮书 |
| **C 不知名/无法验证** | 个人博客、自媒体号、聚合站、链接 404、来源空泛（"网络资料"/"百科"） | 不在白名单 + 无法验证主体可信度 |

## 输入

`ResolvedSource`（含 domain, title, fetch_status）+ Claim 元数据。

## 输出

```json
{
  "claim_id": "...",
  "tier": "A|B|C",
  "tier_reason": "domain mot.gov.cn 在 A 类白名单 (中国民航局)",
  "matched_rule": "whitelist:A:gov-cn"
}
```

## 判定流程

1. **域名白名单**（`config/source_tiers.yaml`）：逐条匹配，命中即返回（A 优先于 B 优先于 C）。
2. **域名后缀规则**：`*.gov` / `*.gov.cn` / `*.edu` → A；
3. **机构名匹配**（无 URL 时）：`source_name_raw` 含「国务院/民航局/统计局/工信部/...」→ A；含「新华社/赛迪/IDC/...」→ B。
4. **fetch 失败**（404/timeout）：直接 C，附 `tier_reason="link_dead"`。
5. **以上都不命中**：调 `gpt-4o-mini` LLM 兜底，给定域名 / 标题 / 简介 → A/B/C，写入 `tier_reason`。
6. **保守原则**：LLM 不确定时降一档（A→B、B→C）。

## 与 Verifier 的关系

二者解耦，**类别不影响 verdict**（即使来源是 A 类，数值不匹配也会标 contradicted；C 类来源若 verdict=supported，照样标 supported）。最终报告把两列**并列**展示，让用户自己判断。

## 关键函数

```python
def classify(source: ResolvedSource, claim: Claim) -> ClassifyResult
def _match_whitelist(domain, raw_name) -> ClassifyResult | None
def _llm_fallback(source, claim) -> ClassifyResult
```

## 配置

见 [config/source_tiers.yaml](../config/source_tiers.yaml)。结构：

```yaml
tiers:
  A:
    domain_suffixes: [".gov.cn", ".gov", ".edu.cn"]
    domains: [mot.gov.cn, stats.gov.cn, ndrc.gov.cn, ...]
    name_keywords: [国务院, 民航局, 工信部, ...]
  B:
    domains: [xinhuanet.com, caixin.com, pdf.dfcfw.com, ...]
    name_keywords: [新华社, 赛迪, 路透社, ...]
  # C 为兜底，不需要白名单
```
