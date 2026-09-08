"""Channel Key 加密/解密单元测试。"""

from app.core.channel_key_crypto import (
    decrypt_channel_key,
    encrypt_channel_key,
    mask_channel_key,
)


def test_encrypt_decrypt_roundtrip() -> None:
    plaintext = "sk-upstream-secret-key"
    encrypted = encrypt_channel_key(plaintext)
    assert encrypted != plaintext.encode()
    assert decrypt_channel_key(encrypted) == plaintext


def test_mask_channel_key_default_suffix() -> None:
    """默认展示后 4 位，与 PRD「仅显示后 4 位」一致。"""
    assert mask_channel_key("sk-abcd1234") == "****1234"
    assert mask_channel_key("ab") == "****"


def test_mask_channel_key_custom_suffix() -> None:
    """visible_suffix 可配置，便于后台展示更长/更短后缀。"""
    assert mask_channel_key("sk-abcd1234", visible_suffix=6) == "****cd1234"
    assert mask_channel_key("sk-abcd1234", visible_suffix=2) == "****34"


def test_mask_channel_key_short_key_always_fully_masked() -> None:
    """Key 长度不超过 visible_suffix 时，不暴露任何字符。"""
    assert mask_channel_key("abcd", visible_suffix=4) == "****"
    assert mask_channel_key("abc", visible_suffix=4) == "****"
