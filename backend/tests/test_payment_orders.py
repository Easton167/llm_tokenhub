"""支付单：状态机、回调幂等入账、个人/组织钱包、过期不入账。"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ORG_ROLE_ADMIN,
    ORG_ROLE_MEMBER,
    PAYMENT_STATUS_CREATED,
    PAYMENT_STATUS_EXPIRED,
    PAYMENT_STATUS_FAILED,
    PAYMENT_STATUS_PAID,
    PAYMENT_TARGET_ORG,
    PAYMENT_TARGET_PERSONAL,
    STATUS_ENABLED,
)
from app.core.errors import ConflictError, ForbiddenError
from app.db.models.user import User
from app.db.session import get_db
from app.main import app
from app.payments.mock import mock_sign_payload
from app.services.org_service import org_service
from app.services.payment_service import payment_ref_request_id, payment_service
from app.services.wallet_service import TX_RECHARGE, wallet_service
from tests.conftest import admin_auth_headers


@pytest_asyncio.fixture
async def user_client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    original_commit = db_session.commit

    async def _fake_commit() -> None:
        await db_session.flush()

    db_session.commit = _fake_commit  # type: ignore[method-assign]

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    db_session.commit = original_commit  # type: ignore[method-assign]
    app.dependency_overrides.clear()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _register_login(
    client: AsyncClient, *, prefix: str
) -> tuple[str, str]:
    email = f"{prefix}-{uuid4().hex[:8]}@example.com"
    password = "password123"
    reg = await client.post(
        "/auth/register",
        json={"email": email, "password": password, "nickname": prefix},
    )
    assert reg.status_code == 201, reg.text
    user_id = reg.json()["id"]
    login = await client.post(
        "/auth/login", json={"email": email, "password": password}
    )
    assert login.status_code == 200, login.text
    return user_id, login.json()["access_token"]


async def _seed_user(session: AsyncSession, suffix: str = "") -> User:
    user = User(
        email=f"pay{suffix}-{uuid4().hex[:6]}@example.com",
        status=STATUS_ENABLED,
    )
    session.add(user)
    await session.flush()
    return user


# =====================================================================
# 状态机
# =====================================================================


@pytest.mark.asyncio
async def test_payment_status_machine_rejects_illegal_transition(
    db_session: AsyncSession,
) -> None:
    user = await _seed_user(db_session, "-sm")
    order, _ = await payment_service.create_order(
        db_session,
        user_id=user.id,
        amount=1_000,
        target_type=PAYMENT_TARGET_PERSONAL,
    )
    assert order.status == PAYMENT_STATUS_CREATED

    payment_service.transition(order, PAYMENT_STATUS_PAID)
    assert order.status == PAYMENT_STATUS_PAID

    with pytest.raises(ConflictError, match="Invalid payment status transition"):
        payment_service.transition(order, PAYMENT_STATUS_CREATED)

    with pytest.raises(ConflictError, match="Invalid payment status transition"):
        payment_service.transition(order, PAYMENT_STATUS_FAILED)


@pytest.mark.asyncio
async def test_payment_status_machine_allows_created_to_failed_or_expired(
    db_session: AsyncSession,
) -> None:
    user = await _seed_user(db_session, "-sm2")
    order, _ = await payment_service.create_order(
        db_session,
        user_id=user.id,
        amount=500,
        target_type=PAYMENT_TARGET_PERSONAL,
    )
    payment_service.transition(order, PAYMENT_STATUS_FAILED)
    assert order.status == PAYMENT_STATUS_FAILED

    order2, _ = await payment_service.create_order(
        db_session,
        user_id=user.id,
        amount=500,
        target_type=PAYMENT_TARGET_PERSONAL,
    )
    payment_service.transition(order2, PAYMENT_STATUS_EXPIRED)
    assert order2.status == PAYMENT_STATUS_EXPIRED


# =====================================================================
# 回调幂等入账
# =====================================================================


@pytest.mark.asyncio
async def test_callback_three_times_credits_wallet_once(
    db_session: AsyncSession,
) -> None:
    user = await _seed_user(db_session, "-idem")
    wallet = await wallet_service.get_or_create_personal_wallet(db_session, user.id)
    assert wallet.balance == 0

    amount = 12_345
    order, create_result = await payment_service.create_order(
        db_session,
        user_id=user.id,
        amount=amount,
        target_type=PAYMENT_TARGET_PERSONAL,
    )

    for _ in range(3):
        paid = await payment_service.apply_mock_success(
            db_session,
            order_no=order.order_no,
            external_order_no=create_result.external_order_no,
            amount=amount,
        )
        assert paid.status == PAYMENT_STATUS_PAID

    await db_session.refresh(wallet)
    assert wallet.balance == amount

    txs = await wallet_service.get_transactions(
        db_session,
        wallet_id=wallet.id,
        tx_type=TX_RECHARGE,
        ref_request_id=payment_ref_request_id(order.order_no),
        limit=20,
    )
    assert len(txs) == 1
    assert txs[0].amount == amount


@pytest.mark.asyncio
async def test_expired_order_callback_does_not_credit(
    db_session: AsyncSession,
) -> None:
    user = await _seed_user(db_session, "-exp")
    wallet = await wallet_service.get_or_create_personal_wallet(db_session, user.id)
    order, create_result = await payment_service.create_order(
        db_session,
        user_id=user.id,
        amount=8_000,
        target_type=PAYMENT_TARGET_PERSONAL,
    )
    # 人为设为已过期
    order.expired_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db_session.flush()

    with pytest.raises(ConflictError, match="expired"):
        await payment_service.apply_mock_success(
            db_session,
            order_no=order.order_no,
            external_order_no=create_result.external_order_no,
            amount=order.amount,
        )

    await db_session.refresh(order)
    await db_session.refresh(wallet)
    assert order.status == PAYMENT_STATUS_EXPIRED
    assert wallet.balance == 0
    assert (
        await payment_service.count_recharge_txs(
            db_session, order_no=order.order_no, wallet_id=wallet.id
        )
        == 0
    )


@pytest.mark.asyncio
async def test_failed_callback_marks_failed_without_credit(
    db_session: AsyncSession,
) -> None:
    user = await _seed_user(db_session, "-fail")
    wallet = await wallet_service.get_or_create_personal_wallet(db_session, user.id)
    order, create_result = await payment_service.create_order(
        db_session,
        user_id=user.id,
        amount=3_000,
        target_type=PAYMENT_TARGET_PERSONAL,
    )
    sign = mock_sign_payload(
        order_no=order.order_no,
        external_order_no=create_result.external_order_no,
        success=False,
        amount=order.amount,
    )
    result = await payment_service.handle_callback(
        db_session,
        provider_name="mock",
        headers={},
        body={
            "order_no": order.order_no,
            "external_order_no": create_result.external_order_no,
            "success": False,
            "amount": order.amount,
            "sign": sign,
            "fail_reason": "user_cancelled",
        },
    )
    assert result.status == PAYMENT_STATUS_FAILED
    assert result.fail_reason == "user_cancelled"
    await db_session.refresh(wallet)
    assert wallet.balance == 0


# =====================================================================
# 个人 / 组织钱包
# =====================================================================


@pytest.mark.asyncio
async def test_personal_and_org_wallet_payment_credit(
    db_session: AsyncSession,
) -> None:
    owner = await _seed_user(db_session, "-own")
    member = await _seed_user(db_session, "-mem")

    # 个人钱包入账
    personal_order, _ = await payment_service.create_order(
        db_session,
        user_id=owner.id,
        amount=2_000,
        target_type=PAYMENT_TARGET_PERSONAL,
    )
    await payment_service.apply_mock_success(
        db_session, order_no=personal_order.order_no, amount=2_000
    )
    personal_wallet = await wallet_service.get_or_create_personal_wallet(
        db_session, owner.id
    )
    assert personal_wallet.balance == 2_000

    # 组织钱包：Owner 可创建并入账
    org = await org_service.create_org(
        db_session, name=f"PayOrg-{uuid4().hex[:6]}", owner_user_id=owner.id
    )
    org_order, _ = await payment_service.create_order(
        db_session,
        user_id=owner.id,
        amount=5_000,
        target_type=PAYMENT_TARGET_ORG,
        org_id=org.id,
    )
    assert org_order.target_type == PAYMENT_TARGET_ORG
    assert org_order.org_id == org.id
    await payment_service.apply_mock_success(
        db_session, order_no=org_order.order_no, amount=5_000
    )
    org_wallet = await wallet_service.require_org_wallet(db_session, org.id)
    assert org_wallet.balance == 5_000
    assert org_wallet.id == org_order.target_wallet_id

    # 非 Owner（Admin）不可创建组织支付单
    await org_service.invite_member(
        db_session,
        org=org,
        actor_user_id=owner.id,
        user_id=member.id,
        org_role=ORG_ROLE_ADMIN,
    )
    with pytest.raises(ForbiddenError):
        await payment_service.create_order(
            db_session,
            user_id=member.id,
            amount=1_000,
            target_type=PAYMENT_TARGET_ORG,
            org_id=org.id,
        )


# =====================================================================
# API
# =====================================================================


@pytest.mark.asyncio
async def test_payment_api_create_list_callback_and_admin(
    user_client: AsyncClient, db_session: AsyncSession
) -> None:
    user_id, token = await _register_login(user_client, prefix="pay-api")
    headers = _auth(token)

    create = await user_client.post(
        "/payments",
        headers=headers,
        json={
            "amount": 9_999,
            "target_type": "personal",
            "provider": "mock",
        },
    )
    assert create.status_code == 201, create.text
    body = create.json()
    order_no = body["order"]["order_no"]
    external = body["external_order_no"]
    assert body["order"]["status"] == PAYMENT_STATUS_CREATED
    assert body["pay_url"]

    listed = await user_client.get("/payments", headers=headers)
    assert listed.status_code == 200
    assert any(o["order_no"] == order_no for o in listed.json())

    detail = await user_client.get(f"/payments/{order_no}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["order_no"] == order_no

    sign = mock_sign_payload(
        order_no=order_no,
        external_order_no=external,
        success=True,
        amount=9_999,
    )
    # 重复回调 3 次
    for _ in range(3):
        cb = await user_client.post(
            "/payments/callbacks/mock",
            json={
                "order_no": order_no,
                "external_order_no": external,
                "success": True,
                "amount": 9_999,
                "sign": sign,
            },
        )
        assert cb.status_code == 200, cb.text
        assert cb.json()["status"] == PAYMENT_STATUS_PAID

    wallet = await user_client.get("/wallets/me", headers=headers)
    assert wallet.status_code == 200
    assert wallet.json()["balance"] == 9_999

    admin = await user_client.get(
        f"/admin/payments/{order_no}",
        headers=admin_auth_headers(),
    )
    assert admin.status_code == 200, admin.text
    assert admin.json()["order_no"] == order_no
    assert admin.json()["user_id"] == user_id

    admin_list = await user_client.get(
        "/admin/payments",
        headers=admin_auth_headers(),
        params={"user_id": user_id},
    )
    assert admin_list.status_code == 200
    assert any(o["order_no"] == order_no for o in admin_list.json())


@pytest.mark.asyncio
async def test_org_payment_api_requires_owner(
    user_client: AsyncClient, db_session: AsyncSession
) -> None:
    owner_id, owner_token = await _register_login(user_client, prefix="pay-own")
    member_id, member_token = await _register_login(user_client, prefix="pay-mem")

    org_resp = await user_client.post(
        "/orgs",
        headers=_auth(owner_token),
        json={"name": f"PayAPI-{uuid4().hex[:6]}"},
    )
    assert org_resp.status_code == 201, org_resp.text
    org_id = org_resp.json()["id"]

    invite = await user_client.post(
        f"/orgs/{org_id}/members",
        headers=_auth(owner_token),
        json={"user_id": member_id, "org_role": ORG_ROLE_MEMBER},
    )
    assert invite.status_code == 201, invite.text

    forbidden = await user_client.post(
        "/payments",
        headers=_auth(member_token),
        json={
            "amount": 1_000,
            "target_type": "org",
            "org_id": org_id,
        },
    )
    assert forbidden.status_code == 403, forbidden.text

    ok = await user_client.post(
        "/payments",
        headers=_auth(owner_token),
        json={
            "amount": 1_000,
            "target_type": "org",
            "org_id": org_id,
        },
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["order"]["org_id"] == org_id
    assert ok.json()["order"]["user_id"] == owner_id
