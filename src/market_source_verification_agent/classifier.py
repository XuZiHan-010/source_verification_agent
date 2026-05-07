"""A/B/C source tier classification."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path

from .config import Settings, load_source_tiers
from .prompts import CLASSIFIER_SYSTEM, CLASSIFIER_USER
from .resolver import _abs_path
from .schema import Claim, ClassifyResult, ResolvedSource
from .usage import record_openai_usage

logger = logging.getLogger(__name__)

FAILURE_STATUSES = {"404", "forbidden", "timeout", "paywalled"}


def classify(
    source: ResolvedSource,
    claim: Claim,
    source_tiers: dict | None = None,
    settings: Settings | None = None,
) -> ClassifyResult:
    tiers = source_tiers or load_source_tiers()
    forced = _force_c(source, claim, tiers)
    if forced:
        return forced

    for tier in ("A", "B"):
        result = _match_tier(tier, source, claim, tiers)
        if result:
            return result

    llm_result = _classify_by_llm(source, claim, settings)
    if llm_result:
        return llm_result

    return ClassifyResult(
        claim_id=claim.claim_id,
        tier="C",
        tier_reason="未命中 A/B 白名单，且 LLM 判定不可用",
        matched_rule="fallback:C",
    )


def _force_c(source: ResolvedSource, claim: Claim, tiers: dict) -> ClassifyResult | None:
    c_cfg = tiers.get("tiers", {}).get("C_force", {})
    if c_cfg.get("fetch_status_failure") and source.fetch_status in FAILURE_STATUSES:
        return ClassifyResult(
            claim_id=claim.claim_id,
            tier="C",
            tier_reason=f"source fetch failed: {source.fetch_status}",
            matched_rule="fetch_failed",
        )
    raw = claim.source_name_raw or ""
    for keyword in c_cfg.get("name_keywords", []):
        if keyword and keyword in raw:
            return ClassifyResult(
                claim_id=claim.claim_id,
                tier="C",
                tier_reason=f"来源名称包含 C 类关键词：{keyword}",
                matched_rule=f"keyword:C:{keyword}",
            )
    return None


def _match_tier(tier: str, source: ResolvedSource, claim: Claim, tiers: dict) -> ClassifyResult | None:
    cfg = tiers.get("tiers", {}).get(tier, {})
    domain = _normalize_domain(source.domain or _domain_from_raw(claim.source_name_raw))
    if domain:
        for suffix in cfg.get("domain_suffixes", []):
            suffix_text = str(suffix).lower()
            apex = suffix_text.lstrip(".")
            if domain == apex or domain.endswith("." + apex):
                return ClassifyResult(
                    claim_id=claim.claim_id,
                    tier=tier,  # type: ignore[arg-type]
                    tier_reason=f"domain {domain} matches {tier} suffix {suffix}",
                    matched_rule=f"suffix:{tier}:{suffix}",
                )
        for whitelisted in cfg.get("domains", []):
            normalized = str(whitelisted).lower().removeprefix("www.")
            if domain == normalized or domain.endswith("." + normalized):
                return ClassifyResult(
                    claim_id=claim.claim_id,
                    tier=tier,  # type: ignore[arg-type]
                    tier_reason=f"domain {domain} 在 {tier} 类白名单",
                    matched_rule=f"whitelist:{tier}:{normalized}",
                )

    raw = claim.source_name_raw or ""
    for keyword in cfg.get("name_keywords", []):
        if keyword and keyword in raw:
            return ClassifyResult(
                claim_id=claim.claim_id,
                tier=tier,  # type: ignore[arg-type]
                tier_reason=f"来源名称包含 {tier} 类关键词：{keyword}",
                matched_rule=f"keyword:{tier}:{keyword}",
            )
    return None


# ── LLM fallback classifier with disk cache ──────────────────────────────────


def _classify_by_llm(
    source: ResolvedSource,
    claim: Claim,
    settings: Settings | None,
) -> ClassifyResult | None:
    domain = _normalize_domain(source.domain or _domain_from_raw(claim.source_name_raw)) or ""
    source_name = (claim.source_name_raw or "").strip()
    title = (source.title or "").strip()

    if not domain and not source_name:
        return None

    cache_path, ttl_days = _llm_cache_paths(domain, source_name, settings)
    cached = _read_cache(cache_path, ttl_days)
    if cached:
        return ClassifyResult(
            claim_id=claim.claim_id,
            tier=cached["tier"],  # type: ignore[arg-type]
            tier_reason=cached.get("reason") or "LLM 缓存命中",
            matched_rule="llm_cache",
        )

    try:
        from openai import OpenAI

        model = "gpt-4o-mini"
        if settings is not None:
            try:
                model = settings.models.classifier
            except AttributeError:
                pass

        user_msg = CLASSIFIER_USER.format(
            source_name=source_name or "（未提供）",
            domain=domain or "（未提供）",
            title=title or "（未提供）",
        )
        client = OpenAI()
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": CLASSIFIER_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            max_completion_tokens=300,
            response_format={"type": "json_object"},
        )
        record_openai_usage(settings, model, response)
        raw = response.choices[0].message.content or ""
        data = json.loads(raw)
        tier = str(data.get("tier", "")).strip().upper()
        reason = str(data.get("reason", "")).strip() or "LLM 判定"
        if tier not in {"A", "B", "C"}:
            tier = "C"
            reason = f"LLM 输出非法级别，保守归 C：{reason}"

        if cache_path is not None:
            _write_cache(cache_path, {"tier": tier, "reason": reason})

        return ClassifyResult(
            claim_id=claim.claim_id,
            tier=tier,  # type: ignore[arg-type]
            tier_reason=f"LLM 判定：{reason}",
            matched_rule="llm",
        )
    except Exception as exc:
        logger.warning("LLM classifier failed for %s/%s: %s", domain, source_name, exc)
        return None


def _llm_cache_paths(
    domain: str,
    source_name: str,
    settings: Settings | None,
) -> tuple[Path | None, int]:
    if settings is None:
        return None, 90
    key = hashlib.sha1(f"{domain}||{source_name}".encode("utf-8")).hexdigest()
    cache_root = _abs_path(settings.cache.dir) / "classify"
    cache_root.mkdir(parents=True, exist_ok=True)
    return cache_root / f"{key}.json", settings.cache.classify_ttl_days


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
        logger.warning("failed to write classify cache %s: %s", path, exc)


# ── Domain helpers ───────────────────────────────────────────────────────────


def _domain_from_raw(raw: str | None) -> str | None:
    if not raw:
        return None
    import re

    match = re.search(r"\b(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}\b", raw)
    return _normalize_domain(match.group(0)) if match else None


def _normalize_domain(domain: str | None) -> str | None:
    return domain.lower().removeprefix("www.") if domain else None
