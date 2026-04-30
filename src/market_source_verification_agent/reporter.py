"""Report rendering for verified claims."""

from __future__ import annotations

import html
import io
import json
from typing import Literal

from .schema import Claim, ClassifyResult, IR, VerifyResult

ReportFormat = Literal["xlsx", "md", "html", "json"]

VERDICT_LABELS = {
    "supported": "✅ 支持",
    "partially_supported": "⚠️ 部分支持",
    "not_found": "❌ 未找到",
    "contradicted": "❗ 矛盾",
    "not_verifiable": "❓ 无法验证",
}

FILL_COLORS = {
    "supported": "C6EFCE",
    "partially_supported": "FFEB9C",
    "not_found": "D9D9D9",
    "contradicted": "FFC7CE",
    "not_verifiable": "BFBFBF",
}


def render(
    ir: IR,
    claims: list[Claim],
    verifies: dict[str, VerifyResult],
    classes: dict[str, ClassifyResult],
    fmt: ReportFormat = "xlsx",
    detailed: bool = False,
) -> bytes:
    rows = _report_rows(claims, verifies, classes, detailed)
    if fmt == "json":
        return json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8")
    if fmt == "md":
        return _render_md(rows, detailed).encode("utf-8")
    if fmt == "html":
        return _render_html(rows, detailed).encode("utf-8")
    return _render_xlsx(rows, detailed)


def summarize(verifies: dict[str, VerifyResult], classes: dict[str, ClassifyResult]) -> dict[str, int]:
    summary: dict[str, int] = {"total": len(verifies)}
    for result in verifies.values():
        summary[result.verdict] = summary.get(result.verdict, 0) + 1
    for result in classes.values():
        summary[result.tier] = summary.get(result.tier, 0) + 1
    return summary


def _report_rows(
    claims: list[Claim],
    verifies: dict[str, VerifyResult],
    classes: dict[str, ClassifyResult],
    detailed: bool,
) -> list[dict[str, str | None]]:
    rows: list[dict[str, str | None]] = []
    for claim in claims:
        verify = verifies[claim.claim_id]
        category = classes[claim.claim_id]
        row: dict[str, str | None] = {
            "章节": " / ".join(claim.section_path),
            "指标": claim.metric,
            "数值": claim.value,
            "年份": claim.year,
            "地区/口径": claim.region,
            "事实声明": claim.statement,
            "来源名称": claim.source_name_raw,
            "来源URL提示": claim.source_url_hint,
            "来源是否真实": VERDICT_LABELS[verify.verdict],
            "来源类别": category.tier,
        }
        if detailed:
            row["核验佐证"] = _detail_text(verify, category)
        rows.append(row)
    return rows


def _render_xlsx(rows: list[dict[str, str | None]], detailed: bool) -> bytes:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("xlsx output requires openpyxl; install project dependencies first") from exc

    wb = Workbook()
    ws = wb.active
    ws.title = "source_verification"
    headers = _headers(detailed)
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)

    for row in rows:
        ws.append([row.get(header) for header in headers])
        verdict_cell = ws.cell(row=ws.max_row, column=headers.index("来源是否真实") + 1)
        verdict_key = _verdict_key(str(verdict_cell.value))
        verdict_cell.fill = PatternFill(fill_type="solid", fgColor=FILL_COLORS.get(verdict_key, "FFFFFF"))

    widths = {"章节": 24, "指标": 24, "数值": 18, "年份": 14, "地区/口径": 18, "事实声明": 42, "来源名称": 34, "来源URL提示": 30, "来源是否真实": 16, "来源类别": 10, "核验佐证": 60}
    for idx, header in enumerate(headers, start=1):
        ws.column_dimensions[ws.cell(row=1, column=idx).column_letter].width = widths.get(header, 18)

    stream = io.BytesIO()
    wb.save(stream)
    return stream.getvalue()


def _render_md(rows: list[dict[str, str | None]], detailed: bool) -> str:
    headers = _headers(detailed)
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(_md_cell(row.get(header)) for header in headers) + " |")
    return "\n".join(lines) + "\n"


def _render_html(rows: list[dict[str, str | None]], detailed: bool) -> str:
    headers = _headers(detailed)
    head = "".join(f"<th>{html.escape(header)}</th>" for header in headers)
    body = []
    for row in rows:
        cells = "".join(f"<td>{html.escape(str(row.get(header) or ''))}</td>" for header in headers)
        body.append(f"<tr>{cells}</tr>")
    return f"""<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<title>来源核验报告</title>
<style>
body{{font-family:Arial,"Microsoft YaHei",sans-serif;margin:24px;color:#17202a}}
table{{border-collapse:collapse;width:100%;font-size:14px}}
th,td{{border:1px solid #d7dde5;padding:8px;vertical-align:top}}
th{{background:#f4f6f8;text-align:left}}
tr:nth-child(even){{background:#fbfcfd}}
</style>
<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>
</html>"""


def _headers(detailed: bool) -> list[str]:
    headers = ["章节", "指标", "数值", "年份", "地区/口径", "事实声明", "来源名称", "来源URL提示", "来源是否真实", "来源类别"]
    if detailed:
        headers.append("核验佐证")
    return headers


def _detail_text(verify: VerifyResult, category: ClassifyResult) -> str:
    parts = [verify.evidence_quote, verify.evidence_locator, verify.discrepancy, verify.reasoning, category.tier_reason]
    return "；".join(part for part in parts if part)


def _md_cell(value: str | None) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", "<br>")


def _verdict_key(label: str) -> str:
    for key, value in VERDICT_LABELS.items():
        if value == label:
            return key
    return ""
