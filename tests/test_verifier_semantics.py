from market_source_verification_agent import verifier
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
