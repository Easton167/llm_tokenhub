"""支付渠道抽象：创建支付、验签回调、查询订单。"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CreatePaymentResult:
    """渠道侧创建支付结果。"""

    external_order_no: str
    pay_url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class CallbackVerificationResult:
    """验签后的回调解析结果。"""

    order_no: str
    external_order_no: str
    success: bool
    amount: int | None = None
    fail_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    # 验签通过但不改状态（如 Stripe 无关事件）；为 False 时不入账、不失败
    apply: bool = True


@dataclass
class QueryPaymentResult:
    """渠道侧查单结果。"""

    external_order_no: str
    # paid / pending / failed / expired
    status: str
    amount: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class PaymentProvider(abc.ABC):
    """支付渠道抽象接口。

    真实渠道（支付宝 / Stripe）在 Task 8 实现；本阶段仅 Mock。
    """

    name: str

    @abc.abstractmethod
    async def create_payment(
        self,
        *,
        order_no: str,
        amount: int,
        currency: str = "CNY",
        subject: str = "Wallet recharge",
        notify_url: str | None = None,
        return_url: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> CreatePaymentResult:
        """向渠道发起支付，返回外部单号与支付跳转信息。"""

    @abc.abstractmethod
    async def verify_callback(
        self,
        *,
        headers: dict[str, str],
        body: bytes | str | dict[str, Any],
    ) -> CallbackVerificationResult:
        """验签并解析回调；验签失败应抛出 InvalidRequestError。"""

    @abc.abstractmethod
    async def query_order(
        self,
        *,
        order_no: str,
        external_order_no: str | None = None,
    ) -> QueryPaymentResult:
        """向渠道查询订单支付状态。"""
