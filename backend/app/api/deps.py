"""FastAPI 依赖：API Key 鉴权（/v1）与 JWT 用户会话（用户端 REST）。

两条鉴权链路互不混用：
- ``get_auth_context``：Authorization: Bearer sk-... → AuthContext（网关 /v1）
- ``get_user_context``：Authorization: Bearer JWT → UserContext（前台 REST）
- ``get_optional_auth_context``：没有 Bearer 时返回 None，不抛 401（公开+可选鉴权）

``get_db`` 与鉴权共用同一请求会话：鉴权里 load 的 User/ApiKey 在后续 Service 里可继续用。
管理端鉴权不在本模块，见 ``api/admin/deps.py``。
"""

from typing import Annotated
from uuid import UUID

import jwt
from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidApiKeyError, UnauthorizedError
from app.core.security import decode_access_token, parse_bearer_token
from app.db.session import get_db
from app.schemas.auth import AuthContext, ModelAccess, UserContext
from app.services.auth_service import auth_service
from app.services.permission_service import permission_service
from app.services.user_service import user_service


async def get_auth_context(
    authorization: Annotated[str | None, Header()] = None,
    db: AsyncSession = Depends(get_db),
) -> AuthContext:
    """解析平台 API Key。格式错误与哈希未命中都映射为 InvalidApiKeyError（401），不区分原因。"""
    raw_key = parse_bearer_token(authorization)
    if not raw_key:
        raise InvalidApiKeyError()
    return await auth_service.verify_api_key(db, raw_key)


async def get_optional_auth_context(
    authorization: Annotated[str | None, Header()] = None,
    db: AsyncSession = Depends(get_db),
) -> AuthContext | None:
    """可选 API Key：没带 Authorization 视为匿名；带了则必须合法。"""
    raw_key = parse_bearer_token(authorization)
    if not raw_key:
        return None
    return await auth_service.verify_api_key(db, raw_key)


async def get_user_context(
    authorization: Annotated[str | None, Header()] = None,
    db: AsyncSession = Depends(get_db),
) -> UserContext:
    """解析 Bearer JWT，返回前台用户上下文。过期/签名错误统一 401。"""
    token = parse_bearer_token(authorization)
    if not token:
        raise UnauthorizedError("Missing or invalid authorization")
    try:
        user_id: UUID = decode_access_token(token)
    except jwt.PyJWTError:
        raise UnauthorizedError("Invalid or expired token") from None
    user = await user_service.require_active_user(db, user_id)
    return UserContext(user=user)


def require_model_access(model_alias: str):
    """工厂依赖：在鉴权通过后校验指定模型别名权限（含 Key 模型范围）。

    chat 接口的模型来自请求体，不能用这个静态工厂，而是在 handler 里调
    ``permission_service.check_model_access``。
    """

    async def _dependency(
        auth: AuthContext = Depends(get_auth_context),
        db: AsyncSession = Depends(get_db),
    ) -> ModelAccess:
        return await permission_service.check_model_access(
            db,
            user_id=auth.user_id,
            model_alias=model_alias,
            api_key=auth.api_key,
        )

    return _dependency
