"""管理端鉴权：校验 admin JWT，并按「URL + 方法」推导所需权限码。

权限不在每个路由上单独声明。``enforce_admin_permission`` 挂在 ``/admin`` 路由树上，
由 ``core.admin_permissions.resolve_required_permission`` 从请求方法与路径反查权限码，
新增管理路由无需改这里。路径未登记时解析结果为 None，一律只放行超管（fail-closed），
这样漏配的后果是「暂时用不了」而不是「谁都能用」。
"""

from typing import Annotated
from uuid import UUID

import jwt
from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_permissions import AUTH_EXEMPT_PREFIX, resolve_required_permission
from app.core.errors import ForbiddenError, UnauthorizedError
from app.core.security import decode_admin_token, parse_bearer_token
from app.db.session import get_db
from app.schemas.admin_rbac import AdminContext
from app.services import admin_rbac_service


async def require_admin(
    authorization: Annotated[str | None, Header()] = None,
    db: AsyncSession = Depends(get_db),
) -> AdminContext:
    """要求 Authorization: Bearer <admin_jwt>，返回管理端上下文。"""
    token = parse_bearer_token(authorization)
    if not token:
        raise UnauthorizedError("Missing or invalid authorization")
    try:
        username = decode_admin_token(token)
    except jwt.PyJWTError:
        raise UnauthorizedError("Invalid or expired admin token") from None
    return await admin_rbac_service.load_context(db, username)


async def enforce_admin_permission(
    request: Request,
    ctx: AdminContext = Depends(require_admin),
) -> AdminContext:
    """校验当前管理员是否具备访问该请求所需的权限码。"""
    if request.url.path.startswith(AUTH_EXEMPT_PREFIX):
        return ctx
    if ctx.is_superadmin:
        return ctx
    required = resolve_required_permission(request.method, request.url.path)
    if required is None or required not in ctx.permissions:
        raise ForbiddenError("You do not have permission to perform this action")
    return ctx


async def get_admin_actor_id(
    _ctx: AdminContext = Depends(require_admin),
) -> UUID | None:
    """管理写操作审计用 actor。

    仍返回 None：``audit_logs.actor_user_id`` 外键指向前台 ``users``，
    装不下 ``admin_users`` 的主键。待审计表解耦后再回填真实管理员 id。
    """
    return None
