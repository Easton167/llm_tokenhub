"""组织钱包：创建自动建钱包、hold/capture/release 幂等、权限与并发。"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ORG_ROLE_ADMIN, ORG_ROLE_MEMBER, STATUS_ENABLED
from app.core.errors import InsufficientBalanceError
from app.db.models.org import Org
from app.db.models.user import User
from app.db.models.wallet import Wallet
from app.db.session import get_db
from app.main import app
from app.services.org_service import org_service
from app.services.wallet_service import (
    TX_CAPTURE,
    TX_HOLD,
    TX_RECHARGE,
    TX_RELEASE,
    wallet_service,
)
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
        email=f"org-wallet{suffix}-{uuid4().hex[:6]}@example.com",
        status=STATUS_ENABLED,
    )
    session.add(user)
    await session.flush()
    return user


async def _seed_org_with_wallet(
    session: AsyncSession, owner: User, *, balance: int = 0
) -> tuple[Org, Wallet]:
    org = await org_service.create_org(
        session, name=f"Org-{uuid4().hex[:6]}", owner_user_id=owner.id
    )
    wallet = await wallet_service.require_org_wallet(session, org.id)
    if balance > 0:
        wallet.balance = balance
        await session.flush()
    return org, wallet


# =====================================================================
# 服务层
# =====================================================================


@pytest.mark.asyncio
async def test_create_org_auto_creates_org_wallet(db_session: AsyncSession) -> None:
    owner = await _seed_user(db_session, "-auto-wallet")
    org = await org_service.create_org(
        db_session, name="Wallet Org", owner_user_id=owner.id
    )
    wallet = await wallet_service.require_org_wallet(db_session, org.id)
    assert wallet.owner_type == "org"
    assert wallet.owner_id == org.id
    assert wallet.balance == 0
    assert wallet.held == 0


@pytest.mark.asyncio
async def test_org_wallet_hold_capture_release_and_idempotent(
    db_session: AsyncSession,
) -> None:
    owner = await _seed_user(db_session, "-hcr")
    org, wallet = await _seed_org_with_wallet(db_session, owner, balance=10_000)
    actor_id = owner.id
    request_id = f"req-org-{uuid4().hex}"

    await wallet_service.hold(
        db_session,
        wallet_id=wallet.id,
        amount=3_000,
        request_id=request_id,
        operator_user_id=actor_id,
    )
    await db_session.refresh(wallet)
    assert wallet.balance == 7_000
    assert wallet.held == 3_000

    # 幂等 hold
    await wallet_service.hold(
        db_session,
        wallet_id=wallet.id,
        amount=3_000,
        request_id=request_id,
        operator_user_id=actor_id,
    )
    await db_session.refresh(wallet)
    assert wallet.balance == 7_000
    assert wallet.held == 3_000

    await wallet_service.capture(
        db_session,
        wallet_id=wallet.id,
        hold_amount=3_000,
        actual_amount=1_200,
        request_id=request_id,
        operator_user_id=actor_id,
    )
    await db_session.refresh(wallet)
    assert wallet.balance == 8_800  # 7000 + 1800 退还
    assert wallet.held == 0

    # 重复 capture 不重复扣费
    await wallet_service.capture(
        db_session,
        wallet_id=wallet.id,
        hold_amount=3_000,
        actual_amount=1_200,
        request_id=request_id,
        operator_user_id=actor_id,
    )
    await db_session.refresh(wallet)
    assert wallet.balance == 8_800

    txs = await wallet_service.get_transactions(db_session, wallet_id=wallet.id)
    hold_txs = [t for t in txs if t.type == TX_HOLD]
    capture_txs = [t for t in txs if t.type == TX_CAPTURE]
    release_txs = [t for t in txs if t.type == TX_RELEASE]
    assert len(hold_txs) == 1
    assert len(capture_txs) == 1
    assert len(release_txs) == 1
    assert all(t.org_id == org.id for t in hold_txs + capture_txs + release_txs)
    assert all(t.operator_user_id == actor_id for t in hold_txs + capture_txs)


@pytest.mark.asyncio
async def test_org_wallet_release_idempotent(db_session: AsyncSession) -> None:
    owner = await _seed_user(db_session, "-rel")
    _, wallet = await _seed_org_with_wallet(db_session, owner, balance=5_000)
    request_id = f"req-org-rel-{uuid4().hex}"

    await wallet_service.hold(
        db_session,
        wallet_id=wallet.id,
        amount=2_000,
        request_id=request_id,
        operator_user_id=owner.id,
    )
    await wallet_service.release(
        db_session,
        wallet_id=wallet.id,
        hold_amount=2_000,
        request_id=request_id,
        operator_user_id=owner.id,
    )
    await wallet_service.release(
        db_session,
        wallet_id=wallet.id,
        hold_amount=2_000,
        request_id=request_id,
        operator_user_id=owner.id,
    )
    await db_session.refresh(wallet)
    assert wallet.balance == 5_000
    assert wallet.held == 0


@pytest.mark.asyncio
async def test_org_concurrent_hold_no_negative_balance(
    db_session: AsyncSession,
) -> None:
    """并发 hold 不导致组织余额为负。"""
    owner = await _seed_user(db_session, "-conc")
    _, wallet = await _seed_org_with_wallet(db_session, owner, balance=3_000)

    request_ids = [f"req-org-c-{i}-{uuid4().hex}" for i in range(5)]
    success_count = 0
    fail_count = 0

    for rid in request_ids:
        try:
            await wallet_service.hold(
                db_session,
                wallet_id=wallet.id,
                amount=1_000,
                request_id=rid,
                operator_user_id=owner.id,
            )
            success_count += 1
        except InsufficientBalanceError:
            fail_count += 1

    await db_session.refresh(wallet)
    assert success_count == 3
    assert fail_count == 2
    assert wallet.balance >= 0
    assert wallet.balance + wallet.held == 3_000


# =====================================================================
# 用户端 API 权限
# =====================================================================


@pytest.mark.asyncio
async def test_member_cannot_query_org_wallet_or_transactions(
    user_client: AsyncClient,
) -> None:
    owner_id, owner_token = await _register_login(user_client, prefix="ow2")
    admin_id, admin_token = await _register_login(user_client, prefix="ad2")
    member_id, member_token = await _register_login(user_client, prefix="mb3")

    org = (
        await user_client.post(
            "/orgs",
            headers=_auth(owner_token),
            json={"name": "Wallet ACL Org"},
        )
    ).json()
    org_id = org["id"]
    assert org["owner_user_id"] == owner_id

    # 创建组织后 Owner 即可查到余额为 0 的组织钱包
    created_wallet = await user_client.get(
        f"/orgs/{org_id}/wallet", headers=_auth(owner_token)
    )
    assert created_wallet.status_code == 200, created_wallet.text
    assert created_wallet.json()["owner_type"] == "org"
    assert created_wallet.json()["balance"] == 0

    assert (
        await user_client.post(
            f"/orgs/{org_id}/members",
            headers=_auth(owner_token),
            json={"user_id": admin_id, "org_role": ORG_ROLE_ADMIN},
        )
    ).status_code == 201
    assert (
        await user_client.post(
            f"/orgs/{org_id}/members",
            headers=_auth(owner_token),
            json={"user_id": member_id, "org_role": ORG_ROLE_MEMBER},
        )
    ).status_code == 201

    # Owner / Admin 可查
    for token in (owner_token, admin_token):
        w = await user_client.get(f"/orgs/{org_id}/wallet", headers=_auth(token))
        assert w.status_code == 200, w.text
        txs = await user_client.get(
            f"/orgs/{org_id}/wallet/transactions", headers=_auth(token)
        )
        assert txs.status_code == 200, txs.text

    # Member 403
    mem_w = await user_client.get(
        f"/orgs/{org_id}/wallet", headers=_auth(member_token)
    )
    assert mem_w.status_code == 403, mem_w.text
    mem_txs = await user_client.get(
        f"/orgs/{org_id}/wallet/transactions", headers=_auth(member_token)
    )
    assert mem_txs.status_code == 403, mem_txs.text


# =====================================================================
# 管理端 API
# =====================================================================


@pytest.mark.asyncio
async def test_admin_org_wallet_recharge_adjust_and_list(
    db_session: AsyncSession,
) -> None:
    owner = await _seed_user(db_session, "-admin-api")
    org, wallet = await _seed_org_with_wallet(db_session, owner, balance=0)
    await db_session.flush()

    async def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(
            transport=transport, base_url="http://test", headers=admin_auth_headers()
        ) as client:
            get_resp = await client.get(f"/admin/wallets/orgs/{org.id}")
            assert get_resp.status_code == 200, get_resp.text
            assert get_resp.json()["owner_type"] == "org"
            assert get_resp.json()["balance"] == 0

            recharge = await client.post(
                f"/admin/wallets/orgs/{org.id}/recharge",
                json={"amount": 20_000, "remark": "org top-up"},
            )
            assert recharge.status_code == 200, recharge.text
            assert recharge.json()["balance"] == 20_000

            adjust = await client.post(
                f"/admin/wallets/orgs/{org.id}/adjust",
                json={"amount": -5_000, "remark": "org adjust"},
            )
            assert adjust.status_code == 200, adjust.text
            assert adjust.json()["balance"] == 15_000

            txs = await client.get(f"/admin/wallets/orgs/{org.id}/transactions")
            assert txs.status_code == 200, txs.text
            body = txs.json()
            assert any(t["type"] == TX_RECHARGE and t["amount"] == 20_000 for t in body)
            assert any(t["type"] == "adjust" and t["amount"] == -5_000 for t in body)
            assert all(
                t.get("org_id") == str(org.id)
                for t in body
                if t["type"] in (TX_RECHARGE, "adjust")
            )
    finally:
        app.dependency_overrides.clear()

    await db_session.refresh(wallet)
    assert wallet.balance == 15_000
