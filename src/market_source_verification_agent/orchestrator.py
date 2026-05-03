"""Synchronous local pipeline orchestration."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from . import classifier, extractor, ingestor, reporter, resolver, verifier
from .config import ROOT, Settings, load_settings, load_source_tiers
from .schema import Claim, ClassifyResult, Report, ResolvedSource, VerifyResult


def run(
    input_path: str | Path,
    out_path: str | Path | None = None,
    fmt: str = "xlsx",
    config: Settings | str | Path | None = None,
    detailed: bool = False,
    no_cache: bool = False,
    limit: int | None = None,
) -> Report:
    started = datetime.now()
    settings = _settings(config)
    source_tiers = load_source_tiers()
    input_path = Path(input_path)
    fmt = fmt.lower()
    output_path = Path(out_path) if out_path else _default_output_path(input_path, fmt)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    ir = ingestor.ingest(input_path)
    claims = extractor.extract_claims(ir, limit=limit or settings.limits.max_claims_per_run)
    claims = _dedupe_claims(claims)

    with ThreadPoolExecutor(max_workers=settings.concurrency.fetch_workers) as pool:
        source_groups = list(pool.map(lambda claim: _resolve_claim_sources(claim, settings, source_tiers), claims))

    with ThreadPoolExecutor(max_workers=settings.concurrency.llm_workers) as pool:
        verify_groups = list(
            pool.map(
                lambda item: [
                    verifier.verify(item[0], source, settings=settings)
                    for source in item[1]
                ],
                zip(claims, source_groups),
            )
        )

    class_groups = [
        [classifier.classify(source, claim, source_tiers) for source in sources]
        for claim, sources in zip(claims, source_groups)
    ]
    verifies = [
        _aggregate_verifies(claim, sources, claim_verifies, claim_classes)
        for claim, sources, claim_verifies, claim_classes in zip(claims, source_groups, verify_groups, class_groups)
    ]
    classes = [_aggregate_classes(claim, sources, claim_classes) for claim, sources, claim_classes in zip(claims, source_groups, class_groups)]
    verify_map = {item.claim_id: item for item in verifies}
    class_map = {item.claim_id: item for item in classes}

    payload = reporter.render(ir, claims, verify_map, class_map, fmt=fmt, detailed=detailed or settings.output.include_detail_column)  # type: ignore[arg-type]
    output_path.write_bytes(payload)

    # Write sidecar summary (verdict counts + per-domain failure rates + llm_extract_calls)
    try:
        import json as _json

        summary_path = output_path.with_name(output_path.stem + "_summary.json")
        summary_path.write_text(
            _json.dumps(reporter.detailed_summary(verify_map, class_map), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass

    finished = datetime.now()
    return Report(
        run_id=str(uuid4()),
        input_path=str(input_path),
        output_path=str(output_path),
        summary=reporter.summarize(verify_map, class_map),
        started_at=started,
        finished_at=finished,
        cost_usd=0.0,
        cache_hit_rate=0.0 if no_cache else 0.0,
    )


def _settings(config: Settings | str | Path | None) -> Settings:
    if isinstance(config, Settings):
        return config
    return load_settings(config)


def _default_output_path(input_path: Path, fmt: str) -> Path:
    return ROOT / "data" / "reports" / f"{input_path.stem}_verified.{fmt}"


def _resolve_claim_sources(claim: Claim, settings: Settings, source_tiers: dict) -> list[ResolvedSource]:
    sources = []
    urls = _claim_urls(claim)
    if not urls:
        return [resolver.resolve(claim, settings, source_tiers)]
    for url in urls:
        source_claim = claim.model_copy(update={"source_url_hint": url, "extra_source_urls": [], "source_urls": [url]})
        sources.append(resolver.resolve(source_claim, settings, source_tiers))
    return sources


def _claim_urls(claim: Claim) -> list[str]:
    values = claim.source_urls or [claim.source_url_hint, *claim.extra_source_urls]
    urls = []
    seen = set()
    for value in values:
        if value and value not in seen:
            urls.append(value)
            seen.add(value)
    claim.source_urls = urls
    return urls


VERDICT_PRIORITY = {
    "supported": 5,
    "partially_supported": 4,
    "contradicted": 3,
    "not_found": 2,
    "not_verifiable": 1,
}


def _aggregate_verifies(
    claim: Claim,
    sources: list[ResolvedSource],
    verifies: list[VerifyResult],
    classes: list[ClassifyResult],
) -> VerifyResult:
    best = max(verifies, key=lambda item: VERDICT_PRIORITY.get(item.verdict, 0))
    details = []
    for source, verify_result, class_result in zip(sources, verifies, classes):
        details.append(
            {
                "url": source.url,
                "domain": source.domain,
                "resolution_method": source.resolution_method,
                "fetch_status": source.fetch_status,
                "content_type": source.content_type,
                "verdict": verify_result.verdict,
                "confidence": verify_result.confidence,
                "tier": class_result.tier,
                "evidence_quote": verify_result.evidence_quote,
                "reasoning": verify_result.reasoning,
            }
        )
    if len(details) == 1:
        best.source_details = details
        return best

    from . import reporter

    final_verdict, policy_reason = reporter.aggregate_multisource_verdict(details)
    return VerifyResult(
        claim_id=claim.claim_id,
        verdict=final_verdict,  # type: ignore[arg-type]
        confidence=best.confidence,
        evidence_quote=best.evidence_quote,
        evidence_locator=best.evidence_locator,
        discrepancy=best.discrepancy,
        reasoning=f"multi-source aggregate: {policy_reason} ({len(details)} URLs); best detail: {best.reasoning}",
        source_details=details,
    )


def _aggregate_classes(claim: Claim, sources: list[ResolvedSource], classes: list[ClassifyResult]) -> ClassifyResult:
    tier_rank = {"A": 3, "B": 2, "C": 1}
    ok_items = [(source, item) for source, item in zip(sources, classes) if source.fetch_status == "ok"]
    candidates = ok_items or list(zip(sources, classes))
    _, best = max(candidates, key=lambda item: tier_rank.get(item[1].tier, 0))
    return ClassifyResult(
        claim_id=claim.claim_id,
        tier=best.tier,
        tier_reason=best.tier_reason,
        matched_rule=best.matched_rule,
    )


def _dedupe_claims(claims: list[Claim]) -> list[Claim]:
    merged: dict[str, Claim] = {}
    order: list[str] = []
    for claim in claims:
        _claim_urls(claim)
        key = _claim_signature(claim)
        if key not in merged:
            claim.duplicate_count = 1
            claim.duplicate_claim_ids = [claim.claim_id]
            merged[key] = claim
            order.append(key)
        else:
            target = merged[key]
            target.duplicate_count += 1
            target.duplicate_claim_ids.append(claim.claim_id)
    return [merged[key] for key in order]


def _claim_signature(claim: Claim) -> str:
    import re

    parts = [
        claim.metric,
        claim.value,
        claim.year,
        claim.region,
        claim.statement,
        claim.source_name_raw,
        "|".join(claim.source_urls),
    ]
    return re.sub(r"\s+", "", "||".join(part or "" for part in parts)).lower()
