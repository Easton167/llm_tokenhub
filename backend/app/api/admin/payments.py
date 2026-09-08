"""后台支付单查询 API。"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas.payment import PaymentOrderOut
from app.services.payment_service import payment_service

router = APIRouter(prefix="/payments", tags=["admin-payments"])


@router.get("", response_model=list[PaymentOrderOut])
async def list_payment_orders(
    user_id: UUID | None = Query(default=None),
    org_id: UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    provider: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[PaymentOrderOut]:
    """管理端按用户/组织/状态筛选支付单。"""
    orders = await payment_service.list_orders(
        db,
        user_id=user_id,
        org_id=org_id,
        status=status,
        provider=provider,
        limit=limit,
        offset=offset,
    )
    return [PaymentOrderOut.model_validate(o) for o in orders]


@router.get("/{order_no}", response_model=PaymentOrderOut)
async def get_payment_order(
    order_no: str,
    db: AsyncSession = Depends(get_db),
) -> PaymentOrderOut:
    """管理端查看支付单详情（含回调原文不在 Out 中，避免过大；可按需扩展）。"""
    order = await payment_service.require_order_by_no(db, order_no)
    return PaymentOrderOut.model_validate(order)
