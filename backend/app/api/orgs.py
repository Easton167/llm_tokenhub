"""用户端组织与成员管理 API（JWT）。"""

from datetime import date, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_user_context
from app.api.org_deps import get_org_admin_access, get_org_member_access, get_org_owner_access
from app.core.errors import ResourceNotFoundError
from app.db.models.org import OrgMember
from app.db.models.wallet import Wallet, WalletTransaction
from app.db.session import get_db
from app.schemas.admin import WalletOut, WalletTransactionOut
from app.schemas.auth import UserContext
from app.schemas.org import (
    OrgAccessContext,
    OrgCreate,
    OrgDetailOut,
    OrgMemberInvite,
    OrgMemberOut,
    OrgMemberUpdate,
    OrgOut,
    OrgUpdate,
    OrgWithRoleOut,
    org_member_to_out,
)
from app.schemas.report import OrgDailyReportOut, OrgMemberShareReportOut
from app.services import report_service
from app.services.org_service import org_service
from app.services.quota_service import quota_service
from app.services.wallet_service import wallet_service

router = APIRouter(prefix="/orgs", tags=["orgs"])


async def _member_out(db: AsyncSession, member: OrgMember) -> OrgMemberOut:
    """刷新标量字段并显式带上额度，避免异步懒加载。"""
    await db.refresh(member)
    quota = await quota_service.ensure_fresh_quota(db, member.id)
    return org_member_to_out(member, quota=quota)


