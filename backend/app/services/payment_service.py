"""支付服务：创建支付单、状态机流转、回调验签与幂等入账。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.constants import (
    ORG_ROLE_OWNER,
    PAYMENT_PROVIDER_MOCK,
    PAYMENT_STATUS_CREATED,
    PAYMENT_STATUS_EXPIRED,
    PAYMENT_STATUS_FAILED,
    PAYMENT_STATUS_PAID,
    PAYMENT_TARGET_ORG,
    PAYMENT_TARGET_PERSONAL,
    PAYMENT_TARGETS,
)
from app.core.errors import ConflictError, InvalidRequestError, ResourceNotFoundError
from app.db.models.payment import PaymentOrder
from app.payments import get_payment_provider
from app.payments.base import CallbackVerificationResult, CreatePaymentResult, QueryPaymentResult
from app.services.org_service import org_service
from app.services.wallet_service import TX_RECHARGE, wallet_service

# 合法状态流转：仅 created 可转到终态
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    PAYMENT_STATUS_CREATED: frozenset(
        {PAYMENT_STATUS_PAID, PAYMENT_STATUS_EXPIRED, PAYMENT_STATUS_FAILED}
    ),
    PAYMENT_STATUS_PAID: frozenset(),
    PAYMENT_STATUS_EXPIRED: frozenset(),
    PAYMENT_STATUS_FAILED: frozenset(),
}


def payment_ref_request_id(order_no: str) -> str:
    """钱包流水幂等键：同一支付单只产生一笔 recharge。"""
    return f"payment:{order_no}"


class PaymentService:
    """支付单与入账编排；不自行 commit，由调用方控制事务。"""

    def transition(self, order: PaymentOrder, new_status: str) -> None:
        """状态机校验并写入新状态；非法流转抛 ConflictError。"""
        allowed = _ALLOWED_TRANSITIONS.get(order.status, frozenset())
        if new_status not in allowed:
            raise ConflictError(
                f"Invalid payment status transition: {order.status} → {new_status}"
            )
        order.status = new_status

    def generate_order_no(self) -> str:
        """业务订单号：时间戳前缀 + 短随机，保证可读且唯一。"""
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        return f"PO{ts}{uuid4().hex[:10].upper()}"

    async def create_order(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        amount: int,
        target_type: str,
        org_id: UUID | None = None,
        provider_name: str = PAYMENT_PROVIDER_MOCK,
        currency: str = "CNY",
        notify_url: str | None = None,
        return_url: str | None = None,
    ) -> tuple[PaymentOrder, CreatePaymentResult]:
        """创建支付单并调用渠道 create_payment。

        - target_type=personal：入账当前用户个人钱包
        - target_type=org：入账组织钱包，要求调用人为该组织 Owner
        """
        if amount <= 0:
            raise InvalidRequestError("Payment amount must be positive")
        if target_type not in PAYMENT_TARGETS:
            raise InvalidRequestError(f"Invalid target_type: {target_type}")

        if target_type == PAYMENT_TARGET_PERSONAL:
            if org_id is not None:
                raise InvalidRequestError("org_id must be omitted for personal target")
            wallet = await wallet_service.get_or_create_personal_wallet(session, user_id)
            resolved_org_id: UUID | None = None
        else:
            if org_id is None:
                raise InvalidRequestError("org_id is required for org target")
            await org_service.require_min_role(
                session,
                org_id=org_id,
                user_id=user_id,
                min_role=ORG_ROLE_OWNER,
            )
            wallet = await wallet_service.get_or_create_org_wallet(session, org_id)
            resolved_org_id = org_id

        provider = get_payment_provider(provider_name)
        order_no = self.generate_order_no()
        expire_minutes = max(1, settings.payment_order_expire_minutes)
        expired_at = datetime.now(timezone.utc) + timedelta(minutes=expire_minutes)

        resolved_notify = notify_url
        if resolved_notify is None and settings.payment_public_base_url:
            resolved_notify = (
                f"{settings.payment_public_base_url.rstrip('/')}"
                f"/payments/callbacks/{provider_name}"
            )

        order = PaymentOrder(
            order_no=order_no,
            user_id=user_id,
            target_wallet_id=wallet.id,
            target_type=target_type,
            org_id=resolved_org_id,
            amount=amount,
            currency=currency,
            provider=provider_name,
            status=PAYMENT_STATUS_CREATED,
            expired_at=expired_at,
        )
        session.add(order)
        await session.flush()

        create_result = await provider.create_payment(
            order_no=order_no,
            amount=amount,
            currency=currency,
            subject=f"Wallet recharge {order_no}",
            notify_url=resolved_notify,
            return_url=return_url,
        )
        order.external_order_no = create_result.external_order_no
        await session.flush()
        await session.refresh(order)
        return order, create_result

    async def get_order_by_no(
        self, session: AsyncSession, order_no: str
    ) -> PaymentOrder | None:
        stmt = select(PaymentOrder).where(PaymentOrder.order_no == order_no)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def require_order_by_no(
        self, session: AsyncSession, order_no: str
    ) -> PaymentOrder:
        order = await self.get_order_by_no(session, order_no)
        if order is None:
            raise ResourceNotFoundError("Payment order not found")
        return order

    async def _get_order_for_update(
        self, session: AsyncSession, order_no: str
    ) -> PaymentOrder:
        stmt = (
            select(PaymentOrder)
            .where(PaymentOrder.order_no == order_no)
            .with_for_update()
        )
        result = await session.execute(stmt)
        order = result.scalar_one_or_none()
        if order is None:
            raise ResourceNotFoundError("Payment order not found")
        return order

    def _is_past_expiry(self, order: PaymentOrder, *, now: datetime) -> bool:
        if order.expired_at is None:
            return False
        expired_at = order.expired_at
        if expired_at.tzinfo is None:
            expired_at = expired_at.replace(tzinfo=timezone.utc)
        return now >= expired_at

    async def handle_callback(
        self,
        session: AsyncSession,
        *,
        provider_name: str,
        headers: dict[str, str],
        body: bytes | str | dict[str, Any],
    ) -> PaymentOrder | None:
        """验签 → 状态机 → 幂等入账。

        - 已 paid：幂等返回，不再入账
        - 已过期 / expired：不入账（created 且超时则标为 expired）
        - success=false：created → failed
        - verified.apply=False：验签通过但不改状态，可能返回 None
        """
        provider = get_payment_provider(provider_name)
        verified = await provider.verify_callback(headers=headers, body=body)
        if not verified.apply:
            if verified.order_no and verified.order_no != "unknown":
                return await self.get_order_by_no(session, verified.order_no)
            return None
        return await self._apply_verified_callback(session, verified=verified, body=body)

    async def apply_query_result(
        self,
        session: AsyncSession,
        *,
        order: PaymentOrder,
        query: QueryPaymentResult,
        credit_even_if_expired: bool = True,
    ) -> PaymentOrder:
        """根据渠道查单结果补状态（对账任务用）。

        - paid：幂等入账；credit_even_if_expired=True 时即使本地已过期也入账（掉单补账）
        - failed：created → failed
        - expired：created → expired
        - pending：保持 created（等待渠道终态或下次对账）
        """
        locked = await self._get_order_for_update(session, order.order_no)
        if query.external_order_no:
            locked.external_order_no = query.external_order_no
        locked.callback_payload = self._serialize_callback(
            {"source": "reconcile_query", **(query.raw or {})}
        )

        if locked.status == PAYMENT_STATUS_PAID:
            return locked
        if locked.status in (PAYMENT_STATUS_EXPIRED, PAYMENT_STATUS_FAILED):
            return locked
        if locked.status != PAYMENT_STATUS_CREATED:
            raise ConflictError(f"Unexpected payment status: {locked.status}")

        now = datetime.now(timezone.utc)
        past_expiry = self._is_past_expiry(locked, now=now)

        if query.status == "paid":
            if past_expiry and not credit_even_if_expired:
                self.transition(locked, PAYMENT_STATUS_EXPIRED)
                await session.flush()
                await session.refresh(locked)
                return locked
            if query.amount is not None and query.amount != locked.amount:
                raise InvalidRequestError(
                    f"Query amount mismatch: order={locked.amount}, query={query.amount}"
                )
            self.transition(locked, PAYMENT_STATUS_PAID)
            locked.paid_at = now
            await session.flush()
            await wallet_service.recharge(
                session,
                wallet_id=locked.target_wallet_id,
                amount=locked.amount,
                operator_user_id=locked.user_id,
                remark=f"Payment {locked.order_no} (reconcile)",
                ref_request_id=payment_ref_request_id(locked.order_no),
            )
            await session.refresh(locked)
            return locked

        if query.status == "failed":
            locked.fail_reason = "provider_query_failed"
            self.transition(locked, PAYMENT_STATUS_FAILED)
            await session.flush()
            await session.refresh(locked)
            return locked

        if query.status == "expired":
            self.transition(locked, PAYMENT_STATUS_EXPIRED)
            await session.flush()
            await session.refresh(locked)
            return locked

        # pending：即使本地已过 expired_at 也保持 created，等待渠道终态或下次对账
        await session.flush()
        await session.refresh(locked)
        return locked

    async def list_stale_created_orders(
        self,
        session: AsyncSession,
        *,
        stale_before: datetime,
        limit: int = 200,
    ) -> list[PaymentOrder]:
        """查找仍为 created 且创建时间早于 stale_before 的订单（供对账扫描）。"""
        stmt = (
            select(PaymentOrder)
            .where(
                PaymentOrder.status == PAYMENT_STATUS_CREATED,
                PaymentOrder.created_at < stale_before,
            )
            .order_by(PaymentOrder.created_at.asc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def reconcile_order(
        self,
        session: AsyncSession,
        order: PaymentOrder,
    ) -> PaymentOrder:
        """向渠道查单并补本地状态。"""
        provider = get_payment_provider(order.provider)
        query = await provider.query_order(
            order_no=order.order_no,
            external_order_no=order.external_order_no,
        )
        return await self.apply_query_result(session, order=order, query=query)

    async def apply_mock_success(
        self,
        session: AsyncSession,
        *,
        order_no: str,
        external_order_no: str | None = None,
        amount: int | None = None,
    ) -> PaymentOrder:
        """测试辅助：跳过 HTTP，直接构造已验签成功回调。"""
        order = await self.require_order_by_no(session, order_no)
        verified = CallbackVerificationResult(
            order_no=order.order_no,
            external_order_no=external_order_no or order.external_order_no or "",
            success=True,
            amount=amount if amount is not None else order.amount,
            raw={"source": "apply_mock_success"},
        )
        return await self._apply_verified_callback(
            session, verified=verified, body=verified.raw
        )

    async def _apply_verified_callback(
        self,
        session: AsyncSession,
        *,
        verified: CallbackVerificationResult,
        body: bytes | str | dict[str, Any],
    ) -> PaymentOrder:
        order = await self._get_order_for_update(session, verified.order_no)
        order.callback_payload = self._serialize_callback(body)
        if verified.external_order_no:
            order.external_order_no = verified.external_order_no

        now = datetime.now(timezone.utc)

        # 终态幂等：已支付直接返回
        if order.status == PAYMENT_STATUS_PAID:
            return order

        # 已过期 / 失败终态：不再入账
        if order.status in (PAYMENT_STATUS_EXPIRED, PAYMENT_STATUS_FAILED):
            raise ConflictError(
                f"Payment order is {order.status}; callback will not credit wallet"
            )

        # created 但已超时：标 expired，不入账
        if self._is_past_expiry(order, now=now):
            self.transition(order, PAYMENT_STATUS_EXPIRED)
            await session.flush()
            raise ConflictError("Payment order has expired; callback will not credit wallet")

        if not verified.success:
            order.fail_reason = verified.fail_reason or "payment_failed"
            self.transition(order, PAYMENT_STATUS_FAILED)
            await session.flush()
            await session.refresh(order)
            return order

        if verified.amount is not None and verified.amount != order.amount:
            raise InvalidRequestError(
                f"Callback amount mismatch: order={order.amount}, callback={verified.amount}"
            )

        self.transition(order, PAYMENT_STATUS_PAID)
        order.paid_at = now
        await session.flush()

        await wallet_service.recharge(
            session,
            wallet_id=order.target_wallet_id,
            amount=order.amount,
            operator_user_id=order.user_id,
            remark=f"Payment {order.order_no}",
            ref_request_id=payment_ref_request_id(order.order_no),
        )
        await session.refresh(order)
        return order

    async def mark_expired_if_needed(
        self, session: AsyncSession, order: PaymentOrder
    ) -> PaymentOrder:
        """主动将超时未支付订单标为 expired。"""
        if order.status != PAYMENT_STATUS_CREATED:
            return order
        now = datetime.now(timezone.utc)
        if not self._is_past_expiry(order, now=now):
            return order
        locked = await self._get_order_for_update(session, order.order_no)
        if locked.status == PAYMENT_STATUS_CREATED and self._is_past_expiry(locked, now=now):
            self.transition(locked, PAYMENT_STATUS_EXPIRED)
            await session.flush()
            await session.refresh(locked)
            return locked
        return locked

    async def list_orders(
        self,
        session: AsyncSession,
        *,
        user_id: UUID | None = None,
        org_id: UUID | None = None,
        status: str | None = None,
        provider: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[PaymentOrder]:
        stmt = select(PaymentOrder).order_by(PaymentOrder.created_at.desc())
        if user_id is not None:
            stmt = stmt.where(PaymentOrder.user_id == user_id)
        if org_id is not None:
            stmt = stmt.where(PaymentOrder.org_id == org_id)
        if status is not None:
            stmt = stmt.where(PaymentOrder.status == status)
        if provider is not None:
            stmt = stmt.where(PaymentOrder.provider == provider)
        stmt = stmt.offset(offset).limit(limit)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def count_recharge_txs(
        self, session: AsyncSession, *, order_no: str, wallet_id: UUID
    ) -> int:
        """测试/对账辅助：统计该支付单对应的 recharge 流水条数。"""
        txs = await wallet_service.get_transactions(
            session,
            wallet_id=wallet_id,
            tx_type=TX_RECHARGE,
            ref_request_id=payment_ref_request_id(order_no),
            limit=10,
        )
        return len(txs)

    @staticmethod
    def _serialize_callback(body: bytes | str | dict[str, Any]) -> str:
        if isinstance(body, dict):
            return json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        if isinstance(body, bytes):
            return body.decode("utf-8", errors="replace")
        return body


payment_service = PaymentService()
