"""未结算 hold 对账任务。

MVP：可作为 CLI 命令或 cron 调用，不依赖完整任务队列。

判定「悬挂 hold」：
  - 存在 type=hold 流水
  - 同 (wallet_id, ref_request_id) 不存在 capture
  - 同 (wallet_id, ref_request_id) 不存在 release
  - hold.created_at < now - HOLD_EXPIRE_SECONDS

处理策略：
  - 若已有 usage_log（成功计费痕迹）→ 补 capture（实扣 = charge_amount）
    - 组织钱包：同时按 usage.charge_amount 累计发起成员额度（幂等尽力）
  - 否则 → 全额 release（不累计额度）

个人与组织钱包共用同一扫描逻辑（按 wallet_id / hold 流水），无需按 owner_type 分支查询。
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from app.core.datetime_utils import utc_now

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.core.metrics import RECONCILE_OPS
from app.db.models.log import RequestLog, UsageLog
from app.db.models.org import OrgMember
from app.db.models.wallet import Wallet, WalletTransaction
from app.db.session import close_db, get_session_factory
from app.services.quota_service import quota_service
from app.services.wallet_service import TX_CAPTURE, TX_HOLD, TX_RELEASE, wallet_service

logger = get_logger(__name__)


@dataclass
class ReconcileResult:
    scanned: int = 0
    released: int = 0
    captured: int = 0
    skipped: int = 0
    errors: int = 0
    quota_consumed: int = 0


async def find_stale_holds(
    session: AsyncSession,
    *,
    expire_before: datetime,
    limit: int = 500,
) -> list[WalletTransaction]:
    """查找超时且尚未 capture/release 的 hold 流水（含个人与组织钱包）。"""
    HoldTx = aliased(WalletTransaction)
    OtherTx = aliased(WalletTransaction)

    has_capture = (
        select(OtherTx.id)
        .where(
            OtherTx.wallet_id == HoldTx.wallet_id,
            OtherTx.ref_request_id == HoldTx.ref_request_id,
            OtherTx.type == TX_CAPTURE,
        )
        .exists()
    )
    has_release = (
        select(OtherTx.id)
        .where(
            OtherTx.wallet_id == HoldTx.wallet_id,
            OtherTx.ref_request_id == HoldTx.ref_request_id,
            OtherTx.type == TX_RELEASE,
        )
        .exists()
    )

    stmt = (
        select(HoldTx)
        .where(
            HoldTx.type == TX_HOLD,
            HoldTx.ref_request_id.is_not(None),
            HoldTx.created_at < expire_before,
            ~has_capture,
            ~has_release,
        )
        .order_by(HoldTx.created_at.asc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _resolve_org_member_for_hold(
    session: AsyncSession,
    hold_tx: WalletTransaction,
    usage: UsageLog | None,
) -> UUID | None:
    """组织钱包补 capture 时解析应累计额度的成员。"""
    wallet = await session.get(Wallet, hold_tx.wallet_id)
    if wallet is None or wallet.owner_type != "org":
        return None

    org_id = hold_tx.org_id or wallet.owner_id
    actor_user_id = hold_tx.operator_user_id
    if actor_user_id is None and usage is not None:
        # 回退：usage / request_log 上的发起成员
        actor_user_id = usage.actor_user_id
        if actor_user_id is None:
            req = await session.scalar(
                select(RequestLog).where(RequestLog.request_id == usage.request_id)
            )
            if req is not None:
                actor_user_id = req.actor_user_id or req.user_id

    if actor_user_id is None:
        return None

    member = await session.scalar(
        select(OrgMember).where(
            OrgMember.org_id == org_id,
            OrgMember.user_id == actor_user_id,
        )
    )
    return member.id if member is not None else None


async def reconcile_stale_holds(
    session: AsyncSession,
    *,
    expire_seconds: int | None = None,
    limit: int = 500,
    dry_run: bool = False,
    commit: bool = False,
) -> ReconcileResult:
    """扫描并处理超时未结算 hold（个人 + 组织钱包）。

    commit=False（默认）时仅 flush，由调用方控制事务；
    CLI 场景可传 commit=True，按条提交以便错误隔离。
    """
    expire_seconds = (
        expire_seconds if expire_seconds is not None else settings.hold_expire_seconds
    )
    expire_before = utc_now() - timedelta(seconds=expire_seconds)
    result = ReconcileResult()

    stale_holds = await find_stale_holds(
        session, expire_before=expire_before, limit=limit
    )
    result.scanned = len(stale_holds)

    for hold_tx in stale_holds:
        request_id = hold_tx.ref_request_id
        if not request_id:
            result.skipped += 1
            continue

        hold_amount = abs(int(hold_tx.amount))
        if hold_amount <= 0:
            result.skipped += 1
            continue

        try:
            usage = await session.scalar(
                select(UsageLog).where(UsageLog.request_id == request_id)
            )

            if dry_run:
                action = "capture" if usage is not None else "release"
                logger.info(
                    "reconcile_dry_run",
                    request_id=request_id,
                    wallet_id=str(hold_tx.wallet_id),
                    org_id=str(hold_tx.org_id) if hold_tx.org_id else None,
                    hold_amount=hold_amount,
                    action=action,
                )
                result.skipped += 1
                continue

            operator_user_id = hold_tx.operator_user_id
            if usage is not None:
                await wallet_service.capture(
                    session,
                    wallet_id=hold_tx.wallet_id,
                    hold_amount=hold_amount,
                    actual_amount=int(usage.charge_amount),
                    request_id=request_id,
                    operator_user_id=operator_user_id,
                )
                org_member_id = await _resolve_org_member_for_hold(
                    session, hold_tx, usage
                )
                if org_member_id is not None and int(usage.charge_amount) > 0:
                    try:
                        await quota_service.quota_consume(
                            session,
                            org_member_id=org_member_id,
                            amount=int(usage.charge_amount),
                        )
                        result.quota_consumed += 1
                    except Exception:
                        # 额度累计失败不阻断资金对账；记录后继续
                        logger.exception(
                            "reconcile_quota_consume_error",
                            request_id=request_id,
                            org_member_id=str(org_member_id),
                        )
                if commit:
                    await session.commit()
                else:
                    await session.flush()
                result.captured += 1
                RECONCILE_OPS.labels(action="capture").inc()
                logger.info(
                    "reconcile_captured",
                    request_id=request_id,
                    wallet_id=str(hold_tx.wallet_id),
                    org_id=str(hold_tx.org_id) if hold_tx.org_id else None,
                    hold_amount=hold_amount,
                    charge_amount=int(usage.charge_amount),
                )
            else:
                await wallet_service.release(
                    session,
                    wallet_id=hold_tx.wallet_id,
                    hold_amount=hold_amount,
                    request_id=request_id,
                    operator_user_id=operator_user_id,
                )
                if commit:
                    await session.commit()
                else:
                    await session.flush()
                result.released += 1
                RECONCILE_OPS.labels(action="release").inc()
                logger.info(
                    "reconcile_released",
                    request_id=request_id,
                    wallet_id=str(hold_tx.wallet_id),
                    org_id=str(hold_tx.org_id) if hold_tx.org_id else None,
                    hold_amount=hold_amount,
                )
        except Exception:
            if commit:
                await session.rollback()
            result.errors += 1
            RECONCILE_OPS.labels(action="error").inc()
            logger.exception(
                "reconcile_error",
                request_id=request_id,
                wallet_id=str(hold_tx.wallet_id),
            )

    logger.info(
        "reconcile_finished",
        scanned=result.scanned,
        released=result.released,
        captured=result.captured,
        skipped=result.skipped,
        errors=result.errors,
        quota_consumed=result.quota_consumed,
        expire_seconds=expire_seconds,
        dry_run=dry_run,
    )
    return result


async def _async_main(args: argparse.Namespace) -> int:
    configure_logging(debug=settings.debug)
    session_factory = get_session_factory()
    async with session_factory() as session:
        result = await reconcile_stale_holds(
            session,
            expire_seconds=args.expire_seconds,
            limit=args.limit,
            dry_run=args.dry_run,
            commit=True,
        )
    await close_db()
    print(
        f"reconcile done: scanned={result.scanned} released={result.released} "
        f"captured={result.captured} skipped={result.skipped} "
        f"quota_consumed={result.quota_consumed} errors={result.errors}"
    )
    return 1 if result.errors else 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Reconcile stale wallet holds")
    parser.add_argument(
        "--expire-seconds",
        type=int,
        default=None,
        help="Override HOLD_EXPIRE_SECONDS",
    )
    parser.add_argument("--limit", type=int, default=500, help="Max holds per run")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only report actions without mutating wallets",
    )
    args = parser.parse_args(argv)
    raise SystemExit(asyncio.run(_async_main(args)))


if __name__ == "__main__":
    main()
