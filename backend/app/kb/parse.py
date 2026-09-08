"""统一解析入口：parse(file_bytes, file_type) -> ParsedDocument。"""

from __future__ import annotations

from collections.abc import Callable

from app.core.constants import KB_SUPPORTED_FILE_TYPES
from app.kb.errors import (
    CorruptedDocumentError,
    EmptyDocumentError,
    KbEngineError,
)
from app.kb.parsers import (
    parse_csv,
    parse_docx,
    parse_html,
    parse_md,
    parse_pdf,
    parse_txt,
    parse_xlsx,
)
from app.kb.types import ParsedDocument, ParsedElement

_PARSER_MAP: dict[str, Callable[[bytes], list[ParsedElement]]] = {
    "txt": parse_txt,
    "md": parse_md,
    "pdf": parse_pdf,
    "docx": parse_docx,
    "html": parse_html,
    "csv": parse_csv,
    "xlsx": parse_xlsx,
}


def parse(file_bytes: bytes, file_type: str) -> ParsedDocument:
    """解析文件字节为结构化元素列表。

    Raises:
        CorruptedDocumentError: 类型未知/损坏。
        EmptyDocumentError: 无有效正文。
        UnparsableDocumentError: 如 PDF 扫描件。
    """
    normalized = (file_type or "").strip().lower()
    if normalized not in KB_SUPPORTED_FILE_TYPES:
        raise CorruptedDocumentError(
            f"Unsupported file type for parsing: {file_type!r}"
        )
    parser = _PARSER_MAP[normalized]
    try:
        elements = parser(file_bytes)
    except KbEngineError:
        raise
    except Exception as exc:  # noqa: BLE001 — 解析器未覆盖异常统一为损坏
        raise CorruptedDocumentError(f"Failed to parse {normalized}: {exc}") from exc

    cleaned = tuple(el for el in elements if (el.text or "").strip())
    if not cleaned:
        raise EmptyDocumentError("Document has no extractable text content")
    return ParsedDocument(file_type=normalized, elements=cleaned)
