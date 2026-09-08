"""支付单请求/响应模型。"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.constants import PAYMENT_PROVIDER_MOCK, PAYMENT_TARGET_PERSONAL
from app.core.datetime_utils import UtcDateTime


class PaymentSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class PaymentOrderCreate(BaseModel):
    amount: int = Field(..., ge=1, description="充值金额（最小货币单位）")
    target_type: str = Field(
        default=PAYMENT_TARGET_PERSONAL,
        description="personal=个人钱包；org=组织钱包（需 Owner）",
    )
    org_id: UUID | None = Field(
        default=None, description="target_type=org 时必填"
    )
    provider: str = Field(
        default=PAYMENT_PROVIDER_MOCK,
        max_length=32,
        description="支付渠道：mock / stripe",
    )
    return_url: str | None = Field(default=None, max_length=512)


class PaymentOrderOut(PaymentSchema):
    id: UUID
    order_no: str
    user_id: UUID
    target_wallet_id: UUID
    target_type: str
    org_id: UUID | None
    amount: int
    currency: str
    provider: str
    status: str
    external_order_no: str | None
    paid_at: UtcDateTime | None
    expired_at: UtcDateTime | None
    fail_reason: str | None
    created_at: UtcDateTime
    updated_at: UtcDateTime


class PaymentCreateResponse(BaseModel):
    """创建支付单响应：含渠道支付跳转信息。"""

    order: PaymentOrderOut
    pay_url: str | None = None
    external_order_no: str | None = None


class MockPaymentCallback(BaseModel):
    """Mock 渠道回调体（Task 7 测试用）。"""

    order_no: str = Field(..., min_length=1, max_length=64)
    external_order_no: str = Field(..., min_length=1, max_length=128)
    success: bool = True
    amount: int = Field(..., ge=1)
    sign: str = Field(..., min_length=1, description="HMAC-SHA256 签名")
    fail_reason: str | None = Field(default=None, max_length=512)
