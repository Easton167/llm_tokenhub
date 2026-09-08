"""知识库文档解析与分块引擎（纯逻辑层，不依赖 DB）。

选型说明（见 pyproject.toml 对应依赖）：
- pypdf：轻量 PDF 文本层提取，足够识别扫描件（无文本层）。
- python-docx：读取 docx 正文与 Heading 样式层级。
- openpyxl：读取 xlsx 表头与行值；只读模式适合解析场景。
HTML/TXT/MD/CSV 使用标准库，无需额外依赖。

Token 估算与网关计费预估一致：约 4 字符 ≈ 1 token。
"""

from app.kb.chunking import chunk
from app.kb.errors import (
    CorruptedDocumentError,
    EmptyDocumentError,
    InvalidChunkParamsError,
    UnparsableDocumentError,
)
from app.kb.parse import parse
from app.kb.tokens import estimate_tokens
from app.kb.types import Chunk, ElementKind, ParsedDocument, ParsedElement, SourceLocation

__all__ = [
    "Chunk",
    "CorruptedDocumentError",
    "ElementKind",
    "EmptyDocumentError",
    "InvalidChunkParamsError",
    "ParsedDocument",
    "ParsedElement",
    "SourceLocation",
    "UnparsableDocumentError",
    "chunk",
    "estimate_tokens",
    "parse",
]
