"""各文件类型解析器。"""

from __future__ import annotations

import csv
import io
import re
from html.parser import HTMLParser

from app.kb.encoding import decode_text_bytes
from app.kb.errors import CorruptedDocumentError, UnparsableDocumentError
from app.kb.types import ElementKind, ParsedElement, SourceLocation

_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_DOCX_HEADING_RE = re.compile(r"^Heading\s*([1-6])$", re.IGNORECASE)
_DOCX_HEADING_CN_RE = re.compile(r"^标题\s*([1-6])$")


def parse_txt(data: bytes) -> list[ParsedElement]:
    text = decode_text_bytes(data)
    return _paragraphs_from_text(text)


def parse_md(data: bytes) -> list[ParsedElement]:
    text = decode_text_bytes(data)
    elements: list[ParsedElement] = []
    heading_stack: list[tuple[int, str]] = []
    paragraph_lines: list[str] = []

    def flush_paragraph() -> None:
        nonlocal paragraph_lines
        body = "\n".join(paragraph_lines).strip()
        paragraph_lines = []
        if not body:
            return
        path = tuple(t for _, t in heading_stack)
        elements.append(
            ParsedElement(
                kind=ElementKind.PARAGRAPH,
                text=body,
                location=SourceLocation(heading_path=path),
            )
        )

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        m = _MD_HEADING_RE.match(line)
        if m:
            flush_paragraph()
            level = len(m.group(1))
            title = m.group(2).strip()
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
            elements.append(
                ParsedElement(
                    kind=ElementKind.HEADING,
                    text=title,
                    level=level,
                    location=SourceLocation(
                        heading_path=tuple(t for _, t in heading_stack)
                    ),
                )
            )
            continue
        if not line.strip():
            flush_paragraph()
            continue
        paragraph_lines.append(line)

    flush_paragraph()
    return elements


def parse_html(data: bytes) -> list[ParsedElement]:
    text = decode_text_bytes(data)
    parser = _HtmlStructureParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:  # noqa: BLE001 — 统一映射为损坏
        raise CorruptedDocumentError(f"Invalid HTML: {exc}") from exc
    return parser.elements


def parse_csv(data: bytes) -> list[ParsedElement]:
    text = decode_text_bytes(data)
    if not text.strip():
        return []
    try:
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
    except csv.Error as exc:
        raise CorruptedDocumentError(f"Invalid CSV: {exc}") from exc
    return _tabular_rows_to_elements(rows)


def parse_xlsx(data: bytes) -> list[ParsedElement]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("openpyxl is required to parse xlsx") from exc

    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise CorruptedDocumentError(f"Invalid XLSX: {exc}") from exc

    elements: list[ParsedElement] = []
    try:
        for sheet in wb.worksheets:
            rows: list[list[str]] = []
            for row in sheet.iter_rows(values_only=True):
                rows.append(["" if c is None else str(c).strip() for c in row])
            # 去掉尾部全空行
            while rows and all(not c for c in rows[-1]):
                rows.pop()
            sheet_elements = _tabular_rows_to_elements(rows, sheet=sheet.title)
            if sheet_elements:
                elements.append(
                    ParsedElement(
                        kind=ElementKind.HEADING,
                        text=sheet.title,
                        level=1,
                        location=SourceLocation(
                            heading_path=(sheet.title,), sheet=sheet.title
                        ),
                    )
                )
                elements.extend(sheet_elements)
    finally:
        wb.close()
    return elements


def parse_docx(data: bytes) -> list[ParsedElement]:
    try:
        from docx import Document
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("python-docx is required to parse docx") from exc

    try:
        document = Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        raise CorruptedDocumentError(f"Invalid DOCX: {exc}") from exc

    elements: list[ParsedElement] = []
    heading_stack: list[tuple[int, str]] = []

    for para in document.paragraphs:
        text = (para.text or "").strip()
        if not text:
            continue
        level = _docx_heading_level(para.style.name if para.style else None)
        if level is not None:
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, text))
            elements.append(
                ParsedElement(
                    kind=ElementKind.HEADING,
                    text=text,
                    level=level,
                    location=SourceLocation(
                        heading_path=tuple(t for _, t in heading_stack)
                    ),
                )
            )
            continue
        path = tuple(t for _, t in heading_stack)
        elements.append(
            ParsedElement(
                kind=ElementKind.PARAGRAPH,
                text=text,
                location=SourceLocation(heading_path=path),
            )
        )

    # 表格：每行转 table_row
    for table in document.tables:
        headers: list[str] = []
        for i, row in enumerate(table.rows):
            cells = [(_cell_text(c)).strip() for c in row.cells]
            if i == 0 and any(cells):
                headers = cells
                continue
            line = _format_table_row(headers, cells)
            if not line:
                continue
            elements.append(
                ParsedElement(
                    kind=ElementKind.TABLE_ROW,
                    text=line,
                    location=SourceLocation(
                        heading_path=tuple(t for _, t in heading_stack),
                        row_index=i,
                    ),
                )
            )

    return elements


