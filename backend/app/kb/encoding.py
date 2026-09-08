"""文本字节解码：UTF-8 优先，兼容 GBK 等，结果统一为 Unicode 字符串。"""

from __future__ import annotations

from app.kb.errors import CorruptedDocumentError

_ENCODINGS = ("utf-8-sig", "utf-8", "gbk", "gb2312")


def decode_text_bytes(data: bytes) -> str:
    """按优先级解码；全部失败则视为损坏文件。"""
    if not data:
        return ""
    for encoding in _ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise CorruptedDocumentError("Unable to decode text with UTF-8/GBK")
