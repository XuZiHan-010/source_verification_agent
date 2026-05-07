"""Evidence retrieval and claim verification (rules fast-path + LLM layer)."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .config import ROOT, load_yaml
from .prompts import VERIFIER_SYSTEM, VERIFIER_USER
from .resolver import _abs_path, diagnose_cached_pdf, load_cached_source_text
from .schema import Claim, ResolvedSource, VerifyResult
from .usage import record_openai_usage

logger = logging.getLogger(__name__)

_DEFAULT_K = 8


@dataclass
class Passage:
    text: str
    locator: str
    score: float = 0.0


def verify(
    claim: Claim,
    source: ResolvedSource,
    k: int = _DEFAULT_K,
    settings=None,
) -> VerifyResult:
    if source.fetch_status != "ok" or source.resolution_method == "failed":
        return VerifyResult(
            claim_id=claim.claim_id,
            verdict="not_verifiable",
            confidence=0.0,
            reasoning=f"source fetch status is {source.fetch_status}",
        )

    source_text = load_cached_source_text(source, settings=settings)
    if not source_text.strip():
        content_kind = "PDF" if source.content_type == "pdf" else "HTML/text"
        diag = ""
        if source.content_type == "pdf" and source.local_cache_path:
            try:
                diag = diagnose_cached_pdf(Path(source.local_cache_path))
            except Exception as exc:  # diagnostic must never break verification
                diag = f"diagnostic helper failed: {exc}"
        suffix = f" [diagnostic: {diag}]" if diag else ""
        return VerifyResult(
            claim_id=claim.claim_id,
            verdict="not_verifiable",
            confidence=0.1,
            reasoning=(
                f"source {content_kind} downloaded but text extraction returned empty or unreadable "
                f"(likely scanned PDF or image-only document; OCR not enabled in this system){suffix}"
            ),
        )

    passages = retrieve_passages(claim, source_text, k=k)
    if not passages:
        return VerifyResult(
            claim_id=claim.claim_id,
            verdict="not_found",
            confidence=0.2,
            reasoning="no source passage mentions the claim keywords",
        )
    if _all_fallback_passages(passages) and not claim.original_columns:
        return VerifyResult(
            claim_id=claim.claim_id,
            verdict="not_found",
            confidence=0.15,
            reasoning="LLM skipped because lexical retrieval found no claim keyword signal",
        )

    # 新格式 claim 是任意文本陈述，BM25 字符 bigram 经常无强命中，仍给 LLM 兜底机会做语义判断。
    # strict 数值快路径仍保留：如果 fast 命中则直接返回 supported（数值类 PDF 无 token 成本）。

    # Layer 1: rules fast path — only fires for unambiguous exact matches.
    fast = _judge_by_rules_strict(claim, passages)
    if fast is not None:
        return fast

    # Layer 2: LLM judgment for fuzzy cases (approx values, synonyms, contradictions).
    llm_result = _judge_by_llm(claim, source, passages, k=k, settings=settings)
    if llm_result is not None:
        return llm_result

    # Fallback to lenient rules if LLM is unavailable.
    return _judge_by_rules_lenient(claim, passages)


def retrieve_passages(claim: Claim, source_text: str, k: int = _DEFAULT_K) -> list[Passage]:
    chunks = _chunk_text(source_text)
    if not chunks:
        return []
    query_text = _expand_query_text(
        " ".join(part for part in [claim.metric, claim.value, claim.year, claim.region, claim.statement] if part)
    )
    query_terms = _tokenize(query_text)
    if query_terms:
        tokenized = [_tokenize(chunk) for chunk in chunks]
        scores = _scores(tokenized, query_terms)
        ranked = sorted(enumerate(scores), key=lambda item: item[1], reverse=True)[:k]
        if any(score > 0 for _, score in ranked):
            return [Passage(text=chunks[idx], locator=f"para={idx + 1}", score=float(score)) for idx, score in ranked]

    # If lexical retrieval has no signal, still give the semantic verifier a small,
    # deterministic window from the source instead of making a zero-recall decision.
    return [Passage(text=chunks[idx], locator=f"para={idx + 1} (fallback)", score=0.0) for idx in range(min(k, len(chunks)))]


def _all_fallback_passages(passages: list[Passage]) -> bool:
    return bool(passages) and (
        all(p.score == 0.0 for p in passages)
        or all("(fallback)" in p.locator for p in passages)
    )


def _expand_query_text(text: str, synonyms: dict[str, list[str]] | None = None) -> str:
    synonyms = synonyms if synonyms is not None else _load_verifier_synonyms()
    if not synonyms:
        return text

    compact_text = _compact(text)
    expanded = [text]
    for key, values in synonyms.items():
        terms = [key, *values]
        compact_terms = [_compact(term) for term in terms if term]
        if any(term and term in compact_text for term in compact_terms):
            expanded.extend(terms)
    return " ".join(_unique(expanded))


@lru_cache(maxsize=1)
def _load_verifier_synonyms() -> dict[str, list[str]]:
    path = ROOT / "config" / "verifier_synonyms.yaml"
    if not path.exists():
        return {}
    raw = load_yaml(path)
    groups = raw.get("synonyms", raw)
    if not isinstance(groups, dict):
        return {}

    normalized: dict[str, list[str]] = {}
    for key, values in groups.items():
        if not key:
            continue
        if isinstance(values, str):
            values = [values]
        if isinstance(values, list):
            normalized[str(key)] = [str(value) for value in values if value]
    return normalized


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _scores(tokenized_chunks: list[list[str]], query_terms: list[str]) -> list[float]:
    try:
        from rank_bm25 import BM25Okapi

        return list(BM25Okapi(tokenized_chunks).get_scores(query_terms))
    except ImportError:
        query_set = set(query_terms)
        return [float(sum(1 for token in tokens if token in query_set)) for tokens in tokenized_chunks]


# ── Layer 1: strict rules (exact compact match only) ─────────────────────────

def _judge_by_rules_strict(claim: Claim, passages: list[Passage]) -> VerifyResult | None:
    """Return supported only when value, year, and metric all match exactly (compact).
    Returns None otherwise to let LLM take over."""
    combined = "\n".join(p.text for p in passages)
    value_ok = _value_exact(combined, claim.value)
    year_ok = _contains_plain(combined, claim.year)
    metric_ok = _metric_exact(combined, claim.metric)

    if value_ok and (year_ok or not claim.year) and metric_ok:
        best = passages[0]
        return VerifyResult(
            claim_id=claim.claim_id,
            verdict="supported",
            confidence=0.92,
            evidence_quote=_short_quote(best.text, claim.value or claim.metric or claim.statement),
            evidence_locator=best.locator,
            reasoning="exact match: value, year and metric all found in source passage",
        )
    return None


def _value_exact(text: str, value: str | None) -> bool:
    """Exact compact string match only — no partial-number OR fallback."""
    if not value:
        return True
    items = _split_list_value(value)
    if len(items) >= 2:
        hits = sum(1 for item in items if _list_item_exact(text, item))
        return hits / len(items) >= 0.8
    raw = _compact(value)
    return bool(raw) and raw in _compact(text)


def _metric_exact(text: str, metric: str | None) -> bool:
    """All tokens from metric must appear in text (AND, not OR)."""
    if not metric:
        return False
    tokens = _tokenize(metric)
    haystack = text.lower()
    return bool(tokens) and all(token in haystack for token in tokens)


# ── Layer 2: LLM judgment ─────────────────────────────────────────────────────

def _judge_by_llm(
    claim: Claim,
    source: ResolvedSource,
    passages: list[Passage],
    k: int,
    settings,
) -> VerifyResult | None:
    cache_path, ttl_days = _llm_cache_paths(source, claim, settings)
    cached = _read_cache(cache_path, ttl_days)
    if cached:
        verdict = cached.get("verdict", "not_found")
        if verdict not in {"supported", "partially_supported", "contradicted", "not_found"}:
            verdict = "not_found"
        return VerifyResult(
            claim_id=claim.claim_id,
            verdict=verdict,  # type: ignore[arg-type]
            confidence=float(cached.get("confidence", 0.7)),
            evidence_quote=cached.get("evidence_quote"),
            evidence_locator=cached.get("evidence_locator"),
            reasoning=cached.get("reasoning") or "LLM cache hit",
        )

    try:
        from openai import OpenAI

        model = "gpt-4o-mini"
        if settings is not None:
            try:
                model = settings.models.verifier
            except AttributeError:
                pass

        passages_text = "\n\n---\n\n".join(
            f"[段落 {i + 1}]\n{p.text}" for i, p in enumerate(passages)
        )
        user_msg = VERIFIER_USER.format(
            metric=claim.metric or "（未知）",
            value=claim.value or "（未知）",
            year=claim.year or "（未知）",
            region=claim.region or "（未知）",
            statement=claim.statement or "（未知）",
            k=k,
            passages=passages_text,
        )

        client = OpenAI()
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": VERIFIER_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            max_completion_tokens=2000,
            response_format={"type": "json_object"},
        )
        record_openai_usage(settings, model, response)
        raw = response.choices[0].message.content or ""
        data = json.loads(raw)

        verdict = data.get("verdict", "not_found")
        if verdict not in {"supported", "partially_supported", "contradicted", "not_found"}:
            verdict = "not_found"

        best = passages[0]
        result = VerifyResult(
            claim_id=claim.claim_id,
            verdict=verdict,  # type: ignore[arg-type]
            confidence=float(data.get("confidence", 0.7)),
            evidence_quote=data.get("evidence_quote") or _short_quote(best.text, claim.value or claim.metric),
            evidence_locator=data.get("evidence_locator") or best.locator,
            reasoning=data.get("reasoning", "LLM判定"),
        )
        if cache_path is not None:
            _write_cache(
                cache_path,
                {
                    "verdict": result.verdict,
                    "confidence": result.confidence,
                    "evidence_quote": result.evidence_quote,
                    "evidence_locator": result.evidence_locator,
                    "reasoning": result.reasoning,
                },
            )
        return result
    except Exception as exc:
        logger.warning("LLM verifier failed for %s: %s", claim.claim_id, exc)
        return None


def _llm_cache_paths(
    source: ResolvedSource,
    claim: Claim,
    settings,
) -> tuple[Path | None, int]:
    if settings is None:
        return None, 7
    content_hash = source.content_hash or ""
    key_parts = [
        content_hash,
        claim.statement or "",
        claim.value or "",
        claim.year or "",
    ]
    key = hashlib.sha1("||".join(key_parts).encode("utf-8")).hexdigest()
    cache_root = _abs_path(settings.cache.dir) / "verify"
    cache_root.mkdir(parents=True, exist_ok=True)
    return cache_root / f"{key}.json", settings.cache.verify_ttl_days


def _read_cache(path: Path | None, ttl_days: int) -> dict | None:
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    ts = data.get("ts")
    if not isinstance(ts, (int, float)):
        return None
    if (time.time() - ts) > ttl_days * 86400:
        return None
    return data


def _write_cache(path: Path, payload: dict) -> None:
    try:
        path.write_text(
            json.dumps({**payload, "ts": time.time()}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("failed to write verify cache %s: %s", path, exc)


# ── Lenient rule fallback (used only when LLM unavailable) ───────────────────

def _judge_by_rules_lenient(claim: Claim, passages: list[Passage]) -> VerifyResult:
    combined = "\n".join(p.text for p in passages)
    value_ok = _contains_value(combined, claim.value)
    year_ok = _contains_plain(combined, claim.year)
    metric_ok = _contains_metric(combined, claim.metric, claim.statement)
    best = passages[0]

    if value_ok and (year_ok or not claim.year) and metric_ok:
        return VerifyResult(
            claim_id=claim.claim_id,
            verdict="supported",
            confidence=0.75,
            evidence_quote=_short_quote(best.text, claim.value or claim.metric or claim.statement),
            evidence_locator=best.locator,
            reasoning="rules fallback: value, year and metric keywords matched",
        )

    if value_ok or (metric_ok and year_ok):
        return VerifyResult(
            claim_id=claim.claim_id,
            verdict="partially_supported",
            confidence=0.50,
            evidence_quote=_short_quote(best.text, claim.value or claim.metric or claim.statement),
            evidence_locator=best.locator,
            discrepancy=_missing_reason(value_ok, year_ok, metric_ok, claim),
            reasoning="rules fallback: partial match",
        )

    return VerifyResult(
        claim_id=claim.claim_id,
        verdict="not_found",
        confidence=0.30,
        evidence_quote=_short_quote(best.text, claim.metric or claim.statement),
        evidence_locator=best.locator,
        reasoning="rules fallback: passages do not contain the core claim value or metric",
    )


# ── Text utilities ────────────────────────────────────────────────────────────

def _chunk_text(text: str, max_chars: int = 800) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n+|(?<=[。！？])", text) if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if len(current) + len(para) > max_chars and current:
            chunks.append(current.strip())
            current = para
        else:
            current = f"{current}\n{para}".strip()
    if current:
        chunks.append(current.strip())
    return chunks


def _tokenize(text: str) -> list[str]:
    text = text.lower()
    tokens: list[str] = []
    tokens.extend(re.findall(r"[a-z0-9.]+", text))
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(run) >= 2:
            tokens.extend(run[idx : idx + 2] for idx in range(len(run) - 1))
        elif run:
            tokens.append(run)
    return tokens


def _contains_value(text: str, value: str | None) -> bool:
    if not value:
        return True
    compact = _compact(text)
    raw = _compact(value)
    if raw and raw in compact:
        return True
    numbers = re.findall(r"\d+(?:\.\d+)?", value)
    return bool(numbers) and all(number in compact for number in numbers)


def _contains_plain(text: str, value: str | None) -> bool:
    return True if not value else _compact(value) in _compact(text)


def _contains_metric(text: str, metric: str | None, statement: str) -> bool:
    haystack = text.lower()
    if not metric:
        words = _tokenize(statement)
        return any(word in haystack for word in words[:8])
    tokens = _tokenize(metric)
    return not tokens or any(token in haystack for token in tokens)


def _compact(text: str | None) -> str:
    return re.sub(r"\s+", "", text or "").lower()


def _split_list_value(value: str) -> list[str]:
    parts = [part.strip(" \t\r\n;；,，、/（）()“”\"'") for part in re.split(r"[、,，/；;]+", value)]
    return [part for part in parts if len(_compact(part)) >= 2]


def _list_item_exact(text: str, item: str) -> bool:
    haystack = _compact(text)
    for variant in _list_item_variants(item):
        needle = _compact(variant)
        if needle and needle in haystack:
            return True
    return False


def _list_item_variants(item: str) -> set[str]:
    variants = {item}
    pairs = {
        "直升机": "直升飞机",
        "固定翼飞机": "传统固定翼飞机",
        "固定翼飞行器": "固定翼飞机",
        "无人机": "无人驾驶航空器",
        "eVTOL": "电动垂直起降航空器",
    }
    for left, right in pairs.items():
        if left in item:
            variants.add(item.replace(left, right))
        if right in item:
            variants.add(item.replace(right, left))
    return variants


def _short_quote(text: str, anchor: str | None, window: int = 160) -> str:
    compact_anchor = (anchor or "").strip()
    if compact_anchor and compact_anchor in text:
        idx = text.find(compact_anchor)
        start = max(0, idx - window // 2)
        end = min(len(text), idx + len(compact_anchor) + window // 2)
        return text[start:end].strip()
    return text[:window].strip()


def _missing_reason(value_ok: bool, year_ok: bool, metric_ok: bool, claim: Claim) -> str | None:
    missing = []
    if claim.value and not value_ok:
        missing.append("value")
    if claim.year and not year_ok:
        missing.append("year")
    if claim.metric and not metric_ok:
        missing.append("metric")
    return "missing " + ", ".join(missing) if missing else None
