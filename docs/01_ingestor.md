# 01 · Ingestor — 文档解析

## 职责

把多种输入统一转成「中间结构」(Intermediate Representation, IR)，**只做忠实抽取，不做语义理解**。语义层交给 [02 Extractor](02_extractor.md)。

## 输入

| 输入类型 | 处理方式 |
|---|---|
| `.pdf` | `pdfplumber` 提取文本 + 表格 bbox |
| `.docx` | `python-docx` 遍历段落和表格 |
| `.txt` / 粘贴文本 | 直接当作纯文本块 |
| `.md` | 当作纯文本块（保留 GFM 表格） |

## 输出（IR Schema）

详见 [09 Schema](09_schema.md#ir)。简化版：

```json
{
  "doc_id": "lkjj_2025",
  "source_format": "pdf",
  "blocks": [
    {"type": "heading", "level": 2, "text": "1. 整体市场规模、增速、驱动因素"},
    {"type": "paragraph", "text": "..."},
    {"type": "table", "page": 1, "rows": [
      ["指标","数值","年份","地区/口径","来源名称","发布时间","备注"],
      ["低空经济规模","5059.5 亿元","2023","中国","赛迪研究院/赛迪智库 (https://www.news.cn/tech/20240402/1854417fb84a4b6d8322410c2eca31db/c.html)","2024","同源披露同比增速 33.8%"]
    ], "hyperlinks": {"4,1": "https://www.news.cn/tech/20240402/1854417fb84a4b6d8322410c2eca31db/c.html..."}}
  ]
}
```

关键：**保留原始超链接 / 脚注**。`pdfplumber` 默认不抽超链接 → 用 `PyMuPDF (fitz)` 补抽 annotations；页脚形如 `1 https://...` 的脚注会进入 `Block.footnotes`，供 Extractor 把来源列末尾上标解析为 URL。

## 关键函数

```python
def ingest(path_or_bytes: str | bytes, fmt: Literal["pdf","docx","txt","md","auto"]="auto") -> IR
def _parse_pdf(path) -> IR     # pdfplumber + fitz 合并
def _parse_docx(path) -> IR
def _parse_text(text: str) -> IR
```

## 已知坑

- PDF 跨页表格：基于「表头是否重复」启发式合并，续页重复表头会在拼接前丢弃。
- 合并单元格：`pdfplumber` 返回 `None`，需向上 forward-fill。
- 中文 PDF 字体嵌入异常 → 行内出现 `(cid:xxx)`：fallback 到 `pdfminer.six` 或调用 OCR（`paddleocr`，可选依赖）。
- Word 表格嵌套：递归展开，记录嵌套层级在 block 元数据里。

## 错误处理

- 解析失败抛 `IngestError(message, doc_id, page)`；上层 [Orchestrator](07_orchestrator.md) 决定中止或跳过。
- 空文档 / 0 表格 / 0 文本：返回空 IR，下游会报 `NoClaimsFound` 给用户。

## 依赖

`pdfplumber`, `pymupdf`, `python-docx`, `markdown-it-py`（解析 md 表格）。
