"""管理端用量与请求日志查询 API；日聚合手动触发。"""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin.common import audit_write
from app.api.admin.deps import get_admin_actor_id
from app.db.session import get_db
from app.schemas.admin import (
    DailyAggregateOut,
    DailyAggregateRequest,
    RequestLogOut,
    UsageLogOut,
    UsageSummaryOut,
)
from app.services import usage_service
from app.services.usage_daily_service import aggregate_usage_daily
from app.services.usage_service import FUND_SOURCES

router = APIRouter(tags=["admin-usage"])


@router.get("/request-logs", response_model=list[RequestLogOut])
async def list_request_logs(
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    user_id: UUID | None = None,
    model_id: UUID | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[RequestLogOut]:
    """查询请求日志，可按时间、用户、模型、状态过滤。"""
    logs = await usage_service.list_request_logs(
        db,
        from_time=from_time,
        to_time=to_time,
        user_id=user_id,
        model_id=model_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return [RequestLogOut.model_validate(log) for log in logs]


@router.get("/usage-logs", response_model=list[UsageLogOut])
async def list_usage_logs(
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    user_id: UUID | None = None,
    model_id: UUID | None = None,
    source: str | None = Query(
        default=None, description="来源：chat / api / kb_ingest / kb_query"
    ),
    fund_source: str | None = Query(
        default=None, description="资金来源：gift / cash / mixed（两者都 > 0）"
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[UsageLogOut]:
    """查询用量日志（含关联的 user_id / model_id）。"""
    if fund_source is not None and fund_source not in FUND_SOURCES:
        fund_source = None
    rows = await usage_service.list_usage_logs(
        db,
        from_time=from_time,
        to_time=to_time,
        user_id=user_id,
        model_id=model_id,
        source=source,
        fund_source=fund_source,
        limit=limit,
        offset=offset,
    )
    out: list[UsageLogOut] = []
    for usage, req in rows:
        item = UsageLogOut.model_validate(usage)
        item.user_id = req.user_id
        item.model_id = req.model_id
        out.append(item)
    return out


@router.get("/usage-summary", response_model=list[UsageSummaryOut])
async def get_usage_summary(
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    user_id: UUID | None = None,
    group_by: str = Query(default="model", pattern="^(model|user)$"),
    db: AsyncSession = Depends(get_db),
) -> list[UsageSummaryOut]:
    """用量聚合：按模型或用户分组。"""
    rows = await usage_service.usage_summary(
        db,
        from_time=from_time,
        to_time=to_time,
        user_id=user_id,
        group_by=group_by,
    )
    return [UsageSummaryOut.model_validate(row) for row in rows]


@router.post("/usage/daily-aggregate", response_model=DailyAggregateOut)
async def trigger_daily_aggregate(
    body: DailyAggregateRequest,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID | None = Depends(get_admin_actor_id),
) -> DailyAggregateOut:
    """手动触发（或重跑）指定 UTC 日的用量日聚合；同一天幂等。"""
    result = await aggregate_usage_daily(
        db,
        summary_date=body.summary_date,
        dry_run=body.dry_run,
    )
    if not body.dry_run:
        await audit_write(
            db,
            action="usage.daily_aggregate",
            target=f"usage_daily_summary:{body.summary_date.isoformat()}",
            actor_user_id=actor_id,
            detail={
                "deleted": result.deleted,
                "inserted": result.inserted,
                "revenue_total": result.revenue_total,
                "cost_total": result.cost_total,
                "cost_missing_rows": result.cost_missing_rows,
            },
        )
    return DailyAggregateOut(
        summary_date=result.summary_date,
        deleted=result.deleted,
        inserted=result.inserted,
        revenue_total=result.revenue_total,
        cost_total=result.cost_total,
        cost_missing_rows=result.cost_missing_rows,
    )
