import shutil
import uuid
from pathlib import Path

from market_source_verification_agent import verifier
from market_source_verification_agent.config import Settings
from market_source_verification_agent.schema import Claim, ResolvedSource


def test_chinese_tokenize_emits_bigrams_for_substring_recall():
    tokens = verifier._tokenize("民用无人机保有量 eVTOL")

    assert "民用" in tokens
    assert "无人" in tokens
    assert "人机" in tokens
    assert "evtol" in tokens


def test_retrieve_passages_falls_back_when_bm25_has_no_keyword_signal():
    claim = Claim(
        claim_id="c1",
        metric="低空经济规模",
        value="5059.5亿元",
        year="2023",
        statement="2023年低空经济规模为5059.5亿元",
        source_name_raw="来源",
    )
    passages = verifier.retrieve_passages(claim, "这是一段完全不同主题的新闻。\n这是第二段。", k=2)

    assert len(passages) == 1
    assert all(p.score == 0 for p in passages)
    assert all("fallback" in p.locator for p in passages)


def test_retrieve_passages_expands_query_synonyms_for_lexical_recall():
    claim = Claim(
        claim_id="c1",
        metric="低空经济",
        value=None,
        year=None,
        statement="低空经济纳入交通规划",
        source_name_raw="来源",
    )
    source_text = "\n".join(
        [
            "无关内容" * 260,
            "通用航空产业是综合交通运输体系的重要组成部分。",
            "其他段落" * 260,
        ]
    )

    passages = verifier.retrieve_passages(claim, source_text, k=1)

    assert passages[0].locator == "para=2"
    assert passages[0].score > 0
    assert "fallback" not in passages[0].locator


def test_verify_skips_llm_when_bm25_has_no_keyword_signal(monkeypatch):
    claim = Claim(
        claim_id="c1",
        metric="低空经济规模",
        value="5059.5亿元",
        year="2023",
        statement="2023年低空经济规模为5059.5亿元",
        source_name_raw="来源",
    )
    source = ResolvedSource(
        claim_id="c1",
        resolution_method="hyperlink",
        url="https://example.com/a",
        domain="example.com",
        fetch_status="ok",
        content_type="html",
    )
    monkeypatch.setattr(verifier, "load_cached_source_text", lambda _, settings=None: "这是一段完全不同主题的新闻。")

    def fake_llm(*args, **kwargs):
        raise AssertionError("LLM should be skipped when all passages are fallback")

    monkeypatch.setattr(verifier, "_judge_by_llm", fake_llm)

    result = verifier.verify(claim, source)

    assert result.verdict == "not_found"
    assert result.confidence == 0.15
    assert "LLM skipped" in result.reasoning


def test_verify_llm_cache_hit_skips_openai_call():
    cache_dir = Path("data/test-cache") / uuid.uuid4().hex
    claim = Claim(
        claim_id="c1",
        metric="低空经济规模",
        value="5059.5亿元",
        year="2023",
        statement="2023年低空经济规模为5059.5亿元",
        source_name_raw="来源",
    )
    source = ResolvedSource(
        claim_id="c1",
        resolution_method="hyperlink",
        url="https://example.com/a",
        domain="example.com",
        fetch_status="ok",
        content_type="html",
        content_hash="source-content-sha256",
    )
    try:
        settings = Settings(cache={"dir": str(cache_dir), "verify_ttl_days": 7})
        cache_path, _ = verifier._llm_cache_paths(source, claim, settings)
        assert cache_path is not None
        verifier._write_cache(
            cache_path,
            {
                "verdict": "supported",
                "confidence": 0.88,
                "evidence_quote": "2023年低空经济规模为5059.5亿元",
                "evidence_locator": "para=3",
                "reasoning": "cached verdict",
            },
        )

        result = verifier._judge_by_llm(
            claim,
            source,
            [verifier.Passage(text="低空经济相关内容", locator="para=1", score=1.0)],
            k=1,
            settings=settings,
        )

        assert result is not None
        assert result.verdict == "supported"
        assert result.confidence == 0.88
        assert result.evidence_locator == "para=3"
        assert result.reasoning == "cached verdict"
    finally:
        shutil.rmtree(cache_dir, ignore_errors=True)


def test_list_value_exact_matches_semantic_majority_aliases():
    text = "相关产品包括无人机、eVTOL、直升飞机、传统固定翼飞机。"

    assert verifier._value_exact(text, "无人机、eVTOL、直升机、固定翼飞机")


def test_verify_empty_pdf_reasoning_mentions_ocr(monkeypatch):
    claim = Claim(
        claim_id="c1",
        metric="低空经济规模",
        value="5059.5亿元",
        year="2023",
        statement="2023年低空经济规模为5059.5亿元",
        source_name_raw="来源",
    )
    source = ResolvedSource(
        claim_id="c1",
        resolution_method="hyperlink",
        url="https://example.com/a.pdf",
        domain="example.com",
        fetch_status="ok",
        content_type="pdf",
    )
    monkeypatch.setattr(verifier, "load_cached_source_text", lambda _, settings=None: "")

    result = verifier.verify(claim, source)

    assert result.verdict == "not_verifiable"
    assert "OCR not enabled" in result.reasoning
    assert "PDF" in result.reasoning
