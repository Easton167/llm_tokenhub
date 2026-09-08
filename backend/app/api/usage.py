"""用户端用量查询 API（JWT）。"""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_user_context
from app.core.constants import USAGE_SOURCES
from app.db.session import get_db
from app.schemas.admin import UsageLogOut
from app.schemas.auth import UserContext
from app.schemas.report import UserDailyReportOut
from app.services import report_service, usage_service
from app.services.usage_service import FUND_SOURCES

router = APIRouter(tags=["usage"])

UsageSourceParam = Literal["chat", "api", "kb_ingest", "kb_query"]
FundSourceParam = Literal["gift", "cash", "mixed"]


@router.get("/usage", response_model=list[UsageLogOut])
async def get_my_usage(
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    model: str | None = Query(default=None, description="模型别名，如 gpt-4o"),
    source: UsageSourceParam | None = Query(
        default=None,
        description="来源：chat / api / kb_ingest / kb_query",
    ),
    fund_source: FundSourceParam | None = Query(
        default=None,
        description="资金来源：gift / cash / mixed（两者都 > 0）",
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> list[UsageLogOut]:
    """查询当前用户的用量明细，可按时间、模型别名、来源与资金来源过滤。"""
    if source is not None and source not in USAGE_SOURCES:
        source = None
    if fund_source is not None and fund_source not in FUND_SOURCES:
        fund_source = None
    rows = await usage_service.list_usage_logs(
        db,
        from_time=from_time,
        to_time=to_time,
        user_id=ctx.user_id,
        model_alias=model,
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


@router.get("/usage/daily", response_model=UserDailyReportOut)
async def get_my_daily_usage(
    from_date: date = Query(..., description="UTC 起始日 YYYY-MM-DD（含）"),
    to_date: date = Query(..., description="UTC 结束日 YYYY-MM-DD（含）"),
    model_id: UUID | None = Query(default=None, description="可选：按模型过滤"),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> UserDailyReportOut:
    """个人按天用量汇总（读 usage_daily_summary；含 UTC 当日时先重算当日）。"""
    items = await report_service.user_daily_usage(
        db,
        user_id=ctx.user_id,
        from_date=from_date,
        to_date=to_date,
        model_id=model_id,
    )
    return UserDailyReportOut(
        timezone="UTC",
        from_date=from_date,
        to_date=to_date,
        items=items,
    )
