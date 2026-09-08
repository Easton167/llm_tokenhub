"""后台管理员的认证与权限装载。

环境变量兜底：当 ``admin_users`` 里不存在与 ``settings.admin_username`` 同名的记录时，
该用户名 + ``settings.admin_password`` 仍可登录并取得超管权限。这既保持了引入本表
之前的行为不变，也保证「删光账号 / 忘记密码」不会把自己彻底锁在门外。一旦建了同名
账号，兜底立即失效，改由数据库里的密码与角色说了算。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.constants import STATUS_ENABLED
from app.core.errors import UnauthorizedError
from app.core.security import verify_password
from app.db.models.admin import AdminRolePermission, AdminUser, AdminUserRole
from app.schemas.admin_rbac import AdminContext


async def _get_admin_user(db: AsyncSession, username: str) -> AdminUser | None:
    result = await db.execute(select(AdminUser).where(AdminUser.username == username))
    return result.scalar_one_or_none()


async def load_permissions(db: AsyncSession, admin_user: AdminUser) -> frozenset[str]:
    """汇总该账号所有角色被授予的权限码（并集）。"""
    result = await db.execute(
        select(AdminRolePermission.code)
        .join(AdminUserRole, AdminUserRole.role_id == AdminRolePermission.role_id)
        .where(AdminUserRole.admin_user_id == admin_user.id)
    )
    return frozenset(result.scalars().all())


def _env_fallback_context() -> AdminContext:
    return AdminContext(
        username=settings.admin_username,
        is_superadmin=True,
        permissions=frozenset(),
    )


async def load_context(db: AsyncSession, username: str) -> AdminContext:
    """按用户名装载管理端上下文；账号不存在或被禁用时抛 401。"""
    admin_user = await _get_admin_user(db, username)
    if admin_user is None:
        if username == settings.admin_username:
            return _env_fallback_context()
        raise UnauthorizedError("Admin account not found")
    if admin_user.status != STATUS_ENABLED:
        raise UnauthorizedError("Admin account is disabled")
    return AdminContext(
        username=admin_user.username,
        is_superadmin=admin_user.is_superadmin,
        permissions=await load_permissions(db, admin_user),
        admin_user=admin_user,
    )


async def authenticate(db: AsyncSession, username: str, password: str) -> AdminContext:
    """校验用户名密码，成功返回上下文；任何失败都抛同一个 401，不区分原因。"""
    admin_user = await _get_admin_user(db, username)
    if admin_user is None:
        if username == settings.admin_username and password == settings.admin_password:
            return _env_fallback_context()
        raise UnauthorizedError("Invalid username or password")
    if not verify_password(password, admin_user.password_hash):
        raise UnauthorizedError("Invalid username or password")
    if admin_user.status != STATUS_ENABLED:
        raise UnauthorizedError("Admin account is disabled")
    return AdminContext(
        username=admin_user.username,
        is_superadmin=admin_user.is_superadmin,
        permissions=await load_permissions(db, admin_user),
        admin_user=admin_user,
    )


async def count_active_superadmins(db: AsyncSession, *, exclude_id=None) -> int:
    """统计仍启用的超管数量，用于阻止删除/降级最后一个超管。"""
    stmt = select(func.count()).select_from(AdminUser).where(
        AdminUser.is_superadmin.is_(True), AdminUser.status == STATUS_ENABLED
    )
    if exclude_id is not None:
        stmt = stmt.where(AdminUser.id != exclude_id)
    result = await db.execute(stmt)
    return int(result.scalar_one())
