"""赠金到期回收任务。

MVP：可作为 CLI 或 cron 调用，不依赖完整任务队列。

扫描：status=active AND remaining>0 AND expires_at<=now。
回收：gift_service.expire()（写 gift_expire 流水，remaining=0，status=expired）。
跳过：钱包 gift_held>0（有请求正在进行）则整钱包本轮不收，下一轮再试。

幂等：ref_request_id = gift_expire:{grant_id}，重复执行不重复扣减。
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.logging import configure_logging, get_logger
from app.core.metrics import GIFT_OPS
from app.db.models.wallet import GiftGrant
from app.db.session import close_db, get_session_factory
from app.services.gift_service import gift_service
from app.services.wallet_service import wallet_service

logger = get_logger(__name__)


@dataclass
class ExpireGiftsResult:
    scanned: int = 0
    expired: int = 0
    skipped: int = 0
    wallets_skipped: int = 0
    errors: int = 0


async def expire_due_gifts(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = 500,
    dry_run: bool = False,
    commit: bool = False,
) -> ExpireGiftsResult:
    """扫描并回收已到期赠金批次。

    commit=False（默认）时仅 flush，由调用方控制事务；
    CLI 场景可传 commit=True，按钱包提交以便错误隔离。
    """
    now = now if now is not None else utc_now()
    result = ExpireGiftsResult()

    grant_ids = await gift_service.find_expired_grant_ids(
        session, now=now, limit=limit
    )
    result.scanned = len(grant_ids)
    if not grant_ids:
        logger.info("expire_gifts_finished", scanned=0, dry_run=dry_run)
        return result

    grants = await _load_grants(session, grant_ids)
    by_wallet: dict[UUID, list[GiftGrant]] = defaultdict(list)
    for grant in grants:
        by_wallet[grant.wallet_id].append(grant)

    for wallet_id, wallet_grants in by_wallet.items():
        try:
            wallet = await wallet_service._get_wallet_for_update(session, wallet_id)
            # ponytail: 有进行中 hold 时整钱包跳过。上限是过期最长延迟一个任务周期；
            # 升级路径是 hold 阶段就核销批次 remaining，过期回收便不再与冻结抢钱。
            if wallet.gift_held > 0:
                result.wallets_skipped += 1
                result.skipped += len(wallet_grants)
                GIFT_OPS.labels(op="expire", result="skipped").inc(len(wallet_grants))
                logger.info(
                    "expire_gifts_wallet_skipped",
                    wallet_id=str(wallet_id),
                    gift_held=wallet.gift_held,
                    grants=len(wallet_grants),
                )
                continue

            if dry_run:
                for grant in wallet_grants:
                    logger.info(
                        "expire_gifts_dry_run",
                        grant_id=str(grant.id),
                        wallet_id=str(wallet_id),
                        remaining=grant.remaining,
                    )
                result.skipped += len(wallet_grants)
                continue

            for grant in wallet_grants:
                remaining = int(grant.remaining)
                await gift_service.expire(session, grant_id=grant.id)
                result.expired += 1
                logger.info(
                    "expire_gifts_expired",
                    grant_id=str(grant.id),
                    wallet_id=str(wallet_id),
                    remaining=remaining,
                )

            if commit:
                await session.commit()
            else:
                await session.flush()
        except Exception:
            if commit:
                await session.rollback()
            result.errors += 1
            logger.exception(
                "expire_gifts_error",
                wallet_id=str(wallet_id),
            )

    logger.info(
        "expire_gifts_finished",
        scanned=result.scanned,
        expired=result.expired,
        skipped=result.skipped,
        wallets_skipped=result.wallets_skipped,
        errors=result.errors,
        dry_run=dry_run,
    )
    return result


async def _load_grants(
    session: AsyncSession, grant_ids: list[UUID]
) -> list[GiftGrant]:
    """按扫描顺序加载批次，丢掉扫描后已消失的行。"""
    rows = (
        await session.execute(select(GiftGrant).where(GiftGrant.id.in_(grant_ids)))
    ).scalars().all()
    by_id = {g.id: g for g in rows}
    return [by_id[gid] for gid in grant_ids if gid in by_id]


async def _async_main(args: argparse.Namespace) -> int:
    configure_logging(debug=settings.debug)
    session_factory = get_session_factory()
    async with session_factory() as session:
        result = await expire_due_gifts(
            session,
            limit=args.limit,
            dry_run=args.dry_run,
            commit=not args.dry_run,
        )
    await close_db()
    print(
        f"expire gifts done: scanned={result.scanned} expired={result.expired} "
        f"skipped={result.skipped} wallets_skipped={result.wallets_skipped} "
        f"errors={result.errors} dry_run={args.dry_run}"
    )
    return 1 if result.errors else 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Expire due gift grants")
    parser.add_argument("--limit", type=int, default=500, help="Max grants per run")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only report actions without mutating wallets or grants",
    )
    args = parser.parse_args(argv)
    raise SystemExit(asyncio.run(_async_main(args)))


if __name__ == "__main__":
    main()
