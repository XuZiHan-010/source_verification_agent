import json

from market_source_verification_agent import orchestrator, reporter
from market_source_verification_agent.schema import Claim, ClassifyResult, IR, ResolvedSource, VerifyResult


def test_dedupe_claims_merges_duplicate_rows():
    first = Claim(
        claim_id="doc#t1#r1",
        statement="2024 market size 10",
        source_name_raw="Source",
        source_url_hint="https://example.com/a",
        source_urls=["https://example.com/a"],
    )
    second = first.model_copy(update={"claim_id": "doc#t1#r2"})

    claims = orchestrator._dedupe_claims([first, second])

    assert len(claims) == 1
    assert claims[0].duplicate_count == 2
    assert claims[0].duplicate_claim_ids == ["doc#t1#r1", "doc#t1#r2"]


def test_multi_url_aggregate_prefers_any_supported_and_best_tier():
    claim = Claim(
        claim_id="doc#t1#r1",
        statement="2024 market size 10",
        source_name_raw="Source",
        source_url_hint="https://example.com/a",
        source_urls=["https://example.com/a", "https://gov.cn/b"],
    )
    sources = [
        ResolvedSource(claim_id=claim.claim_id, resolution_method="hyperlink", url="https://example.com/a", domain="example.com", fetch_status="ok", content_type="html"),
        ResolvedSource(claim_id=claim.claim_id, resolution_method="hyperlink", url="https://gov.cn/b", domain="gov.cn", fetch_status="ok", content_type="html"),
    ]
    verifies = [
        VerifyResult(claim_id=claim.claim_id, verdict="not_found", confidence=0.2, reasoning="missing"),
        VerifyResult(claim_id=claim.claim_id, verdict="supported", confidence=0.9, reasoning="matched", evidence_quote="market size 10"),
    ]
    classes = [
        ClassifyResult(claim_id=claim.claim_id, tier="C", tier_reason="fallback", matched_rule="fallback:C"),
        ClassifyResult(claim_id=claim.claim_id, tier="A", tier_reason="gov", matched_rule="suffix:A:.gov.cn"),
    ]

    verify = orchestrator._aggregate_verifies(claim, sources, verifies, classes)
    category = orchestrator._aggregate_classes(claim, sources, classes)

    assert verify.verdict == "supported"
    assert len(verify.source_details) == 2
    assert category.tier == "A"


def test_report_includes_multi_source_and_duplicate_fields():
    claim = Claim(
        claim_id="doc#t1#r1",
        statement="2024 market size 10",
        source_name_raw="Source",
        source_url_hint="https://example.com/a",
        source_urls=["https://example.com/a", "https://gov.cn/b"],
        duplicate_count=2,
        duplicate_claim_ids=["doc#t1#r1", "doc#t1#r2"],
    )
    verify = VerifyResult(
        claim_id=claim.claim_id,
        verdict="supported",
        confidence=0.9,
        reasoning="multi",
        source_details=[
            {"url": "https://example.com/a", "domain": "example.com", "fetch_status": "ok", "verdict": "not_found", "confidence": 0.2, "tier": "C", "evidence_quote": None, "reasoning": "missing"},
            {"url": "https://gov.cn/b", "domain": "gov.cn", "fetch_status": "ok", "verdict": "supported", "confidence": 0.9, "tier": "A", "evidence_quote": "market size 10", "reasoning": "matched"},
        ],
    )
    category = ClassifyResult(claim_id=claim.claim_id, tier="A", tier_reason="gov", matched_rule="suffix:A:.gov.cn")

    payload = reporter.render(IR(doc_id="doc", source_format="md"), [claim], {claim.claim_id: verify}, {claim.claim_id: category}, fmt="json")
    text = payload.decode("utf-8")

    assert "来源URL列表" in text
    assert "多源核验明细" in text
    assert "重复条数" in text
    assert "https://gov.cn/b" in text


def test_report_diagnoses_fetch_failure_pdf_empty_and_not_found():
    claim = Claim(
        claim_id="doc#t1#r1",
        statement="claim text",
        source_name_raw="Source",
        source_url_hint="https://example.com/a.pdf",
        source_urls=["https://example.com/a.pdf", "https://example.com/b", "https://example.com/missing"],
    )
    verify = VerifyResult(
        claim_id=claim.claim_id,
        verdict="not_found",
        confidence=0.2,
        reasoning="multi",
        source_details=[
            {
                "url": "https://example.com/a.pdf",
                "domain": "example.com",
                "resolution_method": "hyperlink",
                "fetch_status": "ok",
                "content_type": "pdf",
                "verdict": "not_verifiable",
                "confidence": 0.0,
                "tier": "C",
                "evidence_quote": None,
                "reasoning": "source content is empty or unreadable",
            },
            {
                "url": "https://example.com/b",
                "domain": "example.com",
                "resolution_method": "hyperlink",
                "fetch_status": "ok",
                "content_type": "html",
                "verdict": "not_found",
                "confidence": 0.2,
                "tier": "C",
                "evidence_quote": None,
                "reasoning": "no source passage mentions the claim keywords",
            },
            {
                "url": "https://example.com/missing",
                "domain": "example.com",
                "resolution_method": "hyperlink",
                "fetch_status": "404",
                "content_type": "html",
                "verdict": "not_verifiable",
                "confidence": 0.0,
                "tier": "C",
                "evidence_quote": None,
                "reasoning": "source fetch status is 404",
            },
        ],
    )
    category = ClassifyResult(claim_id=claim.claim_id, tier="C", tier_reason="fallback", matched_rule="fallback:C")

    payload = reporter.render(IR(doc_id="doc", source_format="md"), [claim], {claim.claim_id: verify}, {claim.claim_id: category}, fmt="json")
    row = json.loads(payload.decode("utf-8"))[0]
    text = json.dumps(row, ensure_ascii=False)

    assert "核验诊断" in row
    assert "PDF已下载" in row["核验诊断"]
    assert "未命中声明关键词" in row["核验诊断"]
    assert "诊断：PDF已下载，但文本提取为空" in text
    assert "诊断：来源已读取，但没有命中声明关键词" in text
    assert "诊断：链接无法访问：404" in text


def test_report_diagnoses_empty_body_status():
    diagnosis = reporter._diagnose_reasoning(
        verdict="not_verifiable",
        status="empty_body",
        content_type="pdf",
        method="hyperlink",
        reason="source fetch status is empty_body",
    )

    assert diagnosis == "服务器返回空响应（疑似反爬或登录重定向），无法核验"
