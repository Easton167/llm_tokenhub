"""知识库原文件存储。"""

from functools import lru_cache

from app.core.config import settings
from app.storage.base import FileStorage
from app.storage.local import LocalFileStorage


@lru_cache
def get_file_storage() -> FileStorage:
    """进程内单例；当前为 LocalFileStorage，后续可按配置切换 S3/OSS。"""
    return LocalFileStorage(settings.kb_storage_root)


__all__ = ["FileStorage", "LocalFileStorage", "get_file_storage"]
