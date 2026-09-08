"""Stripe Checkout 支付渠道（沙箱/生产共用；密钥仅来自环境变量）。"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.config import settings
from app.core.constants import PAYMENT_PROVIDER_STRIPE
from app.core.errors import InvalidRequestError
from app.payments.base import (
    CallbackVerificationResult,
    CreatePaymentResult,
    PaymentProvider,
    QueryPaymentResult,
)

STRIPE_API_BASE = "https://api.stripe.com/v1"
# Stripe 多数法币（含 CNY）以分为最小单位；本系统金额为 money_scale 最小单位
_STRIPE_MINOR_PER_MAJOR = 100


def to_stripe_amount(amount: int, *, money_scale: int | None = None) -> int:
    """内部金额 → Stripe 分（或等值最小单位）。"""
    scale = money_scale if money_scale is not None else settings.money_scale
    if scale <= 0 or amount <= 0:
        raise InvalidRequestError("Invalid amount for Stripe")
    if amount * _STRIPE_MINOR_PER_MAJOR % scale != 0:
        raise InvalidRequestError(
            f"Amount {amount} is not convertible to Stripe minor units "
            f"(money_scale={scale})"
        )
    return amount * _STRIPE_MINOR_PER_MAJOR // scale


def from_stripe_amount(stripe_amount: int, *, money_scale: int | None = None) -> int:
    """Stripe 分 → 内部金额。"""
    scale = money_scale if money_scale is not None else settings.money_scale
    return int(stripe_amount) * scale // _STRIPE_MINOR_PER_MAJOR


def build_stripe_signature(
    payload: bytes | str,
    *,
    secret: str,
    timestamp: int | None = None,
) -> str:
    """构造 Stripe-Signature 头（测试/本地重放用）。"""
    if isinstance(payload, str):
        payload_bytes = payload.encode("utf-8")
    else:
        payload_bytes = payload
    ts = int(time.time()) if timestamp is None else timestamp
    signed = f"{ts}.".encode("utf-8") + payload_bytes
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


def verify_stripe_signature(
    *,
    payload: bytes,
    signature_header: str,
    secret: str,
    tolerance_seconds: int = 300,
) -> None:
    """校验 Stripe webhook 签名；失败抛 InvalidRequestError。"""
    if not secret:
        raise InvalidRequestError("Stripe webhook secret is not configured")
    if not signature_header:
        raise InvalidRequestError("Missing Stripe-Signature header")

    parts: dict[str, list[str]] = {}
    for item in signature_header.split(","):
        item = item.strip()
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        parts.setdefault(key.strip(), []).append(value.strip())

    try:
        timestamp = int((parts.get("t") or [""])[0])
    except (TypeError, ValueError) as exc:
        raise InvalidRequestError("Invalid Stripe-Signature timestamp") from exc

    v1_list = parts.get("v1") or []
    if not v1_list:
        raise InvalidRequestError("Invalid Stripe-Signature: missing v1")

    signed = f"{timestamp}.".encode("utf-8") + payload
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, candidate) for candidate in v1_list):
        raise InvalidRequestError("Stripe webhook signature invalid")

    now = int(time.time())
    if tolerance_seconds > 0 and abs(now - timestamp) > tolerance_seconds:
        raise InvalidRequestError("Stripe webhook timestamp outside tolerance")


class StripePaymentProvider(PaymentProvider):
    """Stripe Checkout Session：创建会话、验签 webhook、查单。"""

    name = PAYMENT_PROVIDER_STRIPE

    def __init__(
        self,
        *,
        secret_key: str | None = None,
        webhook_secret: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        api_base: str = STRIPE_API_BASE,
    ) -> None:
        self._secret_key = (
            secret_key if secret_key is not None else settings.stripe_secret_key
        )
        self._webhook_secret = (
            webhook_secret
            if webhook_secret is not None
            else settings.stripe_webhook_secret
        )
        self._api_base = api_base.rstrip("/")
        self._http = http_client
        self._owns_http = http_client is None

    def _require_secret_key(self) -> str:
        if not self._secret_key:
            raise InvalidRequestError(
                "Stripe is not configured (STRIPE_SECRET_KEY is empty)"
            )
        return self._secret_key

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=30.0)
            self._owns_http = True
        return self._http

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

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
        secret = self._require_secret_key()
        stripe_amount = to_stripe_amount(amount)
        success_url = return_url or (
            f"{settings.payment_public_base_url.rstrip('/')}/payments/success"
            f"?order_no={order_no}&session_id={{CHECKOUT_SESSION_ID}}"
        )
        cancel_url = (
            (extra or {}).get("cancel_url")
            or return_url
            or f"{settings.payment_public_base_url.rstrip('/')}/payments/cancel"
            f"?order_no={order_no}"
        )

        form: dict[str, str] = {
            "mode": "payment",
            "client_reference_id": order_no,
            "success_url": success_url,
            "cancel_url": cancel_url,
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": currency.lower(),
            "line_items[0][price_data][unit_amount]": str(stripe_amount),
            "line_items[0][price_data][product_data][name]": subject[:120],
            "metadata[order_no]": order_no,
            "payment_intent_data[metadata][order_no]": order_no,
        }
        if notify_url:
            # Checkout 本身用 Dashboard webhook；此处仅记入 raw 便于审计
            form["metadata[notify_url]"] = notify_url

        data = await self._request("POST", "/checkout/sessions", form=form, secret=secret)
        session_id = str(data.get("id") or "")
        pay_url = data.get("url")
        if not session_id:
            raise InvalidRequestError("Stripe create session missing id")
        return CreatePaymentResult(
            external_order_no=session_id,
            pay_url=str(pay_url) if pay_url else None,
            raw=data,
        )

    async def verify_callback(
        self,
        *,
        headers: dict[str, str],
        body: bytes | str | dict[str, Any],
    ) -> CallbackVerificationResult:
        payload_bytes, event = self._parse_event_body(body)
        sig = (
            headers.get("stripe-signature")
            or headers.get("Stripe-Signature")
            or ""
        )
        verify_stripe_signature(
            payload=payload_bytes,
            signature_header=sig,
            secret=self._webhook_secret or settings.stripe_webhook_secret,
        )

        event_type = str(event.get("type") or "")
        obj = (event.get("data") or {}).get("object") or {}
        if not isinstance(obj, dict):
            raise InvalidRequestError("Stripe event data.object must be an object")

        external_order_no = str(obj.get("id") or "")
        amount_total = obj.get("amount_total")
        amount = (
            from_stripe_amount(int(amount_total))
            if amount_total is not None
            else None
        )

        if event_type == "checkout.session.completed":
            order_no = self._extract_order_no(obj)
            payment_status = str(obj.get("payment_status") or "")
            success = payment_status == "paid"
            return CallbackVerificationResult(
                order_no=order_no,
                external_order_no=external_order_no,
                success=success,
                amount=amount,
                fail_reason=None if success else f"stripe_{payment_status or 'unpaid'}",
                raw=event,
            )

        if event_type in (
            "checkout.session.expired",
            "checkout.session.async_payment_failed",
        ):
            order_no = self._extract_order_no(obj)
            return CallbackVerificationResult(
                order_no=order_no,
                external_order_no=external_order_no,
                success=False,
                amount=amount,
                fail_reason=event_type,
                raw=event,
            )

        # 其它事件：验签通过但不改本地状态（由对账任务兜底）
        order_no = self._extract_order_no(obj, required=False) or "unknown"
        return CallbackVerificationResult(
            order_no=order_no,
            external_order_no=external_order_no,
            success=False,
            amount=amount,
            fail_reason=f"ignored_event:{event_type or 'unknown'}",
            raw=event,
            apply=False,
        )

    async def query_order(
        self,
        *,
        order_no: str,
        external_order_no: str | None = None,
    ) -> QueryPaymentResult:
        secret = self._require_secret_key()
        if not external_order_no:
            raise InvalidRequestError("Stripe query requires external_order_no")

        data = await self._request(
            "GET",
            f"/checkout/sessions/{external_order_no}",
            secret=secret,
        )
        status = self._map_session_status(data)
        amount_total = data.get("amount_total")
        amount = (
            from_stripe_amount(int(amount_total))
            if amount_total is not None
            else None
        )
        # 校验 metadata 中的 order_no（若有）
        meta_order = self._extract_order_no(data, required=False)
        if meta_order and meta_order != order_no:
            raise InvalidRequestError(
                f"Stripe session order_no mismatch: expected={order_no}, got={meta_order}"
            )
        return QueryPaymentResult(
            external_order_no=str(data.get("id") or external_order_no),
            status=status,
            amount=amount,
            raw=data,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        secret: str,
        form: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        client = await self._client()
        url = f"{self._api_base}{path}"
        headers = {"Authorization": f"Bearer {secret}"}
        if method.upper() == "GET":
            resp = await client.get(url, headers=headers)
        else:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            resp = await client.post(
                url,
                headers=headers,
                content=urlencode(form or {}),
            )
        try:
            data = resp.json()
        except Exception as exc:
            raise InvalidRequestError(
                f"Stripe API returned non-JSON (HTTP {resp.status_code})"
            ) from exc
        if resp.status_code >= 400:
            err = data.get("error") if isinstance(data, dict) else None
            message = (
                err.get("message")
                if isinstance(err, dict)
                else f"Stripe API error HTTP {resp.status_code}"
            )
            raise InvalidRequestError(str(message))
        if not isinstance(data, dict):
            raise InvalidRequestError("Stripe API response must be an object")
        return data

    @staticmethod
    def _map_session_status(data: dict[str, Any]) -> str:
        session_status = str(data.get("status") or "")
        payment_status = str(data.get("payment_status") or "")
        if payment_status == "paid":
            return "paid"
        if session_status == "expired":
            return "expired"
        if payment_status in ("unpaid", "no_payment_required") and session_status == "open":
            return "pending"
        if session_status == "complete" and payment_status != "paid":
            return "failed"
        return "pending"

    @staticmethod
    def _extract_order_no(obj: dict[str, Any], *, required: bool = True) -> str:
        meta = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
        order_no = str(
            meta.get("order_no")
            or obj.get("client_reference_id")
            or ""
        )
        if required and not order_no:
            raise InvalidRequestError("Stripe event missing order_no metadata")
        return order_no

    @staticmethod
    def _parse_event_body(
        body: bytes | str | dict[str, Any],
    ) -> tuple[bytes, dict[str, Any]]:
        if isinstance(body, dict):
            payload_bytes = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
            return payload_bytes, body
        if isinstance(body, bytes):
            payload_bytes = body
            text = body.decode("utf-8")
        else:
            text = body
            payload_bytes = body.encode("utf-8")
        try:
            event = json.loads(text)
        except json.JSONDecodeError as exc:
            raise InvalidRequestError("Stripe webhook body is not valid JSON") from exc
        if not isinstance(event, dict):
            raise InvalidRequestError("Stripe webhook body must be a JSON object")
        return payload_bytes, event
