import pytest

from market_source_verification_agent import classifier, extractor, ingestor, reporter
from market_source_verification_agent.schema import ClassifyResult, ResolvedSource, VerifyResult


def test_markdown_table_pipeline_renders_json():
    text = """
# 低空经济

| 指标 | 数值 | 年份 | 地区/口径 | 来源名称 | 备注 |
|---|---|---|---|---|---|
| 低空经济规模 | 5059.5 亿元 | 2023 | 中国 | 赛迪研究院/赛迪智库 (pdf.dfcfw.com) | 同比增速 33.8% |
"""
    ir = ingestor.ingest(text, fmt="md", doc_id="demo")
    claims = extractor.extract_claims(ir)

    assert len(claims) == 1
    assert claims[0].metric == "低空经济规模"
    assert claims[0].source_url_hint == "pdf.dfcfw.com"

    verify = VerifyResult(
        claim_id=claims[0].claim_id,
        verdict="supported",
        confidence=0.9,
        evidence_quote="2023 年中国低空经济规模达到 5059.5 亿元",
        evidence_locator="para=1",
        discrepancy=None,
        reasoning="matched",
    )
    category = ClassifyResult(
        claim_id=claims[0].claim_id,
        tier="B",
        tier_reason="domain whitelist",
        matched_rule="whitelist:B:pdf.dfcfw.com",
    )
    payload = reporter.render(ir, claims, {verify.claim_id: verify}, {category.claim_id: category}, fmt="json")

    assert "✅ 支持".encode("utf-8") in payload
    assert "低空经济规模".encode("utf-8") in payload


def test_classifier_matches_government_suffix():
    claim = extractor.extract_claims(
        ingestor.ingest(
            "| 指标 | 数值 | 年份 | 来源名称 |\n|---|---|---|---|\n| 注册无人机数量 | 217.7 万架 | 2024 年底 | 中国民航局 |\n",
            fmt="md",
            doc_id="demo",
        )
    )[0]
    source = ResolvedSource(
        claim_id=claim.claim_id,
        resolution_method="whitelist",
        url="https://caac.gov.cn",
        domain="caac.gov.cn",
        title=None,
        fetch_status="ok",
        local_cache_path=None,
        content_type="html",
        content_hash=None,
    )

    result = classifier.classify(source, claim)

    assert result.tier == "A"


def test_xlsx_render_writes_bytes():
    pytest.importorskip("openpyxl")

    text = "| 指标 | 数值 | 年份 | 来源名称 |\n|---|---|---|---|\n| 测试指标 | 10 | 2024 | 新华社 |\n"
    ir = ingestor.ingest(text, fmt="md", doc_id="demo")
    claim = extractor.extract_claims(ir)[0]
    verify = VerifyResult(claim_id=claim.claim_id, verdict="not_verifiable", confidence=0, reasoning="offline")
    category = ClassifyResult(claim_id=claim.claim_id, tier="B", tier_reason="keyword", matched_rule="keyword:B:新华社")

    payload = reporter.render(ir, [claim], {claim.claim_id: verify}, {claim.claim_id: category}, fmt="xlsx")

    assert len(payload) > 1000
