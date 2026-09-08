"""后台账号、后台角色与权限目录的管理 API。"""

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.admin.common import audit_write, get_or_404, handle_integrity_error, list_all
from app.api.admin.deps import get_admin_actor_id, require_admin
from app.core.admin_permissions import ALL_PERMISSIONS, permission_catalog
from app.core.constants import STATUS_DISABLED
from app.core.errors import ForbiddenError, InvalidRequestError
from app.core.security import hash_password
from app.db.models.admin import AdminRole, AdminRolePermission, AdminUser, AdminUserRole
from app.db.session import get_db
from app.schemas.admin_rbac import (
    AdminAccountCreate,
    AdminAccountOut,
    AdminAccountUpdate,
    AdminContext,
    AdminPasswordReset,
    AdminRoleCreate,
    AdminRoleOut,
    AdminRoleUpdate,
    PermissionGroupOut,
    validate_data_scope,
)
from app.services import admin_rbac_service

router = APIRouter(prefix="/accounts", tags=["admin-accounts"])
roles_router = APIRouter(prefix="/admin-roles", tags=["admin-accounts"])
permissions_router = APIRouter(prefix="/permissions", tags=["admin-accounts"])


def _check_grantable(ctx: AdminContext, codes: list[str]) -> list[str]:
    """校验权限码合法，并禁止非超管授出自己没有的权限（防提权）。"""
    unknown = sorted(set(codes) - ALL_PERMISSIONS)
    if unknown:
        raise InvalidRequestError(f"Unknown permission codes: {', '.join(unknown)}")
    if not ctx.is_superadmin:
        excess = sorted(set(codes) - ctx.permissions)
        if excess:
            raise ForbiddenError(
                f"Cannot grant permissions you do not hold: {', '.join(excess)}"
            )
    return sorted(set(codes))


def _role_out(role: AdminRole) -> AdminRoleOut:
    return AdminRoleOut(
        id=role.id,
        code=role.code,
        name=role.name,
        description=role.description,
        data_scope=role.data_scope,
        permissions=sorted(p.code for p in role.permissions),
        created_at=role.created_at,
        updated_at=role.updated_at,
    )


def _account_out(admin_user: AdminUser) -> AdminAccountOut:
    return AdminAccountOut(
        id=admin_user.id,
        username=admin_user.username,
        display_name=admin_user.display_name,
        status=admin_user.status,
        is_superadmin=admin_user.is_superadmin,
        role_ids=[ur.role_id for ur in admin_user.user_roles],
        created_at=admin_user.created_at,
        updated_at=admin_user.updated_at,
    )


async def _get_role_with_permissions(db: AsyncSession, role_id: UUID) -> AdminRole:
    await get_or_404(db, AdminRole, role_id)
    result = await db.execute(
        select(AdminRole)
        .options(selectinload(AdminRole.permissions))
        .where(AdminRole.id == role_id)
    )
    return result.scalar_one()


async def _get_account_with_roles(db: AsyncSession, account_id: UUID) -> AdminUser:
    await get_or_404(db, AdminUser, account_id)
    result = await db.execute(
        select(AdminUser)
        .options(selectinload(AdminUser.user_roles))
        .where(AdminUser.id == account_id)
    )
    return result.scalar_one()


# --- 权限目录 ---


@permissions_router.get("", response_model=list[PermissionGroupOut])
async def list_permissions() -> list[dict]:
    """可授予的权限码目录，按资源分组；由代码常量派生，不落库。"""
    return permission_catalog()


# --- 后台角色 ---


