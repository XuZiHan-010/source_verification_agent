"""Claim extraction from the intermediate representation."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import NoClaimsFound
from .schema import Block, Claim, IR

SOURCE_HEADERS = ("来源", "出处", "引用", "source")
METRIC_HEADERS = ("指标", "项目", "名称", "事件", "metric")
VALUE_HEADERS = ("数值", "规模", "金额", "数量", "value", "数据")
YEAR_HEADERS = ("年份", "时间", "年度", "日期", "year")
REGION_HEADERS = ("地区", "国家", "范围", "口径", "region")
PUBLISH_HEADERS = ("发布", "披露", "publish")
NOTE_HEADERS = ("备注", "说明", "notes", "口径")


@dataclass
class HeaderMap:
    metric: int | None = None
    value: int | None = None
    year: int | None = None
    region: int | None = None
    source: int | None = None
    publish_time: int | None = None
    notes: list[int] | None = None


def extract_claims(ir: IR, limit: int | None = None) -> list[Claim]:
    """Extract row-level claims using deterministic table heuristics."""

    claims: list[Claim] = []
    section_path: list[str] = []
    table_idx = 0
    para_idx = 0

    for block in ir.blocks:
        if block.type == "heading" and block.text:
            level = max(block.level or 1, 1)
            section_path = section_path[: level - 1] + [block.text.strip()]
        elif block.type == "table" and block.rows:
            table_idx += 1
            claims.extend(_table_to_claims(ir.doc_id, table_idx, block, section_path))
        elif block.type in {"paragraph", "list"} and block.text:
            para_idx += 1
            claim = _paragraph_to_claim(ir.doc_id, para_idx, block.text, section_path)
            if claim:
                claims.append(claim)
        if limit and len(claims) >= limit:
            return claims[:limit]

    if not claims:
        raise NoClaimsFound("No claims with source information were found")
    return claims


def _table_to_claims(doc_id: str, table_idx: int, block: Block, section_path: list[str]) -> list[Claim]:
    rows = [row for row in block.rows or [] if any(_clean(cell) for cell in row)]
    if len(rows) < 2:
        return []

    header_idx = _find_header_row(rows)
    headers = [_clean(cell) for cell in rows[header_idx]]
    mapping = _map_headers(headers)
    data_rows = rows[header_idx + 1 :]
    claims: list[Claim] = []

    for row_offset, row in enumerate(data_rows, start=header_idx + 2):
        cells = [_clean(cell) for cell in row]
        if not any(cells):
            continue
        raw_source = _get(cells, mapping.source)
        url_hint = _extract_url(raw_source) or _extract_row_url(block, row_offset - 1, mapping.source)
        if not raw_source and not url_hint:
            continue

        metric = _get(cells, mapping.metric)
        value = _get(cells, mapping.value)
        year = _get(cells, mapping.year)
        region = _get(cells, mapping.region)
        publish_time = _get(cells, mapping.publish_time)
        notes = _join_notes(cells, mapping)
        is_forecast = any("预测" in item or "预计" in item for item in (value, notes, metric) if item)
        statement = _build_statement(metric, value, year, region, cells)

        claims.append(
            Claim(
                claim_id=f"{doc_id}#t{table_idx}#r{row_offset}",
                section_path=list(section_path),
                metric=metric,
                value=value,
                year=year,
                region=region,
                statement=statement,
                source_name_raw=raw_source or url_hint or "",
                source_url_hint=url_hint,
                publish_time=publish_time,
                notes=notes,
                is_forecast=is_forecast,
            )
        )
    return claims


def _paragraph_to_claim(doc_id: str, para_idx: int, text: str, section_path: list[str]) -> Claim | None:
    source = _extract_parenthetical_source(text) or _extract_url(text)
    if not source:
        return None
    url_hint = _extract_url(text)
    statement = re.sub(r"（?来源[:：].*?）?$", "", text).strip()
    return Claim(
        claim_id=f"{doc_id}#p{para_idx}",
        section_path=list(section_path),
        metric=None,
        value=None,
        year=_extract_year(text),
        region=None,
        statement=statement,
        source_name_raw=source,
        source_url_hint=url_hint,
        publish_time=None,
        notes=None,
    )


def _find_header_row(rows: list[list[str | None]]) -> int:
    best_idx = 0
    best_score = -1
    for idx, row in enumerate(rows[:5]):
        text = "|".join(_clean(cell) for cell in row)
        score = sum(1 for key in SOURCE_HEADERS + METRIC_HEADERS + VALUE_HEADERS + YEAR_HEADERS if key.lower() in text.lower())
        if score > best_score:
            best_idx = idx
            best_score = score
    return best_idx


def _map_headers(headers: list[str]) -> HeaderMap:
    return HeaderMap(
        metric=_match_header(headers, METRIC_HEADERS),
        value=_match_header(headers, VALUE_HEADERS),
        year=_match_header(headers, YEAR_HEADERS),
        region=_match_header(headers, REGION_HEADERS),
        source=_match_header(headers, SOURCE_HEADERS),
        publish_time=_match_header(headers, PUBLISH_HEADERS),
        notes=[idx for idx, header in enumerate(headers) if any(key.lower() in header.lower() for key in NOTE_HEADERS)],
    )


def _match_header(headers: list[str], keywords: tuple[str, ...]) -> int | None:
    for idx, header in enumerate(headers):
        lowered = header.lower()
        if any(key.lower() in lowered for key in keywords):
            return idx
    return None


def _get(cells: list[str], idx: int | None) -> str | None:
    if idx is None or idx >= len(cells):
        return None
    return cells[idx] or None


def _join_notes(cells: list[str], mapping: HeaderMap) -> str | None:
    indexes = mapping.notes or []
    used = {mapping.metric, mapping.value, mapping.year, mapping.region, mapping.source, mapping.publish_time}
    values = [cells[idx] for idx in indexes if idx < len(cells) and idx not in used and cells[idx]]
    return "；".join(values) or None


def _build_statement(metric: str | None, value: str | None, year: str | None, region: str | None, cells: list[str]) -> str:
    parts = [year, region, metric, value]
    statement = " ".join(part for part in parts if part)
    if statement:
        return statement
    return "；".join(cell for cell in cells if cell)


def _extract_row_url(block: Block, zero_based_row: int, source_col: int | None) -> str | None:
    if source_col is None:
        return None
    return block.hyperlinks.get(f"{zero_based_row},{source_col}")


def _extract_url(text: str | None) -> str | None:
    if not text:
        return None
    match = re.search(r"https?://[^\s)）\]]+", text)
    if match:
        return match.group(0)
    domain = re.search(r"\b(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}\b", text)
    return domain.group(0) if domain else None


def _extract_parenthetical_source(text: str) -> str | None:
    match = re.search(r"来源[:：]\s*([^。；;\n）)]+)", text)
    return match.group(1).strip() if match else None


def _extract_year(text: str) -> str | None:
    match = re.search(r"(20\d{2}|19\d{2})(?:\s*年(?:底|末)?)?", text)
    return match.group(0) if match else None


def _clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()
