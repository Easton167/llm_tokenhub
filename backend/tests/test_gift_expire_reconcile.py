"""赠金过期回收任务与账本对账测试。

覆盖：到期回收、未到期/永久有效不受影响、gift_held>0 跳过且下一轮可收、
重复执行幂等、--dry-run 不写入、对账检出不一致且不修数。
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import STATUS_ENABLED, VISIBILITY_ROLE_RESTRICTED
from app.core.datetime_utils import utc_now
from app.db.models.llm_model import LLMModel
from app.db.models.user import User
from app.db.models.wallet import GiftGrant, Wallet, WalletTransaction
from app.services import usage_service
from app.services.gift_service import (
    GRANT_STATUS_ACTIVE,
    GRANT_STATUS_EXPIRED,
    gift_service,
)
from app.services.wallet_service import TX_GIFT_EXPIRE, wallet_service
from app.tasks.expire_gifts import expire_due_gifts
from app.tasks.reconcile_gifts import reconcile_gifts


async def _seed_user(session: AsyncSession, suffix: str = "") -> User:
    user = User(
        email=f"gift-task{suffix}-{uuid4().hex[:8]}@example.com",
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
    expires_at=None,
    key: str | None = None,
) -> GiftGrant:
    return await gift_service.grant(
        session,
        wallet_id=wallet.id,
        amount=amount,
        reason="promotion",
        expires_at=expires_at,
        idempotency_key=key or f"key-{uuid4().hex}",
        granted_by=user.id,
        remark="过期任务测试",
    )


async def _active_remaining(session: AsyncSession, wallet_id: UUID) -> int:
    stmt = select(func.coalesce(func.sum(GiftGrant.remaining), 0)).where(
        GiftGrant.wallet_id == wallet_id,
        GiftGrant.status == GRANT_STATUS_ACTIVE,
    )
    return int((await session.execute(stmt)).scalar_one())


async def _expire_tx_count(session: AsyncSession, wallet_id: UUID) -> int:
    stmt = select(func.count(WalletTransaction.id)).where(
        WalletTransaction.wallet_id == wallet_id,
        WalletTransaction.type == TX_GIFT_EXPIRE,
    )
    return int((await session.execute(stmt)).scalar_one())


async def _assert_invariant(session: AsyncSession, wallet: Wallet) -> None:
    await session.refresh(wallet)
    remaining = await _active_remaining(session, wallet.id)
    assert remaining == wallet.gift_balance + wallet.gift_held
    assert wallet.gift_balance >= 0
    assert wallet.gift_held >= 0


# =====================================================================
# 过期回收
# =====================================================================


@pytest.mark.asyncio
async def test_expire_due_grants_reduces_gift_balance(
    db_session: AsyncSession,
) -> None:
    user = await _seed_user(db_session)
    wallet = await _seed_wallet(db_session, user)
    now = utc_now()
    due = await _grant(
        db_session, wallet, user, amount=80, expires_at=now - timedelta(hours=1)
    )
    future = await _grant(
        db_session, wallet, user, amount=50, expires_at=now + timedelta(days=7)
    )
    forever = await _grant(db_session, wallet, user, amount=30, expires_at=None)
    await db_session.refresh(wallet)
    assert wallet.gift_balance == 160

    result = await expire_due_gifts(
        db_session, now=now, dry_run=False, limit=10_000
    )
    assert result.errors == 0
    assert result.expired >= 1

    await db_session.refresh(due)
    await db_session.refresh(future)
    await db_session.refresh(forever)
    await db_session.refresh(wallet)

    assert due.status == GRANT_STATUS_EXPIRED
    assert due.remaining == 0
    assert future.status == GRANT_STATUS_ACTIVE
    assert future.remaining == 50
    assert forever.status == GRANT_STATUS_ACTIVE
    assert forever.remaining == 30
    assert wallet.gift_balance == 80
    assert await _expire_tx_count(db_session, wallet.id) == 1
    await _assert_invariant(db_session, wallet)


@pytest.mark.asyncio
async def test_expire_skips_wallet_with_gift_held_then_recovers(
    db_session: AsyncSession,
) -> None:
    user = await _seed_user(db_session)
    wallet = await _seed_wallet(db_session, user)
    now = utc_now()
    grant = await _grant(
        db_session, wallet, user, amount=100, expires_at=now - timedelta(minutes=5)
    )
    request_id = f"hold-{uuid4().hex[:12]}"
    await wallet_service.hold(
        db_session,
        wallet_id=wallet.id,
        amount=40,
        request_id=request_id,
    )
    await db_session.refresh(wallet)
    assert wallet.gift_held == 40
    assert wallet.gift_balance == 60

    skipped = await expire_due_gifts(
        db_session, now=now, dry_run=False, limit=10_000
    )
    assert skipped.wallets_skipped >= 1
    assert skipped.skipped >= 1

    await db_session.refresh(grant)
    await db_session.refresh(wallet)
    assert grant.status == GRANT_STATUS_ACTIVE
    assert grant.remaining == 100
    assert wallet.gift_held == 40
    assert wallet.gift_balance == 60
    assert await _expire_tx_count(db_session, wallet.id) == 0

    await wallet_service.release(
        db_session,
        wallet_id=wallet.id,
        hold_amount=40,
        request_id=request_id,
    )
    await db_session.refresh(wallet)
    assert wallet.gift_held == 0
    assert wallet.gift_balance == 100

    recovered = await expire_due_gifts(
        db_session, now=now, dry_run=False, limit=10_000
    )
    assert recovered.expired >= 1
    assert recovered.errors == 0

    await db_session.refresh(grant)
    await db_session.refresh(wallet)
    assert grant.status == GRANT_STATUS_EXPIRED
    assert grant.remaining == 0
    assert wallet.gift_balance == 0
    await _assert_invariant(db_session, wallet)


@pytest.mark.asyncio
async def test_expire_is_idempotent_on_rerun(db_session: AsyncSession) -> None:
    user = await _seed_user(db_session)
    wallet = await _seed_wallet(db_session, user)
    now = utc_now()
    await _grant(
        db_session, wallet, user, amount=25, expires_at=now - timedelta(days=1)
    )

    first = await expire_due_gifts(
        db_session, now=now, dry_run=False, limit=10_000
    )
    assert first.expired >= 1
    assert await _expire_tx_count(db_session, wallet.id) == 1
    second = await expire_due_gifts(
        db_session, now=now, dry_run=False, limit=10_000
    )
    assert second.errors == 0
    assert await _expire_tx_count(db_session, wallet.id) == 1
    await db_session.refresh(wallet)
    assert wallet.gift_balance == 0
    await _assert_invariant(db_session, wallet)


@pytest.mark.asyncio
async def test_expire_dry_run_writes_nothing(db_session: AsyncSession) -> None:
    user = await _seed_user(db_session)
    wallet = await _seed_wallet(db_session, user)
    now = utc_now()
    grant = await _grant(
        db_session, wallet, user, amount=70, expires_at=now - timedelta(hours=2)
    )

    result = await expire_due_gifts(
        db_session, now=now, dry_run=True, limit=10_000
    )
    assert result.scanned >= 1
    assert result.expired == 0

    await db_session.refresh(grant)
    await db_session.refresh(wallet)
    assert grant.status == GRANT_STATUS_ACTIVE
    assert grant.remaining == 70
    assert wallet.gift_balance == 70
    assert await _expire_tx_count(db_session, wallet.id) == 0
    await _assert_invariant(db_session, wallet)


# =====================================================================
# 账本对账
# =====================================================================


@pytest.mark.asyncio
async def test_reconcile_gifts_ok_for_consistent_wallet(
    db_session: AsyncSession,
) -> None:
    user = await _seed_user(db_session)
    wallet = await _seed_wallet(db_session, user)
    await _grant(db_session, wallet, user, amount=120, expires_at=None)

    report = await reconcile_gifts(db_session)
    mismatched_ids = {m.wallet_id for m in report.wallet_mismatches}
    assert wallet.id not in mismatched_ids
    assert report.gift_balance_total >= 120
    await _assert_invariant(db_session, wallet)


@pytest.mark.asyncio
async def test_reconcile_detects_wallet_mismatch_without_autofix(
    db_session: AsyncSession,
) -> None:
    user = await _seed_user(db_session)
    wallet = await _seed_wallet(db_session, user)
    grant = await _grant(db_session, wallet, user, amount=200, expires_at=None)
    await db_session.refresh(wallet)

    wallet.gift_balance += 1
    await db_session.flush()

    report = await reconcile_gifts(db_session)
    found = next(
        (m for m in report.wallet_mismatches if m.wallet_id == wallet.id), None
    )
    assert found is not None
    assert found.grants_remaining == 200
    assert found.wallet_gift_total == 201
    assert found.delta == -1
    assert report.ok is False

    await db_session.refresh(wallet)
    await db_session.refresh(grant)
    assert wallet.gift_balance == 201
    assert grant.remaining == 200
    assert grant.status == GRANT_STATUS_ACTIVE


@pytest.mark.asyncio
async def test_reconcile_detects_usage_capture_gift_split_mismatch(
    db_session: AsyncSession,
) -> None:
    user = await _seed_user(db_session)
    model = LLMModel(
        alias=f"gift-task-{uuid4().hex[:6]}",
        display_name="Gift Task Model",
        status=STATUS_ENABLED,
        visibility=VISIBILITY_ROLE_RESTRICTED,
    )
    db_session.add(model)
    await db_session.flush()

    before = await reconcile_gifts(db_session)
    request_id = f"gift-split-{uuid4().hex[:12]}"
    await usage_service.write_request_log(
        db_session,
        request_id=request_id,
        api_key_id=None,
        user_id=user.id,
        billing_subject=f"user:{user.id}",
        model_id=model.id,
        channel_id=None,
        status="success",
        http_status=200,
    )
    log = await usage_service.write_usage_log(
        db_session,
        request_id=request_id,
        input_tokens=1,
        output_tokens=1,
        billing_rule_version=1,
        cost_amount=0,
        charge_amount=40,
    )
    log.gift_charge_amount = 40
    log.cash_charge_amount = 0
    await db_session.flush()

    after = await reconcile_gifts(db_session)
    assert after.gift_split_matched is False
    assert after.usage_gift_total == before.usage_gift_total + 40
    assert after.capture_gift_total == before.capture_gift_total
    assert after.ok is False

    await db_session.refresh(log)
    assert log.gift_charge_amount == 40
