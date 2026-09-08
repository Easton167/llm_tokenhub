"""赠金账本每日对账任务。

只告警与输出报告，不自动修数（资金问题必须人工确认）。

校验：
  1. 按钱包：Σ(status=active 批次 remaining) == gift_balance + gift_held
  2. 全平台：Σ usage_logs.gift_charge_amount == Σ capture 流水的 -gift_amount

并刷新 GIFT_BALANCE_TOTAL（全平台未消耗赠金敞口 = Σ(gift_balance + gift_held)）。
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.core.metrics import GIFT_BALANCE_TOTAL
from app.db.models.log import UsageLog
from app.db.models.wallet import GiftGrant, Wallet, WalletTransaction
from app.db.session import close_db, get_session_factory
from app.services.gift_service import GRANT_STATUS_ACTIVE
from app.services.wallet_service import TX_CAPTURE

logger = get_logger(__name__)


@dataclass
class GiftWalletMismatch:
    wallet_id: UUID
    grants_remaining: int
    wallet_gift_total: int
    delta: int


@dataclass
class GiftReconcileResult:
    wallets_scanned: int = 0
    wallet_mismatches: list[GiftWalletMismatch] = field(default_factory=list)
    usage_gift_total: int = 0
    capture_gift_total: int = 0
    gift_split_matched: bool = True
    gift_balance_total: int = 0

    @property
    def ok(self) -> bool:
        return not self.wallet_mismatches and self.gift_split_matched


async def reconcile_gifts(session: AsyncSession) -> GiftReconcileResult:
    """校验赠金账本与用量拆分；不一致只记录，不改任何金额。"""
    result = GiftReconcileResult()

    result.wallets_scanned = int(
        await session.scalar(select(func.count(Wallet.id))) or 0
    )
    result.gift_balance_total = int(
        await session.scalar(
            select(
                func.coalesce(func.sum(Wallet.gift_balance + Wallet.gift_held), 0)
            )
        )
        or 0
    )
    GIFT_BALANCE_TOTAL.set(result.gift_balance_total)

    result.wallet_mismatches = await _find_wallet_mismatches(session)
    result.usage_gift_total = int(
        await session.scalar(
            select(func.coalesce(func.sum(UsageLog.gift_charge_amount), 0))
        )
        or 0
    )
    result.capture_gift_total = int(
        await session.scalar(
            select(func.coalesce(func.sum(-WalletTransaction.gift_amount), 0)).where(
                WalletTransaction.type == TX_CAPTURE,
            )
        )
        or 0
    )
    result.gift_split_matched = result.usage_gift_total == result.capture_gift_total

    if result.wallet_mismatches:
        for item in result.wallet_mismatches:
            logger.error(
                "gift_ledger_wallet_mismatch",
                wallet_id=str(item.wallet_id),
                grants_remaining=item.grants_remaining,
                wallet_gift_total=item.wallet_gift_total,
                delta=item.delta,
            )
    if not result.gift_split_matched:
        logger.error(
            "gift_split_mismatch",
            usage_gift_total=result.usage_gift_total,
            capture_gift_total=result.capture_gift_total,
            delta=result.usage_gift_total - result.capture_gift_total,
        )

    logger.info(
        "gift_reconcile_finished",
        wallets_scanned=result.wallets_scanned,
        mismatches=len(result.wallet_mismatches),
        usage_gift_total=result.usage_gift_total,
        capture_gift_total=result.capture_gift_total,
        gift_split_matched=result.gift_split_matched,
        gift_balance_total=result.gift_balance_total,
        ok=result.ok,
    )
    return result


async def _find_wallet_mismatches(
    session: AsyncSession,
) -> list[GiftWalletMismatch]:
    remaining_sq = (
        select(
            GiftGrant.wallet_id.label("wallet_id"),
            func.coalesce(func.sum(GiftGrant.remaining), 0).label("remaining"),
        )
        .where(GiftGrant.status == GRANT_STATUS_ACTIVE)
        .group_by(GiftGrant.wallet_id)
        .subquery()
    )
    stmt = (
        select(
            Wallet.id,
            Wallet.gift_balance,
            Wallet.gift_held,
            func.coalesce(remaining_sq.c.remaining, 0).label("grants_remaining"),
        )
        .outerjoin(remaining_sq, remaining_sq.c.wallet_id == Wallet.id)
        .where(
            func.coalesce(remaining_sq.c.remaining, 0)
            != (Wallet.gift_balance + Wallet.gift_held)
        )
    )
    rows = (await session.execute(stmt)).all()
    mismatches: list[GiftWalletMismatch] = []
    for wallet_id, gift_balance, gift_held, grants_remaining in rows:
        wallet_gift_total = int(gift_balance) + int(gift_held)
        remaining = int(grants_remaining)
        mismatches.append(
            GiftWalletMismatch(
                wallet_id=wallet_id,
                grants_remaining=remaining,
                wallet_gift_total=wallet_gift_total,
                delta=remaining - wallet_gift_total,
            )
        )
    return mismatches


async def _async_main(_args: argparse.Namespace) -> int:
    configure_logging(debug=settings.debug)
    session_factory = get_session_factory()
    async with session_factory() as session:
        result = await reconcile_gifts(session)
    await close_db()
    print(
        f"gift reconcile done: wallets_scanned={result.wallets_scanned} "
        f"mismatches={len(result.wallet_mismatches)} "
        f"usage_gift={result.usage_gift_total} capture_gift={result.capture_gift_total} "
        f"gift_split_matched={result.gift_split_matched} "
        f"gift_balance_total={result.gift_balance_total} ok={result.ok}"
    )
    for item in result.wallet_mismatches:
        print(
            f"  wallet mismatch: wallet_id={item.wallet_id} "
            f"grants_remaining={item.grants_remaining} "
            f"wallet_gift_total={item.wallet_gift_total} delta={item.delta}"
        )
    return 0 if result.ok else 1


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Reconcile gift ledger (report only, never auto-fix)"
    )
    args = parser.parse_args(argv)
    raise SystemExit(asyncio.run(_async_main(args)))


if __name__ == "__main__":
    main()
