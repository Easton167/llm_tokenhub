"""文件存储抽象：接口签名兼容后续 S3/OSS 实现。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import BinaryIO


class FileStorage(ABC):
    """原文件存储后端。存储键使用随机不可预测标识，不使用原始文件名。"""

    @abstractmethod
    def save(self, data: bytes, *, key: str | None = None) -> str:
        """写入字节并返回存储键；key 为空时由实现生成随机键。"""

    @abstractmethod
    def open(self, key: str) -> BinaryIO:
        """以二进制只读方式打开对象；不存在时抛 FileNotFoundError。"""

    @abstractmethod
    def delete(self, key: str) -> None:
        """删除对象；不存在时视为成功（幂等）。"""

    @abstractmethod
    def exists(self, key: str) -> bool:
        """判断对象是否存在。"""
