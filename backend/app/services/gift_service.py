"""赠金批次账本：发放、撤回、过期与 FIFO 核销。

约定：gift_grants 的所有变更都必须在持有对应 wallet 行锁的事务内进行。
调用方负责事务边界，本服务不自行 commit。

consume 只核销批次 remaining，不改 wallet.gift_balance
（由跨池分摊的调用方统一维护）。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.datetime_utils import utc_now
from app.core.errors import ConflictError, InvalidRequestError, ResourceNotFoundError
from app.core.logging import get_logger
from app.core.metrics import GIFT_OPS
from app.db.models.wallet import GiftGrant, Wallet
from app.services.wallet_service import (
    TX_GIFT_EXPIRE,
    TX_GIFT_GRANT,
    TX_GIFT_REVOKE,
    wallet_service,
)

logger = get_logger(__name__)

GRANT_STATUS_ACTIVE = "active"
GRANT_STATUS_USED_UP = "used_up"
GRANT_STATUS_EXPIRED = "expired"
GRANT_STATUS_REVOKED = "revoked"

GIFT_REASONS = frozenset(
    {"promotion", "compensation", "internal", "partnership", "other"}
)


class GiftLedgerService:
    """赠金批次账本服务。"""

    async def grant(
        self,
        session: AsyncSession,
        *,
        wallet_id: UUID,
        amount: int,
        reason: str,
        expires_at: datetime | None,
        idempotency_key: str,
        granted_by: UUID,
        remark: str | None,
    ) -> GiftGrant:
        """发放一批赠金。同一 (wallet_id, idempotency_key) 重复调用返回已有批次。"""
        if amount <= 0:
            raise InvalidRequestError("Gift grant amount must be positive")
        if reason not in GIFT_REASONS:
            raise InvalidRequestError(f"Invalid gift grant reason: {reason}")
        if not idempotency_key:
            raise InvalidRequestError("Gift grant idempotency_key is required")

        existing = await self._find_grant_by_idempotency(
            session, wallet_id, idempotency_key
        )
        if existing is not None:
            GIFT_OPS.labels(op="grant", result="idempotent").inc()
            return existing

        wallet = await wallet_service._get_wallet_for_update(session, wallet_id)

        existing = await self._find_grant_by_idempotency(
            session, wallet_id, idempotency_key
        )
        if existing is not None:
            GIFT_OPS.labels(op="grant", result="idempotent").inc()
            return existing

        try:
            async with session.begin_nested():
                grant = GiftGrant(
                    wallet_id=wallet.id,
                    amount=amount,
                    remaining=amount,
                    reason=reason,
                    expires_at=expires_at,
                    status=GRANT_STATUS_ACTIVE,
                    granted_by=granted_by,
                    idempotency_key=idempotency_key,
                    remark=remark,
                )
                session.add(grant)
                await session.flush()

                wallet.gift_balance += amount
                wallet_service._append_transaction(
                    session,
                    wallet=wallet,
                    tx_type=TX_GIFT_GRANT,
                    amount=amount,
                    gift_amount=amount,
                    gift_grant_id=grant.id,
                    ref_request_id=f"gift:{idempotency_key}",
                    operator_user_id=granted_by,
                    remark=remark,
                )
                await session.flush()
        except IntegrityError:
            existing = await self._find_grant_by_idempotency(
                session, wallet_id, idempotency_key
            )
            if existing is None:
                GIFT_OPS.labels(op="grant", result="error").inc()
                raise
            GIFT_OPS.labels(op="grant", result="idempotent").inc()
            return existing

        await session.refresh(grant)
        GIFT_OPS.labels(op="grant", result="ok").inc()
        return grant

    async def revoke(
        self,
        session: AsyncSession,
        *,
        grant_id: UUID,
        revoked_by: UUID,
        remark: str | None,
    ) -> GiftGrant:
        """撤回未消耗剩余。已耗尽或非 active 抛 ConflictError（409）。"""
        try:
            grant = await self._close_grant(
                session,
                grant_id=grant_id,
                new_status=GRANT_STATUS_REVOKED,
                tx_type=TX_GIFT_REVOKE,
                ref_request_id=f"gift_revoke:{grant_id}",
                operator_user_id=revoked_by,
                remark=remark,
                revoked_by=revoked_by,
                conflict_code="gift_already_consumed",
            )
        except Exception:
            GIFT_OPS.labels(op="revoke", result="error").inc()
            raise
        GIFT_OPS.labels(op="revoke", result="ok").inc()
        return grant

    async def expire(self, session: AsyncSession, *, grant_id: UUID) -> GiftGrant:
        """过期回收未消耗剩余。已 expired 则幂等返回，不重复扣减。"""
        try:
            grant = await self._close_grant(
                session,
                grant_id=grant_id,
                new_status=GRANT_STATUS_EXPIRED,
                tx_type=TX_GIFT_EXPIRE,
                ref_request_id=f"gift_expire:{grant_id}",
                operator_user_id=None,
                remark=None,
                idempotent_if_status=GRANT_STATUS_EXPIRED,
            )
        except Exception:
            GIFT_OPS.labels(op="expire", result="error").inc()
            raise
        GIFT_OPS.labels(op="expire", result="ok").inc()
        return grant

    async def consume(
        self,
        session: AsyncSession,
        *,
        wallet: Wallet,
        amount: int,
    ) -> list[tuple[UUID, int]]:
        """按 FIFO 核销批次 remaining，不改 wallet.gift_balance。

        调用方必须已持有 wallet 行锁（本方法会再次 FOR UPDATE 以确保）。
        """
        if amount <= 0:
            raise InvalidRequestError("Gift consume amount must be positive")

        locked = await wallet_service._get_wallet_for_update(session, wallet.id)
        grants = await self._lock_active_grants_fifo(session, locked.id)
        available = sum(g.remaining for g in grants)
        if available < amount:
            logger.error(
                "gift_ledger_mismatch",
                wallet_id=str(locked.id),
                gift_balance=locked.gift_balance,
                grants_remaining=available,
                consume_amount=amount,
            )
            GIFT_OPS.labels(op="consume", result="error").inc()
            raise ConflictError(
                f"Gift ledger mismatch: grants remaining={available} < amount={amount}"
            )

        leftover = amount
        allocations: list[tuple[UUID, int]] = []
        for grant in grants:
            if leftover <= 0:
                break
            take = leftover if leftover < grant.remaining else grant.remaining
            grant.remaining -= take
            leftover -= take
            if grant.remaining == 0:
                grant.status = GRANT_STATUS_USED_UP
            allocations.append((grant.id, take))

        await session.flush()
        GIFT_OPS.labels(op="consume", result="ok").inc()
        return allocations

    async def get_by_idempotency(
        self,
        session: AsyncSession,
        *,
        wallet_id: UUID,
        idempotency_key: str,
    ) -> GiftGrant | None:
        """按 (wallet_id, idempotency_key) 查找已有批次，供 API 幂等短路。"""
        return await self._find_grant_by_idempotency(session, wallet_id, idempotency_key)

    async def sum_granted_by_operator(
        self,
        session: AsyncSession,
        *,
        granted_by: UUID,
        from_time: datetime,
        to_time: datetime,
    ) -> int:
        """操作人在时间窗内已发放金额合计（不含已撤回批次）。"""
        stmt = select(func.coalesce(func.sum(GiftGrant.amount), 0)).where(
            GiftGrant.granted_by == granted_by,
            GiftGrant.created_at >= from_time,
            GiftGrant.created_at < to_time,
            GiftGrant.status != GRANT_STATUS_REVOKED,
        )
        return int((await session.execute(stmt)).scalar_one())

    async def list_grants(
        self,
        session: AsyncSession,
        *,
        wallet_id: UUID,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[GiftGrant]:
        """按钱包查询赠送批次，供后续 API 使用。"""
        stmt = (
            select(GiftGrant)
            .where(GiftGrant.wallet_id == wallet_id)
            .order_by(GiftGrant.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        if status is not None:
            stmt = stmt.where(GiftGrant.status == status)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def find_expired_grant_ids(
        self,
        session: AsyncSession,
        *,
        now: datetime,
        limit: int = 100,
    ) -> list[UUID]:
        """扫描已到期仍 active 且有剩余的批次，供过期回收任务使用。"""
        stmt = (
            select(GiftGrant.id)
            .where(
                GiftGrant.status == GRANT_STATUS_ACTIVE,
                GiftGrant.remaining > 0,
                GiftGrant.expires_at.is_not(None),
                GiftGrant.expires_at <= now,
            )
            .order_by(GiftGrant.expires_at.asc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _find_grant_by_idempotency(
        self,
        session: AsyncSession,
        wallet_id: UUID,
        idempotency_key: str,
    ) -> GiftGrant | None:
        stmt = select(GiftGrant).where(
            GiftGrant.wallet_id == wallet_id,
            GiftGrant.idempotency_key == idempotency_key,
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def _load_grant(self, session: AsyncSession, grant_id: UUID) -> GiftGrant:
        grant = await session.get(GiftGrant, grant_id)
        if grant is None:
            raise ResourceNotFoundError("Gift grant not found")
        return grant

    async def _lock_active_grants_fifo(
        self, session: AsyncSession, wallet_id: UUID
    ) -> list[GiftGrant]:
        stmt = (
            select(GiftGrant)
            .where(
                GiftGrant.wallet_id == wallet_id,
                GiftGrant.status == GRANT_STATUS_ACTIVE,
                GiftGrant.remaining > 0,
            )
            .order_by(GiftGrant.expires_at.asc().nulls_last(), GiftGrant.created_at.asc())
            .with_for_update()
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def _close_grant(
        self,
        session: AsyncSession,
        *,
        grant_id: UUID,
        new_status: str,
        tx_type: str,
        ref_request_id: str,
        operator_user_id: UUID | None,
        remark: str | None,
        revoked_by: UUID | None = None,
        idempotent_if_status: str | None = None,
        conflict_code: str = "conflict",
    ) -> GiftGrant:
        grant = await self._load_grant(session, grant_id)
        wallet = await wallet_service._get_wallet_for_update(session, grant.wallet_id)

        locked = (
            await session.execute(
                select(GiftGrant).where(GiftGrant.id == grant_id).with_for_update()
            )
        ).scalar_one()

        if idempotent_if_status is not None and locked.status == idempotent_if_status:
            return locked
        if locked.status != GRANT_STATUS_ACTIVE or locked.remaining <= 0:
            raise ConflictError(
                "Gift grant has no remaining balance to close",
                code=conflict_code,
            )

        remaining = locked.remaining
        if wallet.gift_balance < remaining:
            logger.error(
                "gift_ledger_mismatch",
                wallet_id=str(wallet.id),
                grant_id=str(locked.id),
                gift_balance=wallet.gift_balance,
                remaining=remaining,
            )
            raise ConflictError(
                f"Gift ledger mismatch: gift_balance={wallet.gift_balance} "
                f"< remaining={remaining}"
            )

        wallet.gift_balance -= remaining
        locked.remaining = 0
        locked.status = new_status
        if revoked_by is not None:
            locked.revoked_by = revoked_by
            locked.revoked_at = utc_now()
        wallet_service._append_transaction(
            session,
            wallet=wallet,
            tx_type=tx_type,
            amount=-remaining,
            gift_amount=-remaining,
            gift_grant_id=locked.id,
            ref_request_id=ref_request_id,
            operator_user_id=operator_user_id,
            remark=remark,
        )
        await session.flush()
        await session.refresh(locked)
        return locked


gift_service = GiftLedgerService()