@roles_router.post("", response_model=AdminRoleOut, status_code=201)
async def create_admin_role(
    body: AdminRoleCreate,
    db: AsyncSession = Depends(get_db),
    ctx: AdminContext = Depends(require_admin),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> AdminRoleOut:
    codes = _check_grantable(ctx, body.permissions)
    role = AdminRole(
        code=body.code,
        name=body.name,
        description=body.description,
        data_scope=validate_data_scope(body.data_scope),
    )
    role.permissions = [AdminRolePermission(code=c) for c in codes]
    db.add(role)
    try:
        await db.flush()
    except IntegrityError as exc:
        handle_integrity_error(exc, field="code")
    await audit_write(
        db,
        action="admin_role.create",
        target=f"admin_role:{role.id}",
        actor_user_id=actor_id,
        detail={"code": role.code, "permissions": codes, "by": ctx.username},
    )
    return _role_out(role)


@roles_router.get("", response_model=list[AdminRoleOut])
async def list_admin_roles(db: AsyncSession = Depends(get_db)) -> list[AdminRoleOut]:
    roles = await list_all(
        db,
        select(AdminRole).options(selectinload(AdminRole.permissions)).order_by(AdminRole.code),
    )
    return [_role_out(r) for r in roles]


@roles_router.patch("/{role_id}", response_model=AdminRoleOut)
async def update_admin_role(
    role_id: UUID,
    body: AdminRoleUpdate,
    db: AsyncSession = Depends(get_db),
    ctx: AdminContext = Depends(require_admin),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> AdminRoleOut:
    role = await _get_role_with_permissions(db, role_id)
    patch = body.model_dump(exclude_unset=True)
    codes = patch.pop("permissions", None)
    if "data_scope" in patch and patch["data_scope"] is not None:
        validate_data_scope(patch["data_scope"])
    for key, value in patch.items():
        if value is not None or key == "description":
            setattr(role, key, value)
    if codes is not None:
        granted = _check_grantable(ctx, codes)
        role.permissions = [AdminRolePermission(code=c) for c in granted]
    await db.flush()
    await audit_write(
        db,
        action="admin_role.update",
        target=f"admin_role:{role_id}",
        actor_user_id=actor_id,
        detail={**patch, "permissions": codes, "by": ctx.username},
    )
    return _role_out(await _get_role_with_permissions(db, role_id))


@roles_router.delete("/{role_id}", status_code=204)
async def delete_admin_role(
    role_id: UUID,
    db: AsyncSession = Depends(get_db),
    ctx: AdminContext = Depends(require_admin),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> None:
    role = await get_or_404(db, AdminRole, role_id)
    bindings = await list_all(
        db, select(AdminUserRole).where(AdminUserRole.role_id == role_id).limit(1)
    )
    if bindings:
        raise InvalidRequestError("Cannot delete role assigned to admin accounts")
    await db.delete(role)
    await audit_write(
        db,
        action="admin_role.delete",
        target=f"admin_role:{role_id}",
        actor_user_id=actor_id,
        detail={"by": ctx.username},
    )


# --- 后台账号 ---


async def _apply_roles(
    db: AsyncSession, admin_user: AdminUser, role_ids: list[UUID], ctx: AdminContext
) -> None:
    """整体覆盖账号的角色绑定。

    非超管只能分配权限不超出自身的角色，否则「有账号管理权」就等于「能给自己
    挂上任意角色」，_check_grantable 挡住的提权会从这里绕回来。
    """
    for role_id in role_ids:
        role = await _get_role_with_permissions(db, role_id)
        if not ctx.is_superadmin:
            excess = sorted({p.code for p in role.permissions} - ctx.permissions)
            if excess:
                raise ForbiddenError(
                    f"Cannot assign role '{role.code}' granting permissions "
                    f"you do not hold: {', '.join(excess)}"
                )
    admin_user.user_roles = [AdminUserRole(role_id=rid) for rid in dict.fromkeys(role_ids)]


@router.post("", response_model=AdminAccountOut, status_code=201)
async def create_account(
    body: AdminAccountCreate,
    db: AsyncSession = Depends(get_db),
    ctx: AdminContext = Depends(require_admin),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> AdminAccountOut:
    if body.is_superadmin and not ctx.is_superadmin:
        raise ForbiddenError("Only a superadmin can create superadmin accounts")
    admin_user = AdminUser(
        username=body.username,
        password_hash=hash_password(body.password),
        display_name=body.display_name,
        is_superadmin=body.is_superadmin,
    )
    await _apply_roles(db, admin_user, body.role_ids, ctx)
    db.add(admin_user)
    try:
        await db.flush()
    except IntegrityError as exc:
        handle_integrity_error(exc, field="username")
    await audit_write(
        db,
        action="admin_account.create",
        target=f"admin_account:{admin_user.id}",
        actor_user_id=actor_id,
        detail={"username": body.username, "by": ctx.username},
    )
    return _account_out(admin_user)


@router.get("", response_model=list[AdminAccountOut])
async def list_accounts(db: AsyncSession = Depends(get_db)) -> list[AdminAccountOut]:
    accounts = await list_all(
        db,
        select(AdminUser)
        .options(selectinload(AdminUser.user_roles))
        .order_by(AdminUser.created_at.desc()),
    )
    return [_account_out(a) for a in accounts]


@router.patch("/{account_id}", response_model=AdminAccountOut)
async def update_account(
    account_id: UUID,
    body: AdminAccountUpdate,
    db: AsyncSession = Depends(get_db),
    ctx: AdminContext = Depends(require_admin),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> AdminAccountOut:
    admin_user = await _get_account_with_roles(db, account_id)
    patch = body.model_dump(exclude_unset=True)
    is_self = admin_user.id == ctx.admin_user_id

    if "is_superadmin" in patch and not ctx.is_superadmin:
        raise ForbiddenError("Only a superadmin can change the superadmin flag")
    if is_self and patch.get("status") == STATUS_DISABLED:
        raise InvalidRequestError("Cannot disable your own account")
    # 降级/停用最后一个超管会让后台永久失去超管；env 兜底只在同名账号不存在时生效
    losing_superadmin = admin_user.is_superadmin and (
        patch.get("is_superadmin") is False or patch.get("status") == STATUS_DISABLED
    )
    if losing_superadmin:
        remaining = await admin_rbac_service.count_active_superadmins(
            db, exclude_id=admin_user.id
        )
        if remaining == 0:
            raise InvalidRequestError("Cannot remove the last active superadmin")

    role_ids = patch.pop("role_ids", None)
    for key, value in patch.items():
        if value is not None or key == "display_name":
            setattr(admin_user, key, value)
    if role_ids is not None:
        await _apply_roles(db, admin_user, role_ids, ctx)
    await db.flush()
    await audit_write(
        db,
        action="admin_account.update",
        target=f"admin_account:{account_id}",
        actor_user_id=actor_id,
        detail={**patch, "role_ids": [str(r) for r in role_ids or []], "by": ctx.username},
    )
    return _account_out(await _get_account_with_roles(db, account_id))


@router.put("/{account_id}/password", status_code=204)
async def reset_account_password(
    account_id: UUID,
    body: AdminPasswordReset,
    db: AsyncSession = Depends(get_db),
    ctx: AdminContext = Depends(require_admin),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> None:
    admin_user = await get_or_404(db, AdminUser, account_id)
    if admin_user.is_superadmin and not ctx.is_superadmin:
        raise ForbiddenError("Only a superadmin can reset a superadmin password")
    admin_user.password_hash = hash_password(body.password)
    await audit_write(
        db,
        action="admin_account.password_reset",
        target=f"admin_account:{account_id}",
        actor_user_id=actor_id,
        detail={"by": ctx.username},
    )


@router.delete("/{account_id}", status_code=204)
async def delete_account(
    account_id: UUID,
    db: AsyncSession = Depends(get_db),
    ctx: AdminContext = Depends(require_admin),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> None:
    admin_user = await get_or_404(db, AdminUser, account_id)
    if admin_user.id == ctx.admin_user_id:
        raise InvalidRequestError("Cannot delete your own account")
    if admin_user.is_superadmin:
        if not ctx.is_superadmin:
            raise ForbiddenError("Only a superadmin can delete a superadmin account")
        remaining = await admin_rbac_service.count_active_superadmins(
            db, exclude_id=admin_user.id
        )
        if remaining == 0:
            raise InvalidRequestError("Cannot remove the last active superadmin")
    await db.delete(admin_user)
    await audit_write(
        db,
        action="admin_account.delete",
        target=f"admin_account:{account_id}",
        actor_user_id=actor_id,
        detail={"username": admin_user.username, "by": ctx.username},
    )
