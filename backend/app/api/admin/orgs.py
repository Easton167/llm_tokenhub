"""管理端组织列表、详情与停用。"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin.common import audit_write, get_or_404
from app.api.admin.deps import get_admin_actor_id
from app.db.models.org import Org
from app.db.session import get_db
from app.schemas.org import (
    AdminOrgDeactivate,
    OrgDetailOut,
    OrgMemberOut,
    OrgOut,
    org_member_to_out,
)
from app.services.org_service import org_service
from app.services.quota_service import quota_service


router = APIRouter(prefix="/orgs", tags=["admin-orgs"])


async def _member_out(db: AsyncSession, member) -> OrgMemberOut:
    await db.refresh(member)
    quota = await quota_service.ensure_fresh_quota(db, member.id)
    return org_member_to_out(member, quota=quota)


@router.get("", response_model=list[OrgOut])
async def list_orgs(
    status: int | None = Query(default=None, description="0=正常 1=停用"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[OrgOut]:
    orgs = await org_service.list_orgs(
        db, status=status, limit=limit, offset=offset
    )
    return [OrgOut.model_validate(o) for o in orgs]


@router.get("/{org_id}", response_model=OrgDetailOut)
async def get_org(
    org_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> OrgDetailOut:
    org = await get_or_404(db, Org, org_id)
    members = await org_service.list_active_members(db, org.id)
    outs = [await _member_out(db, m) for m in members]
    base = OrgOut.model_validate(org)
    return OrgDetailOut(
        **base.model_dump(),
        members=outs,
    )


@router.post("/{org_id}/deactivate", response_model=OrgOut)
async def deactivate_org(
    org_id: UUID,
    body: AdminOrgDeactivate = AdminOrgDeactivate(),
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> OrgOut:
    org = await get_or_404(db, Org, org_id)
    org = await org_service.deactivate_org(db, org)
    detail = {"reason": body.reason} if body.reason else None
    await audit_write(
        db,
        action="org.deactivate",
        target=f"org:{org.id}",
        actor_user_id=actor_id,
        detail=detail,
    )
    return OrgOut.model_validate(org)
