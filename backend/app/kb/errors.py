"""解析/分块引擎可区分错误类型（纯逻辑层，不绑定 HTTP）。"""

from __future__ import annotations


class KbEngineError(Exception):
    """解析/分块引擎异常基类。"""

    def __init__(self, message: str, *, code: str) -> None:
        self.message = message
        self.code = code
        super().__init__(message)


class UnparsableDocumentError(KbEngineError):
    """可打开但无法提取可用文本（如 PDF 扫描件无文本层）。"""

    def __init__(self, message: str = "扫描件暂不支持") -> None:
        super().__init__(message, code="unparsable_document")


class EmptyDocumentError(KbEngineError):
    """文档无有效正文内容。"""

    def __init__(self, message: str = "Document is empty") -> None:
        super().__init__(message, code="empty_document")


class CorruptedDocumentError(KbEngineError):
    """文件损坏或格式非法，无法解析。"""

    def __init__(self, message: str = "Document is corrupted") -> None:
        super().__init__(message, code="corrupted_document")


class InvalidChunkParamsError(KbEngineError):
    """分块参数越界。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="invalid_chunk_params")
