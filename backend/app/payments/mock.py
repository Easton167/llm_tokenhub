"""Mock 支付渠道：本地/测试用，不接真实第三方。"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any
from uuid import uuid4

from app.core.config import settings
from app.core.constants import PAYMENT_PROVIDER_MOCK
from app.core.errors import InvalidRequestError
from app.payments.base import (
    CallbackVerificationResult,
    CreatePaymentResult,
    PaymentProvider,
    QueryPaymentResult,
)


def mock_sign_payload(
    *,
    order_no: str,
    external_order_no: str,
    success: bool,
    amount: int,
    secret: str | None = None,
) -> str:
    """生成 Mock 回调签名（HMAC-SHA256）。"""
    key = (secret or settings.mock_payment_secret).encode("utf-8")
    message = f"{order_no}|{external_order_no}|{int(success)}|{amount}".encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


class MockPaymentProvider(PaymentProvider):
    """内存/确定性 Mock：create 立即返回外部单号；回调靠 HMAC 验签。"""

    name = PAYMENT_PROVIDER_MOCK

    def __init__(self, secret: str | None = None) -> None:
        self._secret = secret or settings.mock_payment_secret
        # order_no -> 最近一次 create 的外部状态（供 query_order）
        self._orders: dict[str, QueryPaymentResult] = {}

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
        external = f"mock_{uuid4().hex[:16]}"
        result = CreatePaymentResult(
            external_order_no=external,
            pay_url=f"mock://pay/{order_no}",
            raw={
                "order_no": order_no,
                "amount": amount,
                "currency": currency,
                "subject": subject,
                "notify_url": notify_url,
                "return_url": return_url,
                **(extra or {}),
            },
        )
        self._orders[order_no] = QueryPaymentResult(
            external_order_no=external,
            status="pending",
            amount=amount,
            raw=result.raw,
        )
        return result

    async def verify_callback(
        self,
        *,
        headers: dict[str, str],
        body: bytes | str | dict[str, Any],
    ) -> CallbackVerificationResult:
        payload = self._parse_body(body)
        order_no = str(payload.get("order_no") or "")
        external_order_no = str(payload.get("external_order_no") or "")
        success = bool(payload.get("success", False))
        amount_raw = payload.get("amount")
        amount = int(amount_raw) if amount_raw is not None else None
        sign = str(payload.get("sign") or headers.get("x-mock-sign") or "")

        if not order_no or not external_order_no or amount is None:
            raise InvalidRequestError("Mock callback missing required fields")

        expected = mock_sign_payload(
            order_no=order_no,
            external_order_no=external_order_no,
            success=success,
            amount=amount,
            secret=self._secret,
        )
        if not hmac.compare_digest(sign, expected):
            raise InvalidRequestError("Mock callback signature invalid")

        if success:
            self._orders[order_no] = QueryPaymentResult(
                external_order_no=external_order_no,
                status="paid",
                amount=amount,
                raw=payload,
            )
        else:
            self._orders[order_no] = QueryPaymentResult(
                external_order_no=external_order_no,
                status="failed",
                amount=amount,
                raw=payload,
            )

        return CallbackVerificationResult(
            order_no=order_no,
            external_order_no=external_order_no,
            success=success,
            amount=amount,
            fail_reason=None if success else str(payload.get("fail_reason") or "mock_failed"),
            raw=payload,
        )

    async def query_order(
        self,
        *,
        order_no: str,
        external_order_no: str | None = None,
    ) -> QueryPaymentResult:
        existing = self._orders.get(order_no)
        if existing is None:
            return QueryPaymentResult(
                external_order_no=external_order_no or "",
                status="pending",
                amount=None,
                raw={},
            )
        return existing

    def set_query_status(
        self,
        order_no: str,
        *,
        status: str,
        external_order_no: str | None = None,
        amount: int | None = None,
    ) -> None:
        """测试辅助：模拟渠道侧状态（用于掉单对账，无需走回调）。"""
        prev = self._orders.get(order_no)
        self._orders[order_no] = QueryPaymentResult(
            external_order_no=external_order_no
            or (prev.external_order_no if prev else "")
            or f"mock_{order_no}",
            status=status,
            amount=amount if amount is not None else (prev.amount if prev else None),
            raw={"source": "set_query_status", "status": status},
        )

    @staticmethod
    def _parse_body(body: bytes | str | dict[str, Any]) -> dict[str, Any]:
        if isinstance(body, dict):
            return body
        if isinstance(body, bytes):
            text = body.decode("utf-8")
        else:
            text = body
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise InvalidRequestError("Mock callback body is not valid JSON") from exc
        if not isinstance(data, dict):
            raise InvalidRequestError("Mock callback body must be a JSON object")
        return data
