"""WalletService 双资金池 hold/capture/release 测试。

覆盖：纯现金回归、纯赠金、PRD 跨池示例、等价性参数化、
失败释放、幂等、并发、批次核销与账本不变式。
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
from app.core.errors import InsufficientBalanceError
from app.db.models.user import User
from app.db.models.wallet import GiftGrant, Wallet, WalletTransaction
from app.services.gift_service import GRANT_STATUS_ACTIVE, gift_service
from app.services.wallet_service import TX_CAPTURE, TX_HOLD, TX_RELEASE, wallet_service


async def _seed_user(session: AsyncSession, suffix: str = "") -> User:
    user = User(
        email=f"gift-bill{suffix}-{uuid4().hex[:8]}@example.com",
        status=STATUS_ENABLED,
    )
    session.add(user)
    await session.flush()
    return user


async def _seed_wallet(
    session: AsyncSession,
    user: User,
    *,
    cash: int = 0,
) -> Wallet:
    wallet = await wallet_service.get_or_create_personal_wallet(session, user.id)
    if cash > 0:
        wallet.balance = cash
        await session.flush()
    return wallet


async def _grant(
    session: AsyncSession,
    wallet: Wallet,
    user: User,
    amount: int,
    *,
    key: str | None = None,
    expires_at=None,
) -> GiftGrant:
    return await gift_service.grant(
        session,
        wallet_id=wallet.id,
        amount=amount,
        reason="promotion",
        expires_at=expires_at,
        idempotency_key=key or f"key-{uuid4().hex}",
        granted_by=user.id,
        remark="测试赠金",
    )


async def _active_remaining(session: AsyncSession, wallet_id: UUID) -> int:
    stmt = select(func.coalesce(func.sum(GiftGrant.remaining), 0)).where(
        GiftGrant.wallet_id == wallet_id,
        GiftGrant.status == GRANT_STATUS_ACTIVE,
    )
    return int((await session.execute(stmt)).scalar_one())


async def _assert_pools_non_negative(wallet: Wallet) -> None:
    assert wallet.gift_balance >= 0
    assert wallet.gift_held >= 0
    assert wallet.balance >= 0
    assert wallet.held >= 0


async def _assert_invariant(session: AsyncSession, wallet: Wallet) -> None:
    """Σ(active 批次 remaining) == gift_balance + gift_held。"""
    await session.refresh(wallet)
    remaining = await _active_remaining(session, wallet.id)
    assert remaining == wallet.gift_balance + wallet.gift_held
    await _assert_pools_non_negative(wallet)


def _direct_priority_charge(
    gift: int, cash: int, actual: int
) -> tuple[int, int, int, int]:
    """直接按 赠送→现金 扣 actual，返回终态 (gift_balance, balance, gift_charge, cash_charge)。"""
    take = actual if actual < gift + cash else gift + cash
    gift_charge = gift if gift < take else take
    cash_charge = take - gift_charge
    return gift - gift_charge, cash - cash_charge, gift_charge, cash_charge


# =====================================================================
# 纯现金回归：gift_balance=0 时行为与改造前一致
# =====================================================================


@pytest.mark.asyncio
async def test_cash_only_hold_capture_release_matches_legacy(
    db_session: AsyncSession,
) -> None:
    user = await _seed_user(db_session, "-cash")
    wallet = await _seed_wallet(db_session, user, cash=10_000)
    assert wallet.gift_balance == 0
    assert wallet.gift_held == 0

    req_capture = f"req-cash-cap-{uuid4().hex}"
    await wallet_service.hold(
        db_session, wallet_id=wallet.id, amount=3_000, request_id=req_capture
    )
    await db_session.refresh(wallet)
    assert wallet.balance == 7_000
    assert wallet.held == 3_000
    assert wallet.gift_balance == 0
    assert wallet.gift_held == 0

    hold_txs = await wallet_service.get_transactions(
        db_session, wallet_id=wallet.id, tx_type=TX_HOLD, ref_request_id=req_capture
    )
    assert len(hold_txs) == 1
    assert hold_txs[0].amount == -3_000
    assert hold_txs[0].gift_amount == 0

    updated = await wallet_service.capture(
        db_session,
        wallet_id=wallet.id,
        hold_amount=3_000,
        actual_amount=1_000,
        request_id=req_capture,
    )
    assert updated.balance == 9_000
    assert updated.held == 0
    assert updated.gift_balance == 0
    assert updated.gift_held == 0

    cap_txs = await wallet_service.get_transactions(
        db_session, wallet_id=wallet.id, tx_type=TX_CAPTURE, ref_request_id=req_capture
    )
    rel_txs = await wallet_service.get_transactions(
        db_session, wallet_id=wallet.id, tx_type=TX_RELEASE, ref_request_id=req_capture
    )
    assert len(cap_txs) == 1
    assert cap_txs[0].amount == -1_000
    assert cap_txs[0].gift_amount == 0
    assert len(rel_txs) == 1
    assert rel_txs[0].amount == 2_000
    assert rel_txs[0].gift_amount == 0

    req_release = f"req-cash-rel-{uuid4().hex}"
    await wallet_service.hold(
        db_session, wallet_id=wallet.id, amount=2_000, request_id=req_release
    )
    released = await wallet_service.release(
        db_session, wallet_id=wallet.id, hold_amount=2_000, request_id=req_release
    )
    assert released.balance == 9_000
    assert released.held == 0
    await _assert_invariant(db_session, wallet)

    split = await wallet_service.get_charge_split(
        db_session, wallet_id=wallet.id, request_id=req_capture
    )
    assert split == (0, 1_000)


@pytest.mark.asyncio
async def test_cash_only_hold_insufficient_unchanged(db_session: AsyncSession) -> None:
    user = await _seed_user(db_session, "-cash-fail")
    wallet = await _seed_wallet(db_session, user, cash=1_000)

    with pytest.raises(InsufficientBalanceError) as exc_info:
        await wallet_service.hold(
            db_session,
            wallet_id=wallet.id,
            amount=2_000,
            request_id=f"req-{uuid4().hex}",
        )
    assert exc_info.value.code == "insufficient_balance"
    assert "gift_balance=0" in exc_info.value.message
    assert "balance=1000" in exc_info.value.message

    await db_session.refresh(wallet)
    assert wallet.balance == 1_000
    assert wallet.held == 0
    await _assert_invariant(db_session, wallet)


@pytest.mark.asyncio
async def test_gift_covers_hold_when_cash_is_zero(db_session: AsyncSession) -> None:
    """现金为 0 但赠金充足时 hold 应成功（改造前会 402）。"""
    user = await _seed_user(db_session, "-gift-covers")
    wallet = await _seed_wallet(db_session, user, cash=0)
    await _grant(db_session, wallet, user, 2_000)

    await wallet_service.hold(
        db_session, wallet_id=wallet.id, amount=1_500, request_id=f"req-{uuid4().hex}"
    )
    await db_session.refresh(wallet)
    assert wallet.gift_balance == 500
    assert wallet.gift_held == 1_500
    assert wallet.balance == 0
    assert wallet.held == 0
    await _assert_invariant(db_session, wallet)


# =====================================================================
# 纯赠金
# =====================================================================


@pytest.mark.asyncio
async def test_gift_only_hold_capture_leaves_cash_untouched(
    db_session: AsyncSession,
) -> None:
    user = await _seed_user(db_session, "-gift-only")
    wallet = await _seed_wallet(db_session, user, cash=5_000)
    cash_before = wallet.balance
    await _grant(db_session, wallet, user, 200)

    request_id = f"req-gift-only-{uuid4().hex}"
    await wallet_service.hold(
        db_session, wallet_id=wallet.id, amount=80, request_id=request_id
    )
    await db_session.refresh(wallet)
    assert wallet.gift_balance == 120
    assert wallet.gift_held == 80
    assert wallet.balance == cash_before
    assert wallet.held == 0

    updated = await wallet_service.capture(
        db_session,
        wallet_id=wallet.id,
        hold_amount=80,
        actual_amount=50,
        request_id=request_id,
    )
    assert updated.gift_balance == 150
    assert updated.gift_held == 0
    assert updated.balance == cash_before
    assert updated.held == 0

    cap_txs = await wallet_service.get_transactions(
        db_session, wallet_id=wallet.id, tx_type=TX_CAPTURE, ref_request_id=request_id
    )
    assert cap_txs[0].amount == -50
    assert cap_txs[0].gift_amount == -50
    assert cap_txs[0].gift_grant_id is not None

    split = await wallet_service.get_charge_split(
        db_session, wallet_id=wallet.id, request_id=request_id
    )
    assert split == (50, 0)
    await _assert_invariant(db_session, wallet)


# =====================================================================
# PRD 跨池示例
# =====================================================================


@pytest.mark.asyncio
async def test_prd_cross_pool_example(db_session: AsyncSession) -> None:
    """赠送池 30、现金池 1000，预扣 100 → 实扣 60：gift 实扣 30、cash 实扣 30。"""
    user = await _seed_user(db_session, "-prd")
    wallet = await _seed_wallet(db_session, user, cash=1_000)
    await _grant(db_session, wallet, user, 30)

    request_id = f"req-prd-{uuid4().hex}"
    await wallet_service.hold(
        db_session, wallet_id=wallet.id, amount=100, request_id=request_id
    )
    await db_session.refresh(wallet)
    assert wallet.gift_balance == 0
    assert wallet.gift_held == 30
    assert wallet.balance == 930
    assert wallet.held == 70
    await _assert_invariant(db_session, wallet)

    hold_txs = await wallet_service.get_transactions(
        db_session, wallet_id=wallet.id, tx_type=TX_HOLD, ref_request_id=request_id
    )
    assert hold_txs[0].amount == -100
    assert hold_txs[0].gift_amount == -30

    updated = await wallet_service.capture(
        db_session,
        wallet_id=wallet.id,
        hold_amount=100,
        actual_amount=60,
        request_id=request_id,
    )
    assert updated.gift_balance == 0
    assert updated.gift_held == 0
    assert updated.balance == 970
    assert updated.held == 0

    cap_txs = await wallet_service.get_transactions(
        db_session, wallet_id=wallet.id, tx_type=TX_CAPTURE, ref_request_id=request_id
    )
    rel_txs = await wallet_service.get_transactions(
        db_session, wallet_id=wallet.id, tx_type=TX_RELEASE, ref_request_id=request_id
    )
    assert cap_txs[0].amount == -60
    assert cap_txs[0].gift_amount == -30
    assert rel_txs[0].amount == 40
    assert rel_txs[0].gift_amount == 0

    split = await wallet_service.get_charge_split(
        db_session, wallet_id=wallet.id, request_id=request_id
    )
    assert split == (30, 30)
    await _assert_invariant(db_session, wallet)


# =====================================================================
# 等价性：hold+capture 终态 == 直接按优先级扣 actual
# =====================================================================


@pytest.mark.parametrize(
    "gift,cash,hold_amount,actual",
    [
        (0, 1000, 100, 0),
        (0, 1000, 100, 100),
        (0, 1000, 100, 110),
        (30, 1000, 100, 0),
        (30, 1000, 100, 60),
        (30, 1000, 100, 100),
        (30, 1000, 100, 110),
        (100, 1000, 50, 50),
        (50, 0, 50, 30),
        (50, 0, 50, 50),
        (10, 10, 20, 25),
        (80, 20, 50, 40),
    ],
)
@pytest.mark.asyncio
async def test_hold_capture_equals_direct_priority_charge(
    db_session: AsyncSession,
    gift: int,
    cash: int,
    hold_amount: int,
    actual: int,
) -> None:
    user = await _seed_user(db_session, f"-eq-{gift}-{cash}-{actual}")
    wallet = await _seed_wallet(db_session, user, cash=cash)
    if gift > 0:
        await _grant(db_session, wallet, user, gift)

    request_id = f"req-eq-{uuid4().hex}"
    await wallet_service.hold(
        db_session, wallet_id=wallet.id, amount=hold_amount, request_id=request_id
    )
    await wallet_service.capture(
        db_session,
        wallet_id=wallet.id,
        hold_amount=hold_amount,
        actual_amount=actual,
        request_id=request_id,
    )
    await db_session.refresh(wallet)

    exp_gift, exp_cash, exp_gift_charge, exp_cash_charge = _direct_priority_charge(
        gift, cash, actual
    )
    assert wallet.gift_balance == exp_gift
    assert wallet.balance == exp_cash
    assert wallet.gift_held == 0
    assert wallet.held == 0

    split = await wallet_service.get_charge_split(
        db_session, wallet_id=wallet.id, request_id=request_id
    )
    assert split == (exp_gift_charge, exp_cash_charge)
    await _assert_invariant(db_session, wallet)


# =====================================================================
# 失败释放 / 幂等 / 无 hold 退化 / get_charge_split
# =====================================================================


@pytest.mark.asyncio
async def test_release_restores_both_pools_exactly(db_session: AsyncSession) -> None:
    user = await _seed_user(db_session, "-rel")
    wallet = await _seed_wallet(db_session, user, cash=1_000)
    await _grant(db_session, wallet, user, 30)

    request_id = f"req-rel-{uuid4().hex}"
    await wallet_service.hold(
        db_session, wallet_id=wallet.id, amount=100, request_id=request_id
    )
    released = await wallet_service.release(
        db_session, wallet_id=wallet.id, hold_amount=100, request_id=request_id
    )
    assert released.gift_balance == 30
    assert released.gift_held == 0
    assert released.balance == 1_000
    assert released.held == 0

    rel_txs = await wallet_service.get_transactions(
        db_session, wallet_id=wallet.id, tx_type=TX_RELEASE, ref_request_id=request_id
    )
    assert rel_txs[0].amount == 100
    assert rel_txs[0].gift_amount == 30
    await _assert_invariant(db_session, wallet)


@pytest.mark.asyncio
async def test_capture_and_release_idempotent_no_double_move(
    db_session: AsyncSession,
) -> None:
    user = await _seed_user(db_session, "-idem")
    wallet = await _seed_wallet(db_session, user, cash=1_000)
    await _grant(db_session, wallet, user, 30)

    req_cap = f"req-idem-cap-{uuid4().hex}"
    await wallet_service.hold(
        db_session, wallet_id=wallet.id, amount=100, request_id=req_cap
    )
    await wallet_service.capture(
        db_session,
        wallet_id=wallet.id,
        hold_amount=100,
        actual_amount=60,
        request_id=req_cap,
    )
    snap = await db_session.get(Wallet, wallet.id)
    assert snap is not None
    gift_after, cash_after = snap.gift_balance, snap.balance

    await wallet_service.capture(
        db_session,
        wallet_id=wallet.id,
        hold_amount=100,
        actual_amount=60,
        request_id=req_cap,
    )
    snap2 = await db_session.get(Wallet, wallet.id)
    assert snap2 is not None
    assert snap2.gift_balance == gift_after
    assert snap2.balance == cash_after
    assert snap2.gift_held == 0
    assert snap2.held == 0

    cap_txs = await wallet_service.get_transactions(
        db_session, wallet_id=wallet.id, tx_type=TX_CAPTURE, ref_request_id=req_cap
    )
    assert len(cap_txs) == 1

    req_rel = f"req-idem-rel-{uuid4().hex}"
    await wallet_service.hold(
        db_session, wallet_id=wallet.id, amount=50, request_id=req_rel
    )
    await wallet_service.release(
        db_session, wallet_id=wallet.id, hold_amount=50, request_id=req_rel
    )
    after_rel = await db_session.get(Wallet, wallet.id)
    assert after_rel is not None
    await wallet_service.release(
        db_session, wallet_id=wallet.id, hold_amount=50, request_id=req_rel
    )
    after_rel2 = await db_session.get(Wallet, wallet.id)
    assert after_rel2 is not None
    assert after_rel2.gift_balance == after_rel.gift_balance
    assert after_rel2.balance == after_rel.balance
    await _assert_invariant(db_session, wallet)


@pytest.mark.asyncio
async def test_capture_without_hold_degrades_to_cash(db_session: AsyncSession) -> None:
    user = await _seed_user(db_session, "-nohold")
    wallet = await _seed_wallet(db_session, user, cash=1_000)
    await _grant(db_session, wallet, user, 30)
    request_id = f"req-nohold-{uuid4().hex}"

    updated = await wallet_service.capture(
        db_session,
        wallet_id=wallet.id,
        hold_amount=0,
        actual_amount=0,
        request_id=request_id,
    )
    assert updated.gift_balance == 30
    assert updated.balance == 1_000
    split = await wallet_service.get_charge_split(
        db_session, wallet_id=wallet.id, request_id=request_id
    )
    assert split == (0, 0)
    missing = await wallet_service.get_charge_split(
        db_session, wallet_id=wallet.id, request_id="never-seen"
    )
    assert missing == (0, 0)
    await _assert_invariant(db_session, wallet)


@pytest.mark.asyncio
async def test_get_or_create_initializes_gift_pools_zero(
    db_session: AsyncSession,
) -> None:
    user = await _seed_user(db_session, "-init")
    wallet = await wallet_service.get_or_create_personal_wallet(db_session, user.id)
    assert wallet.gift_balance == 0
    assert wallet.gift_held == 0


# =====================================================================
# 批次核销
# =====================================================================


@pytest.mark.asyncio
async def test_capture_consumes_grants_fifo_and_keeps_invariant(
    db_session: AsyncSession,
) -> None:
    user = await _seed_user(db_session, "-fifo")
    wallet = await _seed_wallet(db_session, user, cash=1_000)
    now = utc_now()
    # 显式不同到期时间，避免同事务内 created_at=now() 相同导致 FIFO 顺序不稳定
    g1 = await _grant(
        db_session, wallet, user, 20, key="g1", expires_at=now + timedelta(days=1)
    )
    g2 = await _grant(
        db_session, wallet, user, 30, key="g2", expires_at=now + timedelta(days=2)
    )

    request_id = f"req-fifo-{uuid4().hex}"
    await wallet_service.hold(
        db_session, wallet_id=wallet.id, amount=100, request_id=request_id
    )
    await wallet_service.capture(
        db_session,
        wallet_id=wallet.id,
        hold_amount=100,
        actual_amount=45,
        request_id=request_id,
    )
    await db_session.refresh(g1)
    await db_session.refresh(g2)
    # 实扣 45：赠金优先 50 中扣 45，g1 20 用尽、g2 扣 25 剩 5
    assert g1.remaining == 0
    assert g1.status == "used_up"
    assert g2.remaining == 5
    assert g2.status == GRANT_STATUS_ACTIVE

    await db_session.refresh(wallet)
    assert wallet.gift_balance == 5
    assert wallet.gift_held == 0
    await _assert_invariant(db_session, wallet)

    cap_txs = await wallet_service.get_transactions(
        db_session, wallet_id=wallet.id, tx_type=TX_CAPTURE, ref_request_id=request_id
    )
    assert cap_txs[0].gift_grant_id == g1.id
    assert cap_txs[0].gift_amount == -45


@pytest.mark.asyncio
async def test_release_does_not_consume_grants(db_session: AsyncSession) -> None:
    user = await _seed_user(db_session, "-rel-grant")
    wallet = await _seed_wallet(db_session, user, cash=1_000)
    grant = await _grant(db_session, wallet, user, 30)

    request_id = f"req-rel-g-{uuid4().hex}"
    await wallet_service.hold(
        db_session, wallet_id=wallet.id, amount=100, request_id=request_id
    )
    await wallet_service.release(
        db_session, wallet_id=wallet.id, hold_amount=100, request_id=request_id
    )
    await db_session.refresh(grant)
    assert grant.remaining == 30
    assert grant.status == GRANT_STATUS_ACTIVE
    await _assert_invariant(db_session, wallet)


# =====================================================================
# 并发 hold：总扣减不超过总可用额
# =====================================================================


@pytest.mark.asyncio
async def test_concurrent_hold_does_not_exceed_total_available() -> None:
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    user_id = None
    wallet_id = None
    try:
        async with factory() as session:
            async with session.begin():
                user = await _seed_user(session, "-conc")
                wallet = await _seed_wallet(session, user, cash=50)
                await _grant(session, wallet, user, 50)
                user_id = user.id
                wallet_id = wallet.id

        async def _hold_once(idx: int) -> bool:
            async with factory() as s:
                async with s.begin():
                    try:
                        await wallet_service.hold(
                            s,
                            wallet_id=wallet_id,
                            amount=80,
                            request_id=f"req-conc-{idx}-{uuid4().hex}",
                        )
                        return True
                    except InsufficientBalanceError:
                        return False

        results = await asyncio.gather(_hold_once(0), _hold_once(1))
        success = sum(1 for ok in results if ok)
        assert success <= 1  # 总可用 100，每次 80，最多成功 1 次

        async with factory() as session:
            async with session.begin():
                wallet = await session.get(Wallet, wallet_id)
                assert wallet is not None
                frozen = wallet.gift_held + wallet.held
                leftover = wallet.gift_balance + wallet.balance
                assert frozen + leftover == 100
                assert frozen <= 100
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
