"""组织域模型基础行为测试（不覆盖业务 API）。"""

from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MemberQuota, Org, OrgMember, User, Wallet


async def _make_user(session: AsyncSession, suffix: str) -> User:
    user = User(
        id=uuid4(),
        email=f"org-{suffix}-{uuid4().hex[:8]}@example.com",
        nickname=suffix,
        status=0,
        default_billing_subject="personal",
    )
    session.add(user)
    await session.flush()
    return user


@pytest.mark.asyncio
async def test_org_member_org_user_unique(db_session: AsyncSession) -> None:
    user = await _make_user(db_session, "owner")
    org = Org(
        id=uuid4(),
        name="Acme",
        owner_user_id=user.id,
        status=0,
        billing_fallback="deny",
    )
    db_session.add(org)
    await db_session.flush()

    db_session.add(
        OrgMember(
            id=uuid4(),
            org_id=org.id,
            user_id=user.id,
            org_role="owner",
            can_consume_org_wallet=True,
            status=0,
        )
    )
    await db_session.flush()

    async with db_session.begin_nested():
        db_session.add(
            OrgMember(
                id=uuid4(),
                org_id=org.id,
                user_id=user.id,
                org_role="member",
                can_consume_org_wallet=False,
                status=0,
            )
        )
        with pytest.raises(IntegrityError):
            await db_session.flush()


@pytest.mark.asyncio
async def test_one_org_one_wallet(db_session: AsyncSession) -> None:
    user = await _make_user(db_session, "wallet")
    org = Org(
        id=uuid4(),
        name="Beta",
        owner_user_id=user.id,
        status=0,
        billing_fallback="personal",
    )
    db_session.add(org)
    await db_session.flush()

    db_session.add(
        Wallet(
            id=uuid4(),
            owner_type="org",
            owner_id=org.id,
            balance=100,
            held=0,
            currency="CNY",
            credit_limit=0,
        )
    )
    await db_session.flush()

    async with db_session.begin_nested():
        db_session.add(
            Wallet(
                id=uuid4(),
                owner_type="org",
                owner_id=org.id,
                balance=0,
                held=0,
                currency="CNY",
                credit_limit=0,
            )
        )
        with pytest.raises(IntegrityError):
            await db_session.flush()


@pytest.mark.asyncio
async def test_member_quota_amount_fields(db_session: AsyncSession) -> None:
    user = await _make_user(db_session, "quota")
    org = Org(
        id=uuid4(),
        name="Quota Org",
        owner_user_id=user.id,
        status=0,
        billing_fallback="deny",
    )
    db_session.add(org)
    await db_session.flush()
    member = OrgMember(
        id=uuid4(),
        org_id=org.id,
        user_id=user.id,
        org_role="owner",
        can_consume_org_wallet=True,
        status=0,
    )
    db_session.add(member)
    await db_session.flush()

    quota = MemberQuota(
        id=uuid4(),
        org_member_id=member.id,
        quota_period="monthly",
        quota_limit=1_000_000,
        quota_used=0,
    )
    db_session.add(quota)
    await db_session.flush()

    assert quota.quota_limit == 1_000_000
    assert quota.quota_used == 0
    assert isinstance(quota.quota_limit, int)
