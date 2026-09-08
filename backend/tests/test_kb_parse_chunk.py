"""知识库专项任务 3：文档解析与分块引擎。

验收：
  - 各类型样例文件解析出的中文内容完整无乱码
  - 同一输入 + 同一参数分块结果确定（可重复）
  - 分块 token 数不超过 chunk_size，重叠区符合 overlap
  - 损坏 / 空 / 扫描件抛出可区分错误
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.kb import (
    CorruptedDocumentError,
    EmptyDocumentError,
    InvalidChunkParamsError,
    UnparsableDocumentError,
    chunk,
    estimate_tokens,
    parse,
)
from app.kb.chunking import DEFAULT_SEPARATORS
from app.kb.types import ElementKind

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "kb"


def _load(name: str) -> bytes:
    path = FIXTURES / name
    assert path.is_file(), f"missing fixture: {path} (run generate_fixtures.py)"
    return path.read_bytes()


# ---------------------------------------------------------------------------
# 解析：各类型中文完整
# ---------------------------------------------------------------------------


def test_parse_txt_utf8_chinese() -> None:
    doc = parse(_load("sample.txt"), "txt")
    joined = "\n".join(el.text for el in doc.elements)
    assert "欢迎使用知识库解析引擎" in joined
    assert "UTF-8 中文不被破坏" in joined
    assert "??" not in joined


def test_parse_txt_gbk_chinese() -> None:
    doc = parse(_load("sample_gbk.txt"), "txt")
    assert "GBK 编码的中文样例" in doc.elements[0].text


def test_parse_md_heading_hierarchy() -> None:
    doc = parse(_load("sample.md"), "md")
    headings = [el for el in doc.elements if el.kind == ElementKind.HEADING]
    assert [(h.level, h.text) for h in headings] == [
        (1, "总览"),
        (2, "解析"),
        (3, "细节"),
    ]
    paras = [el for el in doc.elements if el.kind == ElementKind.PARAGRAPH]
    assert any("标题层级应被保留" in p.text for p in paras)
    detail = next(p for p in paras if "中文段落" in p.text)
    assert detail.location.heading_path == ("总览", "解析", "细节")


def test_parse_html_strips_tags_keeps_headings() -> None:
    doc = parse(_load("sample.html"), "html")
    texts = [el.text for el in doc.elements]
    assert "网页标题" in texts
    assert any("HTML 正文，含中文" in t for t in texts)
    assert "var x=1" not in "\n".join(texts)
    h2 = next(el for el in doc.elements if el.text == "小节")
    assert h2.kind == ElementKind.HEADING and h2.level == 2


def test_parse_csv_header_row_join() -> None:
    doc = parse(_load("sample.csv"), "csv")
    assert doc.elements[0].kind == ElementKind.TABLE_ROW
    assert "城市: 北京" in doc.elements[0].text
    assert "人口: 2000万" in doc.elements[0].text
    assert "首都" in doc.elements[0].text


def test_parse_xlsx_chinese() -> None:
    doc = parse(_load("sample.xlsx"), "xlsx")
    joined = "\n".join(el.text for el in doc.elements)
    assert "清单" in joined
    assert "苹果" in joined
    assert "红富士" in joined


def test_parse_docx_headings_and_chinese() -> None:
    doc = parse(_load("sample.docx"), "docx")
    headings = [el for el in doc.elements if el.kind == ElementKind.HEADING]
    assert any(h.text == "产品介绍" and h.level == 1 for h in headings)
    assert any(h.text == "核心能力" and h.level == 2 for h in headings)
    joined = "\n".join(el.text for el in doc.elements)
    assert "大模型聚合网关" in joined
    assert "UTF-8 完整" in joined


def test_parse_pdf_text_layer() -> None:
    doc = parse(_load("sample.pdf"), "pdf")
    joined = "\n".join(el.text for el in doc.elements)
    assert "知识库" in joined
    assert "??" not in joined
    assert doc.elements[0].location.page == 1


# ---------------------------------------------------------------------------
# 错误：空 / 损坏 / 扫描件
# ---------------------------------------------------------------------------


def test_empty_txt_raises() -> None:
    with pytest.raises(EmptyDocumentError) as ei:
        parse(_load("empty.txt"), "txt")
    assert ei.value.code == "empty_document"


def test_empty_md_raises() -> None:
    with pytest.raises(EmptyDocumentError):
        parse(_load("empty.md"), "md")


def test_empty_xlsx_raises() -> None:
    with pytest.raises(EmptyDocumentError):
        parse(_load("empty.xlsx"), "xlsx")


def test_scanned_pdf_raises_unparsable() -> None:
    with pytest.raises(UnparsableDocumentError) as ei:
        parse(_load("scanned.pdf"), "pdf")
    assert ei.value.code == "unparsable_document"
    assert "扫描件暂不支持" in ei.value.message


def test_corrupted_pdf_raises() -> None:
    with pytest.raises(CorruptedDocumentError) as ei:
        parse(_load("corrupted.pdf"), "pdf")
    assert ei.value.code == "corrupted_document"


def test_corrupted_docx_raises() -> None:
    with pytest.raises(CorruptedDocumentError):
        parse(_load("corrupted.docx"), "docx")


def test_corrupted_xlsx_raises() -> None:
    with pytest.raises(CorruptedDocumentError):
        parse(_load("corrupted.xlsx"), "xlsx")


def test_unknown_type_raises() -> None:
    with pytest.raises(CorruptedDocumentError):
        parse(b"hello", "exe")


# ---------------------------------------------------------------------------
# 分块：确定性、上限、重叠、参数边界
# ---------------------------------------------------------------------------


def test_chunk_deterministic() -> None:
    doc = parse(_load("sample.md"), "md")
    a = chunk(doc, chunk_size=100, chunk_overlap=20)
    b = chunk(doc, chunk_size=100, chunk_overlap=20)
    assert [(c.index, c.content, c.token_count) for c in a] == [
        (c.index, c.content, c.token_count) for c in b
    ]


def test_chunk_token_not_exceed_size() -> None:
    # 构造超长段落，迫使二次切分
    long_para = "这是一句测试。" * 200  # 远超 100 token
    from app.kb.types import ParsedDocument, ParsedElement, SourceLocation

    doc = ParsedDocument(
        file_type="txt",
        elements=(
            ParsedElement(
                kind=ElementKind.PARAGRAPH,
                text=long_para,
                location=SourceLocation(),
            ),
        ),
    )
    chunks = chunk(doc, chunk_size=100, chunk_overlap=10)
    assert len(chunks) > 1
    for c in chunks:
        assert c.token_count <= 100
        assert c.token_count == estimate_tokens(c.content)


def test_chunk_overlap_shared_suffix_prefix() -> None:
    long_para = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" * 40
    from app.kb.types import ParsedDocument, ParsedElement, SourceLocation

    doc = ParsedDocument(
        file_type="txt",
        elements=(
            ParsedElement(
                kind=ElementKind.PARAGRAPH,
                text=long_para,
                location=SourceLocation(),
            ),
        ),
    )
    overlap = 20
    chunks = chunk(doc, chunk_size=100, chunk_overlap=overlap, separators=["",])
    assert len(chunks) >= 2
    # 后一块应包含前一块末尾的重叠文本
    prev = chunks[0].content
    cur = chunks[1].content
    # overlap 按 token，换算约 overlap*4 字符
    tail = prev[-(overlap * 4) :]
    assert tail[:16] in cur or any(
        prev[-k:] in cur for k in range(overlap * 2, overlap * 4 + 1)
    )


def test_chunk_structure_preserves_heading_path() -> None:
    doc = parse(_load("sample.md"), "md")
    chunks = chunk(doc, chunk_size=500, chunk_overlap=0)
    assert chunks
    # 至少一个块带有标题路径
    assert any(c.source_location.heading_path for c in chunks)


def test_chunk_param_boundaries() -> None:
    doc = parse(_load("sample.txt"), "txt")
    with pytest.raises(InvalidChunkParamsError):
        chunk(doc, chunk_size=99, chunk_overlap=0)
    with pytest.raises(InvalidChunkParamsError):
        chunk(doc, chunk_size=2001, chunk_overlap=0)
    with pytest.raises(InvalidChunkParamsError):
        chunk(doc, chunk_size=100, chunk_overlap=100)
    with pytest.raises(InvalidChunkParamsError):
        chunk(doc, chunk_size=100, chunk_overlap=201)
    # 边界合法值
    ok = chunk(doc, chunk_size=100, chunk_overlap=0)
    assert ok
    ok2 = chunk(doc, chunk_size=2000, chunk_overlap=200)
    assert ok2


def test_chunk_indexes_contiguous() -> None:
    doc = parse(_load("sample.md"), "md")
    chunks = chunk(doc, chunk_size=100, chunk_overlap=10)
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_default_separators_non_empty() -> None:
    assert "" in DEFAULT_SEPARATORS
    assert "\n\n" in DEFAULT_SEPARATORS