@router.post("", response_model=OrgOut, status_code=201)
async def create_org(
    body: OrgCreate,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> OrgOut:
    org = await org_service.create_org(
        db,
        name=body.name,
        owner_user_id=ctx.user_id,
        billing_fallback=body.billing_fallback,
    )
    return OrgOut.model_validate(org)


@router.get("", response_model=list[OrgWithRoleOut])
async def list_my_orgs(
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> list[OrgWithRoleOut]:
    rows = await org_service.list_my_orgs(db, ctx.user_id)
    return [
        OrgWithRoleOut(
            **OrgOut.model_validate(org).model_dump(),
            my_org_role=membership.org_role,  # type: ignore[arg-type]
        )
        for org, membership in rows
    ]


@router.get("/{org_id}", response_model=OrgDetailOut)
async def get_org(
    access: OrgAccessContext = Depends(get_org_member_access),
    db: AsyncSession = Depends(get_db),
) -> OrgDetailOut:
    members = await org_service.list_active_members(db, access.org_id)
    outs = [await _member_out(db, m) for m in members]
    base = OrgOut.model_validate(access.org)
    return OrgDetailOut(
        **base.model_dump(),
        members=outs,
    )


@router.patch("/{org_id}", response_model=OrgOut)
async def update_org(
    body: OrgUpdate,
    access: OrgAccessContext = Depends(get_org_owner_access),
    db: AsyncSession = Depends(get_db),
) -> OrgOut:
    """编辑组织信息：仅 Owner（资金/组织级设置）。"""
    org = await org_service.update_org(
        db,
        access.org,
        name=body.name,
        billing_fallback=body.billing_fallback,
    )
    return OrgOut.model_validate(org)


@router.get("/{org_id}/wallet", response_model=WalletOut)
async def get_org_wallet(
    access: OrgAccessContext = Depends(get_org_admin_access),
    db: AsyncSession = Depends(get_db),
) -> Wallet:
    """查询组织钱包余额：仅 Owner/Admin（Member 返回 403）。"""
    return await wallet_service.get_or_create_org_wallet(db, access.org_id)


@router.get("/{org_id}/wallet/transactions", response_model=list[WalletTransactionOut])
async def get_org_wallet_transactions(
    tx_type: str | None = Query(
        default=None, description="流水类型: hold/capture/release/recharge/adjust"
    ),
    ref_request_id: str | None = Query(default=None, description="关联请求 ID"),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    access: OrgAccessContext = Depends(get_org_admin_access),
    db: AsyncSession = Depends(get_db),
) -> list[WalletTransaction]:
    """查询组织钱包流水：仅 Owner/Admin（Member 返回 403）。"""
    wallet = await wallet_service.get_or_create_org_wallet(db, access.org_id)
    return await wallet_service.get_transactions(
        db,
        wallet_id=wallet.id,
        tx_type=tx_type,
        ref_request_id=ref_request_id,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
        offset=offset,
    )


@router.get("/{org_id}/reports/daily", response_model=OrgDailyReportOut)
async def get_org_daily_usage(
    from_date: date = Query(..., description="UTC 起始日 YYYY-MM-DD（含）"),
    to_date: date = Query(..., description="UTC 结束日 YYYY-MM-DD（含）"),
    model_id: UUID | None = Query(default=None, description="可选：按模型过滤"),
    access: OrgAccessContext = Depends(get_org_member_access),
    db: AsyncSession = Depends(get_db),
) -> OrgDailyReportOut:
    """组织按天用量汇总：活跃成员可见（仅本组织数据；UTC 日历日）。"""
    items = await report_service.org_daily_usage(
        db,
        org_id=access.org_id,
        from_date=from_date,
        to_date=to_date,
        model_id=model_id,
    )
    return OrgDailyReportOut(
        timezone="UTC",
        from_date=from_date,
        to_date=to_date,
        org_id=access.org_id,
        items=items,
    )


@router.get("/{org_id}/reports/members", response_model=OrgMemberShareReportOut)
async def get_org_member_share(
    from_date: date = Query(..., description="UTC 起始日 YYYY-MM-DD（含）"),
    to_date: date = Query(..., description="UTC 结束日 YYYY-MM-DD（含）"),
    model_id: UUID | None = Query(default=None, description="可选：按模型过滤"),
    access: OrgAccessContext = Depends(get_org_admin_access),
    db: AsyncSession = Depends(get_db),
) -> OrgMemberShareReportOut:
    """组织成员用量分摊：仅 Owner/Admin（Member 返回 403）。"""
    items = await report_service.org_member_share(
        db,
        org_id=access.org_id,
        from_date=from_date,
        to_date=to_date,
        model_id=model_id,
    )
    return OrgMemberShareReportOut(
        timezone="UTC",
        from_date=from_date,
        to_date=to_date,
        org_id=access.org_id,
        items=items,
    )


@router.get("/{org_id}/members", response_model=list[OrgMemberOut])
async def list_members(
    access: OrgAccessContext = Depends(get_org_member_access),
    db: AsyncSession = Depends(get_db),
) -> list[OrgMemberOut]:
    members = await org_service.list_active_members(db, access.org_id)
    return [await _member_out(db, m) for m in members]


@router.post("/{org_id}/members", response_model=OrgMemberOut, status_code=201)
async def invite_member(
    body: OrgMemberInvite,
    access: OrgAccessContext = Depends(get_org_admin_access),
    db: AsyncSession = Depends(get_db),
) -> OrgMemberOut:
    member = await org_service.invite_member(
        db,
        org=access.org,
        actor_user_id=access.user_id,
        user_id=body.user_id,
        org_role=body.org_role,
    )
    return await _member_out(db, member)


@router.patch("/{org_id}/members/{member_id}", response_model=OrgMemberOut)
async def update_member(
    member_id: UUID,
    body: OrgMemberUpdate,
    access: OrgAccessContext = Depends(get_org_admin_access),
    db: AsyncSession = Depends(get_db),
) -> OrgMemberOut:
    """更新成员角色、消费开关与额度上限（Owner/Admin）。"""
    member = None
    if body.org_role is not None:
        member = await org_service.update_member_role(
            db,
            org=access.org,
            actor_user_id=access.user_id,
            member_id=member_id,
            new_role=body.org_role,
            actor_role=access.org_role,
        )
    if (
        body.can_consume_org_wallet is not None
        or body.quota_period is not None
        or body.quota_limit is not None
    ):
        member = await quota_service.configure_member_quota(
            db,
            org=access.org,
            actor_user_id=access.user_id,
            member_id=member_id,
            can_consume_org_wallet=body.can_consume_org_wallet,
            quota_period=body.quota_period,
            quota_limit=body.quota_limit,
        )
    if member is None:
        member = await db.get(OrgMember, member_id)
        if member is None:
            raise ResourceNotFoundError("Organization member not found")
    return await _member_out(db, member)


@router.delete("/{org_id}/members/{member_id}", status_code=204)
async def remove_member(
    member_id: UUID,
    access: OrgAccessContext = Depends(get_org_admin_access),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await org_service.remove_member(
        db,
        org=access.org,
        actor_user_id=access.user_id,
        member_id=member_id,
    )
    return Response(status_code=204)
