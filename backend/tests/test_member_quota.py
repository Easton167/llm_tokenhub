"""成员额度：配置 API、quota_check/consume、惰性周期重置、并发超限、BillingSubject 回退。"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    BILLING_FALLBACK_DENY,
    BILLING_FALLBACK_PERSONAL,
    BILLING_SUBJECT_TYPE_ORG,
    BILLING_SUBJECT_TYPE_PERSONAL,
    QUOTA_PERIOD_MONTHLY,
    QUOTA_PERIOD_NONE,
    STATUS_ENABLED,
)
from app.core.errors import InsufficientBalanceError
from app.db.models.org import MemberQuota, Org, OrgMember
from app.db.models.user import User
from app.db.session import get_db
from app.main import app
from app.services.auth_service import auth_service
from app.services.billing_subject_service import (
    billing_subject_service,
    format_org_subject_ref,
)
from app.services.org_service import org_service
from app.services.quota_service import next_period_reset_at, quota_service
from app.services.wallet_service import wallet_service


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
        email=f"quota-{suffix}-{uuid4().hex[:8]}@example.com",
        status=STATUS_ENABLED,
    )
    session.add(user)
    await session.flush()
    await wallet_service.get_or_create_personal_wallet(session, user.id)
    return user


async def _seed_org_member(
    session: AsyncSession,
    owner: User,
    *,
    billing_fallback: str = BILLING_FALLBACK_DENY,
    can_consume: bool = True,
    org_balance: int = 10_000,
) -> tuple[Org, OrgMember]:
    org = await org_service.create_org(
        session,
        name=f"QuotaOrg-{uuid4().hex[:6]}",
        owner_user_id=owner.id,
        billing_fallback=billing_fallback,
    )
    membership = await org_service.get_active_membership(
        session, org_id=org.id, user_id=owner.id
    )
    assert membership is not None
    membership.can_consume_org_wallet = can_consume
    wallet = await wallet_service.require_org_wallet(session, org.id)
    wallet.balance = org_balance
    await session.flush()
    return org, membership


# =====================================================================
# 周期锚点 / 惰性重置
# =====================================================================


def test_next_period_reset_at_monthly_and_none() -> None:
    now = datetime(2026, 7, 23, 15, 0, tzinfo=UTC)
    assert next_period_reset_at(QUOTA_PERIOD_NONE, now) is None
    assert next_period_reset_at(QUOTA_PERIOD_MONTHLY, now) == datetime(
        2026, 8, 1, 0, 0, tzinfo=UTC
    )
    assert next_period_reset_at("daily", now) == datetime(
        2026, 7, 24, 0, 0, tzinfo=UTC
    )


@pytest.mark.asyncio
async def test_lazy_period_reset_zeros_used(db_session: AsyncSession) -> None:
    """跨周期后读取/消费前惰性归零。"""
    owner = await _seed_user(db_session, "lazy")
    _, member = await _seed_org_member(db_session, owner)
    past = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    quota = MemberQuota(
        org_member_id=member.id,
        quota_period=QUOTA_PERIOD_MONTHLY,
        quota_limit=5_000,
        quota_used=4_000,
        period_reset_at=past,
    )
    db_session.add(quota)
    await db_session.flush()

    now = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    fresh = await quota_service.ensure_fresh_quota(
        db_session, member.id, for_update=True, now=now
    )
    assert fresh is not None
    assert fresh.quota_used == 0
    assert fresh.period_reset_at == datetime(2026, 8, 1, 0, 0, tzinfo=UTC)


# =====================================================================
# quota_check / quota_consume
# =====================================================================


@pytest.mark.asyncio
async def test_quota_check_stricter_of_balance_and_quota(
    db_session: AsyncSession,
) -> None:
    owner = await _seed_user(db_session, "chk")
    org, member = await _seed_org_member(db_session, owner, org_balance=3_000)
    await quota_service.configure_member_quota(
        db_session,
        org=org,
        actor_user_id=owner.id,
        member_id=member.id,
        can_consume_org_wallet=True,
        quota_period=QUOTA_PERIOD_NONE,
        quota_limit=1_000,
    )

    # 额度更严
    r1 = await quota_service.quota_check(
        db_session, org_id=org.id, org_member_id=member.id, amount=1_500
    )
    assert r1.ok is False
    assert r1.quota_remaining == 1_000
    assert r1.org_available == 3_000
    assert r1.effective_available == 1_000

    # 余额更严：提高额度后余额卡住
    await quota_service.configure_member_quota(
        db_session,
        org=org,
        actor_user_id=owner.id,
        member_id=member.id,
        quota_limit=10_000,
    )
    r2 = await quota_service.quota_check(
        db_session, org_id=org.id, org_member_id=member.id, amount=5_000
    )
    assert r2.ok is False
    assert r2.effective_available == 3_000

    r3 = await quota_service.quota_check(
        db_session, org_id=org.id, org_member_id=member.id, amount=2_000
    )
    assert r3.ok is True


@pytest.mark.asyncio
async def test_quota_consume_and_exhaust(db_session: AsyncSession) -> None:
    owner = await _seed_user(db_session, "csm")
    org, member = await _seed_org_member(db_session, owner)
    await quota_service.configure_member_quota(
        db_session,
        org=org,
        actor_user_id=owner.id,
        member_id=member.id,
        can_consume_org_wallet=True,
        quota_period=QUOTA_PERIOD_NONE,
        quota_limit=1_000,
    )

    await quota_service.quota_consume(
        db_session, org_member_id=member.id, amount=600
    )
    q = await quota_service.get_quota(db_session, member.id)
    assert q is not None
    assert q.quota_used == 600

    with pytest.raises(InsufficientBalanceError):
        await quota_service.quota_consume(
            db_session, org_member_id=member.id, amount=500
        )

    await quota_service.quota_consume(
        db_session, org_member_id=member.id, amount=400
    )
    await db_session.refresh(q)
    assert q.quota_used == 1_000
    assert await quota_service.is_quota_available(
        db_session, org_member_id=member.id, amount=1
    ) is False


@pytest.mark.asyncio
async def test_concurrent_consume_does_not_exceed_limit(
    db_session: AsyncSession,
) -> None:
    """串行模拟并发竞争：累计不超过上限（行锁语义由 FOR UPDATE 保证）。"""
    owner = await _seed_user(db_session, "conc")
    org, member = await _seed_org_member(db_session, owner)
    await quota_service.configure_member_quota(
        db_session,
        org=org,
        actor_user_id=owner.id,
        member_id=member.id,
        can_consume_org_wallet=True,
        quota_period=QUOTA_PERIOD_NONE,
        quota_limit=3_000,
    )

    success = 0
    fail = 0
    for _ in range(5):
        try:
            await quota_service.quota_consume(
                db_session, org_member_id=member.id, amount=1_000
            )
            success += 1
        except InsufficientBalanceError:
            fail += 1

    q = await quota_service.get_quota(db_session, member.id)
    assert q is not None
    assert success == 3
    assert fail == 2
    assert q.quota_used == 3_000


@pytest.mark.asyncio
async def test_period_quota_resets_then_consume_again(
    db_session: AsyncSession,
) -> None:
    owner = await _seed_user(db_session, "period")
    org, member = await _seed_org_member(db_session, owner)
    await quota_service.configure_member_quota(
        db_session,
        org=org,
        actor_user_id=owner.id,
        member_id=member.id,
        can_consume_org_wallet=True,
        quota_period=QUOTA_PERIOD_MONTHLY,
        quota_limit=2_000,
        now=datetime(2026, 7, 10, tzinfo=UTC),
    )
    await quota_service.quota_consume(
        db_session,
        org_member_id=member.id,
        amount=2_000,
        now=datetime(2026, 7, 10, tzinfo=UTC),
    )
    assert (
        await quota_service.is_quota_available(
            db_session,
            org_member_id=member.id,
            amount=1,
            now=datetime(2026, 7, 20, tzinfo=UTC),
        )
        is False
    )

    # 跨入 8 月：应归零并可再消费
    await quota_service.quota_consume(
        db_session,
        org_member_id=member.id,
        amount=1_500,
        now=datetime(2026, 8, 2, tzinfo=UTC),
    )
    q = await quota_service.get_quota(db_session, member.id)
    assert q is not None
    assert q.quota_used == 1_500
    assert q.period_reset_at == datetime(2026, 9, 1, 0, 0, tzinfo=UTC)


# =====================================================================
# BillingSubject 接入
# =====================================================================


@pytest.mark.asyncio
async def test_billing_subject_quota_exhausted_fallback_personal(
    db_session: AsyncSession,
) -> None:
    owner = await _seed_user(db_session, "fb-q")
    org, member = await _seed_org_member(
        db_session,
        owner,
        billing_fallback=BILLING_FALLBACK_PERSONAL,
        can_consume=True,
    )
    await quota_service.configure_member_quota(
        db_session,
        org=org,
        actor_user_id=owner.id,
        member_id=member.id,
        quota_period=QUOTA_PERIOD_NONE,
        quota_limit=100,
    )
    await quota_service.quota_consume(
        db_session, org_member_id=member.id, amount=100
    )
    key, _ = await auth_service.create_api_key(
        db_session,
        user_id=owner.id,
        name="k",
        billing_subject=format_org_subject_ref(org.id),
    )

    result = await billing_subject_service.resolve(
        db_session, user=owner, api_key=key, header_subject=None
    )
    assert result.subject_type == BILLING_SUBJECT_TYPE_PERSONAL
    assert result.fell_back_to_personal is True


@pytest.mark.asyncio
async def test_billing_subject_quota_exhausted_deny_402(
    db_session: AsyncSession,
) -> None:
    owner = await _seed_user(db_session, "deny-q")
    org, member = await _seed_org_member(
        db_session,
        owner,
        billing_fallback=BILLING_FALLBACK_DENY,
        can_consume=True,
    )
    await quota_service.configure_member_quota(
        db_session,
        org=org,
        actor_user_id=owner.id,
        member_id=member.id,
        quota_period=QUOTA_PERIOD_NONE,
        quota_limit=50,
    )
    await quota_service.quota_consume(
        db_session, org_member_id=member.id, amount=50
    )
    key, _ = await auth_service.create_api_key(
        db_session,
        user_id=owner.id,
        name="k",
        billing_subject=format_org_subject_ref(org.id),
    )

    with pytest.raises(InsufficientBalanceError) as exc:
        await billing_subject_service.resolve(
            db_session, user=owner, api_key=key, header_subject=None
        )
    assert exc.value.status_code == 402


@pytest.mark.asyncio
async def test_billing_subject_header_quota_exhausted_still_fallback(
    db_session: AsyncSession,
) -> None:
    """显式指定组织但额度耗尽：走回退（非 403）。"""
    owner = await _seed_user(db_session, "hdr-q")
    org, member = await _seed_org_member(
        db_session,
        owner,
        billing_fallback=BILLING_FALLBACK_PERSONAL,
        can_consume=True,
    )
    await quota_service.configure_member_quota(
        db_session,
        org=org,
        actor_user_id=owner.id,
        member_id=member.id,
        quota_limit=10,
    )
    await quota_service.quota_consume(
        db_session, org_member_id=member.id, amount=10
    )
    key, _ = await auth_service.create_api_key(
        db_session, user_id=owner.id, name="k"
    )

    result = await billing_subject_service.resolve(
        db_session,
        user=owner,
        api_key=key,
        header_subject=format_org_subject_ref(org.id),
    )
    assert result.subject_type == BILLING_SUBJECT_TYPE_PERSONAL
    assert result.fell_back_to_personal is True


@pytest.mark.asyncio
async def test_disable_consume_switch_blocks_org_immediately(
    db_session: AsyncSession,
) -> None:
    owner = await _seed_user(db_session, "sw")
    org, member = await _seed_org_member(
        db_session,
        owner,
        billing_fallback=BILLING_FALLBACK_DENY,
        can_consume=True,
    )
    key, _ = await auth_service.create_api_key(
        db_session,
        user_id=owner.id,
        name="k",
        billing_subject=format_org_subject_ref(org.id),
    )
    ok = await billing_subject_service.resolve(
        db_session, user=owner, api_key=key, header_subject=None
    )
    assert ok.subject_type == BILLING_SUBJECT_TYPE_ORG

    await quota_service.configure_member_quota(
        db_session,
        org=org,
        actor_user_id=owner.id,
        member_id=member.id,
        can_consume_org_wallet=False,
    )
    with pytest.raises(InsufficientBalanceError):
        await billing_subject_service.resolve(
            db_session, user=owner, api_key=key, header_subject=None
        )


# =====================================================================
# 组织端配置 API
# =====================================================================


@pytest.mark.asyncio
async def test_org_api_set_member_quota_and_switch(
    user_client: AsyncClient,
) -> None:
    owner_id, owner_token = await _register_login(user_client, prefix="q-own")
    member_id, member_token = await _register_login(user_client, prefix="q-mem")

    org = (
        await user_client.post(
            "/orgs",
            headers=_auth(owner_token),
            json={"name": "Quota API Org", "billing_fallback": "deny"},
        )
    ).json()
    org_id = org["id"]
    assert org["owner_user_id"] == owner_id

    invite = await user_client.post(
        f"/orgs/{org_id}/members",
        headers=_auth(owner_token),
        json={"user_id": member_id, "org_role": "member"},
    )
    assert invite.status_code == 201, invite.text
    mid = invite.json()["id"]

    # Member 不可改额度
    forbidden = await user_client.patch(
        f"/orgs/{org_id}/members/{mid}",
        headers=_auth(member_token),
        json={"can_consume_org_wallet": True, "quota_limit": 1000},
    )
    assert forbidden.status_code == 403

    # Owner 设置
    updated = await user_client.patch(
        f"/orgs/{org_id}/members/{mid}",
        headers=_auth(owner_token),
        json={
            "can_consume_org_wallet": True,
            "quota_period": "monthly",
            "quota_limit": 5000,
        },
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["can_consume_org_wallet"] is True
    assert body["quota"]["quota_period"] == "monthly"
    assert body["quota"]["quota_limit"] == 5000
    assert body["quota"]["quota_used"] == 0
    assert body["quota"]["period_reset_at"] is not None

    listed = await user_client.get(
        f"/orgs/{org_id}/members", headers=_auth(owner_token)
    )
    assert listed.status_code == 200
    row = next(m for m in listed.json() if m["id"] == mid)
    assert row["quota"]["quota_limit"] == 5000

    # 关闭开关
    closed = await user_client.patch(
        f"/orgs/{org_id}/members/{mid}",
        headers=_auth(owner_token),
        json={"can_consume_org_wallet": False},
    )
    assert closed.status_code == 200
    assert closed.json()["can_consume_org_wallet"] is False


@pytest.mark.asyncio
async def test_unlimited_quota_limit_zero(db_session: AsyncSession) -> None:
    """quota_limit=0 表示不限制；consume 不累计。"""
    owner = await _seed_user(db_session, "unlim")
    org, member = await _seed_org_member(db_session, owner, org_balance=100)
    await quota_service.configure_member_quota(
        db_session,
        org=org,
        actor_user_id=owner.id,
        member_id=member.id,
        can_consume_org_wallet=True,
        quota_limit=0,
        quota_period=QUOTA_PERIOD_NONE,
    )
    check = await quota_service.quota_check(
        db_session, org_id=org.id, org_member_id=member.id, amount=100
    )
    assert check.ok is True
    assert check.quota_remaining is None
    assert check.effective_available == 100

    q = await quota_service.quota_consume(
        db_session, org_member_id=member.id, amount=100
    )
    assert q is not None
    assert q.quota_used == 0
