"""成员额度：配置读取、quota_check / quota_consume、UTC 周期惰性重置。

周期重置策略（惰性判断）：
  读取 / 校验 / 消费时，若 now >= period_reset_at，则将 quota_used 归零并推进到
  下一 UTC 周期锚点。理由：二期无需独立定时任务即可保证跨周期可用；与持有行锁
  的消费路径天然一致，避免「定时重置与并发消费」竞态。

额度约定：
  - 无 member_quotas 行，或 quota_limit == 0：不限制（仅受组织余额约束）
  - quota_check 与组织钱包可用余额取更严者
  - quota_consume 在网关 capture 时调用（含流式 partial 结算与对账补 capture）
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    QUOTA_PERIOD_DAILY,
    QUOTA_PERIOD_MONTHLY,
    QUOTA_PERIOD_NONE,
    QUOTA_PERIODS,
    STATUS_ENABLED,
)
from app.core.datetime_utils import to_utc_aware, utc_now
from app.core.errors import (
    InsufficientBalanceError,
    InvalidRequestError,
    ResourceNotFoundError,
)
from app.db.models.org import MemberQuota, Org, OrgMember
from app.services.audit_service import record_audit
from app.services.org_service import org_service
from app.services.wallet_service import wallet_service


def next_period_reset_at(period: str, now: datetime | None = None) -> datetime | None:
    """计算下一 UTC 周期锚点（周期结束时刻 = 下一周期起点）。

    daily：次日 00:00:00 UTC；monthly：下月 1 日 00:00:00 UTC。
    none：不重置，返回 None。成员额度与业务时区无关，刻意用 UTC 避免「各组织时区不同」。
    """
    if period == QUOTA_PERIOD_NONE:
        return None
    ts = to_utc_aware(now or utc_now())
    if period == QUOTA_PERIOD_DAILY:
        tomorrow = ts.date() + timedelta(days=1)
        return datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=UTC)
    if period == QUOTA_PERIOD_MONTHLY:
        if ts.month == 12:
            return datetime(ts.year + 1, 1, 1, tzinfo=UTC)
        return datetime(ts.year, ts.month + 1, 1, tzinfo=UTC)
    raise InvalidRequestError(f"Unsupported quota_period: {period}")


@dataclass(frozen=True, slots=True)
class QuotaCheckResult:
    """quota_check 结果：effective_available 为组织余额与成员剩余额度的更严值。"""

    ok: bool
    amount: int
    org_available: int
    quota_remaining: int | None  # None = 不限制
    effective_available: int
    quota_limit: int | None
    quota_used: int
    quota_period: str


class QuotaService:
    """成员额度服务；调用方负责事务边界。"""

    async def get_quota(
        self, session: AsyncSession, org_member_id: UUID
    ) -> MemberQuota | None:
        stmt = select(MemberQuota).where(MemberQuota.org_member_id == org_member_id)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def _get_quota_for_update(
        self, session: AsyncSession, org_member_id: UUID
    ) -> MemberQuota | None:
        stmt = (
            select(MemberQuota)
            .where(MemberQuota.org_member_id == org_member_id)
            .with_for_update()
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_or_create_quota(
        self,
        session: AsyncSession,
        org_member_id: UUID,
        *,
        for_update: bool = False,
    ) -> MemberQuota:
        quota = (
            await self._get_quota_for_update(session, org_member_id)
            if for_update
            else await self.get_quota(session, org_member_id)
        )
        if quota is not None:
            return quota
        quota = MemberQuota(
            org_member_id=org_member_id,
            quota_period=QUOTA_PERIOD_NONE,
            quota_limit=0,
            quota_used=0,
            period_reset_at=None,
        )
        session.add(quota)
        await session.flush()
        if for_update:
            locked = await self._get_quota_for_update(session, org_member_id)
            assert locked is not None
            return locked
        return quota

    def apply_lazy_period_reset(
        self, quota: MemberQuota, *, now: datetime | None = None
    ) -> bool:
        """若已越过周期锚点则归零已用并推进锚点。返回是否发生了重置。"""
        if quota.quota_period == QUOTA_PERIOD_NONE:
            return False

        ts = to_utc_aware(now or utc_now())
        if quota.period_reset_at is None:
            quota.period_reset_at = next_period_reset_at(quota.quota_period, ts)
            return False

        reset_at = to_utc_aware(quota.period_reset_at)
        if ts < reset_at:
            return False

        # 可能跨越多个周期：直接按「当前时刻」锚到下一周期，已用清零
        quota.quota_used = 0
        quota.period_reset_at = next_period_reset_at(quota.quota_period, ts)
        return True

    def remaining_after_reset(self, quota: MemberQuota | None) -> int | None:
        """剩余额度；None 表示不限制。"""
        if quota is None or quota.quota_limit == 0:
            return None
        return max(0, int(quota.quota_limit) - int(quota.quota_used))

    async def ensure_fresh_quota(
        self,
        session: AsyncSession,
        org_member_id: UUID,
        *,
        for_update: bool = False,
        now: datetime | None = None,
    ) -> MemberQuota | None:
        """读取额度并执行惰性周期重置；无行则返回 None（视为不限制）。"""
        quota = (
            await self._get_quota_for_update(session, org_member_id)
            if for_update
            else await self.get_quota(session, org_member_id)
        )
        if quota is None:
            return None
        if self.apply_lazy_period_reset(quota, now=now):
            await session.flush()
        return quota

    async def is_quota_available(
        self,
        session: AsyncSession,
        *,
        org_member_id: UUID,
        amount: int = 1,
        now: datetime | None = None,
    ) -> bool:
        """是否仍有至少 amount 的成员额度（不含组织余额）。"""
        if amount < 0:
            raise InvalidRequestError("Quota check amount cannot be negative")
        if amount == 0:
            return True
        quota = await self.ensure_fresh_quota(
            session, org_member_id, for_update=False, now=now
        )
        remaining = self.remaining_after_reset(quota)
        if remaining is None:
            return True
        return remaining >= amount

    async def quota_check(
        self,
        session: AsyncSession,
        *,
        org_id: UUID,
        org_member_id: UUID,
        amount: int,
        now: datetime | None = None,
    ) -> QuotaCheckResult:
        """校验成员额度与组织可用余额，取更严者。

        不抛异常：由调用方根据 ok 决定回退或 402。
        """
        if amount < 0:
            raise InvalidRequestError("Quota check amount cannot be negative")

        wallet = await wallet_service.require_org_wallet(session, org_id)
        org_available = int(wallet.balance) + int(wallet.credit_limit)

        quota = await self.ensure_fresh_quota(
            session, org_member_id, for_update=False, now=now
        )
        remaining = self.remaining_after_reset(quota)
        if remaining is None:
            effective = org_available
        else:
            effective = min(remaining, org_available)

        return QuotaCheckResult(
            ok=amount <= effective,
            amount=amount,
            org_available=org_available,
            quota_remaining=remaining,
            effective_available=effective,
            quota_limit=None if quota is None else int(quota.quota_limit),
            quota_used=0 if quota is None else int(quota.quota_used),
            quota_period=QUOTA_PERIOD_NONE if quota is None else quota.quota_period,
        )

    async def quota_consume(
        self,
        session: AsyncSession,
        *,
        org_member_id: UUID,
        amount: int,
        now: datetime | None = None,
    ) -> MemberQuota | None:
        """累计已用额度（供 capture 时调用）。

        - amount == 0：空操作
        - 无额度行或 limit==0：不累计，返回现有行（可能为 None）
        - 并发下用 FOR UPDATE，保证 used 不超过 limit
        """
        if amount < 0:
            raise InvalidRequestError("Quota consume amount cannot be negative")
        if amount == 0:
            return await self.get_quota(session, org_member_id)

        quota = await self.ensure_fresh_quota(
            session, org_member_id, for_update=True, now=now
        )
        if quota is None or quota.quota_limit == 0:
            return quota

        remaining = self.remaining_after_reset(quota)
        assert remaining is not None
        if remaining < amount:
            raise InsufficientBalanceError(
                f"Insufficient member quota: remaining={remaining}, required={amount}"
            )

        quota.quota_used = int(quota.quota_used) + amount
        await session.flush()
        await session.refresh(quota)
        return quota

    async def configure_member_quota(
        self,
        session: AsyncSession,
        *,
        org: Org,
        actor_user_id: UUID,
        member_id: UUID,
        can_consume_org_wallet: bool | None = None,
        quota_period: str | None = None,
        quota_limit: int | None = None,
        now: datetime | None = None,
    ) -> OrgMember:
        """Owner/Admin 设置消费开关与额度上限；写审计。"""
        org_service.ensure_org_writable(org)
        member = await session.get(OrgMember, member_id)
        if member is None or member.org_id != org.id or member.status != STATUS_ENABLED:
            raise ResourceNotFoundError("Organization member not found")

        if quota_period is not None and quota_period not in QUOTA_PERIODS:
            raise InvalidRequestError(
                f"quota_period must be one of: {', '.join(QUOTA_PERIODS)}"
            )
        if quota_limit is not None and quota_limit < 0:
            raise InvalidRequestError("quota_limit must be >= 0")

        detail: dict = {"user_id": str(member.user_id)}
        changed = False

        if can_consume_org_wallet is not None:
            if member.can_consume_org_wallet != can_consume_org_wallet:
                member.can_consume_org_wallet = can_consume_org_wallet
                detail["can_consume_org_wallet"] = can_consume_org_wallet
                changed = True

        need_quota_row = quota_period is not None or quota_limit is not None
        if need_quota_row:
            quota = await self.get_or_create_quota(
                session, member.id, for_update=True
            )
            ts = to_utc_aware(now or utc_now())
            if quota_period is not None and quota.quota_period != quota_period:
                quota.quota_period = quota_period
                detail["quota_period"] = quota_period
                changed = True
                if quota_period == QUOTA_PERIOD_NONE:
                    quota.period_reset_at = None
                else:
                    # 切换周期：从当前时刻起算下一锚点，已用清零避免跨周期口径混乱
                    quota.quota_used = 0
                    quota.period_reset_at = next_period_reset_at(quota_period, ts)
                    detail["quota_used_reset"] = True

            if quota_limit is not None and int(quota.quota_limit) != quota_limit:
                quota.quota_limit = quota_limit
                detail["quota_limit"] = quota_limit
                changed = True
                # 降低上限且已用超限时，截断到上限（不允许 used > limit）
                if quota.quota_limit > 0 and quota.quota_used > quota.quota_limit:
                    quota.quota_used = quota.quota_limit
                    detail["quota_used_clamped"] = True

            # 若仅改 limit、周期已是 daily/monthly 但锚点缺失，补齐
            if (
                quota.quota_period in (QUOTA_PERIOD_DAILY, QUOTA_PERIOD_MONTHLY)
                and quota.period_reset_at is None
            ):
                quota.period_reset_at = next_period_reset_at(quota.quota_period, ts)

        if changed:
            await session.flush()
            await record_audit(
                session,
                action="org.member.quota_update",
                target=f"org:{org.id}:member:{member.id}",
                actor_user_id=actor_user_id,
                detail=detail,
            )

        await session.flush()
        return member

    async def load_member_with_quota(
        self, session: AsyncSession, member: OrgMember, *, now: datetime | None = None
    ) -> OrgMember:
        """供 API 序列化：惰性重置后把 quota 挂到成员对象上。"""
        quota = await self.ensure_fresh_quota(
            session, member.id, for_update=False, now=now
        )
        # 显式赋值，避免 identity-map 中旧的「已加载为 None」缓存
        member.quota = quota
        return member


quota_service = QuotaService()
