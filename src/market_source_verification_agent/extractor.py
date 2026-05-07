"""Claim extraction from the intermediate representation."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from .errors import NoClaimsFound
from .schema import Block, Claim, IR

logger = logging.getLogger(__name__)


def _compact_text(value: str | None) -> str:
    return re.sub(r"\s+", "", value or "")


def _looks_like_broken_fragment(value: str | None) -> bool:
    """识别 PDF 表格切片把一个 cell 拆成残片的情况。命中即应丢弃。"""
    if not value:
        return False
    text = value.strip()
    compact = _compact_text(text)
    if not compact:
        return False
    # 规则 1：以连接符/标点开头（"、维修"、"/应用场景"、"，金"）
    if text[0] in "、，,/／；;。.":
        return True
    # 规则 2：极短 + 无实义（"等。"、"护"、";"）
    if len(compact) <= 3 and not re.search(r"[0-9A-Za-z]", compact):
        return True
    # 规则 3：明显被前缀截掉（启发式）
    leading_broken_prefixes = ("器器", "护、", "控系统", "金、", "事项；", "护，")
    if any(text.startswith(prefix) for prefix in leading_broken_prefixes):
        return True
    # 规则 4：以引号"半开"结尾（如 "事项；'低空经"），未闭合
    if text.count("“") != text.count("”") and len(compact) <= 12:
        return True
    return False


SOURCE_NOISE = {
    "",
    "等",
    "类（试行）",
    "类（试行）。",
    "当前状态",
    "原文表述",
    "原文表述/摘要",
    "摘要",
    "来源",
    "来源名称",
    "发布",
    "报告年份",
    "未找到可靠公开来源",
    "未找到可靠公开来源。",
}

SOURCE_HINT_KEYWORDS = (
    "网",
    "院",
    "部",
    "局",
    "署",
    "委",
    "协会",
    "报告",
    "白皮书",
    "官网",
    "新华",
    "政府",
    "信通院",
    "赛迪",
    "财经",
    "report",
)

SOURCE_HEADERS = ("来源", "出处", "引用", "source")
METRIC_HEADERS = ("指标", "项目", "名称", "事件", "政策", "法规", "标准", "监管事项", "metric")
VALUE_HEADERS = ("数值", "规模", "金额", "数量", "核心内容", "具体要求", "value", "数据")
YEAR_HEADERS = ("年份", "时间", "年度", "日期", "year")
REGION_HEADERS = ("地区", "国家", "范围", "口径", "适用", "适用地区", "region")
PUBLISH_HEADERS = ("发布时间", "披露", "publish")
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


def extract_claims(ir: IR, limit: int | None = None, settings=None) -> list[Claim]:
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
            claims.extend(_table_to_claims(ir.doc_id, table_idx, block, section_path, settings=settings))
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


def _table_to_claims(doc_id: str, table_idx: int, block: Block, section_path: list[str], settings=None) -> list[Claim]:
    rows = [row for row in block.rows or [] if any(_clean(cell) for cell in row)]
    if len(rows) < 2:
        return []

    header_idx = _find_header_row(rows)
    headers = [_clean(cell) for cell in rows[header_idx]]
    mapping = _map_headers(headers)
    if _looks_like_policy_table(headers):
        return _policy_table_to_claims(doc_id, table_idx, rows[header_idx + 1 :], headers, block, section_path, settings=settings)
    if _looks_like_regulatory_table(headers):
        return _regulatory_table_to_claims(doc_id, table_idx, rows[header_idx + 1 :], headers, block, section_path, settings=settings)
    if _looks_like_matrix_table(headers):
        return _matrix_table_to_claims(doc_id, table_idx, rows[header_idx + 1 :], headers, block, section_path, settings=settings)
    data_rows = _merge_logical_rows(rows[header_idx + 1 :], mapping)
    claims: list[Claim] = []

    for row_offset, row in enumerate(data_rows, start=header_idx + 2):
        cells = [_clean(cell) for cell in row]
        if not any(cells) or _is_non_claim_row(cells, mapping):
            continue
        raw_source = _get(cells, mapping.source)
        clean_source, footnote_urls = _resolve_footnote_urls(raw_source or "", block.footnotes)
        if not footnote_urls or (clean_source and not _is_valid_source_candidate(clean_source) and not re.search(r"[A-Za-z]{4,}", clean_source)):
            fallback_source, fallback_urls = _resolve_row_footnote_source(cells, block.footnotes, mapping)
            if fallback_urls:
                raw_source = fallback_source
                clean_source, footnote_urls = _resolve_footnote_urls(fallback_source, block.footnotes)
        url_hint = (
            footnote_urls[0]
            if footnote_urls
            else _extract_url(raw_source) or _extract_row_url(block, row_offset - 1, mapping.source)
        )
        if not raw_source and not url_hint:
            continue
        source_name = _sanitize_source_name(clean_source or raw_source or url_hint or "")
        if not url_hint and not source_name:
            continue
        if not url_hint and not _is_valid_source_candidate(source_name):
            continue

        metric = _get(cells, mapping.metric)
        value = _get(cells, mapping.value)
        if _looks_like_broken_fragment(metric) or _looks_like_broken_fragment(value):
            continue
        year = _normalize_year(_get(cells, mapping.year))
        region = _get(cells, mapping.region)
        publish_time = _get(cells, mapping.publish_time)
        notes = _join_notes(cells, mapping)
        is_forecast = any("预测" in item or "预计" in item for item in (value, notes, metric) if item)
        statement = _build_statement(metric, value, year, region, cells, mapping)

        claims.append(
            Claim(
                claim_id=f"{doc_id}#t{table_idx}#r{row_offset}",
                section_path=list(section_path),
                metric=metric,
                value=value,
                year=year,
                region=region,
                statement=statement,
                source_name_raw=source_name,
                source_url_hint=url_hint,
                source_urls=_dedupe_urls([url_hint, *footnote_urls[1:]]),
                extra_source_urls=footnote_urls[1:],
                source_name_with_marks=raw_source if footnote_urls else None,
                publish_time=publish_time,
                notes=notes,
                is_forecast=is_forecast,
            )
        )
    return claims


def _paragraph_to_claim(doc_id: str, para_idx: int, text: str, section_path: list[str]) -> Claim | None:
    if _looks_like_pdf_table_dump(text):
        return None
    source = _extract_parenthetical_source(text) or _extract_url(text)
    if not source:
        return None
    url_hint = _extract_url(text)
    statement = _strip_footnote_url_lines(re.sub(r"（?来源[:：].*?）?$", "", text)).strip()
    return Claim(
        claim_id=f"{doc_id}#p{para_idx}",
        section_path=list(section_path),
        metric=None,
        value=None,
        year=_normalize_year(_extract_year(text)),
        region=None,
        statement=statement,
        source_name_raw=_sanitize_source_name(source),
        source_url_hint=url_hint,
        source_urls=_dedupe_urls([url_hint]),
        publish_time=None,
        notes=None,
    )


def _looks_like_pdf_table_dump(text: str) -> bool:
    compact = _compact_text(text)
    url_count = len(re.findall(r"https?://", text))
    header_hits = sum(1 for item in ("指标", "数值", "年份", "来源", "备注") if item in compact)
    numbered_url_lines = len(re.findall(r"(?:^|\n)\s*\d{1,3}\s*https?://", text))
    return url_count >= 3 and (header_hits >= 3 or numbered_url_lines >= 2)


def _strip_footnote_url_lines(text: str) -> str:
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if re.match(r"^\d{1,3}\s*https?://", stripped):
            continue
        lines.append(line)
    clean = "\n".join(lines)
    return re.sub(r"\s+\d{1,3}\s*https?://\S+", "", clean)


def _looks_like_matrix_table(headers: list[str]) -> bool:
    compact = [_compact_text(header) for header in headers]
    return any("分类" in cell for cell in compact) and any("来源" in cell for cell in compact) and any("备注" in cell for cell in compact)


def _looks_like_policy_table(headers: list[str]) -> bool:
    compact = [_compact_text(header) for header in headers]
    return (
        any(any(key in cell for key in ("政策", "法规", "标准")) for cell in compact)
        and any("核心内容" in cell for cell in compact)
        and any("来源" in cell for cell in compact)
    )


def _looks_like_regulatory_table(headers: list[str]) -> bool:
    compact = [_compact_text(header) for header in headers]
    return (
        any("具体要求" in cell for cell in compact)
        and any("适用对象" in cell for cell in compact)
        and any("来源" in cell for cell in compact)
    )


def _policy_table_to_claims(
    doc_id: str,
    table_idx: int,
    rows: list[list[str | None]],
    headers: list[str],
    block: Block,
    section_path: list[str],
    settings=None,
) -> list[Claim]:
    normalized = [[_clean(cell) for cell in row] for row in rows if any(_clean(cell) for cell in row)]
    if not normalized:
        return []

    # 检测列数不一致（在填充前）
    expected_cols = len(headers)
    has_column_mismatch = any(len(row) != expected_cols for row in normalized)

    # 如果有列数不匹配，先尝试用LLM修复
    if has_column_mismatch and settings:
        llm_result = _llm_normalize_table_columns(normalized, headers, settings=settings)
        if llm_result:
            normalized = llm_result
            has_column_mismatch = False

    # 如果LLM未能修复，则用简单的填充方法
    if has_column_mismatch:
        normalized = [row + [""] * (expected_cols - len(row)) if len(row) < expected_cols else row for row in normalized]

    metric_idx = _header_index(headers, ("政策", "法规", "标准")) or 0
    agency_idx = _header_index(headers, ("发布机构",))
    year_idx = _header_index(headers, ("发布时间", "时间", "日期"))
    source_idx = _header_index(headers, ("来源",))
    if source_idx is None:
        return []

    if has_column_mismatch:
        # 对于列数不一致的表格，从源列往前查找内容列
        # 源信息通常在末尾，所以内容在它之前
        if source_idx is not None and source_idx > 0:
            core_start = source_idx - 1
            # 如果倒数第二列是空的，往前找
            while core_start > metric_idx and all(not _clean(row[core_start] if core_start < len(row) else "") for row in normalized):
                core_start -= 1
        else:
            # 如果没有source_idx，查找除了最后几列的最长内容列
            core_start = _find_content_column_by_characteristics(normalized, start_search=metric_idx + 1, end_search=max(len(headers) - 2, metric_idx + 2))
    else:
        # 列数一致，优先用列名，其次用智能检测
        core_start = _header_index(headers, ("核心内容",))
        if core_start is None:
            core_start = _find_content_column_by_characteristics(normalized, start_search=metric_idx + 1, end_search=source_idx or len(headers))

    core_indexes = _content_indexes_between(headers, core_start, source_idx)
    region_indexes = [idx for idx in (3, 4, 5) if idx < source_idx]

    claims: list[Claim] = []
    for row_offset, group in enumerate(_policy_groups(normalized, metric_idx), start=1):
        policy_text, policy_urls = _resolve_footnote_urls(_join_group_column(group, metric_idx), block.footnotes)
        source_text, source_urls = _resolve_footnote_urls(_join_group_column(group, source_idx), block.footnotes)
        urls = _dedupe_urls([*source_urls, *policy_urls])
        if not urls:
            fallback_source, fallback_urls = _resolve_any_footnote_source(group, block.footnotes)
            if fallback_urls:
                source_text = source_text or fallback_source
                urls = fallback_urls
        policy_name = _clean_statement_part(policy_text)
        core_text = _join_policy_cells(group, core_indexes)
        if not policy_name or not core_text or not urls:
            continue
        agency = _clean_statement_part(_join_group_column(group, agency_idx)) if agency_idx is not None else None
        year = _normalize_year(_join_group_column(group, year_idx)) if year_idx is not None else None
        region = _first_meaningful_region(group, region_indexes)
        source_name = _sanitize_source_name(source_text) or _source_name_from_urls(urls)
        statement = _clean(" ".join(part for part in [year, region, policy_name, agency, core_text] if part))
        claims.append(
            Claim(
                claim_id=f"{doc_id}#t{table_idx}#r{row_offset}",
                section_path=list(section_path),
                metric=policy_name,
                value=core_text,
                year=year,
                region=region,
                statement=statement,
                source_name_raw=source_name,
                source_url_hint=urls[0],
                source_urls=_dedupe_urls(urls),
                extra_source_urls=urls[1:],
                source_name_with_marks=source_text or None,
                publish_time=None,
                notes=None,
                is_forecast=False,
            )
        )
    return claims


def _regulatory_table_to_claims(
    doc_id: str,
    table_idx: int,
    rows: list[list[str | None]],
    headers: list[str],
    block: Block,
    section_path: list[str],
    settings=None,
) -> list[Claim]:
    normalized = [[_clean(cell) for cell in row] for row in rows if any(_clean(cell) for cell in row)]
    if not normalized:
        return []

    # Handle rows with mismatched column counts (from PDF merged cells)
    expected_cols = len(headers)
    has_column_mismatch = any(len(row) != expected_cols for row in normalized)

    # 如果有列数不匹配，先尝试用LLM修复
    if has_column_mismatch and settings:
        llm_result = _llm_normalize_table_columns(normalized, headers, settings=settings)
        if llm_result:
            normalized = llm_result
            has_column_mismatch = False

    # 如果LLM未能修复，则用简单的填充方法
    if has_column_mismatch:
        normalized = [row + [""] * (expected_cols - len(row)) if len(row) < expected_cols else row for row in normalized]

    req_idx = _header_index(headers, ("具体要求",))
    if req_idx is None or len(normalized) > 0 and len(normalized[0]) != len(headers):
        # 使用智能检测找具体要求列
        req_idx = _find_content_column_by_characteristics(normalized, start_search=1, end_search=min(6, len(headers)))
    else:
        # 验证找到的列
        req_idx_smart = _find_content_column_by_characteristics(normalized, start_search=1, end_search=min(6, len(headers)))
        if abs(req_idx_smart - req_idx) > 2:
            req_idx = req_idx_smart

    object_idx = _header_index(headers, ("适用对象",))
    if object_idx is None or len(normalized) > 0 and len(normalized[0]) != len(headers):
        # 使用智能检测找适用对象列
        search_start = min((req_idx or 3) + 1, len(headers) - 1)
        object_idx = _find_content_column_by_characteristics(normalized, start_search=search_start, end_search=len(headers) - 2)
    else:
        # 验证找到的列
        search_start = min((req_idx or 3) + 1, len(headers) - 1)
        object_idx_smart = _find_content_column_by_characteristics(normalized, start_search=search_start, end_search=len(headers) - 2)
        if abs(object_idx_smart - object_idx) > 2:
            object_idx = object_idx_smart
    agency_idx = _header_index(headers, ("发布机构",))
    source_idx = _header_index(headers, ("来源",))
    note_idx = _header_index(headers, ("备注",))
    if source_idx is None:
        return []

    groups = _regulatory_groups(normalized, req_idx, object_idx, source_idx, note_idx)
    claims: list[Claim] = []
    previous_metric = ""
    for row_offset, group in enumerate(groups, start=1):
        metric = _regulatory_metric(group, req_idx, previous_metric)
        if metric:
            previous_metric = metric
        requirement = _join_policy_cells(group, range(req_idx, object_idx))
        objects = _join_policy_cells(group, range(object_idx, agency_idx or source_idx))
        agency = _join_policy_cells(group, range(agency_idx, source_idx)) if agency_idx is not None else ""
        source_text, urls = _resolve_footnote_urls(_join_group_column(group, source_idx), block.footnotes)
        if not urls:
            fallback_source, urls = _resolve_any_footnote_source(group, block.footnotes)
            if fallback_source and not source_text:
                source_text = fallback_source
        notes = _join_policy_cells(group, range(note_idx, len(headers))) if note_idx is not None else ""
        if not metric or not requirement or not urls:
            continue
        source_name = _sanitize_source_name(source_text) or _source_name_from_urls(urls)
        statement = _clean(" ".join(part for part in [metric, requirement, objects, agency, notes] if part))
        claims.append(
            Claim(
                claim_id=f"{doc_id}#t{table_idx}#r{row_offset}",
                section_path=list(section_path),
                metric=metric,
                value=requirement,
                year=None,
                region=objects or None,
                statement=statement,
                source_name_raw=source_name,
                source_url_hint=urls[0],
                source_urls=_dedupe_urls(urls),
                extra_source_urls=urls[1:],
                source_name_with_marks=source_text or None,
                publish_time=None,
                notes=notes or None,
                is_forecast=False,
            )
        )
    return claims


def _matrix_table_to_claims(
    doc_id: str,
    table_idx: int,
    rows: list[list[str | None]],
    headers: list[str],
    block: Block,
    section_path: list[str],
    settings=None,
) -> list[Claim]:
    normalized = [[_clean(cell) for cell in row] for row in rows if any(_clean(cell) for cell in row)]
    if not normalized:
        return []

    # Handle rows with mismatched column counts (from PDF merged cells)
    expected_cols = len(headers)
    has_column_mismatch = any(len(row) != expected_cols for row in normalized)

    # 如果有列数不匹配，先尝试用LLM修复
    if has_column_mismatch and settings:
        llm_result = _llm_normalize_table_columns(normalized, headers, settings=settings)
        if llm_result:
            normalized = llm_result
            has_column_mismatch = False

    # 如果LLM未能修复，则用简单的填充方法
    if has_column_mismatch:
        normalized = [row + [""] * (expected_cols - len(row)) if len(row) < expected_cols else row for row in normalized]

    category_idx = _header_index(headers, ("分类",)) or 0
    product_idx = _header_index(headers, ("代表产品", "服务"))
    source_idx = _header_index(headers, ("来源",))
    note_idx = _header_index(headers, ("备注",))
    non_empty_headers = [idx for idx, header in enumerate(headers) if _clean(header)]
    field_indexes = [idx for idx in non_empty_headers if idx not in {category_idx, source_idx}]

    claims: list[Claim] = []
    groups = _matrix_groups(normalized, category_idx, source_idx, note_idx)
    for row_offset, group in enumerate(groups, start=1):
        base = group[0]
        category = _cell_at(base, category_idx) or ""
        if not category or _looks_like_broken_fragment(category):
            continue
        source_text = _join_group_column(group, source_idx) if source_idx is not None else ""
        source_name = _sanitize_source_name(source_text)
        _, source_col_urls = _resolve_footnote_urls(source_text, block.footnotes)

        # Collect every non-empty field cell in the group as a (label, text)
        # contribution to a single aggregated claim, instead of emitting one
        # claim per cell (which used to inflate ~30 logical rows into ~80
        # claims).
        kept: list[tuple[int, str, str, str, list[str]]] = []  # (col_idx, label, clean_text, value, urls)
        used_columns: set[int] = set()
        for field_idx in field_indexes:
            start_idx, end_idx = _matrix_field_bounds(headers, non_empty_headers, field_idx)
            for col_idx in _matrix_field_columns(group, headers, field_idx, start_idx, end_idx, used_columns, block.footnotes):
                cell_text = _join_group_column(group, col_idx)
                clean_text, urls = _resolve_footnote_urls(cell_text, block.footnotes)
                value = _sanitize_matrix_value(clean_text)
                if not value or value in {_clean(headers[field_idx]), category}:
                    continue
                if _looks_like_broken_fragment(value):
                    continue
                used_columns.add(col_idx)
                label = _clean(headers[field_idx])
                kept.append((col_idx, label, clean_text, value, urls))

        all_urls = _dedupe_urls([*source_col_urls, *(u for entry in kept for u in entry[4])])
        if not all_urls:
            continue
        if not kept:
            primary_value = category
            statement = _clean(category)
        else:
            primary_entry = next(
                (entry for entry in kept if product_idx is not None and entry[0] == product_idx),
                kept[0],
            )
            primary_value = primary_entry[3]
            statement_parts = [
                f"{label}：{text}" if label else text for _, label, text, _, _ in kept
            ]
            statement = _clean(f"{category}｜" + "；".join(statement_parts))

        claims.append(
            Claim(
                claim_id=f"{doc_id}#t{table_idx}#r{row_offset}",
                section_path=list(section_path),
                metric=_clean(category),
                value=primary_value,
                year=None,
                region=None,
                statement=statement,
                source_name_raw=source_name or _source_name_from_urls(all_urls),
                source_url_hint=all_urls[0],
                source_urls=all_urls,
                extra_source_urls=all_urls[1:],
                source_name_with_marks=source_text or None,
                publish_time=None,
                notes=None,
                is_forecast=False,
            )
        )
    return claims


def _merge_logical_rows(rows: list[list[str | None]], mapping: HeaderMap) -> list[list[str | None]]:
    merged: list[list[str | None]] = []
    for row in rows:
        cells = [_clean(cell) for cell in row]
        if not any(cells):
            continue
        if merged and _is_fragment_of_previous(merged[-1], cells, mapping):
            merged[-1] = _merge_row_cells(merged[-1], cells, mapping)
        else:
            merged.append(cells)
    return merged


def _header_index(headers: list[str], keywords: tuple[str, ...]) -> int | None:
    for idx, header in enumerate(headers):
        if header is None:
            continue
        lowered = header.lower() if isinstance(header, str) else str(header).lower()
        if any(keyword.lower() in lowered for keyword in keywords):
            return idx
    return None


def _neighbor_content_col(rows: list[list[str]], header_idx: int | None, prefer: str) -> int | None:
    if header_idx is None:
        return None
    candidates = [header_idx, header_idx + 1, header_idx + 2, header_idx - 1, header_idx - 2]
    if prefer == "left":
        candidates = [header_idx, header_idx - 1, header_idx - 2, header_idx + 1, header_idx + 2]
    header_text = _compact_text(rows[0][header_idx]) if rows and header_idx < len(rows[0]) and rows[0][header_idx] else ""
    for idx in candidates:
        if idx < 0:
            continue
        values = []
        for row in rows[:20]:
            if idx < len(row) and row[idx]:
                compact = _compact_text(row[idx])
                if compact and compact != header_text:
                    values.append(compact)
        if values:
            return idx
    return header_idx


def _matrix_groups(rows: list[list[str]], category_idx: int, source_idx: int | None, note_idx: int | None) -> list[list[list[str]]]:
    groups: list[list[list[str]]] = []
    current: list[list[str]] = []
    current_key: tuple[str, ...] | None = None
    for row in rows:
        key = tuple(
            _compact_text(_cell_at(row, idx))
            for idx in (category_idx, source_idx, note_idx)
            if idx is not None
        )
        if current and (not any(key) or (not key[0] and any(key[1:]))):
            current.append(row)
            continue
        if current_key is None or key == current_key:
            current.append(row)
            current_key = key
        else:
            groups.append(current)
            current = [row]
            current_key = key
    if current:
        groups.append(current)
    return groups


def _matrix_field_bounds(headers: list[str], field_indexes: list[int], field_idx: int) -> tuple[int, int]:
    position = field_indexes.index(field_idx)
    prev_idx = field_indexes[position - 1] if position > 0 else -1
    next_idx = field_indexes[position + 1] if position + 1 < len(field_indexes) else len(headers)
    left = field_idx if prev_idx < 0 else (prev_idx + field_idx) // 2 + 1
    right = max(field_idx, next_idx - 1 if next_idx == len(headers) else (field_idx + next_idx) // 2)
    return left, right


def _matrix_field_columns(
    rows: list[list[str]],
    headers: list[str],
    field_idx: int,
    start_idx: int,
    end_idx: int,
    used_columns: set[int],
    footnotes: dict[int, str],
) -> list[int]:
    candidates: list[tuple[float, int]] = []
    label = _clean(headers[field_idx])
    for idx in range(start_idx, end_idx + 1):
        if idx in used_columns:
            continue
        joined = _join_group_column(rows, idx)
        compact = _compact_text(joined)
        if not compact or compact == _compact_text(label):
            continue
        score = _matrix_column_score(rows, idx, label, footnotes)
        if score > 0:
            candidates.append((score, idx))
    candidates.sort(reverse=True)
    return [idx for _, idx in candidates]


def _matrix_column_score(rows: list[list[str]], idx: int, label: str, footnotes: dict[int, str]) -> float:
    snippets = [_cell_at(row, idx) or "" for row in rows]
    non_empty = [item for item in snippets if _clean(item)]
    if not non_empty:
        return 0.0
    joined = _join_group_column(rows, idx)
    clean_text, urls = _resolve_footnote_urls(joined, footnotes)
    compact_label = _compact_text(label)
    compact_joined = _compact_text(clean_text)
    if not urls or compact_joined == compact_label:
        return 0.0

    score = 0.0
    score += len(urls) * 10
    score += len(non_empty)
    score += len(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", clean_text)) / 12
    if any(_compact_text(item) == compact_label for item in non_empty):
        score -= 2
    if all(_looks_like_short_fragment(item) for item in non_empty):
        score -= 4
    return score


def _matrix_metric_label(
    category: str,
    headers: list[str],
    idx: int,
    product_idx: int | None,
    note_idx: int | None,
) -> str:
    if idx == product_idx:
        return _clean(category)
    if idx == note_idx:
        return _clean(f"{category} / 备注")
    label = ""
    for probe in (idx, idx - 1, idx + 1, idx - 2, idx + 2):
        if 0 <= probe < len(headers):
            candidate = _clean(headers[probe])
            if candidate and candidate not in {category, "来源", "备注", "分类", "代表产品/服务"}:
                label = candidate
                break
    if not label:
        label = f"字段{idx + 1}"
    return _clean(f"{category} / {label}")


def _join_group_column(rows: list[list[str]], idx: int) -> str:
    parts: list[str] = []
    for row in rows:
        # Handle rows with variable column counts (from PDF merged cells)
        if idx < len(row):
            value = _cell_at(row, idx)
            if value and value not in parts:
                parts.append(value)
    return _clean("".join(parts))


def _find_content_column_by_characteristics(
    rows: list[list[str]],
    start_search: int = 0,
    end_search: int | None = None,
) -> int:
    """智能检测内容列，基于内容特征而非位置。

    查找看起来最像"政策内容"的列：
    - 有实质内容的行数多
    - 内容相对较长
    - 不完全是日期、地区等特殊类型
    """
    if end_search is None and rows:
        end_search = len(rows[0])
    elif end_search is None:
        return start_search

    # 判断内容类型的关键词
    SOURCE_KEYWORDS = {"政府", "网", "部", "机构", "报", "社", "新华", "人民", "国务院"}
    DATE_INDICATORS = {"20", "202", "202", "年", "月", "日", "-"}
    LOCATION_KEYWORDS = {"全国", "地区", "区域", "省", "市", "县"}

    # 评分每一列
    column_scores: dict[int, tuple[int, float, int]] = {}  # (non_empty_count, avg_len, source_penalty)

    for col_idx in range(start_search, min(end_search, 20)):
        non_empty_count = 0
        total_length = 0
        source_penalty = 0
        date_like_rows = 0

        for row in rows[:min(10, len(rows))]:
            if col_idx >= len(row):
                continue
            cell = _clean(row[col_idx])
            if not cell:
                continue

            non_empty_count += 1
            compact = _compact_text(cell)
            total_length += len(compact)

            # 检查是否包含源信息关键词
            if any(kw in compact for kw in SOURCE_KEYWORDS):
                source_penalty += 5

            # 检查是否看起来像日期
            if any(d in compact for d in DATE_INDICATORS) and len(compact) < 20:
                date_like_rows += 1

        if non_empty_count > 0:
            avg_len = total_length / non_empty_count
            # 实际得分：有内容的行数 * (1 + 内容长度系数) - 源信息惩罚
            score = non_empty_count * (1.0 + min(avg_len / 50, 1.0)) - source_penalty - (date_like_rows * 0.5)
            column_scores[col_idx] = (non_empty_count, avg_len, source_penalty)

    # 返回得分高且有足够内容的列
    if column_scores:
        # 优先选择有内容的行数最多的，其次选择平均长度最长的
        best_col = max(column_scores.items(), key=lambda x: (x[1][0], x[1][1]))[0]
        return best_col

    return start_search


def _content_indexes_between(headers: list[str], start_idx: int | None, end_idx: int) -> list[int]:
    if start_idx is None:
        return [idx for idx in range(max(0, end_idx - 3), end_idx)]
    return [idx for idx in range(start_idx, end_idx) if idx < len(headers)]


def _policy_groups(rows: list[list[str]], metric_idx: int) -> list[list[list[str]]]:
    groups: list[list[list[str]]] = []
    current: list[list[str]] = []
    for row in rows:
        metric = _clean_policy_cell(_cell_at(row, metric_idx))
        is_header = _compact_text(metric) in {"政策/法规/标准", "政策法规标准"}
        starts_new = bool(metric and not is_header)
        if starts_new and current:
            groups.append(current)
            current = [row]
        else:
            current.append(row)
    if current:
        groups.append(current)
    return groups


def _regulatory_groups(
    rows: list[list[str]],
    req_idx: int,
    object_idx: int,
    source_idx: int,
    note_idx: int | None,
) -> list[list[list[str]]]:
    groups: list[list[list[str]]] = []
    current: list[list[str]] = []
    for row in rows:
        if _is_regulatory_header_row(row):
            continue
        item = _row_regulatory_item(row, req_idx)
        current_item = _row_regulatory_item(current[0], req_idx) if current else ""
        strong_item = len(_compact_text(item)) > 1 and _compact_text(item) != _compact_text(current_item)
        starts_after_blank = bool(item and current and not _row_regulatory_item(current[-1], req_idx))
        source_changed = bool(
            item
            and current
            and _compact_text(item) != _compact_text(current_item)
            and _compact_text(_cell_at(row, source_idx)) != _compact_text(_cell_at(current[-1], source_idx))
        )
        if current and (strong_item or starts_after_blank or source_changed):
            groups.append(current)
            current = [row]
        else:
            current.append(row)
    if current:
        groups.append(current)
    return groups


def _join_policy_cells(rows: list[list[str]], indexes) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for idx in indexes:
            # Handle rows with variable column counts (from PDF merged cells)
            if idx >= len(row):
                continue
            value = _clean_policy_cell(_cell_at(row, idx))
            compact = _compact_text(value)
            if not value or compact in seen or compact in SOURCE_NOISE:
                continue
            if compact in {"核心内容", "具体要求", "适用对象", "发布机构", "来源", "备注", "地区"}:
                continue
            parts.append(value)
            seen.add(compact)
    return _clean("；".join(parts))


def _first_meaningful_region(rows: list[list[str]], indexes: list[int]) -> str | None:
    for idx in indexes:
        for row in rows:
            value = _clean_policy_cell(_cell_at(row, idx))
            compact = _compact_text(value)
            if not value or compact in {"适用", "地区", "适用地区"}:
                continue
            if re.search(r"(全国|[省市区县])", value):
                return value
    return None


def _resolve_any_footnote_source(rows: list[list[str]], footnotes: dict[int, str]) -> tuple[str, list[str]]:
    for row in rows:
        for cell in row:
            clean, urls = _resolve_footnote_urls(cell or "", footnotes)
            if urls and (_is_valid_source_candidate(clean) or "《" in clean):
                return cell or clean, urls
    return "", []


def _clean_policy_cell(value: str | None) -> str:
    text = _clean_statement_part(value)
    text = text.replace("/", "")
    return _clean(text)


def _is_regulatory_header_row(row: list[str]) -> bool:
    compact = [_compact_text(cell) for cell in row if cell]
    if not compact:
        return True
    header_words = {"监管事项", "具体要求", "适用对象", "发布机构", "来源", "备注", "监", "管", "事", "项"}
    return all(cell in header_words for cell in compact)


def _row_regulatory_item(row: list[str], req_idx: int) -> str:
    left = _clean_policy_cell(_cell_at(row, 0))
    if len(_compact_text(left)) > 1:
        return left
    candidates = [_clean_policy_cell(_cell_at(row, idx)) for idx in range(max(0, req_idx))]
    candidates = [item for item in candidates if item and _compact_text(item) not in {"监管事项", "监", "管", "事", "项"}]
    return _clean("".join(candidates))


def _regulatory_metric(group: list[list[str]], req_idx: int, previous_metric: str) -> str:
    full_items: list[str] = []
    vertical_chars: list[str] = []
    previous_compact = _compact_text(previous_metric)
    for row in group:
        left = _clean_policy_cell(_cell_at(row, 0))
        middle = _clean_policy_cell(_cell_at(row, 1))
        left_compact = _compact_text(left)
        if left and len(left_compact) > 1 and left_compact != previous_compact:
            full_items.append(left)
        elif middle and _compact_text(middle) not in {"监", "管", "事", "项"}:
            vertical_chars.append(middle)
    metric = full_items[0] if full_items else "".join(vertical_chars)
    if previous_compact and _compact_text(metric).startswith(previous_compact):
        metric = metric[len(previous_metric) :].strip()
    return _clean(metric)


def _sanitize_matrix_value(text: str) -> str:
    clean = _clean(text)
    clean = re.sub(r"(?:\s+\d{1,3}){1,8}$", "", clean).strip()
    return clean


def _source_name_from_urls(urls: list[str]) -> str:
    hosts = []
    for url in urls:
        match = re.search(r"https?://([^/]+)", url)
        if match:
            hosts.append(match.group(1))
    return "；".join(dict.fromkeys(hosts))


def _is_fragment_of_previous(previous: list[str | None], current: list[str | None], mapping: HeaderMap) -> bool:
    if not previous or not current:
        return False
    for idx in (mapping.value, mapping.year):
        if idx is None:
            continue
        prev = _cell_at(previous, idx)
        curr = _cell_at(current, idx)
        if idx == mapping.value and prev and curr and prev != curr and _looks_like_short_fragment(prev) and _looks_like_short_fragment(curr):
            continue
        if prev and curr and prev != curr:
            return False
    key_indexes = [idx for idx in (mapping.metric, mapping.value, mapping.year, mapping.region, mapping.source) if idx is not None]
    same = 0
    comparable = 0
    for idx in key_indexes:
        prev = _cell_at(previous, idx)
        curr = _cell_at(current, idx)
        if prev and curr:
            comparable += 1
            same += int(prev == curr)
    if comparable >= 2 and same >= max(2, comparable - 1):
        return True

    # Product-matrix PDFs often repeat the left description columns while one
    # middle column drips fragments such as "城市空 / 中交 / 通、物 ...".
    repeated_long = 0
    short_fragments = 0
    for idx, curr in enumerate(current):
        prev = _cell_at(previous, idx)
        if prev and curr and prev == curr and len(_compact_text(curr)) >= 8:
            repeated_long += 1
        elif curr and curr != prev and len(_compact_text(curr)) <= 8:
            short_fragments += 1
    return repeated_long >= 2 and short_fragments >= 1


def _merge_row_cells(previous: list[str | None], current: list[str | None], mapping: HeaderMap) -> list[str | None]:
    out = list(previous)
    ignored = {mapping.source, mapping.publish_time}
    for idx, curr in enumerate(current):
        if not curr:
            continue
        while idx >= len(out):
            out.append(None)
        prev = out[idx]
        if not prev:
            out[idx] = curr
        elif curr != prev and idx not in ignored:
            out[idx] = _append_unique_text(prev, curr)
    return out


def _append_unique_text(left: str, right: str) -> str:
    parts = [part.strip() for part in re.split(r"[;；]\s*", left) if part.strip()]
    if right.strip() not in parts:
        parts.append(right.strip())
    return "；".join(parts)


def _is_non_claim_row(cells: list[str], mapping: HeaderMap) -> bool:
    compact_cells = [_compact_text(cell) for cell in cells if cell]
    if not compact_cells:
        return True
    if all(cell in SOURCE_NOISE or re.fullmatch(r"\d{1,3}", cell) for cell in compact_cells):
        return True
    metric = _get(cells, mapping.metric)
    value = _get(cells, mapping.value)
    source = _get(cells, mapping.source)
    useful_text = _compact_text(" ".join(cell for cell in [metric, value, source] if cell))
    if len(useful_text) < 4 and not any(_extract_url(cell) for cell in cells):
        return True
    return False


def _cell_at(cells: list[str | None], idx: int) -> str | None:
    return cells[idx] if idx < len(cells) else None


def _looks_like_short_fragment(value: str | None) -> bool:
    compact = _compact_text(value)
    return 0 < len(compact) <= 4 and not re.search(r"\d", compact)


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
        metric=_match_header(headers, METRIC_HEADERS + ("指标", "项目", "名称", "事项", "分类")),
        value=_match_header(headers, VALUE_HEADERS + ("数值", "规模", "金额", "数量", "代表产品", "服务")),
        year=_match_header(headers, YEAR_HEADERS + ("年份", "时间", "年度", "日期")),
        region=_match_header(headers, REGION_HEADERS + ("地区", "国家", "范围", "口径")),
        source=_match_header(headers, SOURCE_HEADERS + ("来源", "出处", "引用")),
        publish_time=_match_header(headers, PUBLISH_HEADERS + ("发布", "披露", "报告年份")),
        notes=[
            idx
            for idx, header in enumerate(headers)
            if any(key.lower() in header.lower() for key in NOTE_HEADERS + ("备注", "说明", "原文表述", "摘要"))
        ],
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


def _build_statement(
    metric: str | None,
    value: str | None,
    year: str | None,
    region: str | None,
    cells: list[str],
    mapping: HeaderMap,
) -> str:
    parts = [_clean_statement_part(part) for part in [year, region, metric, value]]
    ignored = {mapping.metric, mapping.value, mapping.year, mapping.region, mapping.source, mapping.publish_time}
    seen = {_compact_text(part) for part in parts if part}
    fact_parts = []
    for idx, cell in enumerate(cells):
        if not cell or idx in ignored or _compact_text(cell) in SOURCE_NOISE:
            continue
        if _looks_like_publish_date_field(cell) or _looks_like_source_text_field(cell):
            continue
        clean_cell = _clean_statement_part(cell)
        compact = _compact_text(clean_cell)
        if clean_cell and compact not in seen:
            fact_parts.append(clean_cell)
            seen.add(compact)
    statement = " ".join(part for part in [*parts, *fact_parts] if part)
    if statement:
        return _clean(statement)
    return "；".join(cell for cell in cells if cell and _compact_text(cell) not in SOURCE_NOISE)


def _clean_statement_part(value: str | None) -> str:
    text = _strip_footnote_url_lines(_clean(value)).strip()
    text = re.sub(r"(?:\s+\d{1,3}){1,10}$", "", text).strip()
    return text


def _looks_like_publish_date_field(value: str | None) -> bool:
    text = _clean(value)
    if not text:
        return False
    date_count = len(re.findall(r"(?:19|20)\d{2}\s*[-/年]\s*\d{1,2}(?:\s*[-/月]\s*\d{1,2})?", text))
    return date_count >= 1 and len(re.findall(r"[\u4e00-\u9fff]", text)) <= 4


def _looks_like_source_text_field(value: str | None) -> bool:
    text = _clean_statement_part(value)
    if not text:
        return False
    return _is_valid_source_candidate(text) and any(
        keyword in text
        for keyword in ("报告", "官网", "政府网", "新华", "财经", "研究院", "顾问", "发布会", "白皮书", "部门", "协会")
    )


def _extract_row_url(block: Block, zero_based_row: int, source_col: int | None) -> str | None:
    if source_col is None:
        return None
    return block.hyperlinks.get(f"{zero_based_row},{source_col}")


SUPERSCRIPT_DIGITS = str.maketrans(
    {
        "⁰": "0",
        "¹": "1",
        "²": "2",
        "³": "3",
        "⁴": "4",
        "⁵": "5",
        "⁶": "6",
        "⁷": "7",
        "⁸": "8",
        "⁹": "9",
        "鹿": "1",
        "虏": "2",
        "鲁": "3",
    }
)


def _resolve_footnote_urls(cell_text: str, footnotes: dict[int, str]) -> tuple[str, list[str]]:
    if not cell_text or not footnotes:
        return cell_text, []

    text = cell_text.strip()
    numbers: list[int] = []
    clean_text = text

    superscript_match = re.search(r"([⁰¹²³⁴⁵⁶⁷⁸⁹鹿虏鲁]+)\s*$", text)
    if superscript_match:
        for char in superscript_match.group(1):
            number_text = char.translate(SUPERSCRIPT_DIGITS)
            if number_text.isdigit() and int(number_text) in footnotes:
                numbers.append(int(number_text))
        if numbers:
            clean_text = text[: superscript_match.start()].strip()

    if not numbers:
        trailing_match = re.search(r"(?:\s+(\d{1,3})){1,8}\s*$", text)
        if trailing_match:
            raw_numbers = [int(item) for item in re.findall(r"\d{1,3}", trailing_match.group(0))]
            if raw_numbers and all(number in footnotes for number in raw_numbers):
                numbers = raw_numbers
                clean_text = text[: trailing_match.start()].strip()

    urls = []
    seen = set()
    for number in numbers:
        url = footnotes.get(number)
        if url and url not in seen:
            urls.append(url)
            seen.add(url)
    return clean_text or text, urls


def _resolve_row_footnote_source(cells: list[str], footnotes: dict[int, str], mapping: HeaderMap) -> tuple[str, list[str]]:
    ignored = {idx for idx in (mapping.metric, mapping.value, mapping.year, mapping.region, mapping.source, mapping.publish_time) if idx is not None}
    for idx, cell in enumerate(cells):
        if idx in ignored or not cell:
            continue
        clean, urls = _resolve_footnote_urls(cell, footnotes)
        if urls and (_is_valid_source_candidate(clean) or re.search(r"[A-Za-z]{4,}", clean or "")):
            return cell, urls
    return "", []


def _sanitize_source_name(value: str | None) -> str:
    text = _clean(value)
    text = re.sub(r"(?:\s+\d{1,3}){1,12}$", "", text).strip()
    text = re.sub(r"^[,，;；:：.。/、\s]+|[,，;；:：.。/、\s]+$", "", text)
    compact = _compact_text(text)
    if compact in SOURCE_NOISE or ("类（试行）" in compact and len(compact) <= 8):
        return ""
    return text


def _is_valid_source_candidate(value: str | None) -> bool:
    text = _sanitize_source_name(value)
    if not text:
        return False
    compact = _compact_text(text)
    if compact in SOURCE_NOISE or len(compact) <= 1:
        return False
    if _extract_url(text) or "《" in text or "》" in text:
        return True
    if any(keyword in text for keyword in SOURCE_HINT_KEYWORDS):
        return True
    return len(re.findall(r"[\u4e00-\u9fff]", text)) >= 6


def _dedupe_urls(values: list[str | None]) -> list[str]:
    urls: list[str] = []
    seen = set()
    for value in values:
        if not value:
            continue
        if value not in seen:
            urls.append(value)
            seen.add(value)
    return urls


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


def _normalize_year(value: str | None) -> str | None:
    text = _clean(value)
    if not text:
        return None
    if re.fullmatch(r"(?:19|20)\d{2}(?:\s*年(?:底|末)?)?", text):
        return text
    if re.fullmatch(r"(?:19|20)\d{2}\s*[-—–至]\s*(?:19|20)\d{2}\s*年?", text):
        return text
    return None


def _clean(value: str | None) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    return re.sub(r"(?<=[一-鿿])\s+(?=[一-鿿])", "", text)


def _llm_normalize_table_columns(rows: list[list[str]], headers: list[str], settings=None) -> list[list[str]] | None:
    """
    使用 LLM 来规范化表格列映射。当PDF合并单元格导致列数不一致时调用。
    """
    if not settings or not hasattr(settings, "models"):
        return None

    try:
        from openai import OpenAI
        from .prompts import TABLE_COLUMN_MAP_SYSTEM, TABLE_COLUMN_MAP_USER
        from .usage import record_openai_usage
    except ImportError:
        return None

    if not rows:
        return None

    # 只在有意义的情况下调用LLM（有多种列数的行）
    col_counts = set(len(row) for row in rows)
    if len(col_counts) <= 1:
        return None

    try:
        # 只用前10行作样本（避免token爆炸）
        sample_rows = rows[:10]
        headers_str = "\n".join(
            f"列{i}: {h}" for i, h in enumerate(headers)
        )
        rows_str = "\n".join(
            f"行{i}: {json.dumps(row, ensure_ascii=False)}"
            for i, row in enumerate(sample_rows)
        )

        model = getattr(settings.models, "extractor", "gpt-4o-mini")
        client = OpenAI()
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": TABLE_COLUMN_MAP_SYSTEM},
                {
                    "role": "user",
                    "content": TABLE_COLUMN_MAP_USER.format(
                        headers=headers_str,
                        rows=rows_str
                    )
                },
            ],
            max_completion_tokens=2000,
        )
        record_openai_usage(settings, model, response)

        result_text = (response.choices[0].message.content or "").strip()

        # 尝试从markdown代码块中提取JSON
        if result_text.startswith("```"):
            # 移除markdown代码块包装
            lines = result_text.split("\n")
            json_lines = []
            in_code_block = False
            for line in lines:
                if line.startswith("```"):
                    in_code_block = not in_code_block
                elif in_code_block or (json_lines and not line.startswith("```")):
                    json_lines.append(line)
            result_text = "\n".join(json_lines).strip()

        # 尝试解析JSON响应
        result_rows = json.loads(result_text)
        if isinstance(result_rows, list) and all(isinstance(r, list) for r in result_rows):
            # 验证列数
            expected_cols = len(headers)
            if all(len(r) == expected_cols for r in result_rows):
                logger.info("LLM successfully normalized table columns")
                return result_rows
            else:
                logger.warning("LLM returned rows with inconsistent column count")
    except json.JSONDecodeError as exc:
        logger.warning("Failed to parse LLM JSON response: %s", exc)
    except Exception as exc:
        logger.warning("LLM table normalization failed: %s", exc)
        return None

    return None
