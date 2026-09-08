"""API Key 生成、哈希与 Bearer 解析单元测试。"""

from app.core.config import settings
from app.core.constants import API_KEY_PREFIX
from app.core.security import (
    generate_api_key_material,
    hash_api_key,
    is_valid_api_key_format,
    parse_bearer_token,
)


def test_hash_api_key_is_deterministic() -> None:
    plaintext = "sk-test-key"
    assert hash_api_key(plaintext) == hash_api_key(plaintext)
    assert hash_api_key(plaintext) != hash_api_key("sk-other-key")


def test_hash_api_key_uses_pepper() -> None:
    plaintext = "sk-test-key"
    assert hash_api_key(plaintext, pepper="pepper-a") != hash_api_key(
        plaintext, pepper="pepper-b"
    )
    assert hash_api_key(plaintext, pepper=settings.api_key_pepper) == hash_api_key(plaintext)


def test_generate_api_key_material_format() -> None:
    plaintext, key_hash, key_prefix = generate_api_key_material()
    assert plaintext.startswith(API_KEY_PREFIX)
    assert key_hash == hash_api_key(plaintext)
    assert key_prefix.startswith(API_KEY_PREFIX)
    assert "..." in key_prefix


def test_is_valid_api_key_format() -> None:
    assert is_valid_api_key_format("sk-abc123")
    assert not is_valid_api_key_format("")
    assert not is_valid_api_key_format("sk-")
    assert not is_valid_api_key_format("Bearer sk-abc")


def test_parse_bearer_token() -> None:
    assert parse_bearer_token("Bearer sk-abc") == "sk-abc"
    assert parse_bearer_token("bearer sk-abc") == "sk-abc"
    assert parse_bearer_token("Token sk-abc") == ""
    assert parse_bearer_token(None) == ""
    assert parse_bearer_token("Bearer ") == ""
