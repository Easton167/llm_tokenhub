"""用户端支付单 API：创建/查询；Mock / Stripe 回调。"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_user_context
from app.core.constants import PAYMENT_PROVIDER_MOCK, PAYMENT_PROVIDER_STRIPE
from app.core.errors import ForbiddenError
from app.db.session import get_db
from app.schemas.auth import UserContext
from app.schemas.payment import (
    MockPaymentCallback,
    PaymentCreateResponse,
    PaymentOrderCreate,
    PaymentOrderOut,
)
from app.services.payment_service import payment_service

router = APIRouter(prefix="/payments", tags=["payments"])


@router.post("", response_model=PaymentCreateResponse, status_code=201)
async def create_payment_order(
    body: PaymentOrderCreate,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> PaymentCreateResponse:
    """创建支付单（个人钱包或组织钱包；组织需 Owner）。"""
    order, create_result = await payment_service.create_order(
        db,
        user_id=ctx.user_id,
        amount=body.amount,
        target_type=body.target_type,
        org_id=body.org_id,
        provider_name=body.provider,
        return_url=body.return_url,
    )
    return PaymentCreateResponse(
        order=PaymentOrderOut.model_validate(order),
        pay_url=create_result.pay_url,
        external_order_no=create_result.external_order_no,
    )


@router.get("", response_model=list[PaymentOrderOut])
async def list_my_payment_orders(
    status: str | None = Query(default=None),
    org_id: UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> list[PaymentOrderOut]:
    """查询当前用户发起的支付单。"""
    orders = await payment_service.list_orders(
        db,
        user_id=ctx.user_id,
        org_id=org_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return [PaymentOrderOut.model_validate(o) for o in orders]


@router.post(
    "/callbacks/mock",
    response_model=PaymentOrderOut,
    summary="Mock 支付回调（仅 mock 渠道）",
)
async def mock_payment_callback(
    body: MockPaymentCallback,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> PaymentOrderOut:
    """Mock 渠道回调入口：验签 → 状态机 → 幂等入账。"""
    headers = {k.lower(): v for k, v in request.headers.items()}
    payload: dict[str, Any] = body.model_dump(exclude_none=True)
    order = await payment_service.handle_callback(
        db,
        provider_name=PAYMENT_PROVIDER_MOCK,
        headers=headers,
        body=payload,
    )
    assert order is not None
    return PaymentOrderOut.model_validate(order)


@router.post(
    "/callbacks/stripe",
    summary="Stripe Webhook 回调",
    response_class=PlainTextResponse,
)
async def stripe_payment_callback(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> PlainTextResponse:
    """Stripe webhook：验签失败返回 4xx 且不入账；通过后交给 PaymentService。

    响应体为纯文本 `ok`，符合 Stripe 重试约定。
    """
    raw_body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    await payment_service.handle_callback(
        db,
        provider_name=PAYMENT_PROVIDER_STRIPE,
        headers=headers,
        body=raw_body,
    )
    return PlainTextResponse("ok")


@router.get("/{order_no}", response_model=PaymentOrderOut)
async def get_my_payment_order(
    order_no: str,
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> PaymentOrderOut:
    """查询本人支付单详情。"""
    order = await payment_service.require_order_by_no(db, order_no)
    if order.user_id != ctx.user_id:
        raise ForbiddenError("Not allowed to view this payment order")
    return PaymentOrderOut.model_validate(order)