def parse_pdf(data: bytes) -> list[ParsedElement]:
    if not data.startswith(b"%PDF"):
        raise CorruptedDocumentError("Not a valid PDF file")
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("pypdf is required to parse pdf") from exc

    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
    except Exception as exc:  # noqa: BLE001
        raise CorruptedDocumentError(f"Invalid PDF: {exc}") from exc

    if reader.is_encrypted:
        try:
            ok = reader.decrypt("")
        except Exception as exc:  # noqa: BLE001
            raise CorruptedDocumentError(f"Encrypted PDF cannot be opened: {exc}") from exc
        if not ok:
            raise CorruptedDocumentError("Encrypted PDF cannot be opened")

    elements: list[ParsedElement] = []
    any_text = False
    try:
        pages = reader.pages
    except Exception as exc:  # noqa: BLE001
        raise CorruptedDocumentError(f"Invalid PDF pages: {exc}") from exc

    for page_idx, page in enumerate(pages, start=1):
        try:
            raw = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001
            raise CorruptedDocumentError(f"Failed to extract PDF page {page_idx}: {exc}") from exc
        text = raw.strip()
        if not text:
            continue
        any_text = True
        for para in _split_paragraphs(text):
            elements.append(
                ParsedElement(
                    kind=ElementKind.PARAGRAPH,
                    text=para,
                    location=SourceLocation(page=page_idx),
                )
            )

    if len(pages) > 0 and not any_text:
        raise UnparsableDocumentError("扫描件暂不支持：PDF 无文本层，OCR 尚未开放")
    return elements


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _paragraphs_from_text(text: str) -> list[ParsedElement]:
    return [
        ParsedElement(kind=ElementKind.PARAGRAPH, text=p)
        for p in _split_paragraphs(text)
    ]


def _split_paragraphs(text: str) -> list[str]:
    parts = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    return [p.strip() for p in parts if p.strip()]


def _tabular_rows_to_elements(
    rows: list[list[str]], *, sheet: str | None = None
) -> list[ParsedElement]:
    if not rows:
        return []
    headers = [h.strip() for h in rows[0]]
    elements: list[ParsedElement] = []
    start = 1
    if not any(headers):
        headers = []
        start = 0
    for i, row in enumerate(rows[start:], start=start):
        line = _format_table_row(headers, row)
        if not line:
            continue
        elements.append(
            ParsedElement(
                kind=ElementKind.TABLE_ROW,
                text=line,
                location=SourceLocation(
                    row_index=i,
                    sheet=sheet,
                    heading_path=(sheet,) if sheet else (),
                ),
            )
        )
    return elements


def _format_table_row(headers: list[str], cells: list[str]) -> str:
    cells = [c.strip() if c is not None else "" for c in cells]
    if headers:
        pairs: list[str] = []
        for idx, header in enumerate(headers):
            value = cells[idx] if idx < len(cells) else ""
            name = header or f"列{idx + 1}"
            pairs.append(f"{name}: {value}")
        # 超出表头的多余列
        for idx in range(len(headers), len(cells)):
            if cells[idx]:
                pairs.append(f"列{idx + 1}: {cells[idx]}")
        return " | ".join(pairs).strip(" |")
    return " | ".join(c for c in cells if c)


def _docx_heading_level(style_name: str | None) -> int | None:
    if not style_name:
        return None
    m = _DOCX_HEADING_RE.match(style_name.strip())
    if m:
        return int(m.group(1))
    m = _DOCX_HEADING_CN_RE.match(style_name.strip())
    if m:
        return int(m.group(1))
    return None


def _cell_text(cell: object) -> str:
    text = getattr(cell, "text", None)
    return text if isinstance(text, str) else ""


class _HtmlStructureParser(HTMLParser):
    """提取标题与段落；忽略 script/style。"""

    _SKIP = frozenset({"script", "style", "noscript"})
    _HEADINGS = {f"h{i}": i for i in range(1, 7)}
    _BLOCK = frozenset({"p", "div", "li", "tr", "br", "section", "article"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[ParsedElement] = []
        self._heading_stack: list[tuple[int, str]] = []
        self._skip_depth = 0
        self._capture_tag: str | None = None
        self._capture_level: int | None = None
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._SKIP:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "br":
            self._buffer.append("\n")
            return
        if tag in self._HEADINGS:
            self._flush_paragraph()
            self._capture_tag = tag
            self._capture_level = self._HEADINGS[tag]
            self._buffer = []
            return
        if tag in self._BLOCK and self._capture_tag is None:
            self._flush_paragraph()
            self._capture_tag = "p"
            self._capture_level = None
            self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if self._capture_tag == tag or (
            self._capture_tag == "p" and tag in self._BLOCK
        ):
            text = "".join(self._buffer).strip()
            level = self._capture_level
            self._capture_tag = None
            self._capture_level = None
            self._buffer = []
            if not text:
                return
            if level is not None:
                while self._heading_stack and self._heading_stack[-1][0] >= level:
                    self._heading_stack.pop()
                self._heading_stack.append((level, text))
                self.elements.append(
                    ParsedElement(
                        kind=ElementKind.HEADING,
                        text=text,
                        level=level,
                        location=SourceLocation(
                            heading_path=tuple(t for _, t in self._heading_stack)
                        ),
                    )
                )
            else:
                path = tuple(t for _, t in self._heading_stack)
                self.elements.append(
                    ParsedElement(
                        kind=ElementKind.PARAGRAPH,
                        text=text,
                        location=SourceLocation(heading_path=path),
                    )
                )

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._capture_tag is not None:
            self._buffer.append(data)

    def close(self) -> None:
        self._flush_paragraph()
        super().close()

    def _flush_paragraph(self) -> None:
        if self._capture_tag == "p":
            text = "".join(self._buffer).strip()
            self._capture_tag = None
            self._buffer = []
            if text:
                path = tuple(t for _, t in self._heading_stack)
                self.elements.append(
                    ParsedElement(
                        kind=ElementKind.PARAGRAPH,
                        text=text,
                        location=SourceLocation(heading_path=path),
                    )
                )
        elif self._capture_tag in self._HEADINGS:
            # 未闭合标题：丢弃缓冲，避免残缺元素
            self._capture_tag = None
            self._capture_level = None
            self._buffer = []
