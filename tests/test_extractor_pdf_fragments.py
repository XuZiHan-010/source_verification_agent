"""Tests for PDF table fragment detection in extractor."""

from market_source_verification_agent import extractor, ingestor
from market_source_verification_agent.extractor import _looks_like_broken_fragment
from market_source_verification_agent.ingestor import _is_continuation_row
from market_source_verification_agent.schema import IR, Block


def test_broken_fragment_detects_punctuation_lead():
    assert _looks_like_broken_fragment("、维修")
    assert _looks_like_broken_fragment("/应用场景")
    assert _looks_like_broken_fragment("，金属")


def test_broken_fragment_detects_short_meaningless():
    assert _looks_like_broken_fragment("等。")
    assert _looks_like_broken_fragment("护")
    assert _looks_like_broken_fragment(";；")


def test_broken_fragment_detects_truncated_prefixes():
    assert _looks_like_broken_fragment("器器材及零部件")
    assert _looks_like_broken_fragment("护、维修。")
    assert _looks_like_broken_fragment("控系统、飞行管理")
    assert _looks_like_broken_fragment("事项；低空经")


def test_broken_fragment_passes_normal_text():
    assert not _looks_like_broken_fragment("低空经济规模")
    assert not _looks_like_broken_fragment("5059.5 亿元")
    assert not _looks_like_broken_fragment("无人机、eVTOL、直升机、固定翼飞机")
    assert not _looks_like_broken_fragment(None)
    assert not _looks_like_broken_fragment("")


def test_main_table_drops_broken_fragment_rows():
    """主表格路径：metric 或 value 是碎片的行应被丢弃，不进 claim 列表。"""
    rows = [
        ["指标", "数值", "年份", "地区", "来源名称"],
        ["低空经济规模", "5059.5 亿元", "2023", "中国", "赛迪研究院（gov.cn）"],
        ["器器材及零部件", "无人机相关", "2024", "中国", "赛迪研究院（gov.cn）"],
        ["飞行配套服务", "等。", "2024", "中国", "赛迪研究院（gov.cn）"],
    ]
    block = Block(type="table", page=1, rows=rows)
    ir = IR(doc_id="test", source_format="pdf", blocks=[block])
    claims = extractor.extract_claims(ir)
    metrics = [c.metric for c in claims]
    values = [c.value for c in claims]
    assert "低空经济规模" in metrics
    assert "器器材及零部件" not in metrics
    assert "等。" not in values


def test_continuation_row_merges_short_fragments():
    """前 4-5 列空 + 短碎片 → 合并到上一行。"""
    row_normal = ["低空经济", "5059.5 亿", "2023", "中国", "新华社"]
    row_fragment = ["", "", "", "", "等。"]
    assert _is_continuation_row(row_fragment)
    assert not _is_continuation_row(row_normal)
    # all-empty leading still works (≥6 columns)
    assert _is_continuation_row(["", "", "", "", "", "继续描述文字内容"])
