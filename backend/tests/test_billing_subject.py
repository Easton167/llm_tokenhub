"""计费主体决策矩阵：覆盖 wallet.md 流程图各分支（不含额度累计与网关接入）。"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    BILLING_FALLBACK_DENY,
    BILLING_FALLBACK_PERSONAL,
    BILLING_SUBJECT_PERSONAL,
    BILLING_SUBJECT_TYPE_ORG,
    BILLING_SUBJECT_TYPE_PERSONAL,
    STATUS_DISABLED,
    STATUS_ENABLED,
)
from app.core.errors import (
    ForbiddenError,
    InsufficientBalanceError,
    InvalidRequestError,
)
from app.db.models.api_key import ApiKey
from app.db.models.org import Org, OrgMember
from app.db.models.user import User
from app.db.session import get_db
from app.main import app
from app.services.auth_service import auth_service
from app.services.billing_subject_service import (
    SubjectSource,
    billing_subject_service,
    format_org_subject_ref,
    format_personal_subject_ref,
    parse_billing_subject_ref,
)
from app.services.org_service import org_service
from app.services.wallet_service import wallet_service


async def _seed_user(
    session: AsyncSession,
    *,
    suffix: str = "",
    default_billing_subject: str = BILLING_SUBJECT_PERSONAL,
) -> User:
    user = User(
        email=f"billing-{suffix}-{uuid4().hex[:8]}@example.com",
        status=STATUS_ENABLED,
        default_billing_subject=default_billing_subject,
    )
    session.add(user)
    await session.flush()
    await wallet_service.get_or_create_personal_wallet(session, user.id)
    return user


async def _seed_key(
    session: AsyncSession,
    user: User,
    *,
    billing_subject: str | None = None,
) -> ApiKey:
    api_key, _ = await auth_service.create_api_key(
        session,
        user_id=user.id,
        name="decision-key",
        billing_subject=billing_subject,
    )
    return api_key


async def _seed_org(
    session: AsyncSession,
    owner: User,
    *,
    billing_fallback: str = BILLING_FALLBACK_DENY,
    owner_can_consume: bool = True,
) -> tuple[Org, OrgMember]:
    org = await org_service.create_org(
        session,
        name=f"Org-{uuid4().hex[:6]}",
        owner_user_id=owner.id,
        billing_fallback=billing_fallback,
    )
    membership = await org_service.get_active_membership(
        session, org_id=org.id, user_id=owner.id
    )
    assert membership is not None
    membership.can_consume_org_wallet = owner_can_consume
    await session.flush()
    return org, membership


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def test_parse_billing_subject_ref_variants() -> None:
    uid = uuid4()
    oid = uuid4()
    assert parse_billing_subject_ref("personal") == (
        BILLING_SUBJECT_TYPE_PERSONAL,
        None,
    )
    assert parse_billing_subject_ref(f"user:{uid}") == (
        BILLING_SUBJECT_TYPE_PERSONAL,
        uid,
    )
    assert parse_billing_subject_ref(f"personal:{uid}") == (
        BILLING_SUBJECT_TYPE_PERSONAL,
        uid,
    )
    assert parse_billing_subject_ref(f"org:{oid}") == (
        BILLING_SUBJECT_TYPE_ORG,
        oid,
    )
    with pytest.raises(InvalidRequestError):
        parse_billing_subject_ref("org:")
    with pytest.raises(InvalidRequestError):
        parse_billing_subject_ref("team:abc")


# ---------------------------------------------------------------------------
# 优先级：请求头 > Key > 账户默认
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_header_overrides_key_and_account(db_session: AsyncSession) -> None:
    user = await _seed_user(db_session, suffix="hdr")
    org, _ = await _seed_org(db_session, user, owner_can_consume=True)
    key = await _seed_key(
        db_session, user, billing_subject=format_org_subject_ref(org.id)
    )
    user.default_billing_subject = format_org_subject_ref(org.id)
    await db_session.flush()

    result = await billing_subject_service.resolve(
        db_session,
        user=user,
        api_key=key,
        header_subject=BILLING_SUBJECT_PERSONAL,
    )
    assert result.subject_type == BILLING_SUBJECT_TYPE_PERSONAL
    assert result.source == SubjectSource.HEADER
    assert result.wallet.owner_type == "personal"
    assert result.wallet.owner_id == user.id


@pytest.mark.asyncio
async def test_key_binding_used_when_header_absent(db_session: AsyncSession) -> None:
    user = await _seed_user(db_session, suffix="key-bind")
    org, member = await _seed_org(db_session, user, owner_can_consume=True)
    key = await _seed_key(
        db_session, user, billing_subject=format_org_subject_ref(org.id)
    )

    result = await billing_subject_service.resolve(
        db_session, user=user, api_key=key, header_subject=None
    )
    assert result.subject_type == BILLING_SUBJECT_TYPE_ORG
    assert result.source == SubjectSource.API_KEY
    assert result.org_id == org.id
    assert result.org_member is not None
    assert result.org_member.id == member.id
    assert result.subject_ref == format_org_subject_ref(org.id)


@pytest.mark.asyncio
async def test_unbound_key_uses_account_default_personal(
    db_session: AsyncSession,
) -> None:
    """验收：未指定时 Key 未绑定 → 按个人（账户默认 personal）。"""
    user = await _seed_user(db_session, suffix="unbound-p")
    key = await _seed_key(db_session, user, billing_subject=None)

    result = await billing_subject_service.resolve(
        db_session, user=user, api_key=key, header_subject=None
    )
    assert result.subject_type == BILLING_SUBJECT_TYPE_PERSONAL
    assert result.source == SubjectSource.ACCOUNT_DEFAULT
    assert result.subject_ref == format_personal_subject_ref(user.id)


@pytest.mark.asyncio
async def test_unbound_key_uses_account_default_org(
    db_session: AsyncSession,
) -> None:
    """流程图：未指定 → 账户默认组织优先 → 允许消费 → 组织钱包。"""
    user = await _seed_user(db_session, suffix="unbound-o")
    org, _ = await _seed_org(db_session, user, owner_can_consume=True)
    user.default_billing_subject = format_org_subject_ref(org.id)
    await db_session.flush()
    key = await _seed_key(db_session, user, billing_subject=None)

    result = await billing_subject_service.resolve(
        db_session, user=user, api_key=key, header_subject=None
    )
    assert result.subject_type == BILLING_SUBJECT_TYPE_ORG
    assert result.source == SubjectSource.ACCOUNT_DEFAULT
    assert result.org_id == org.id


# ---------------------------------------------------------------------------
# 显式指定：个人 / 组织 / 无权
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_header_personal_self_ok(db_session: AsyncSession) -> None:
    user = await _seed_user(db_session, suffix="self")
    key = await _seed_key(db_session, user)

    result = await billing_subject_service.resolve(
        db_session,
        user=user,
        api_key=key,
        header_subject=f"user:{user.id}",
    )
    assert result.subject_type == BILLING_SUBJECT_TYPE_PERSONAL
    assert result.source == SubjectSource.HEADER


@pytest.mark.asyncio
async def test_header_other_user_forbidden(db_session: AsyncSession) -> None:
    user = await _seed_user(db_session, suffix="me")
    other = await _seed_user(db_session, suffix="other")
    key = await _seed_key(db_session, user)

    with pytest.raises(ForbiddenError) as exc:
        await billing_subject_service.resolve(
            db_session,
            user=user,
            api_key=key,
            header_subject=f"user:{other.id}",
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_header_org_non_member_forbidden(db_session: AsyncSession) -> None:
    """验收：非成员指定 org:{id} → 403。"""
    owner = await _seed_user(db_session, suffix="owner")
    stranger = await _seed_user(db_session, suffix="stranger")
    org, _ = await _seed_org(db_session, owner, owner_can_consume=True)
    key = await _seed_key(db_session, stranger)

    with pytest.raises(ForbiddenError) as exc:
        await billing_subject_service.resolve(
            db_session,
            user=stranger,
            api_key=key,
            header_subject=format_org_subject_ref(org.id),
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_header_org_member_without_consume_forbidden(
    db_session: AsyncSession,
) -> None:
    """显式指定组织但消费开关关闭 → 403（不回退）。"""
    user = await _seed_user(db_session, suffix="noconsume")
    org, _ = await _seed_org(
        db_session,
        user,
        billing_fallback=BILLING_FALLBACK_PERSONAL,
        owner_can_consume=False,
    )
    key = await _seed_key(db_session, user)

    with pytest.raises(ForbiddenError) as exc:
        await billing_subject_service.resolve(
            db_session,
            user=user,
            api_key=key,
            header_subject=format_org_subject_ref(org.id),
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_header_org_allowed(db_session: AsyncSession) -> None:
    user = await _seed_user(db_session, suffix="org-ok")
    org, member = await _seed_org(db_session, user, owner_can_consume=True)
    key = await _seed_key(db_session, user)

    result = await billing_subject_service.resolve(
        db_session,
        user=user,
        api_key=key,
        header_subject=format_org_subject_ref(org.id),
    )
    assert result.subject_type == BILLING_SUBJECT_TYPE_ORG
    assert result.wallet.owner_type == "org"
    assert result.wallet.owner_id == org.id
    assert result.actor_user_id == member.user_id
    assert result.fell_back_to_personal is False


# ---------------------------------------------------------------------------
# 默认路径回退：允许回退个人 / deny → 402
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_key_org_cannot_consume_fallback_personal(
    db_session: AsyncSession,
) -> None:
    """流程图：OrgChk 否 → 允许回退个人 → 个人钱包。"""
    user = await _seed_user(db_session, suffix="fb-p")
    org, _ = await _seed_org(
        db_session,
        user,
        billing_fallback=BILLING_FALLBACK_PERSONAL,
        owner_can_consume=False,
    )
    key = await _seed_key(
        db_session, user, billing_subject=format_org_subject_ref(org.id)
    )

    result = await billing_subject_service.resolve(
        db_session, user=user, api_key=key, header_subject=None
    )
    assert result.subject_type == BILLING_SUBJECT_TYPE_PERSONAL
    assert result.fell_back_to_personal is True
    assert result.wallet.owner_id == user.id


@pytest.mark.asyncio
async def test_key_org_cannot_consume_fallback_deny(
    db_session: AsyncSession,
) -> None:
    """流程图：OrgChk 否 → 不允许回退 → 402。"""
    user = await _seed_user(db_session, suffix="fb-deny")
    org, _ = await _seed_org(
        db_session,
        user,
        billing_fallback=BILLING_FALLBACK_DENY,
        owner_can_consume=False,
    )
    key = await _seed_key(
        db_session, user, billing_subject=format_org_subject_ref(org.id)
    )

    with pytest.raises(InsufficientBalanceError) as exc:
        await billing_subject_service.resolve(
            db_session, user=user, api_key=key, header_subject=None
        )
    assert exc.value.status_code == 402


@pytest.mark.asyncio
async def test_disabled_org_via_key_fallback_deny(
    db_session: AsyncSession,
) -> None:
    user = await _seed_user(db_session, suffix="dis-org")
    org, _ = await _seed_org(
        db_session,
        user,
        billing_fallback=BILLING_FALLBACK_DENY,
        owner_can_consume=True,
    )
    org.status = STATUS_DISABLED
    await db_session.flush()
    key = await _seed_key(
        db_session, user, billing_subject=format_org_subject_ref(org.id)
    )

    with pytest.raises(InsufficientBalanceError):
        await billing_subject_service.resolve(
            db_session, user=user, api_key=key, header_subject=None
        )


@pytest.mark.asyncio
async def test_removed_member_key_org_fallback_personal(
    db_session: AsyncSession,
) -> None:
    """移除成员后 Key 仍绑组织：非显式路径按回退策略，不可再走组织钱包。"""
    owner = await _seed_user(db_session, suffix="rm-owner")
    member_user = await _seed_user(db_session, suffix="rm-member")
    org, _ = await _seed_org(
        db_session,
        owner,
        billing_fallback=BILLING_FALLBACK_PERSONAL,
        owner_can_consume=True,
    )
    await org_service.invite_member(
        db_session, org=org, actor_user_id=owner.id, user_id=member_user.id
    )
    membership = await org_service.get_active_membership(
        db_session, org_id=org.id, user_id=member_user.id
    )
    assert membership is not None
    membership.can_consume_org_wallet = True
    await db_session.flush()

    key = await _seed_key(
        db_session, member_user, billing_subject=format_org_subject_ref(org.id)
    )
    await org_service.remove_member(
        db_session, org=org, actor_user_id=owner.id, member_id=membership.id
    )

    result = await billing_subject_service.resolve(
        db_session, user=member_user, api_key=key, header_subject=None
    )
    assert result.subject_type == BILLING_SUBJECT_TYPE_PERSONAL
    assert result.fell_back_to_personal is True


# ---------------------------------------------------------------------------
# Key 管理 API：创建/更新默认主体
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keys_api_set_billing_subject(
    db_session: AsyncSession,
) -> None:
    original_commit = db_session.commit

    async def _fake_commit() -> None:
        await db_session.flush()

    db_session.commit = _fake_commit  # type: ignore[method-assign]

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            email = f"key-api-{uuid4().hex[:8]}@example.com"
            password = "password123"
            await client.post(
                "/auth/register",
                json={"email": email, "password": password, "nickname": "k"},
            )
            token = (
                await client.post(
                    "/auth/login", json={"email": email, "password": password}
                )
            ).json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}

            me = (await client.get("/auth/me", headers=headers)).json()
            user = await db_session.get(User, UUID(me["id"]))
            assert user is not None
            org, _ = await _seed_org(db_session, user, owner_can_consume=True)

            created = await client.post(
                "/keys",
                headers=headers,
                json={
                    "name": "org-key",
                    "billing_subject": format_org_subject_ref(org.id),
                },
            )
            assert created.status_code == 201, created.text
            body = created.json()
            assert body["billing_subject"] == format_org_subject_ref(org.id)
            key_id = body["id"]

            patched = await client.patch(
                f"/keys/{key_id}",
                headers=headers,
                json={"billing_subject": "personal"},
            )
            assert patched.status_code == 200, patched.text
            assert patched.json()["billing_subject"] == "personal"

            cleared = await client.patch(
                f"/keys/{key_id}",
                headers=headers,
                json={"billing_subject": None},
            )
            assert cleared.status_code == 200, cleared.text
            assert cleared.json()["billing_subject"] is None

            other = await _seed_user(db_session, suffix="other-org-owner")
            other_org, _ = await _seed_org(db_session, other, owner_can_consume=True)
            bad = await client.post(
                "/keys",
                headers=headers,
                json={
                    "name": "bad",
                    "billing_subject": format_org_subject_ref(other_org.id),
                },
            )
            assert bad.status_code == 403
    finally:
        db_session.commit = original_commit  # type: ignore[method-assign]
        app.dependency_overrides.clear()
