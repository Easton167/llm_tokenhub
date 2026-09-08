"""管理端运营报表 API（只读 usage_daily_summary）。

时区口径：from_date / to_date 为 UTC 日历日。
"""

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas.report import (
    ChannelShareReportOut,
    GiftDashboardOut,
    ReconcileReportOut,
    TopReportOut,
    TrendReportOut,
)
from app.services import report_service
from app.services.report_service import ReportFilters, TOP_DIMENSIONS

router = APIRouter(prefix="/reports", tags=["admin-reports"])


def _filters(
    from_date: date,
    to_date: date,
    org_id: UUID | None,
    user_id: UUID | None,
    model_id: UUID | None,
    channel_id: UUID | None,
) -> ReportFilters:
    return ReportFilters(
        from_date=from_date,
        to_date=to_date,
        org_id=org_id,
        user_id=user_id,
        model_id=model_id,
        channel_id=channel_id,
    )


@router.get("/trend", response_model=TrendReportOut)
async def report_trend(
    from_date: date = Query(..., description="UTC 起始日 YYYY-MM-DD（含）"),
    to_date: date = Query(..., description="UTC 结束日 YYYY-MM-DD（含）"),
    org_id: UUID | None = None,
    user_id: UUID | None = None,
    model_id: UUID | None = None,
    channel_id: UUID | None = None,
    db: AsyncSession = Depends(get_db),
) -> TrendReportOut:
    """收入/成本/毛利按天趋势。"""
    items = await report_service.trend_by_day(
        db,
        _filters(from_date, to_date, org_id, user_id, model_id, channel_id),
    )
    return TrendReportOut(
        timezone="UTC",
        from_date=from_date,
        to_date=to_date,
        items=items,
    )


@router.get("/top", response_model=TopReportOut)
async def report_top(
    dimension: str = Query(
        ...,
        pattern="^(user|org|model)$",
        description=f"排行维度：{', '.join(TOP_DIMENSIONS)}",
    ),
    from_date: date = Query(..., description="UTC 起始日 YYYY-MM-DD（含）"),
    to_date: date = Query(..., description="UTC 结束日 YYYY-MM-DD（含）"),
    limit: int = Query(default=10, ge=1, le=100),
    org_id: UUID | None = None,
    user_id: UUID | None = None,
    model_id: UUID | None = None,
    channel_id: UUID | None = None,
    db: AsyncSession = Depends(get_db),
) -> TopReportOut:
    """Top 用户 / 组织 / 模型排行（按收入降序）。"""
    items = await report_service.top_by_dimension(
        db,
        _filters(from_date, to_date, org_id, user_id, model_id, channel_id),
        dimension=dimension,
        limit=limit,
    )
    return TopReportOut(
        timezone="UTC",
        from_date=from_date,
        to_date=to_date,
        dimension=dimension,
        items=items,
    )


@router.get("/channel-share", response_model=ChannelShareReportOut)
async def report_channel_share(
    from_date: date = Query(..., description="UTC 起始日 YYYY-MM-DD（含）"),
    to_date: date = Query(..., description="UTC 结束日 YYYY-MM-DD（含）"),
    org_id: UUID | None = None,
    user_id: UUID | None = None,
    model_id: UUID | None = None,
    channel_id: UUID | None = None,
    db: AsyncSession = Depends(get_db),
) -> ChannelShareReportOut:
    """渠道成本占比。"""
    total_cost, items = await report_service.channel_cost_share(
        db,
        _filters(from_date, to_date, org_id, user_id, model_id, channel_id),
    )
    return ChannelShareReportOut(
        timezone="UTC",
        from_date=from_date,
        to_date=to_date,
        total_cost=total_cost,
        items=items,
    )


@router.get("/gifts", response_model=GiftDashboardOut)
async def report_gifts(
    from_date: date = Query(..., description="UTC 起始日 YYYY-MM-DD（含）"),
    to_date: date = Query(..., description="UTC 结束日 YYYY-MM-DD（含）"),
    db: AsyncSession = Depends(get_db),
) -> GiftDashboardOut:
    """赠金看板：敞口、发放/撤回/过期、消耗、上游成本与核销率。"""
    result = await report_service.gift_dashboard(
        db, from_date=from_date, to_date=to_date
    )
    return GiftDashboardOut(
        timezone="UTC",
        from_date=from_date,
        to_date=to_date,
        **result,
    )


@router.get("/reconcile", response_model=ReconcileReportOut)
async def report_reconcile(
    from_date: date = Query(..., description="UTC 起始日 YYYY-MM-DD（含）"),
    to_date: date = Query(..., description="UTC 结束日 YYYY-MM-DD（含）"),
    db: AsyncSession = Depends(get_db),
) -> ReconcileReportOut:
    """报表收入合计与钱包 capture 流水对账（含赠金拆分校验）。"""
    result = await report_service.reconcile_revenue(
        db, from_date=from_date, to_date=to_date
    )
    return ReconcileReportOut(
        timezone="UTC",
        from_date=from_date,
        to_date=to_date,
        **result,
    )
