"""生成知识库解析引擎测试夹具（含中文）。

用法（在 backend 目录、已安装依赖后）::

    python tests/fixtures/kb/generate_fixtures.py
"""

from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _write_bytes(name: str, data: bytes) -> None:
    path = ROOT / name
    path.write_bytes(data)
    print(f"wrote {path.name} ({len(data)} bytes)")


def _write_text(name: str, text: str, encoding: str = "utf-8") -> None:
    path = ROOT / name
    path.write_bytes(text.encode(encoding))
    print(f"wrote {path.name} ({encoding})")


def _build_pdf_chinese() -> bytes:
    """用 fpdf2 + 系统中文字体生成可被 pypdf 正确抽取的 PDF。

    仅用于维护夹具；运行时解析只依赖 pypdf。无可用字体时回退为英文 PDF，
    并提示维护者在含中文字体的环境重新生成。
    """
    try:
        from fpdf import FPDF
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("生成 PDF 夹具需要 fpdf2：pip install fpdf2") from exc

    font_candidates = [
        Path(r"C:\Windows\Fonts\msyh.ttc"),
        Path(r"C:\Windows\Fonts\simhei.ttf"),
        Path(r"C:\Windows\Fonts\simsun.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/System/Library/Fonts/PingFang.ttc"),
    ]
    font = next((p for p in font_candidates if p.exists()), None)
    pdf = FPDF()
    pdf.add_page()
    if font is None:
        pdf.set_font("Helvetica", size=14)
        pdf.multi_cell(0, 10, "LLM TokenHub Knowledge Base\n(Chinese font missing)")
        print("WARNING: no CJK font found; sample.pdf lacks Chinese glyphs")
    else:
        pdf.add_font("cn", fname=str(font))
        pdf.set_font("cn", size=14)
        pdf.multi_cell(0, 10, "LLM TokenHub\n中文解析测试：知识库引擎")
    return bytes(pdf.output())


def _empty_page_pdf() -> bytes:
    """无文本层的合法 PDF（模拟扫描件）。"""
    objects: list[bytes] = [
        b"1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj\n",
        b"2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj\n",
        b"3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>endobj\n",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(len(out))
        out.extend(obj)
    xref_pos = len(out)
    out.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    out.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    out.extend(
        f"trailer<< /Size {len(offsets)} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n".encode("ascii")
    )
    return bytes(out)


def _build_docx() -> bytes:
    from docx import Document

    doc = Document()
    doc.add_heading("产品介绍", level=1)
    doc.add_paragraph("令牌中枢是一个大模型聚合网关，支持计费与知识库。")
    doc.add_heading("核心能力", level=2)
    doc.add_paragraph("支持多种文档格式解析，中文内容保持 UTF-8 完整。")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _build_xlsx() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "清单"
    ws.append(["名称", "数量", "备注"])
    ws.append(["苹果", "10", "红富士"])
    ws.append(["香蕉", "5", "进口"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)

    _write_text(
        "sample.txt",
        "第一段：欢迎使用知识库解析引擎。\n\n"
        "第二段：本文件用于验证 UTF-8 中文不被破坏。\n",
    )
    _write_text(
        "sample_gbk.txt",
        "这是 GBK 编码的中文样例，解析后应转为 Unicode。\n",
        encoding="gbk",
    )
    _write_text("empty.txt", "")
    _write_text(
        "sample.md",
        "# 总览\n\n"
        "知识库支持多种格式。\n\n"
        "## 解析\n\n"
        "Markdown 标题层级应被保留。\n\n"
        "### 细节\n\n"
        "中文段落内容完整可读。\n",
    )
    _write_text(
        "sample.html",
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<script>var x=1;</script><title>忽略</title></head><body>"
        "<h1>网页标题</h1><p>这是 HTML 正文，含中文。</p>"
        "<h2>小节</h2><p>去标签后保留标题结构。</p>"
        "</body></html>",
    )

    csv_buf = io.StringIO()
    writer = csv.writer(csv_buf)
    writer.writerow(["城市", "人口", "备注"])
    writer.writerow(["北京", "2000万", "首都"])
    writer.writerow(["上海", "2400万", "直辖市"])
    _write_text("sample.csv", csv_buf.getvalue())

    _write_bytes("sample.docx", _build_docx())
    _write_bytes("sample.xlsx", _build_xlsx())
    _write_bytes("sample.pdf", _build_pdf_chinese())
    _write_bytes("scanned.pdf", _empty_page_pdf())
    _write_bytes("corrupted.pdf", b"%PDF-1.4\nthis is not a valid pdf structure")
    _write_bytes("corrupted.docx", b"PK\x03\x04not-a-real-docx")
    _write_bytes("empty.md", b"")

    # 空 xlsx：合法工作簿但无数据行
    from openpyxl import Workbook

    wb = Workbook()
    buf = io.BytesIO()
    wb.save(buf)
    _write_bytes("empty.xlsx", buf.getvalue())

    # 损坏 zip 伪 xlsx
    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("xl/workbook.xml", "not-xml")
    _write_bytes("corrupted.xlsx", bad.getvalue())

    print("done.")


if __name__ == "__main__":
    main()
