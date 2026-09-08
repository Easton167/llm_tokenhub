"""平台 API Key 生成与哈希、密码哈希、JWT 签发/校验。

两套凭证互不混用：
- 平台 API Key（``sk-...``）：SHA-256(pepper + plaintext) 入库，明文只在创建时返回一次。
  用于 /v1 网关。pepper 是全局盐，不是每 Key 一盐——为了能用哈希等值查找。
- 用户/管理员密码：bcrypt（自带盐）。
- 前台 JWT ``typ=user``、管理端 JWT ``typ=admin``，同一 ``jwt_secret`` 但 typ 隔离，
  防止用户 token 调管理接口（decode 时校验 typ）。
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import Any
from uuid import UUID

import bcrypt
import jwt

from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.constants import API_KEY_DISPLAY_PREFIX_LEN, API_KEY_PREFIX

JWT_ALGORITHM = "HS256"
# payload.typ：区分前台用户会话与管理端会话，decode 时必须匹配
JWT_SUBJECT_USER = "user"
JWT_SUBJECT_ADMIN = "admin"


def hash_api_key(plaintext: str, pepper: str | None = None) -> str:
    """对明文 Key 做 SHA-256(pepper + plaintext)，用于数据库查找。

    不用 bcrypt：鉴权是热路径，且必须能按哈希精确查询（bcrypt 每次 salt 不同无法等值查找）。
    pepper 来自环境变量，泄漏数据库不等于泄漏明文 Key。
    """
    salt = pepper if pepper is not None else settings.api_key_pepper
    payload = f"{salt}{plaintext}".encode()
    return hashlib.sha256(payload).hexdigest()


def generate_api_key_material() -> tuple[str, str, str]:
    """生成 API Key 明文、哈希与展示前缀。

    Returns:
        (plaintext, key_hash, key_prefix) — 明文仅应在创建时返回一次。
    """
    random_part = secrets.token_urlsafe(32)
    plaintext = f"{API_KEY_PREFIX}{random_part}"
    key_hash = hash_api_key(plaintext)
    if len(plaintext) <= API_KEY_DISPLAY_PREFIX_LEN:
        key_prefix = plaintext
    else:
        key_prefix = f"{plaintext[:API_KEY_DISPLAY_PREFIX_LEN]}..."
    return plaintext, key_hash, key_prefix


def parse_bearer_token(authorization: str | None) -> str:
    """从 Authorization 头解析 Bearer Token；格式错误时返回空字符串。"""
    if not authorization:
        return ""
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return ""
    return parts[1].strip()


def is_valid_api_key_format(raw_key: str) -> bool:
    """廉价格式预检：必须 ``sk-`` 前缀且后面还有内容。不通过则根本不查库。"""
    return bool(raw_key) and raw_key.startswith(API_KEY_PREFIX) and len(raw_key) > len(API_KEY_PREFIX)


def hash_password(password: str) -> str:
    """bcrypt 哈希密码。返回 ASCII 字符串，可直接入库。"""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """校验明文密码与哈希是否匹配。损坏的哈希一律视为不匹配，不抛给调用方。"""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_access_token(*, user_id: UUID, expires_minutes: int | None = None) -> str:
    """签发前台用户 access JWT。"""
    expire_delta = timedelta(
        minutes=expires_minutes if expires_minutes is not None else settings.jwt_expire_minutes
    )
    now = utc_now()
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "typ": JWT_SUBJECT_USER,
        "iat": now,
        "exp": now + expire_delta,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> UUID:
    """解析并校验前台 JWT，返回 user_id。

    失败抛 jwt.PyJWTError（过期、签名错、typ 不对、缺 sub）；
    由 ``get_user_context`` 转成 UnauthorizedError，避免泄露具体原因。
    """
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[JWT_ALGORITHM])
    if payload.get("typ") != JWT_SUBJECT_USER:
        raise jwt.InvalidTokenError("Invalid token type")
    sub = payload.get("sub")
    if not sub:
        raise jwt.InvalidTokenError("Missing subject")
    return UUID(str(sub))


def create_admin_token(*, username: str, expires_minutes: int | None = None) -> str:
    """签发管理端 access JWT（sub 为 username，不绑定前台 users 表）。"""
    expire_delta = timedelta(
        minutes=expires_minutes if expires_minutes is not None else settings.jwt_expire_minutes
    )
    now = utc_now()
    payload: dict[str, Any] = {
        "sub": username,
        "typ": JWT_SUBJECT_ADMIN,
        "iat": now,
        "exp": now + expire_delta,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=JWT_ALGORITHM)


def decode_admin_token(token: str) -> str:
    """解析并校验管理端 JWT，返回 username。"""
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[JWT_ALGORITHM])
    if payload.get("typ") != JWT_SUBJECT_ADMIN:
        raise jwt.InvalidTokenError("Invalid token type")
    sub = payload.get("sub")
    if not sub:
        raise jwt.InvalidTokenError("Missing subject")
    return str(sub)
