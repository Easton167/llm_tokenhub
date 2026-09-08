"""本地磁盘文件存储实现。"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import BinaryIO

from app.storage.base import FileStorage


class LocalFileStorage(FileStorage):
    """根目录下按随机键落盘；路径形如 ``ab/cd/<token>``，不含原始文件名。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, key: str) -> Path:
        # 拒绝路径穿越
        if ".." in key.replace("\\", "/").split("/"):
            raise ValueError(f"Invalid storage key: {key!r}")
        path = (self.root / key).resolve()
        if not str(path).startswith(str(self.root)):
            raise ValueError(f"Invalid storage key: {key!r}")
        return path

    @staticmethod
    def generate_key() -> str:
        token = secrets.token_hex(16)
        return f"{token[:2]}/{token[2:4]}/{token}"

    def save(self, data: bytes, *, key: str | None = None) -> str:
        storage_key = key or self.generate_key()
        path = self._path_for(storage_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return storage_key

    def open(self, key: str) -> BinaryIO:
        path = self._path_for(key)
        return path.open("rb")

    def delete(self, key: str) -> None:
        path = self._path_for(key)
        if path.is_file():
            path.unlink()
        # 尽力清理空父目录（忽略竞争失败）
        for parent in (path.parent, path.parent.parent):
            if parent == self.root or not parent.is_relative_to(self.root):
                break
            try:
                parent.rmdir()
            except OSError:
                break

    def exists(self, key: str) -> bool:
        try:
            return self._path_for(key).is_file()
        except ValueError:
            return False
