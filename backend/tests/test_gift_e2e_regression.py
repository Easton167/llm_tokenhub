"""赠送余额专项任务 9：全链路数值对照。

路径：赠送 100 元(30 天) → 纯赠金调用 → 赠金用尽转现金 → 纯现金调用
     → 撤回已耗尽批次 409 → 撤回未消耗批次 → 过期回收 → 对账无本钱包差异。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import STATUS_ENABLED, VISIBILITY_PUBLIC
from app.core.datetime_utils import utc_now
from app.db.models.llm_model import LLMModel
from app.db.models.user import User
from app.db.models.wallet import GiftGrant, Wallet, WalletTransaction
from app.db.session import get_db
from app.main import app
from app.services import usage_service
from app.services.gift_service import GRANT_STATUS_ACTIVE, GRANT_STATUS_EXPIRED, GRANT_STATUS_REVOKED
from app.services.wallet_service import TX_CAPTURE, TX_HOLD, wallet_service
from app.tasks.expire_gifts import expire_due_gifts
from app.tasks.reconcile_gifts import reconcile_gifts
from tests.conftest import admin_auth_headers

# 1 元 = 10000；对照表与 docs/VALIDATION_PLAN.md §5.4 一致
GRANT_100 = 1_000_000
CASH_50 = 500_000
CALL1_HOLD = 200_000
CALL1_ACTUAL = 150_000
CALL2_AMOUNT = 900_000
CALL3_AMOUNT = 10_000
REVOKE_GRANT = 40_000
EXPIRE_GRANT = 80_000


@pytest_asyncio.fixture
async def api_client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


async def _seed_user(session: AsyncSession) -> User:
    user = User(
        email=f"gift-e2e-{uuid4().hex[:8]}@example.com",
        status=STATUS_ENABLED,
    )
    session.add(user)
    await session.flush()
    return user


async def _seed_model(session: AsyncSession) -> LLMModel:
    model = LLMModel(
        alias=f"e2e-{uuid4().hex[:6]}",
        display_name="赠金全链路模型",
        model_type="chat",
        capabilities={},
        visibility=VISIBILITY_PUBLIC,
        status=STATUS_ENABLED,
    )
    session.add(model)
    await session.flush()
    return model


async def _assert_invariant(session: AsyncSession, wallet: Wallet) -> None:
    await session.refresh(wallet)
    remaining = int(
        (
            await session.execute(
                select(func.coalesce(func.sum(GiftGrant.remaining), 0)).where(
                    GiftGrant.wallet_id == wallet.id,
                    GiftGrant.status == GRANT_STATUS_ACTIVE,
                )
            )
        ).scalar_one()
    )
    assert remaining == wallet.gift_balance + wallet.gift_held
    assert wallet.gift_balance >= 0
    assert wallet.gift_held >= 0


async def _tx(
    session: AsyncSession, wallet_id: UUID, request_id: str, tx_type: str
) -> WalletTransaction:
    rows = await wallet_service.get_transactions(
        session, wallet_id=wallet_id, tx_type=tx_type, ref_request_id=request_id
    )
    assert rows, f"missing {tx_type} for {request_id}"
    return rows[0]


async def _charge(
    session: AsyncSession,
    *,
    user: User,
    wallet: Wallet,
    model: LLMModel,
    hold_amount: int,
    actual_amount: int,
    cost_amount: int,
) -> tuple[WalletTransaction, WalletTransaction, object]:
    request_id = f"e2e-{uuid4().hex[:12]}"
    await wallet_service.hold(
        session, wallet_id=wallet.id, amount=hold_amount, request_id=request_id
    )
    await wallet_service.capture(
        session,
        wallet_id=wallet.id,
        hold_amount=hold_amount,
        actual_amount=actual_amount,
        request_id=request_id,
    )
    await usage_service.write_request_log(
        session,
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
        session,
        request_id=request_id,
        input_tokens=10,
        output_tokens=5,
        billing_rule_version=1,
        cost_amount=cost_amount,
        charge_amount=actual_amount,
        actor_user_id=user.id,
        source="chat",
        wallet_id=wallet.id,
    )
    hold = await _tx(session, wallet.id, request_id, TX_HOLD)
    cap = await _tx(session, wallet.id, request_id, TX_CAPTURE)
    return hold, cap, log


@pytest.mark.asyncio
async def test_gift_e2e_grant_chat_split_revoke_expire_reconcile(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _seed_user(db_session)
    model = await _seed_model(db_session)
    wallet = await wallet_service.get_or_create_personal_wallet(db_session, user.id)
    wallet.balance = CASH_50
    await db_session.flush()

    before = await reconcile_gifts(db_session)

    granted = await api_client.post(
        f"/admin/wallets/users/{user.id}/gifts",
        headers=admin_auth_headers(),
        json={
            "amount": GRANT_100,
            "expires_in_days": 30,
            "reason": "promotion",
            "remark": "全链路验收赠送 100 元",
            "idempotency_key": f"e2e-100-{uuid4().hex}",
        },
    )
    assert granted.status_code == 200, granted.text
    grant = granted.json()
    grant_id = grant["id"]
    assert grant["amount"] == GRANT_100
    assert grant["remaining"] == GRANT_100
    assert grant["status"] == GRANT_STATUS_ACTIVE
    assert grant["expires_at"] is not None

    wallet_resp = await api_client.get(
        f"/admin/wallets/users/{user.id}", headers=admin_auth_headers()
    )
    assert wallet_resp.status_code == 200
    w = wallet_resp.json()
    assert w["gift_balance"] == GRANT_100
    assert w["balance"] == CASH_50
    assert w["total_available"] == GRANT_100 + CASH_50

    hold1, cap1, log1 = await _charge(
        db_session,
        user=user,
        wallet=wallet,
        model=model,
        hold_amount=CALL1_HOLD,
        actual_amount=CALL1_ACTUAL,
        cost_amount=100,
    )
    assert hold1.amount == -CALL1_HOLD
    assert hold1.gift_amount == -CALL1_HOLD
    assert cap1.amount == -CALL1_ACTUAL
    assert cap1.gift_amount == -CALL1_ACTUAL
    assert log1.charge_amount == CALL1_ACTUAL
    assert log1.gift_charge_amount == CALL1_ACTUAL
    assert log1.cash_charge_amount == 0
    await db_session.refresh(wallet)
    assert wallet.gift_balance == 850_000
    assert wallet.balance == CASH_50
    await _assert_invariant(db_session, wallet)

    hold2, cap2, log2 = await _charge(
        db_session,
        user=user,
        wallet=wallet,
        model=model,
        hold_amount=CALL2_AMOUNT,
        actual_amount=CALL2_AMOUNT,
        cost_amount=700,
    )
    assert hold2.amount == -CALL2_AMOUNT
    assert hold2.gift_amount == -850_000
    assert cap2.amount == -CALL2_AMOUNT
    assert cap2.gift_amount == -850_000
    assert log2.charge_amount == CALL2_AMOUNT
    assert log2.gift_charge_amount == 850_000
    assert log2.cash_charge_amount == 50_000
    await db_session.refresh(wallet)
    assert wallet.gift_balance == 0
    assert wallet.balance == 450_000
    await _assert_invariant(db_session, wallet)

    _hold3, cap3, log3 = await _charge(
        db_session,
        user=user,
        wallet=wallet,
        model=model,
        hold_amount=CALL3_AMOUNT,
        actual_amount=CALL3_AMOUNT,
        cost_amount=8,
    )
    assert cap3.gift_amount == 0
    assert log3.gift_charge_amount == 0
    assert log3.cash_charge_amount == CALL3_AMOUNT
    await db_session.refresh(wallet)
    assert wallet.gift_balance == 0
    assert wallet.balance == 440_000

    consumed = await api_client.post(
        f"/admin/gifts/{grant_id}/revoke",
        headers=admin_auth_headers(),
        json={"remark": "已耗尽应 409"},
    )
    assert consumed.status_code == 409
    assert consumed.json()["error"]["code"] == "gift_already_consumed"

    leftover = await api_client.post(
        f"/admin/wallets/users/{user.id}/gifts",
        headers=admin_auth_headers(),
        json={
            "amount": REVOKE_GRANT,
            "never_expires": True,
            "reason": "compensation",
            "remark": "用于撤回剩余",
            "idempotency_key": f"e2e-revoke-{uuid4().hex}",
        },
    )
    assert leftover.status_code == 200, leftover.text
    revoked = await api_client.post(
        f"/admin/gifts/{leftover.json()['id']}/revoke",
        headers=admin_auth_headers(),
        json={"remark": "验收撤回"},
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["status"] == GRANT_STATUS_REVOKED
    assert revoked.json()["remaining"] == 0
    await db_session.refresh(wallet)
    assert wallet.gift_balance == 0
    assert wallet.balance == 440_000

    now = utc_now()
    due = await api_client.post(
        f"/admin/wallets/users/{user.id}/gifts",
        headers=admin_auth_headers(),
        json={
            "amount": EXPIRE_GRANT,
            "expires_at": (now - timedelta(hours=1)).isoformat(),
            "reason": "internal",
            "remark": "已到期待回收",
            "idempotency_key": f"e2e-expire-{uuid4().hex}",
        },
    )
    assert due.status_code == 200, due.text
    due_id = UUID(due.json()["id"])
    await db_session.refresh(wallet)
    assert wallet.gift_balance == EXPIRE_GRANT

    result = await expire_due_gifts(db_session, now=now, dry_run=False, limit=10_000)
    assert result.errors == 0
    due_grant = await db_session.get(GiftGrant, due_id)
    assert due_grant is not None
    assert due_grant.status == GRANT_STATUS_EXPIRED
    assert due_grant.remaining == 0
    await db_session.refresh(wallet)
    assert wallet.gift_balance == 0
    assert wallet.balance == 440_000
    await _assert_invariant(db_session, wallet)

    after = await reconcile_gifts(db_session)
    mismatched_ids = {m.wallet_id for m in after.wallet_mismatches}
    assert wallet.id not in mismatched_ids
    our_gift_charge = CALL1_ACTUAL + 850_000
    assert after.usage_gift_total - before.usage_gift_total == our_gift_charge
    assert after.capture_gift_total - before.capture_gift_total == our_gift_charge
    assert after.gift_split_matched == before.gift_split_matched
