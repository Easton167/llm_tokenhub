"""知识库上传文件：真实类型嗅探（不只看扩展名）。"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from app.core.constants import KB_FILE_EXT_TO_TYPE, KB_SUPPORTED_FILE_TYPES
from app.core.errors import UnsupportedFileTypeError


def claimed_type_from_filename(filename: str) -> str:
    """按扩展名解析声称类型；不支持则抛 unsupported_file_type。"""
    ext = Path(filename).suffix.lower()
    claimed = KB_FILE_EXT_TO_TYPE.get(ext)
    if claimed is None:
        raise UnsupportedFileTypeError(
            f"Unsupported file extension '{ext or '(none)'}'. "
            f"Allowed: {', '.join(sorted({e for e in KB_FILE_EXT_TO_TYPE}))}"
        )
    return claimed


def _try_decode_text(data: bytes) -> str | None:
    for encoding in ("utf-8-sig", "utf-8", "gbk", "gb2312", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def _looks_like_html(text: str) -> bool:
    head = text.lstrip()[:2048].lower()
    return (
        head.startswith("<!doctype html")
        or head.startswith("<html")
        or "<html" in head[:512]
    )


def _sniff_ooxml(data: bytes) -> str | None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
    except zipfile.BadZipFile:
        return None
    if any(n == "word/document.xml" or n.startswith("word/") for n in names):
        return "docx"
    if any(n == "xl/workbook.xml" or n.startswith("xl/") for n in names):
        return "xlsx"
    return None


def sniff_file_type(data: bytes, *, claimed: str | None = None) -> str | None:
    """根据内容嗅探文件类型；无法识别返回 None。

    文本类（txt/md/csv）在内容为合法文本且非 HTML/二进制时，可按 claimed 消歧。
    """
    if data.startswith(b"%PDF"):
        return "pdf"
    # PE/DOS 可执行文件（伪造扩展名常见）
    if data[:2] == b"MZ":
        return None
    if len(data) >= 4 and data[:4] == b"PK\x03\x04":
        return _sniff_ooxml(data)

    text = _try_decode_text(data)
    if text is None:
        return None

    if _looks_like_html(text):
        return "html"

    # 纯文本族：扩展名消歧（内容无法区分 txt/md/csv）
    if claimed in ("txt", "md", "csv", "html"):
        return claimed
    if not data:
        return claimed if claimed in ("txt", "md", "csv", "html") else None
    return None


def detect_and_validate_file_type(filename: str, data: bytes) -> str:
    """扩展名声称类型必须与内容嗅探一致，否则 unsupported_file_type。"""
    claimed = claimed_type_from_filename(filename)
    sniffed = sniff_file_type(data, claimed=claimed)
    if sniffed is None or sniffed != claimed:
        raise UnsupportedFileTypeError(
            f"File content does not match extension for '{filename}'. "
            f"Claimed={claimed}, detected={sniffed or 'unknown'}"
        )
    if sniffed not in KB_SUPPORTED_FILE_TYPES:
        raise UnsupportedFileTypeError(f"Unsupported file type: {sniffed}")
    return sniffed
