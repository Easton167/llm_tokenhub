"""上游 Channel Key 的 AES-GCM 加密与脱敏展示。

平台用户 API Key 只存哈希；上游厂商 Key 必须可逆（路由时要明文带出去），
因此用 AES-GCM 加密后存 ``channel_keys.api_key_encrypted``。

密文格式：``nonce(12 字节) || ciphertext+tag``。
密钥：``CHANNEL_KEY_ENCRYPTION_KEY`` 经 SHA-256 派生为 32 字节 AES-256 密钥，
这样配置字符串不必恰好 32 字节，也避免直接把弱口令当 AES key。

``mask_channel_key`` 只用于管理端展示，绝不把明文写日志。
"""

import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings

# AES-GCM 推荐 nonce 长度；每次加密必须随机，密文才能安全存放同一密钥下的多条 Key
_NONCE_SIZE = 12


def _derive_aes_key(encryption_key: str | None = None) -> bytes:
    """把配置字符串稳定派生为 32 字节 AES-256 密钥。"""
    raw = encryption_key if encryption_key is not None else settings.channel_key_encryption_key
    return hashlib.sha256(raw.encode()).digest()


def encrypt_channel_key(plaintext: str, encryption_key: str | None = None) -> bytes:
    """加密上游 Key。返回可直接写入 BYTEA 的 bytes。"""
    nonce = os.urandom(_NONCE_SIZE)
    aesgcm = AESGCM(_derive_aes_key(encryption_key))
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return nonce + ciphertext


def decrypt_channel_key(encrypted: bytes, encryption_key: str | None = None) -> str:
    """解密上游 Key。路由选中渠道后立刻使用，不要缓存明文到 Redis。"""
    nonce, ciphertext = encrypted[:_NONCE_SIZE], encrypted[_NONCE_SIZE:]
    aesgcm = AESGCM(_derive_aes_key(encryption_key))
    return aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")


def mask_channel_key(plaintext: str, *, visible_suffix: int = 4) -> str:
    """管理端脱敏：``****`` + 末尾若干字符。过短则全部打码。"""
    if visible_suffix <= 0 or len(plaintext) <= visible_suffix:
        return "****"
    return f"****{plaintext[-visible_suffix:]}"
