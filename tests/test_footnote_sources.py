from market_source_verification_agent import extractor, ingestor
from market_source_verification_agent.schema import Block, IR


def test_table_source_footnotes_resolve_to_urls():
    ir = IR(
        doc_id="demo",
        source_format="pdf",
        blocks=[
            Block(
                type="table",
                rows=[
                    ["metric", "value", "year", "source"],
                    ["market size", "10 bn", "2024", "Government briefing 1 2"],
                ],
                footnotes={
                    1: "https://www.gov.cn/lianbo/bumen/202402/content_6934828.htm",
                    2: "https://www.gov.cn/zhengce/202408/content_6966120.htm",
                },
            )
        ],
    )

    claim = extractor.extract_claims(ir)[0]

    assert claim.source_name_raw == "Government briefing"
    assert claim.source_name_with_marks == "Government briefing 1 2"
    assert claim.source_url_hint == "https://www.gov.cn/lianbo/bumen/202402/content_6934828.htm"
    assert claim.extra_source_urls == ["https://www.gov.cn/zhengce/202408/content_6966120.htm"]


def test_superscript_source_footnotes_resolve_to_urls():
    clean, urls = extractor._resolve_footnote_urls(
        "Xinhua report¹²",
        {
            1: "https://www.xinhuanet.com/example-a.htm",
            2: "https://www.xinhuanet.com/example-b.htm",
        },
    )

    assert clean == "Xinhua report"
    assert urls == ["https://www.xinhuanet.com/example-a.htm", "https://www.xinhuanet.com/example-b.htm"]


def test_repeated_table_header_is_detected():
    first = ingestor._header_signature([["metric", "value", "year", "source"]])
    second = ingestor._header_signature([["metric", "value", "year", "source"]])

    assert first is not None
    assert second is not None
    assert ingestor._headers_match(first, second) is True


def test_row_footnote_source_fallback_when_source_column_drifts():
    ir = IR(
        doc_id="demo",
        source_format="pdf",
        blocks=[
            Block(
                type="table",
                rows=[
                    ["metric", "value", "year", "source", "notes"],
                    ["market size", "10 bn", "2024", "2024-04", "Xinhua report 1 2"],
                ],
                footnotes={
                    1: "https://www.xinhuanet.com/example-a.htm",
                    2: "https://www.xinhuanet.com/example-b.htm",
                },
            )
        ],
    )

    claim = extractor.extract_claims(ir)[0]

    assert claim.source_name_raw == "Xinhua report"
    assert claim.source_url_hint == "https://www.xinhuanet.com/example-a.htm"
    assert claim.extra_source_urls == ["https://www.xinhuanet.com/example-b.htm"]


def test_wrapped_no_space_pdf_footnote_text_is_parsed():
    class FakePage:
        def get_links(self):
            return []

    class FakePlumberPage:
        def extract_text(self):
            return (
                "1https://imgs.xinhuanet.com/tech/20240401/abc/c.h\n"
                "tml\n"
                "2https://www.gov.cn/example.htm\n"
            )

    footnotes = ingestor._extract_page_footnotes(FakePage(), FakePlumberPage())

    assert footnotes[1] == "https://imgs.xinhuanet.com/tech/20240401/abc/c.html"
    assert footnotes[2] == "https://www.gov.cn/example.htm"


def test_mojibake_superscript_pdf_footnote_text_is_parsed():
    class FakePage:
        def get_links(self):
            return []

    class FakePlumberPage:
        def extract_text(self):
            return (
                "鹿https://www.gov.cn/yaowen/liebiao/202404/content_6943071.htm\n"
                "虏https://www.xinhuanet.com/example.htm\n"
                "鲁https://www.caict.ac.cn/report.pdf\n"
            )

    footnotes = ingestor._extract_page_footnotes(FakePage(), FakePlumberPage())

    assert footnotes[1] == "https://www.gov.cn/yaowen/liebiao/202404/content_6943071.htm"
    assert footnotes[2] == "https://www.xinhuanet.com/example.htm"
    assert footnotes[3] == "https://www.caict.ac.cn/report.pdf"


def test_mojibake_superscript_near_link_number_is_parsed():
    class Rect:
        x0 = 30
        y0 = 10
        y1 = 20

    class FakePage:
        def get_text(self, mode):
            assert mode == "words"
            return [
                (10, 10, 16, 20, "虏"),
                (60, 10, 90, 20, "https://example.com"),
            ]

    assert ingestor._nearest_link_number(FakePage(), Rect()) == 2


def test_pdf_table_dump_paragraph_is_not_extracted_as_claim():
    text = (
        "指标 数值 年份 地区/口径 来源 备注\n"
        "低空经济规模 10644.6亿元 2026 中国 赛迪顾问报告 19 20\n"
        "19https://m.21jingji.com/article/example.html\n"
        "20https://finance.sina.cn/example.html\n"
        "21https://imgs.xinhuanet.com/tech/example/c.html\n"
    )

    assert extractor._paragraph_to_claim("doc", 1, text, []) is None


def test_year_normalization_rejects_dates_and_table_fragments():
    assert extractor._normalize_year("2024") == "2024"
    assert extractor._normalize_year("2024-2030年") == "2024-2030年"
    assert extractor._normalize_year("2024-04-09") is None
    assert extractor._normalize_year("2024- 中国；低空") is None


def test_matrix_table_is_split_by_cell_footnotes():
    ir = IR(
        doc_id="demo",
        source_format="pdf",
        blocks=[
            Block(
                type="table",
                rows=[
                    ["分类", "代表产品/服务", "应用场景", "", "", "目标客户/用户", "", "", "来源", "备注"],
                    ["航空装备域：低空飞行器", "无人机、eVTOL 1 2", "应用场景", "城市空中交通、物流运输等。 3 4", "", "行业客户、居民消费。 5 6", "", "", "中国信通院；中国政府网；亿航官网 7 8 9", "定义说明。 10"],
                ],
                footnotes={
                    1: "https://example.com/report-a",
                    2: "https://example.com/report-b",
                    3: "https://example.com/app-a",
                    4: "https://example.com/app-b",
                    5: "https://example.com/customer-a",
                    6: "https://example.com/customer-b",
                    7: "https://example.com/source-a",
                    8: "https://example.com/source-b",
                    9: "https://example.com/source-c",
                    10: "https://example.com/note-a",
                },
            )
        ],
    )

    claims = extractor.extract_claims(ir)

    # Matrix tables now aggregate one logical row into a single claim instead
    # of one-claim-per-cell, so a 4-field row produces 1 claim with all URLs
    # collected.
    assert len(claims) == 1
    claim = claims[0]
    assert claim.metric == "航空装备域：低空飞行器"
    assert claim.value.startswith("无人机")
    expected_urls = {
        "https://example.com/report-a",
        "https://example.com/report-b",
        "https://example.com/app-a",
        "https://example.com/app-b",
        "https://example.com/customer-a",
        "https://example.com/customer-b",
        "https://example.com/source-a",
        "https://example.com/source-b",
        "https://example.com/source-c",
        "https://example.com/note-a",
    }
    assert set(claim.source_urls) == expected_urls
    for label in ("应用场景", "目标客户/用户", "备注", "代表产品"):
        assert label in claim.statement
