"""赠金批次账本 GiftLedgerService 单元测试。

覆盖：发放幂等（含并发）、撤回只退未消耗、已耗尽 409、
FIFO（含永久有效）、跨批次消耗、status 流转、账本不变式。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.constants import STATUS_ENABLED
from app.core.datetime_utils import utc_now
from app.core.errors import ConflictError, InvalidRequestError
from app.db.models.user import User
from app.db.models.wallet import GiftGrant, Wallet, WalletTransaction
from app.services.gift_service import (
    GRANT_STATUS_ACTIVE,
    GRANT_STATUS_EXPIRED,
    GRANT_STATUS_REVOKED,
    GRANT_STATUS_USED_UP,
    gift_service,
)
from app.services.wallet_service import (
    TX_GIFT_EXPIRE,
    TX_GIFT_GRANT,
    TX_GIFT_REVOKE,
    wallet_service,
)


async def _seed_user(session: AsyncSession, suffix: str = "") -> User:
    user = User(
        email=f"gift-ledger{suffix}-{uuid4().hex[:8]}@example.com",
        status=STATUS_ENABLED,
    )
    session.add(user)
    await session.flush()
    return user


async def _seed_wallet(session: AsyncSession, user: User) -> Wallet:
    return await wallet_service.get_or_create_personal_wallet(session, user.id)


async def _grant(
    session: AsyncSession,
    wallet: Wallet,
    user: User,
    *,
    amount: int,
    key: str | None = None,
    expires_at=None,
    reason: str = "promotion",
    remark: str | None = "营销活动发放",
) -> GiftGrant:
    return await gift_service.grant(
        session,
        wallet_id=wallet.id,
        amount=amount,
        reason=reason,
        expires_at=expires_at,
        idempotency_key=key or f"key-{uuid4().hex}",
        granted_by=user.id,
        remark=remark,
    )


async def _active_remaining(session: AsyncSession, wallet_id: UUID) -> int:
    stmt = select(func.coalesce(func.sum(GiftGrant.remaining), 0)).where(
        GiftGrant.wallet_id == wallet_id,
        GiftGrant.status == GRANT_STATUS_ACTIVE,
    )
    return int((await session.execute(stmt)).scalar_one())


async def _consumed_active_used_up(session: AsyncSession, wallet_id: UUID) -> int:
    stmt = select(
        func.coalesce(func.sum(GiftGrant.amount - GiftGrant.remaining), 0)
    ).where(
        GiftGrant.wallet_id == wallet_id,
        GiftGrant.status.in_((GRANT_STATUS_ACTIVE, GRANT_STATUS_USED_UP)),
    )
    return int((await session.execute(stmt)).scalar_one())


async def _tx_sum(
    session: AsyncSession, wallet_id: UUID, tx_type: str
) -> int:
    stmt = select(func.coalesce(func.sum(WalletTransaction.amount), 0)).where(
        WalletTransaction.wallet_id == wallet_id,
        WalletTransaction.type == tx_type,
    )
    return int((await session.execute(stmt)).scalar_one())


async def _assert_invariant(
    session: AsyncSession,
    wallet: Wallet,
    *,
    consumed_via_consume: int = 0,
) -> None:
    """Σ active remaining == gift_balance + gift_held；本阶段 gift_held 恒为 0。"""
    await session.refresh(wallet)
    remaining = await _active_remaining(session, wallet.id)
    assert wallet.gift_held == 0
    assert remaining == wallet.gift_balance + wallet.gift_held

    granted = await _tx_sum(session, wallet.id, TX_GIFT_GRANT)
    revoked = -await _tx_sum(session, wallet.id, TX_GIFT_REVOKE)
    expired = -await _tx_sum(session, wallet.id, TX_GIFT_EXPIRE)
    assert wallet.gift_balance == granted - revoked - expired - consumed_via_consume


# =====================================================================
# 发放与幂等
# =====================================================================


@pytest.mark.asyncio
async def test_grant_increases_gift_balance_and_writes_tx(
    db_session: AsyncSession,
) -> None:
    user = await _seed_user(db_session)
    wallet = await _seed_wallet(db_session, user)
    cash_before = wallet.balance

    grant = await _grant(db_session, wallet, user, amount=1_000)
    await db_session.refresh(wallet)

    assert grant.status == GRANT_STATUS_ACTIVE
    assert grant.remaining == 1_000
    assert grant.amount == 1_000
    assert grant.remark == "营销活动发放"
    assert wallet.gift_balance == 1_000
    assert wallet.balance == cash_before
    assert wallet.gift_held == 0

    txs = await wallet_service.get_transactions(
        db_session, wallet_id=wallet.id, tx_type=TX_GIFT_GRANT
    )
    assert len(txs) == 1
    tx = txs[0]
    assert tx.amount == 1_000
    assert tx.gift_amount == 1_000
    assert tx.gift_grant_id == grant.id
    assert tx.gift_balance_after == 1_000
    assert tx.gift_held_after == 0
    assert tx.ref_request_id == f"gift:{grant.idempotency_key}"
    await _assert_invariant(db_session, wallet)


@pytest.mark.asyncio
async def test_grant_idempotent_same_key_no_double_credit(
    db_session: AsyncSession,
) -> None:
    user = await _seed_user(db_session)
    wallet = await _seed_wallet(db_session, user)
    key = f"idem-{uuid4().hex}"

    g1 = await _grant(db_session, wallet, user, amount=500, key=key)
    g2 = await _grant(db_session, wallet, user, amount=500, key=key)
    await db_session.refresh(wallet)

    assert g1.id == g2.id
    assert wallet.gift_balance == 500
    txs = await wallet_service.get_transactions(
        db_session, wallet_id=wallet.id, tx_type=TX_GIFT_GRANT
    )
    assert len(txs) == 1
    grants = await gift_service.list_grants(db_session, wallet_id=wallet.id)
    assert len(grants) == 1
    await _assert_invariant(db_session, wallet)


@pytest.mark.asyncio
async def test_grant_amount_must_be_positive(db_session: AsyncSession) -> None:
    user = await _seed_user(db_session)
    wallet = await _seed_wallet(db_session, user)
    with pytest.raises(InvalidRequestError):
        await _grant(db_session, wallet, user, amount=0)


@pytest.mark.asyncio
async def test_grant_concurrent_same_key_one_batch() -> None:
    """两独立连接并发重复提交同一幂等键：只产生一个批次、一条流水。"""
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    user_id = None
    wallet_id = None
    key = f"conc-{uuid4().hex}"
    try:
        async with factory() as session:
            async with session.begin():
                user = await _seed_user(session, "-conc")
                wallet = await _seed_wallet(session, user)
                user_id = user.id
                wallet_id = wallet.id

        async def _grant_once() -> GiftGrant:
            async with factory() as s:
                async with s.begin():
                    return await gift_service.grant(
                        s,
                        wallet_id=wallet_id,
                        amount=2_000,
                        reason="compensation",
                        expires_at=None,
                        idempotency_key=key,
                        granted_by=user_id,
                        remark="并发幂等",
                    )

        g1, g2 = await asyncio.gather(_grant_once(), _grant_once())
        assert g1.id == g2.id

        async with factory() as session:
            async with session.begin():
                wallet = await session.get(Wallet, wallet_id)
                assert wallet is not None
                assert wallet.gift_balance == 2_000
                n_grants = (
                    await session.execute(
                        select(func.count(GiftGrant.id)).where(
                            GiftGrant.wallet_id == wallet_id
                        )
                    )
                ).scalar_one()
                n_txs = (
                    await session.execute(
                        select(func.count(WalletTransaction.id)).where(
                            WalletTransaction.wallet_id == wallet_id,
                            WalletTransaction.type == TX_GIFT_GRANT,
                        )
                    )
                ).scalar_one()
                assert n_grants == 1
                assert n_txs == 1
                await _assert_invariant(session, wallet)
    finally:
        async with factory() as cleanup:
            async with cleanup.begin():
                if wallet_id is not None:
                    await cleanup.execute(
                        text("DELETE FROM wallet_transactions WHERE wallet_id = :id"),
                        {"id": wallet_id},
                    )
                    await cleanup.execute(
                        text("DELETE FROM gift_grants WHERE wallet_id = :id"),
                        {"id": wallet_id},
                    )
                    await cleanup.execute(
                        text("DELETE FROM wallets WHERE id = :id"),
                        {"id": wallet_id},
                    )
                if user_id is not None:
                    await cleanup.execute(
                        text("DELETE FROM users WHERE id = :id"),
                        {"id": user_id},
                    )
        await engine.dispose()


# =====================================================================
# 撤回 / 过期
# =====================================================================


@pytest.mark.asyncio
async def test_revoke_only_returns_remaining(
    db_session: AsyncSession,
) -> None:
    """撤回只退未消耗部分，已消耗不追回。"""
    user = await _seed_user(db_session)
    wallet = await _seed_wallet(db_session, user)
    grant = await _grant(db_session, wallet, user, amount=100)

    alloc = await gift_service.consume(db_session, wallet=wallet, amount=40)
    assert alloc == [(grant.id, 40)]
    await db_session.refresh(wallet)
    assert wallet.gift_balance == 100  # consume 不改 gift_balance
    wallet.gift_balance -= 40
    await db_session.flush()

    revoked = await gift_service.revoke(
        db_session, grant_id=grant.id, revoked_by=user.id, remark="撤回剩余"
    )
    await db_session.refresh(wallet)
    await db_session.refresh(grant)

    assert revoked.status == GRANT_STATUS_REVOKED
    assert revoked.remaining == 0
    assert revoked.amount == 100
    assert revoked.revoked_by == user.id
    assert revoked.revoked_at is not None
    assert wallet.gift_balance == 0

    txs = await wallet_service.get_transactions(
        db_session, wallet_id=wallet.id, tx_type=TX_GIFT_REVOKE
    )
    assert len(txs) == 1
    assert txs[0].amount == -60
    assert txs[0].gift_amount == -60
    assert txs[0].ref_request_id == f"gift_revoke:{grant.id}"
    await _assert_invariant(db_session, wallet, consumed_via_consume=40)


@pytest.mark.asyncio
async def test_revoke_used_up_returns_409(db_session: AsyncSession) -> None:
    user = await _seed_user(db_session)
    wallet = await _seed_wallet(db_session, user)
    grant = await _grant(db_session, wallet, user, amount=50)
    await gift_service.consume(db_session, wallet=wallet, amount=50)
    wallet.gift_balance -= 50
    await db_session.flush()
    await db_session.refresh(grant)
    assert grant.status == GRANT_STATUS_USED_UP

    with pytest.raises(ConflictError) as exc_info:
        await gift_service.revoke(
            db_session, grant_id=grant.id, revoked_by=user.id, remark=None
        )
    assert exc_info.value.status_code == 409
    await db_session.refresh(wallet)
    assert wallet.gift_balance == 0
    await _assert_invariant(db_session, wallet, consumed_via_consume=50)


@pytest.mark.asyncio
async def test_expire_reduces_gift_balance_and_is_idempotent(
    db_session: AsyncSession,
) -> None:
    user = await _seed_user(db_session)
    wallet = await _seed_wallet(db_session, user)
    past = utc_now() - timedelta(days=1)
    grant = await _grant(db_session, wallet, user, amount=80, expires_at=past)

    expired = await gift_service.expire(db_session, grant_id=grant.id)
    await db_session.refresh(wallet)
    assert expired.status == GRANT_STATUS_EXPIRED
    assert expired.remaining == 0
    assert wallet.gift_balance == 0

    again = await gift_service.expire(db_session, grant_id=grant.id)
    assert again.id == expired.id
    assert again.status == GRANT_STATUS_EXPIRED
    txs = await wallet_service.get_transactions(
        db_session, wallet_id=wallet.id, tx_type=TX_GIFT_EXPIRE
    )
    assert len(txs) == 1
    assert txs[0].amount == -80
    assert txs[0].ref_request_id == f"gift_expire:{grant.id}"
    await _assert_invariant(db_session, wallet)


@pytest.mark.asyncio
async def test_find_expired_grant_ids_skips_future_and_permanent(
    db_session: AsyncSession,
) -> None:
    user = await _seed_user(db_session)
    wallet = await _seed_wallet(db_session, user)
    now = utc_now()
    past = await _grant(
        db_session, wallet, user, amount=10, expires_at=now - timedelta(hours=1)
    )
    await _grant(
        db_session, wallet, user, amount=10, expires_at=now + timedelta(days=7)
    )
    await _grant(db_session, wallet, user, amount=10, expires_at=None)

    ids = await gift_service.find_expired_grant_ids(db_session, now=now, limit=10)
    assert ids == [past.id]


# =====================================================================
# FIFO 消耗
# =====================================================================


@pytest.mark.asyncio
async def test_consume_fifo_expires_at_then_permanent(
    db_session: AsyncSession,
) -> None:
    """先过期的先花，永久有效排最后。"""
    user = await _seed_user(db_session)
    wallet = await _seed_wallet(db_session, user)
    now = utc_now()
    far = await _grant(
        db_session, wallet, user, amount=30, expires_at=now + timedelta(days=30)
    )
    soon = await _grant(
        db_session, wallet, user, amount=50, expires_at=now + timedelta(days=10)
    )
    never = await _grant(db_session, wallet, user, amount=40, expires_at=None)
    await db_session.refresh(wallet)
    assert wallet.gift_balance == 120

    alloc = await gift_service.consume(db_session, wallet=wallet, amount=60)
    assert alloc == [(soon.id, 50), (far.id, 10)]

    await db_session.refresh(soon)
    await db_session.refresh(far)
    await db_session.refresh(never)
    await db_session.refresh(wallet)

    assert soon.status == GRANT_STATUS_USED_UP
    assert soon.remaining == 0
    assert far.status == GRANT_STATUS_ACTIVE
    assert far.remaining == 20
    assert never.status == GRANT_STATUS_ACTIVE
    assert never.remaining == 40
    assert wallet.gift_balance == 120  # consume 不改余额

    wallet.gift_balance -= 60
    await db_session.flush()
    assert await _consumed_active_used_up(db_session, wallet.id) == 60
    await _assert_invariant(db_session, wallet, consumed_via_consume=60)


@pytest.mark.asyncio
async def test_consume_cross_batch_then_used_up(
    db_session: AsyncSession,
) -> None:
    user = await _seed_user(db_session)
    wallet = await _seed_wallet(db_session, user)
    now = utc_now()
    g1 = await _grant(
        db_session, wallet, user, amount=30, expires_at=now + timedelta(days=1)
    )
    g2 = await _grant(
        db_session, wallet, user, amount=50, expires_at=now + timedelta(days=2)
    )
    g3 = await _grant(
        db_session, wallet, user, amount=40, expires_at=now + timedelta(days=3)
    )

    alloc = await gift_service.consume(db_session, wallet=wallet, amount=120)
    assert alloc == [(g1.id, 30), (g2.id, 50), (g3.id, 40)]
    for g in (g1, g2, g3):
        await db_session.refresh(g)
        assert g.status == GRANT_STATUS_USED_UP
        assert g.remaining == 0

    wallet.gift_balance -= 120
    await db_session.flush()
    assert await _consumed_active_used_up(db_session, wallet.id) == 120
    await _assert_invariant(db_session, wallet, consumed_via_consume=120)


@pytest.mark.asyncio
async def test_consume_shortfall_raises_without_partial_commit_intent(
    db_session: AsyncSession,
) -> None:
    user = await _seed_user(db_session)
    wallet = await _seed_wallet(db_session, user)
    grant = await _grant(db_session, wallet, user, amount=10)

    with pytest.raises(ConflictError, match="Gift ledger mismatch"):
        await gift_service.consume(db_session, wallet=wallet, amount=11)

    await db_session.refresh(grant)
    await db_session.refresh(wallet)
    assert grant.remaining == 10
    assert grant.status == GRANT_STATUS_ACTIVE
    assert wallet.gift_balance == 10
    await _assert_invariant(db_session, wallet)


@pytest.mark.asyncio
async def test_list_grants_status_filter(db_session: AsyncSession) -> None:
    user = await _seed_user(db_session)
    wallet = await _seed_wallet(db_session, user)
    a = await _grant(db_session, wallet, user, amount=10)
    b = await _grant(db_session, wallet, user, amount=20)
    await gift_service.revoke(
        db_session, grant_id=b.id, revoked_by=user.id, remark=None
    )

    all_rows = await gift_service.list_grants(db_session, wallet_id=wallet.id)
    assert {g.id for g in all_rows} == {a.id, b.id}
    active = await gift_service.list_grants(
        db_session, wallet_id=wallet.id, status=GRANT_STATUS_ACTIVE
    )
    assert [g.id for g in active] == [a.id]
    revoked = await gift_service.list_grants(
        db_session, wallet_id=wallet.id, status=GRANT_STATUS_REVOKED
    )
    assert [g.id for g in revoked] == [b.id]
