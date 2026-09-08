"""组织内权限依赖：Owner / Admin / Member。"""

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Path
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_user_context
from app.core.constants import ORG_ROLE_ADMIN, ORG_ROLE_OWNER
from app.db.session import get_db
from app.schemas.auth import UserContext
from app.schemas.org import OrgAccessContext
from app.services.org_service import org_service


async def get_org_member_access(
    org_id: Annotated[UUID, Path(description="组织 ID")],
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> OrgAccessContext:
    """要求当前用户为该组织活跃成员（Member 及以上，只读场景）。"""
    org, membership = await org_service.require_active_membership(
        db, org_id=org_id, user_id=ctx.user_id
    )
    return OrgAccessContext(user=ctx.user, org=org, membership=membership)


async def get_org_admin_access(
    org_id: Annotated[UUID, Path(description="组织 ID")],
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> OrgAccessContext:
    """Admin 及以上：成员管理、额度操作（额度逻辑不在本任务）。"""
    org, membership = await org_service.require_min_role(
        db, org_id=org_id, user_id=ctx.user_id, min_role=ORG_ROLE_ADMIN
    )
    return OrgAccessContext(user=ctx.user, org=org, membership=membership)


async def get_org_owner_access(
    org_id: Annotated[UUID, Path(description="组织 ID")],
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> OrgAccessContext:
    """仅 Owner：资金与敏感组织设置。"""
    org, membership = await org_service.require_min_role(
        db, org_id=org_id, user_id=ctx.user_id, min_role=ORG_ROLE_OWNER
    )
    return OrgAccessContext(user=ctx.user, org=org, membership=membership)
