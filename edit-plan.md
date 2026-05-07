# 修复 0 字节 PDF 缓存 & 削减 LLM token 浪费

## Context

上一轮加诊断后已能确认大量「PDF 已下载但文本提取为空」的真实原因是「缓存文件为 0 字节」——HTTP 200 但响应体为空（反爬 / 登录页重定向 / CDN 故障），却被当作成功缓存。诊断只是把"未知问题"变成"已知问题"，PDF 仍然无法核验。

同时本次盘点发现 verifier 完全没有 LLM 缓存，每次跑相同输入都重复烧 token；BM25 零命中时还会喂"前 K 段兜底段落"给 LLM，几乎注定返回 not_found，纯属浪费。

本次目标：
1. **修 0 字节缓存根因**：HTTP 200 但 body 为空时，不当作成功，触发重试或标记失败，绝不写 0 字节文件。
2. **大幅削减 LLM 调用**：给 verifier 加磁盘缓存（仿 classifier）；BM25 零命中时直接 not_found，不调 LLM。

---

## 修复方案

### 修复 1：拒绝 0 字节响应体（[resolver.py](src/market_source_verification_agent/resolver.py)）

**根因位置**：[resolver.py:200-205 `_http_get_with_retry`](src/market_source_verification_agent/resolver.py#L200) 只看 `status_code == 200` 就当成功；[resolver.py:179 `_persist`](src/market_source_verification_agent/resolver.py#L179) 无脑 `cache_path.write_bytes(body)`。

**改造**：
- 在 `_http_get_with_retry` 内 `status_code == 200` 分支增加 `if not response.content or len(response.content) < _MIN_BODY_BYTES:` 判断（PDF 至少 ~1KB，HTML 至少 ~200B），把这种响应当作软失败 → 触发当前已存在的重试逻辑；最终全部失败时返回新状态 `"empty_body"`。
- 在 `_persist` 入口加防御性 `if not body: return None` 保险栓，确保任何路径都不会落 0 字节。
- `ResolvedSource.fetch_status` 已是 Literal，新增 `"empty_body"` 进白名单（[schema.py:54](src/market_source_verification_agent/schema.py#L54)）。
- 重试时切换 `_BROWSER_UA_FALLBACK`（已存在 [resolver.py:26](src/market_source_verification_agent/resolver.py#L26)），覆盖部分对 UA 敏感的反爬。
- [reporter.py:285](src/market_source_verification_agent/reporter.py#L285) 已有 `if status and status != "ok"` 分支，新增 `empty_body` 中文映射「服务器返回空响应（疑似反爬或登录重定向）」。

### 修复 2：Verifier 加磁盘缓存（仿 classifier 模式）

**当前问题**：[verifier.py:209-261 `_judge_by_llm`](src/market_source_verification_agent/verifier.py#L209) 直接调 `client.chat.completions.create()`，无任何缓存。

**改造**：完全照搬 [classifier.py:178-213](src/market_source_verification_agent/classifier.py#L178) 的 `_llm_cache_paths` / `_read_cache` / `_write_cache`：
- 缓存目录：`{cache.dir}/verify/`
- TTL：`settings.cache.verify_ttl_days`（已存在，默认 7 天）
- Cache key：`sha1(source.content_hash + claim.statement + claim.value + claim.year)`——不包含 claim_id，确保不同 claim_id 指向同一 (source 内容, 声明) 时也命中缓存
- 缓存内容：`{verdict, confidence, evidence_quote, evidence_locator, reasoning}`
- 命中即返回 `VerifyResult(...)`，跳过 OpenAI 调用

**预期收益**：第二次跑同一份 PDF 输入的 LLM 调用量降到 0；不同 PDF 但引用相同 source 的 claim 也直接复用。

### 修复 3：BM25 零命中时跳过 LLM

**当前问题**：[verifier.py:87-104 `retrieve_passages`](src/market_source_verification_agent/verifier.py#L87) 当所有 BM25 分数 == 0 时返回前 K 段（locator 标了 `(fallback)`），然后 [verifier.py:73-84 `verify`](src/market_source_verification_agent/verifier.py#L73) 仍把这堆兜底段落喂给 LLM。绝大多数情况 LLM 会判 not_found，纯粹烧 token。

**改造**：在 [verify()](src/market_source_verification_agent/verifier.py#L28) 中检测 passages 是否全是 fallback（`all(p.score == 0.0 for p in passages)` 或 locator 包含 `(fallback)`），是则直接返回：
```python
return VerifyResult(
    claim_id=claim.claim_id,
    verdict="not_found",
    confidence=0.15,
    reasoning="no lexical overlap with source after synonym expansion (LLM skipped to save tokens)",
)
```

**安全网**：同义词扩展 [`_expand_query_text`](src/market_source_verification_agent/verifier.py#L98) 已经存在；用户已经维护了 [verifier_synonyms.yaml](config/verifier_synonyms.yaml)。如果某个真支持的 claim 因为同义词没覆盖被错判 not_found，正确做法是补 `verifier_synonyms.yaml`，不是浪费 LLM token 兜底。

**预期收益**：根据当前截图（33 claim，2 个未命中），保守估计减少 5–15% LLM 调用；如果 PDF 提取失败修好后 source 内容更丰富，BM25 命中率会更高，节省可能更显著。

### 修复 4：[reporter.py](src/market_source_verification_agent/reporter.py) 中文文案补充

新增 fetch_status 映射：
- `empty_body` → 「服务器返回空响应（疑似反爬或登录重定向），无法核验」
- 已有的 `"PDF已下载，但文本提取为空"` 分支保留（覆盖真扫描件）

---

## 不做的事（明确边界）

- ❌ **不引入 OCR**：本轮只解决"下载就是空"的问题；真扫描件留给后续。引入 PaddleOCR 等会带几十 MB 依赖、推理时间 + 模型权重，性价比远低于先把下载修对。
- ❌ **不加 PyMuPDF 备选引擎**：当前问题不是 pdfplumber 解析失败，而是文件本身 0 字节。pdfplumber 没出错就别多此一举。
- ❌ **不重写 source-level 去重逻辑**：orchestrator 已有 claim 级 dedup（[orchestrator.py:187-217](src/market_source_verification_agent/orchestrator.py#L187)），加 verifier 缓存后效果与 source-dedup 等价，避免改 orchestrator 的复杂调度。

---

## 关键文件

- [src/market_source_verification_agent/resolver.py](src/market_source_verification_agent/resolver.py) — `_http_get_with_retry` (line ~191)、`_persist` (line ~178)
- [src/market_source_verification_agent/verifier.py](src/market_source_verification_agent/verifier.py) — `verify` (line ~28)、`_judge_by_llm` (line ~209)、`retrieve_passages` (line ~87)
- [src/market_source_verification_agent/schema.py:54](src/market_source_verification_agent/schema.py#L54) — `fetch_status` Literal 加 `empty_body`
- [src/market_source_verification_agent/reporter.py:282-294](src/market_source_verification_agent/reporter.py#L282) — 中文诊断文案
- 复用：[classifier.py:178-213](src/market_source_verification_agent/classifier.py#L178) 的缓存模板、[config/settings.yaml:13-17](config/settings.yaml#L13) 的 `cache.verify_ttl_days`

## 实施步骤

1. ✅ **schema.py** 加 `"empty_body"` 到 `fetch_status` Literal。
2. ✅ **resolver.py** 在 `_http_get_with_retry` 加 0 字节守卫 + UA 切换重试；`_persist` 加防御性返回 None。
3. ✅ **verifier.py** 把 classifier 的缓存三件套（`_llm_cache_paths` / `_read_cache` / `_write_cache`）抽出来或复制一份，包到 `_judge_by_llm` 入口与出口。
4. ✅ **verifier.py** `verify()` 中检测全 fallback passages，直接返回 not_found（不调 LLM）。
5. ✅ **reporter.py** 中文文案补 `empty_body` 映射。
6. ✅ **测试更新**：
   - [tests/test_resolver_extraction.py](tests/test_resolver_extraction.py) 加用例：HTTP 200 + 空 body 不应写缓存。
   - [tests/test_verifier_semantics.py](tests/test_verifier_semantics.py) 加用例：BM25 零命中时不调 LLM；缓存命中时不调 LLM。
   - [tests/test_multi_source_reporting.py](tests/test_multi_source_reporting.py) 加用例：`empty_body` 中文诊断文案。

## 验证

- 跑 `examples/低空经济_input.pdf`，对比修复前后：
  - 「缓存文件为 0 字节」诊断应大幅减少（重试 + UA 切换能挽救一部分），剩余的应明确显示为「服务器返回空响应」而非含糊的「PDF 已下载但提取为空」。
  - 第二次跑同一份输入，LLM 调用量应接近 0（缓存全命中）。
  - 监控 `data/cache/verify/` 目录文件数应 ≈ 第一轮 LLM 调用次数。
- 单测：上述 3 项新用例全过 + 现有 36 个测试不回归。
