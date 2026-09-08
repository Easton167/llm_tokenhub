"""Role 与模型授权、用户角色分配 API。"""

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin.common import audit_write, get_or_404, handle_integrity_error, list_all
from app.api.admin.deps import get_admin_actor_id
from app.core.security import hash_password
from app.db.models.llm_model import LLMModel
from app.db.models.user import Role, RoleModelGrant, User, UserRole
from app.db.session import get_db
from app.schemas.admin import (
    RoleCreate,
    RoleModelGrantCreate,
    RoleModelGrantOut,
    RoleOut,
    RoleUpdate,
    UserOut,
    UserPasswordReset,
    UserRoleAssign,
    UserRoleOut,
)

router = APIRouter(prefix="/roles", tags=["admin-roles"])
users_router = APIRouter(prefix="/users", tags=["admin-users"])


@router.post("", response_model=RoleOut, status_code=201)
async def create_role(
    body: RoleCreate,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> Role:
    role = Role(**body.model_dump())
    db.add(role)
    try:
        await db.flush()
    except IntegrityError as exc:
        handle_integrity_error(exc, field="name")
    await audit_write(
        db,
        action="role.create",
        target=f"role:{role.id}",
        actor_user_id=actor_id,
        detail=body.model_dump(),
    )
    return role


@router.get("", response_model=list[RoleOut])
async def list_roles(db: AsyncSession = Depends(get_db)) -> list[Role]:
    return await list_all(db, select(Role).order_by(Role.name))


@router.get("/{role_id}", response_model=RoleOut)
async def get_role(role_id: UUID, db: AsyncSession = Depends(get_db)) -> Role:
    return await get_or_404(db, Role, role_id)


@router.patch("/{role_id}", response_model=RoleOut)
async def update_role(
    role_id: UUID,
    body: RoleUpdate,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> Role:
    role = await get_or_404(db, Role, role_id)
    patch = body.model_dump(exclude_unset=True)
    quota_keys = {
        "kb_max_kb_count",
        "kb_max_docs_per_kb",
        "kb_max_storage_bytes",
        "kb_max_file_size_bytes",
    }
    quota_before = {k: getattr(role, k) for k in quota_keys}
    for key, value in patch.items():
        setattr(role, key, value)
    try:
        await db.flush()
    except IntegrityError as exc:
        handle_integrity_error(exc, field="name")
    await audit_write(
        db,
        action="role.update",
        target=f"role:{role.id}",
        actor_user_id=actor_id,
        detail=patch,
    )
    quota_changed = {
        k: {"from": quota_before[k], "to": patch[k]}
        for k in quota_keys
        if k in patch and patch[k] != quota_before[k]
    }
    if quota_changed:
        await audit_write(
            db,
            action="role.quota_update",
            target=f"role:{role.id}",
            actor_user_id=actor_id,
            detail=quota_changed,
        )
    await db.refresh(role)
    return role


@router.delete("/{role_id}", status_code=204)
async def delete_role(
    role_id: UUID,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> None:
    from app.core.errors import InvalidRequestError

    role = await get_or_404(db, Role, role_id)
    bindings = await list_all(
        db, select(UserRole).where(UserRole.role_id == role_id).limit(1)
    )
    if bindings:
        raise InvalidRequestError("Cannot delete role assigned to users")
    grants = await list_all(
        db, select(RoleModelGrant).where(RoleModelGrant.role_id == role_id)
    )
    for grant in grants:
        await db.delete(grant)
    await db.delete(role)
    await audit_write(
        db,
        action="role.delete",
        target=f"role:{role_id}",
        actor_user_id=actor_id,
    )


@router.post("/{role_id}/models", response_model=RoleModelGrantOut, status_code=201)
async def grant_model_to_role(
    role_id: UUID,
    body: RoleModelGrantCreate,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> RoleModelGrant:
    await get_or_404(db, Role, role_id)
    await get_or_404(db, LLMModel, body.model_id)
    grant = RoleModelGrant(role_id=role_id, model_id=body.model_id)
    db.add(grant)
    try:
        await db.flush()
    except IntegrityError as exc:
        handle_integrity_error(exc, field="grant")
    await audit_write(
        db,
        action="role_model_grant.create",
        target=f"role:{role_id}:model:{body.model_id}",
        actor_user_id=actor_id,
    )
    return grant


@router.get("/{role_id}/models", response_model=list[RoleModelGrantOut])
async def list_role_model_grants(
    role_id: UUID, db: AsyncSession = Depends(get_db)
) -> list[RoleModelGrant]:
    await get_or_404(db, Role, role_id)
    return await list_all(
        db,
        select(RoleModelGrant)
        .where(RoleModelGrant.role_id == role_id)
        .order_by(RoleModelGrant.created_at),
    )


@router.delete("/{role_id}/models/{model_id}", status_code=204)
async def revoke_model_from_role(
    role_id: UUID,
    model_id: UUID,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> None:
    result = await db.execute(
        select(RoleModelGrant).where(
            RoleModelGrant.role_id == role_id,
            RoleModelGrant.model_id == model_id,
        )
    )
    grant = result.scalar_one_or_none()
    if grant is None:
        from app.core.errors import ResourceNotFoundError

        raise ResourceNotFoundError("role_model_grant not found")
    await db.delete(grant)
    await audit_write(
        db,
        action="role_model_grant.delete",
        target=f"role:{role_id}:model:{model_id}",
        actor_user_id=actor_id,
    )


@users_router.get("", response_model=list[UserOut])
async def list_users(db: AsyncSession = Depends(get_db)) -> list[User]:
    """列出所有用户（管理后台）。"""
    return await list_all(db, select(User).order_by(User.created_at.desc()))


@users_router.put("/{user_id}/password", status_code=204)
async def reset_user_password(
    user_id: UUID,
    body: UserPasswordReset,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> None:
    """管理员重置用户密码（不校验原密码）。"""
    user = await get_or_404(db, User, user_id)
    user.password_hash = hash_password(body.password)
    await audit_write(
        db,
        action="user.password_reset",
        target=f"user:{user_id}",
        actor_user_id=actor_id,
    )


@users_router.post("/{user_id}/roles", response_model=UserRoleOut, status_code=201)
async def assign_role_to_user(
    user_id: UUID,
    body: UserRoleAssign,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> UserRole:
    await get_or_404(db, User, user_id)
    await get_or_404(db, Role, body.role_id)
    binding = UserRole(user_id=user_id, role_id=body.role_id)
    db.add(binding)
    try:
        await db.flush()
    except IntegrityError as exc:
        handle_integrity_error(exc, field="user_role")
    await audit_write(
        db,
        action="user_role.assign",
        target=f"user:{user_id}:role:{body.role_id}",
        actor_user_id=actor_id,
    )
    return binding


@users_router.get("/{user_id}/roles", response_model=list[UserRoleOut])
async def list_user_roles(user_id: UUID, db: AsyncSession = Depends(get_db)) -> list[UserRole]:
    await get_or_404(db, User, user_id)
    return await list_all(
        db,
        select(UserRole).where(UserRole.user_id == user_id).order_by(UserRole.created_at),
    )


@users_router.delete("/{user_id}/roles/{role_id}", status_code=204)
async def remove_role_from_user(
    user_id: UUID,
    role_id: UUID,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> None:
    result = await db.execute(
        select(UserRole).where(UserRole.user_id == user_id, UserRole.role_id == role_id)
    )
    binding = result.scalar_one_or_none()
    if binding is None:
        from app.core.errors import ResourceNotFoundError

        raise ResourceNotFoundError("user_role not found")
    await db.delete(binding)
    await audit_write(
        db,
        action="user_role.remove",
        target=f"user:{user_id}:role:{role_id}",
        actor_user_id=actor_id,
    )
