"""Evidence retrieval and deterministic claim verification."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .resolver import load_cached_source_text
from .schema import Claim, ResolvedSource, VerifyResult


@dataclass
class Passage:
    text: str
    locator: str
    score: float = 0.0


def verify(claim: Claim, source: ResolvedSource, k: int = 5) -> VerifyResult:
    if source.fetch_status != "ok" or source.resolution_method == "failed":
        return VerifyResult(
            claim_id=claim.claim_id,
            verdict="not_verifiable",
            confidence=0.0,
            reasoning=f"source fetch status is {source.fetch_status}",
        )

    source_text = load_cached_source_text(source)
    if not source_text.strip():
        return VerifyResult(
            claim_id=claim.claim_id,
            verdict="not_verifiable",
            confidence=0.1,
            reasoning="source content is empty or unreadable",
        )

    passages = retrieve_passages(claim, source_text, k=k)
    if not passages:
        return VerifyResult(
            claim_id=claim.claim_id,
            verdict="not_found",
            confidence=0.2,
            reasoning="no source passage mentions the claim keywords",
        )
    return _judge_by_rules(claim, passages)


def retrieve_passages(claim: Claim, source_text: str, k: int = 5) -> list[Passage]:
    chunks = _chunk_text(source_text)
    if not chunks:
        return []
    query_terms = _tokenize(" ".join(part for part in [claim.metric, claim.value, claim.year, claim.region, claim.statement] if part))
    if not query_terms:
        return []
    tokenized = [_tokenize(chunk) for chunk in chunks]
    scores = _scores(tokenized, query_terms)
    ranked = sorted(enumerate(scores), key=lambda item: item[1], reverse=True)[:k]
    return [Passage(text=chunks[idx], locator=f"para={idx + 1}", score=float(score)) for idx, score in ranked if score > 0]


def _scores(tokenized_chunks: list[list[str]], query_terms: list[str]) -> list[float]:
    try:
        from rank_bm25 import BM25Okapi

        return list(BM25Okapi(tokenized_chunks).get_scores(query_terms))
    except ImportError:
        query_set = set(query_terms)
        return [float(sum(1 for token in tokens if token in query_set)) for tokens in tokenized_chunks]


def _judge_by_rules(claim: Claim, passages: list[Passage]) -> VerifyResult:
    combined = "\n".join(p.text for p in passages)
    value_ok = _contains_value(combined, claim.value)
    year_ok = _contains_plain(combined, claim.year)
    metric_ok = _contains_metric(combined, claim.metric, claim.statement)
    best = passages[0]

    if value_ok and (year_ok or not claim.year) and metric_ok:
        return VerifyResult(
            claim_id=claim.claim_id,
            verdict="supported",
            confidence=0.85,
            evidence_quote=_short_quote(best.text, claim.value or claim.metric or claim.statement),
            evidence_locator=best.locator,
            reasoning="source passage matches value, year and metric keywords",
        )

    if value_ok or (metric_ok and year_ok):
        return VerifyResult(
            claim_id=claim.claim_id,
            verdict="partially_supported",
            confidence=0.55,
            evidence_quote=_short_quote(best.text, claim.value or claim.metric or claim.statement),
            evidence_locator=best.locator,
            discrepancy=_missing_reason(value_ok, year_ok, metric_ok, claim),
            reasoning="source passage matches part of the claim",
        )

    return VerifyResult(
        claim_id=claim.claim_id,
        verdict="not_found",
        confidence=0.35,
        evidence_quote=_short_quote(best.text, claim.metric or claim.statement),
        evidence_locator=best.locator,
        reasoning="retrieved passages do not contain the core claim value or metric",
    )


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
    return re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9.]+", text.lower())


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
    if not metric:
        words = _tokenize(statement)
        return any(word in text for word in words[:5])
    tokens = _tokenize(metric)
    return not tokens or any(token in text for token in tokens)


def _compact(text: str | None) -> str:
    return re.sub(r"\s+", "", text or "").lower()


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
