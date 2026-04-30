"""A/B/C source tier classification."""

from __future__ import annotations

from .config import load_source_tiers
from .schema import Claim, ClassifyResult, ResolvedSource

FAILURE_STATUSES = {"404", "forbidden", "timeout", "paywalled"}


def classify(source: ResolvedSource, claim: Claim, source_tiers: dict | None = None) -> ClassifyResult:
    tiers = source_tiers or load_source_tiers()
    forced = _force_c(source, claim, tiers)
    if forced:
        return forced

    for tier in ("A", "B"):
        result = _match_tier(tier, source, claim, tiers)
        if result:
            return result

    return ClassifyResult(
        claim_id=claim.claim_id,
        tier="C",
        tier_reason="未命中 A/B 白名单或可信来源关键词",
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
    domain = source.domain or _domain_from_raw(claim.source_name_raw)
    if domain:
        for suffix in cfg.get("domain_suffixes", []):
            if domain.endswith(str(suffix).lower()):
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


def _domain_from_raw(raw: str | None) -> str | None:
    if not raw:
        return None
    import re

    match = re.search(r"\b(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}\b", raw)
    return match.group(0).lower().removeprefix("www.") if match else None
