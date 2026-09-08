"""赠送余额专项任务 1：数据模型与约束测试。

验收：
  - wallets / wallet_transactions / usage_logs / usage_daily_summary 新列默认 0
  - gift_grants 违反 remaining 范围、非法 reason / status 时数据库报错
  - (wallet_id, idempotency_key) 唯一
  - 中文备注 UTF-8 round-trip 不损坏
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import STATUS_ENABLED, VISIBILITY_PUBLIC
from app.db.models import (
    ApiKey,
    GiftGrant,
    LLMModel,
    RequestLog,
    UsageDailySummary,
    UsageLog,
    User,
    Wallet,
    WalletTransaction,
)


async def _seed_user(session: AsyncSession) -> User:
    user = User(email=f"gift-{uuid4().hex[:8]}@example.com", status=STATUS_ENABLED)
    session.add(user)
    await session.flush()
    return user


async def _seed_wallet(session: AsyncSession, user: User) -> Wallet:
    wallet = Wallet(
        owner_type="personal",
        owner_id=user.id,
        balance=0,
        held=0,
        currency="CNY",
        credit_limit=0,
    )
    session.add(wallet)
    await session.flush()
    return wallet


async def _seed_model(session: AsyncSession) -> LLMModel:
    model = LLMModel(
        alias=f"gift-m-{uuid4().hex[:6]}",
        display_name="赠金测试模型",
        visibility=VISIBILITY_PUBLIC,
        status=STATUS_ENABLED,
    )
    session.add(model)
    await session.flush()
    return model


def _grant_kwargs(wallet: Wallet, user: User, **overrides) -> dict:
    payload = {
        "wallet_id": wallet.id,
        "amount": 100,
        "remaining": 100,
        "reason": "promotion",
        "status": "active",
        "granted_by": user.id,
        "idempotency_key": f"key-{uuid4().hex[:12]}",
        "remark": "营销活动发放",
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_wallet_gift_pool_defaults_zero(db_session: AsyncSession) -> None:
    """未显式写入时赠送池字段默认为 0（存量钱包语义：无赠金）。"""
    user = await _seed_user(db_session)
    wallet = await _seed_wallet(db_session, user)
    await db_session.refresh(wallet)
    assert wallet.gift_balance == 0
    assert wallet.gift_held == 0


@pytest.mark.asyncio
async def test_wallet_gift_balance_negative_rejected(db_session: AsyncSession) -> None:
    user = await _seed_user(db_session)
    async with db_session.begin_nested():
        db_session.add(
            Wallet(
                owner_type="personal",
                owner_id=user.id,
                balance=0,
                held=0,
                gift_balance=-1,
                gift_held=0,
                currency="CNY",
                credit_limit=0,
            )
        )
        with pytest.raises(IntegrityError):
            await db_session.flush()


@pytest.mark.asyncio
async def test_gift_grant_remaining_exceeds_amount_rejected(db_session: AsyncSession) -> None:
    user = await _seed_user(db_session)
    wallet = await _seed_wallet(db_session, user)
    async with db_session.begin_nested():
        db_session.add(GiftGrant(**_grant_kwargs(wallet, user, remaining=101)))
        with pytest.raises(IntegrityError):
            await db_session.flush()


@pytest.mark.asyncio
async def test_gift_grant_remaining_negative_rejected(db_session: AsyncSession) -> None:
    user = await _seed_user(db_session)
    wallet = await _seed_wallet(db_session, user)
    async with db_session.begin_nested():
        db_session.add(GiftGrant(**_grant_kwargs(wallet, user, remaining=-1)))
        with pytest.raises(IntegrityError):
            await db_session.flush()


@pytest.mark.asyncio
async def test_gift_grant_remaining_boundaries_ok(db_session: AsyncSession) -> None:
    """remaining=0 与 remaining=amount 均合法。"""
    user = await _seed_user(db_session)
    wallet = await _seed_wallet(db_session, user)
    g1 = GiftGrant(**_grant_kwargs(wallet, user, remaining=0, amount=50))
    g2 = GiftGrant(**_grant_kwargs(wallet, user, remaining=80, amount=80))
    db_session.add_all([g1, g2])
    await db_session.flush()
    assert g1.remaining == 0
    assert g2.remaining == g2.amount


@pytest.mark.asyncio
async def test_gift_grant_illegal_reason_rejected(db_session: AsyncSession) -> None:
    user = await _seed_user(db_session)
    wallet = await _seed_wallet(db_session, user)
    async with db_session.begin_nested():
        db_session.add(GiftGrant(**_grant_kwargs(wallet, user, reason="bonus")))
        with pytest.raises(IntegrityError):
            await db_session.flush()


@pytest.mark.asyncio
async def test_gift_grant_illegal_status_rejected(db_session: AsyncSession) -> None:
    user = await _seed_user(db_session)
    wallet = await _seed_wallet(db_session, user)
    async with db_session.begin_nested():
        db_session.add(GiftGrant(**_grant_kwargs(wallet, user, status="pending")))
        with pytest.raises(IntegrityError):
            await db_session.flush()


@pytest.mark.asyncio
async def test_gift_grant_idempotency_unique(db_session: AsyncSession) -> None:
    user = await _seed_user(db_session)
    wallet = await _seed_wallet(db_session, user)
    key = f"idem-{uuid4().hex[:12]}"
    db_session.add(GiftGrant(**_grant_kwargs(wallet, user, idempotency_key=key)))
    await db_session.flush()
    async with db_session.begin_nested():
        db_session.add(GiftGrant(**_grant_kwargs(wallet, user, idempotency_key=key)))
        with pytest.raises(IntegrityError):
            await db_session.flush()


@pytest.mark.asyncio
async def test_gift_grant_status_default_active_and_utf8_remark(
    db_session: AsyncSession,
) -> None:
    user = await _seed_user(db_session)
    wallet = await _seed_wallet(db_session, user)
    grant = GiftGrant(
        wallet_id=wallet.id,
        amount=30,
        remaining=30,
        reason="compensation",
        granted_by=user.id,
        idempotency_key=f"key-{uuid4().hex[:12]}",
        remark="客诉补偿-重复扣费",
        expires_at=None,
    )
    db_session.add(grant)
    await db_session.flush()
    await db_session.refresh(grant)
    assert grant.status == "active"
    assert grant.expires_at is None
    assert grant.remark == "客诉补偿-重复扣费"


@pytest.mark.asyncio
async def test_wallet_transaction_gift_columns_default_zero(db_session: AsyncSession) -> None:
    """既有只写 amount/balance_after/held_after 的路径，赠送快照默认为 0。"""
    user = await _seed_user(db_session)
    wallet = await _seed_wallet(db_session, user)
    tx = WalletTransaction(
        wallet_id=wallet.id,
        type="recharge",
        amount=1000,
        balance_after=1000,
        held_after=0,
    )
    db_session.add(tx)
    await db_session.flush()
    await db_session.refresh(tx)
    assert tx.gift_amount == 0
    assert tx.gift_balance_after == 0
    assert tx.gift_held_after == 0
    assert tx.gift_grant_id is None


@pytest.mark.asyncio
async def test_usage_log_gift_cash_defaults_zero(db_session: AsyncSession) -> None:
    user = await _seed_user(db_session)
    model = await _seed_model(db_session)
    api_key = ApiKey(
        user_id=user.id,
        name="gift-test-key",
        key_prefix="sk-test",
        key_hash=f"hash-{uuid4().hex}",
        status=STATUS_ENABLED,
    )
    db_session.add(api_key)
    await db_session.flush()
    request_id = f"req-gift-{uuid4().hex}"
    db_session.add(
        RequestLog(
            request_id=request_id,
            api_key_id=api_key.id,
            user_id=user.id,
            billing_subject="personal",
            model_id=model.id,
            status="success",
            http_status=200,
            is_stream=False,
        )
    )
    await db_session.flush()
    log = UsageLog(
        request_id=request_id,
        input_tokens=10,
        output_tokens=2,
        cached_tokens=0,
        billing_rule_version=1,
        cost_amount=1,
        charge_amount=5,
        is_estimated=False,
    )
    db_session.add(log)
    await db_session.flush()
    await db_session.refresh(log)
    assert log.gift_charge_amount == 0
    assert log.cash_charge_amount == 0


@pytest.mark.asyncio
async def test_usage_daily_gift_revenue_defaults_zero(db_session: AsyncSession) -> None:
    user = await _seed_user(db_session)
    model = await _seed_model(db_session)
    row = UsageDailySummary(
        summary_date=date(2026, 8, 16),
        model_id=model.id,
        user_id=user.id,
        request_count=1,
        input_tokens=10,
        output_tokens=2,
        cached_tokens=0,
        revenue_amount=50,
        cost_amount=10,
        cost_price_missing=False,
    )
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)
    assert row.gift_revenue_amount == 0
    assert row.cash_revenue_amount == 0


@pytest.mark.asyncio
async def test_backfill_sql_sets_cash_equal_to_charge(db_session: AsyncSession) -> None:
    """迁移使用的一次性 UPDATE 能把现金列回填为 charge_amount。"""
    user = await _seed_user(db_session)
    model = await _seed_model(db_session)
    api_key = ApiKey(
        user_id=user.id,
        name="gift-backfill-key",
        key_prefix="sk-test",
        key_hash=f"hash-{uuid4().hex}",
        status=STATUS_ENABLED,
    )
    db_session.add(api_key)
    await db_session.flush()
    request_id = f"req-gift-bf-{uuid4().hex}"
    db_session.add(
        RequestLog(
            request_id=request_id,
            api_key_id=api_key.id,
            user_id=user.id,
            billing_subject="personal",
            model_id=model.id,
            status="success",
            http_status=200,
            is_stream=False,
        )
    )
    await db_session.flush()
    log = UsageLog(
        request_id=request_id,
        input_tokens=1,
        output_tokens=1,
        cached_tokens=0,
        billing_rule_version=1,
        cost_amount=0,
        charge_amount=42,
        is_estimated=False,
    )
    db_session.add(log)
    await db_session.flush()
    await db_session.execute(
        text(
            "UPDATE usage_logs "
            "SET gift_charge_amount = 0, cash_charge_amount = charge_amount "
            "WHERE request_id = :rid"
        ),
        {"rid": request_id},
    )
    await db_session.refresh(log)
    assert log.gift_charge_amount == 0
    assert log.cash_charge_amount == 42
    assert log.gift_charge_amount + log.cash_charge_amount == log.charge_amount
