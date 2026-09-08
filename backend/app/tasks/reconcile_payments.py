"""支付单对账任务：扫描超时仍为 created 的订单，向渠道查单后补状态。

复用一期 reconcile_holds 的 CLI / 会话框架：可 cron 调用，不依赖完整任务队列。

策略：
  - 查找 status=created 且 created_at < now - PAYMENT_RECONCILE_STALE_SECONDS
  - 向对应 PaymentProvider.query_order
  - paid → 幂等入账（即使本地已过 expired_at，用于掉单补账）
  - failed → 标 failed
  - expired → 标 expired
  - pending → 保持 created，等待下次扫描
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.logging import configure_logging, get_logger
from app.core.metrics import PAYMENT_RECONCILE_OPS
from app.db.session import close_db, get_session_factory
from app.services.payment_service import payment_service

logger = get_logger(__name__)


@dataclass
class PaymentReconcileResult:
    scanned: int = 0
    paid: int = 0
    expired: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0


async def reconcile_stale_payments(
    session: AsyncSession,
    *,
    stale_seconds: int | None = None,
    limit: int = 200,
    dry_run: bool = False,
    commit: bool = False,
) -> PaymentReconcileResult:
    """扫描并处理超时未终态的支付单。"""
    stale_seconds = (
        stale_seconds
        if stale_seconds is not None
        else settings.payment_reconcile_stale_seconds
    )
    stale_before = utc_now() - timedelta(seconds=max(1, stale_seconds))
    result = PaymentReconcileResult()

    orders = await payment_service.list_stale_created_orders(
        session, stale_before=stale_before, limit=limit
    )
    result.scanned = len(orders)

    for order in orders:
        try:
            if dry_run:
                logger.info(
                    "payment_reconcile_dry_run",
                    order_no=order.order_no,
                    provider=order.provider,
                    amount=order.amount,
                    external_order_no=order.external_order_no,
                )
                result.skipped += 1
                continue

            before_status = order.status
            updated = await payment_service.reconcile_order(session, order)
            if commit:
                await session.commit()
            else:
                await session.flush()

            if updated.status == "paid" and before_status != "paid":
                result.paid += 1
                PAYMENT_RECONCILE_OPS.labels(action="paid").inc()
                logger.info(
                    "payment_reconcile_paid",
                    order_no=updated.order_no,
                    provider=updated.provider,
                    amount=updated.amount,
                )
            elif updated.status == "expired" and before_status != "expired":
                result.expired += 1
                PAYMENT_RECONCILE_OPS.labels(action="expired").inc()
                logger.info(
                    "payment_reconcile_expired",
                    order_no=updated.order_no,
                    provider=updated.provider,
                )
            elif updated.status == "failed" and before_status != "failed":
                result.failed += 1
                PAYMENT_RECONCILE_OPS.labels(action="failed").inc()
                logger.info(
                    "payment_reconcile_failed",
                    order_no=updated.order_no,
                    provider=updated.provider,
                )
            else:
                result.skipped += 1
                PAYMENT_RECONCILE_OPS.labels(action="skipped").inc()
        except Exception:
            if commit:
                await session.rollback()
            result.errors += 1
            PAYMENT_RECONCILE_OPS.labels(action="error").inc()
            logger.exception(
                "payment_reconcile_error",
                order_no=order.order_no,
                provider=order.provider,
            )

    logger.info(
        "payment_reconcile_finished",
        scanned=result.scanned,
        paid=result.paid,
        expired=result.expired,
        failed=result.failed,
        skipped=result.skipped,
        errors=result.errors,
        stale_seconds=stale_seconds,
        dry_run=dry_run,
    )
    return result


async def _async_main(args: argparse.Namespace) -> int:
    configure_logging(debug=settings.debug)
    session_factory = get_session_factory()
    async with session_factory() as session:
        result = await reconcile_stale_payments(
            session,
            stale_seconds=args.stale_seconds,
            limit=args.limit,
            dry_run=args.dry_run,
            commit=True,
        )
    await close_db()
    print(
        f"payment reconcile done: scanned={result.scanned} paid={result.paid} "
        f"expired={result.expired} failed={result.failed} "
        f"skipped={result.skipped} errors={result.errors}"
    )
    return 1 if result.errors else 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Reconcile stale payment orders")
    parser.add_argument(
        "--stale-seconds",
        type=int,
        default=None,
        help="Override PAYMENT_RECONCILE_STALE_SECONDS",
    )
    parser.add_argument("--limit", type=int, default=200, help="Max orders per run")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only report without mutating orders/wallets",
    )
    args = parser.parse_args(argv)
    raise SystemExit(asyncio.run(_async_main(args)))


if __name__ == "__main__":
    main()
