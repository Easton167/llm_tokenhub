"""解析与分块的数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ElementKind(StrEnum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    TABLE_ROW = "table_row"


@dataclass(frozen=True)
class SourceLocation:
    """来源位置（尽力而为）：页码、标题路径、表格行号等。"""

    page: int | None = None
    heading_path: tuple[str, ...] = ()
    row_index: int | None = None
    sheet: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.page is not None:
            data["page"] = self.page
        if self.heading_path:
            data["heading_path"] = list(self.heading_path)
        if self.row_index is not None:
            data["row_index"] = self.row_index
        if self.sheet is not None:
            data["sheet"] = self.sheet
        return data


@dataclass(frozen=True)
class ParsedElement:
    kind: ElementKind
    text: str
    level: int | None = None  # heading 1–6
    location: SourceLocation = field(default_factory=SourceLocation)


@dataclass(frozen=True)
class ParsedDocument:
    file_type: str
    elements: tuple[ParsedElement, ...]


@dataclass(frozen=True)
class Chunk:
    """分块结果：序号从 0 起，与后续落库 seq 对齐前由调用方决定偏移。"""

    index: int
    content: str
    token_count: int
    source_location: SourceLocation
