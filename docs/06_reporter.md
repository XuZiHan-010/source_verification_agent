# 06 · Reporter — 报告生成

## 职责

把 [Verifier](04_verifier.md) 与 [Classifier](05_classifier.md) 的结果合并回原表格，**追加两列**输出。

## 输入

- 原始 IR（保留原表格结构 / 列顺序）
- `list[VerifyResult]`、`list[ClassifyResult]`（按 `claim_id` join）

## 输出格式

| 格式 | 用途 |
|---|---|
| `.xlsx` | 默认；用 openpyxl，verdict 列做条件着色（绿/黄/红/灰） |
| `.md` | GitHub flavored Markdown，用于直接贴回 issue / 报告 |
| `.html` | 自包含单文件，含可折叠的「证据原文」抽屉 |
| `.json` | 调试用全字段导出 |

## 追加的两列

| 列名 | 取值 | 来源 |
|---|---|---|
| `来源是否真实` | `✅ 支持` / `⚠️ 部分支持` / `❌ 未找到` / `❗ 矛盾` / `❓ 无法验证` | VerifyResult.verdict |
| `来源类别` | `A` / `B` / `C` | ClassifyResult.tier |

可选第三列（`detailed=true` 时输出）：
- `核验佐证` — 引用原文片段 + 链接（`evidence_quote` + `evidence_locator`）

## 渲染规则

- 原表格列**完全不动**（保留原顺序、合并单元格、表头层级）；
- 新列追加到最右，标题行加粗；
- xlsx 中 verdict 单元格背景色：supported=#C6EFCE / partial=#FFEB9C / not_found=#D9D9D9 / contradicted=#FFC7CE / not_verifiable=#BFBFBF；
- 每张表格上方插入一行汇总：`本表 N 行，A=x B=y C=z；✅=a ⚠️=b ❌=c ❗=d ❓=e`。

## 关键函数

```python
def render(ir: IR, verifies: dict[str, VerifyResult], classes: dict[str, ClassifyResult],
           fmt: Literal["xlsx","md","html","json"], detailed: bool=False) -> bytes
def _render_xlsx(...) -> bytes
def _render_md(...) -> str
```

## 端到端样例（基于低空经济 PDF 1.1 节）

输入行：

| 指标 | 数值 | 年份 | 来源名称 |
|---|---|---|---|
| 低空经济规模 | 5059.5 亿元 | 2023 | 赛迪研究院/赛迪智库 (pdf.dfcfw.com) |

输出行（追加两列）：

| 指标 | 数值 | 年份 | 来源名称 | **来源是否真实** | **来源类别** |
|---|---|---|---|---|---|
| 低空经济规模 | 5059.5 亿元 | 2023 | 赛迪研究院/赛迪智库 (pdf.dfcfw.com) | ✅ 支持 | B |

## 依赖

`openpyxl`, `jinja2`（html 模板）, `pandas`。
