"""后台赠送 / 撤回 API：权限、幂等、风控、审计与前台只读。"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, time
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.constants import STATUS_ENABLED
from app.core.datetime_utils import business_day_end_utc
from app.core.security import create_access_token
from app.db.models.log import AuditLog
from app.db.models.user import User
from app.db.models.wallet import GiftGrant, Wallet
from app.db.session import get_db
from app.main import app
from app.services.gift_service import GRANT_STATUS_ACTIVE, GRANT_STATUS_REVOKED, gift_service
from app.services.org_service import org_service
from app.services.wallet_service import wallet_service
from tests.conftest import admin_auth_headers


@pytest_asyncio.fixture
async def api_client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


async def _seed_user(session: AsyncSession, suffix: str = "") -> User:
    user = User(
        email=f"gift-api{suffix}-{uuid4().hex[:8]}@example.com",
        status=STATUS_ENABLED,
    )
    session.add(user)
    await session.flush()
    return user


def _grant_body(
    *,
    amount: int = 1_000,
    key: str | None = None,
    expires_in_days: int | None = 30,
    never_expires: bool = False,
    reason: str = "promotion",
    remark: str | None = "测试赠送",
) -> dict:
    body: dict = {
        "amount": amount,
        "reason": reason,
        "idempotency_key": key or f"key-{uuid4().hex}",
        "remark": remark,
    }
    if never_expires:
        body["never_expires"] = True
    elif expires_in_days is not None:
        body["expires_in_days"] = expires_in_days
    return body


async def _active_remaining(session: AsyncSession, wallet_id: UUID) -> int:
    stmt = select(func.coalesce(func.sum(GiftGrant.remaining), 0)).where(
        GiftGrant.wallet_id == wallet_id,
        GiftGrant.status == GRANT_STATUS_ACTIVE,
    )
    return int((await session.execute(stmt)).scalar_one())


async def _assert_invariant(session: AsyncSession, wallet: Wallet) -> None:
    await session.refresh(wallet)
    remaining = await _active_remaining(session, wallet.id)
    assert remaining == wallet.gift_balance + wallet.gift_held


def test_business_day_end_is_shanghai_235959() -> None:
    now = datetime(2026, 8, 16, 2, 0, tzinfo=UTC)  # 上海 10:00
    end = business_day_end_utc(1, now=now, tz_name="Asia/Shanghai")
    local = end.astimezone(ZoneInfo("Asia/Shanghai"))
    assert local.date().isoformat() == "2026-08-17"
    assert local.time() == time(23, 59, 59)


@pytest.mark.asyncio
async def test_grant_increases_gift_balance(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _seed_user(db_session)
    wallet = await wallet_service.get_or_create_personal_wallet(db_session, user.id)

    resp = await api_client.post(
        f"/admin/wallets/users/{user.id}/gifts",
        headers=admin_auth_headers(),
        json=_grant_body(amount=5_000, remark="营销活动发放"),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["amount"] == 5_000
    assert body["remaining"] == 5_000
    assert body["reason"] == "promotion"
    assert body["status"] == GRANT_STATUS_ACTIVE

    await db_session.refresh(wallet)
    assert wallet.gift_balance == 5_000
    assert wallet.balance == 0
    wallet_resp = await api_client.get(
        f"/admin/wallets/users/{user.id}", headers=admin_auth_headers()
    )
    assert wallet_resp.status_code == 200
    w = wallet_resp.json()
    assert w["gift_balance"] == 5_000
    assert w["gift_held"] == 0
    assert w["balance"] == 0
    assert w["total_available"] == 5_000
    await _assert_invariant(db_session, wallet)


@pytest.mark.asyncio
async def test_grant_idempotency_key_does_not_double(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _seed_user(db_session)
    wallet = await wallet_service.get_or_create_personal_wallet(db_session, user.id)
    key = f"idem-{uuid4().hex}"
    payload = _grant_body(amount=2_000, key=key)

    first = await api_client.post(
        f"/admin/wallets/users/{user.id}/gifts",
        headers=admin_auth_headers(),
        json=payload,
    )
    second = await api_client.post(
        f"/admin/wallets/users/{user.id}/gifts",
        headers=admin_auth_headers(),
        json=payload,
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["id"] == second.json()["id"]

    await db_session.refresh(wallet)
    assert wallet.gift_balance == 2_000
    grants = (
        await db_session.execute(
            select(func.count()).select_from(GiftGrant).where(GiftGrant.wallet_id == wallet.id)
        )
    ).scalar_one()
    assert grants == 1
    await _assert_invariant(db_session, wallet)


@pytest.mark.asyncio
async def test_grant_exceeds_per_grant_limit(
    api_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "gift_max_amount_per_grant", 100)
    user = await _seed_user(db_session)
    resp = await api_client.post(
        f"/admin/wallets/users/{user.id}/gifts",
        headers=admin_auth_headers(),
        json=_grant_body(amount=101),
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "gift_amount_exceeds_limit"


@pytest.mark.asyncio
async def test_grant_exceeds_daily_limit(
    api_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "gift_max_amount_per_grant", 10_000)
    monkeypatch.setattr(settings, "gift_max_amount_per_operator_daily", 1_000)
    user = await _seed_user(db_session)
    first = await api_client.post(
        f"/admin/wallets/users/{user.id}/gifts",
        headers=admin_auth_headers(),
        json=_grant_body(amount=600),
    )
    assert first.status_code == 200, first.text
    second = await api_client.post(
        f"/admin/wallets/users/{user.id}/gifts",
        headers=admin_auth_headers(),
        json=_grant_body(amount=500),
    )
    assert second.status_code == 422
    assert second.json()["error"]["code"] == "gift_amount_exceeds_limit"


@pytest.mark.asyncio
async def test_grant_rejects_non_admin(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _seed_user(db_session)
    token = create_access_token(user_id=user.id)
    resp = await api_client.post(
        f"/admin/wallets/users/{user.id}/gifts",
        headers={"Authorization": f"Bearer {token}"},
        json=_grant_body(amount=100),
    )
    # 前台 JWT 在管理端等同未认证（typ 不匹配），与其余 /admin 路由一致返回 401
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


@pytest.mark.asyncio
async def test_never_expires_requires_explicit_flag(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _seed_user(db_session)
    resp = await api_client.post(
        f"/admin/wallets/users/{user.id}/gifts",
        headers=admin_auth_headers(),
        json={
            "amount": 100,
            "reason": "promotion",
            "idempotency_key": f"key-{uuid4().hex}",
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_revoke_unconsumed_and_audit(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _seed_user(db_session)
    wallet = await wallet_service.get_or_create_personal_wallet(db_session, user.id)
    granted = await api_client.post(
        f"/admin/wallets/users/{user.id}/gifts",
        headers=admin_auth_headers(),
        json=_grant_body(amount=800, never_expires=True),
    )
    assert granted.status_code == 200, granted.text
    grant_id = granted.json()["id"]

    revoked = await api_client.post(
        f"/admin/gifts/{grant_id}/revoke",
        headers=admin_auth_headers(),
        json={"remark": "活动取消"},
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["status"] == GRANT_STATUS_REVOKED
    assert revoked.json()["remaining"] == 0

    await db_session.refresh(wallet)
    assert wallet.gift_balance == 0
    await _assert_invariant(db_session, wallet)

    # 必须按本次 grant 过滤：审计表是共享的，历史遗留行会让 next() 取到别的批次
    logs = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.action.in_(("wallet.gift_grant", "wallet.gift_revoke")),
                AuditLog.detail["grant_id"].astext == grant_id,
            )
        )
    ).scalars().all()
    actions = {log.action for log in logs}
    assert "wallet.gift_grant" in actions
    assert "wallet.gift_revoke" in actions
    grant_log = next(log for log in logs if log.action == "wallet.gift_grant")
    assert grant_log.detail["grant_id"] == grant_id
    assert grant_log.detail["amount"] == 800
    assert grant_log.detail["wallet_id"] == str(wallet.id)


@pytest.mark.asyncio
async def test_revoke_consumed_returns_409(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _seed_user(db_session)
    wallet = await wallet_service.get_or_create_personal_wallet(db_session, user.id)
    granted = await api_client.post(
        f"/admin/wallets/users/{user.id}/gifts",
        headers=admin_auth_headers(),
        json=_grant_body(amount=100, never_expires=True),
    )
    assert granted.status_code == 200, granted.text
    grant_id = granted.json()["id"]

    request_id = f"req-gift-{uuid4().hex}"
    await wallet_service.hold(
        db_session, wallet_id=wallet.id, amount=100, request_id=request_id
    )
    await wallet_service.capture(
        db_session,
        wallet_id=wallet.id,
        hold_amount=100,
        actual_amount=100,
        request_id=request_id,
    )

    resp = await api_client.post(
        f"/admin/gifts/{grant_id}/revoke",
        headers=admin_auth_headers(),
        json={},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "gift_already_consumed"


@pytest.mark.asyncio
async def test_org_wallet_grant(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    owner = await _seed_user(db_session, "-org")
    org = await org_service.create_org(
        db_session, name=f"GiftOrg-{uuid4().hex[:6]}", owner_user_id=owner.id
    )
    wallet = await wallet_service.get_or_create_org_wallet(db_session, org.id)

    resp = await api_client.post(
        f"/admin/wallets/orgs/{org.id}/gifts",
        headers=admin_auth_headers(),
        json=_grant_body(amount=3_000, reason="partnership"),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["amount"] == 3_000
    await db_session.refresh(wallet)
    assert wallet.gift_balance == 3_000
    listed = await api_client.get(
        f"/admin/wallets/{wallet.id}/gifts",
        headers=admin_auth_headers(),
    )
    assert listed.status_code == 200
    assert len(listed.json()) == 1
    await _assert_invariant(db_session, wallet)


@pytest.mark.asyncio
async def test_user_me_gifts_hides_operator(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _seed_user(db_session)
    token = create_access_token(user_id=user.id)
    granted = await api_client.post(
        f"/admin/wallets/users/{user.id}/gifts",
        headers=admin_auth_headers(),
        json=_grant_body(amount=1_200, never_expires=True),
    )
    assert granted.status_code == 200, granted.text

    me = await api_client.get(
        "/wallets/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert me.status_code == 200
    assert me.json()["gift_balance"] == 1_200
    assert me.json()["total_available"] == 1_200
    assert "granted_by" not in me.json()

    gifts = await api_client.get(
        "/wallets/me/gifts", headers={"Authorization": f"Bearer {token}"}
    )
    assert gifts.status_code == 200
    items = gifts.json()
    assert len(items) == 1
    assert items[0]["amount"] == 1_200
    assert items[0]["remaining"] == 1_200
    assert "granted_by" not in items[0]
    assert "revoked_by" not in items[0]
    assert "idempotency_key" not in items[0]


@pytest.mark.asyncio
async def test_grant_hold_capture_revoke_loop(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """赠送 → gift_balance 增加 → 优先扣赠金 → 撤回剩余。"""
    user = await _seed_user(db_session)
    wallet = await wallet_service.get_or_create_personal_wallet(db_session, user.id)
    wallet.balance = 1_000
    await db_session.flush()

    granted = await api_client.post(
        f"/admin/wallets/users/{user.id}/gifts",
        headers=admin_auth_headers(),
        json=_grant_body(amount=100, never_expires=True),
    )
    assert granted.status_code == 200, granted.text
    grant_id = granted.json()["id"]
    await db_session.refresh(wallet)
    assert wallet.gift_balance == 100

    request_id = f"req-loop-{uuid4().hex}"
    await wallet_service.hold(
        db_session, wallet_id=wallet.id, amount=60, request_id=request_id
    )
    await wallet_service.capture(
        db_session,
        wallet_id=wallet.id,
        hold_amount=60,
        actual_amount=60,
        request_id=request_id,
    )
    await db_session.refresh(wallet)
    assert wallet.gift_balance == 40
    assert wallet.balance == 1_000

    revoked = await api_client.post(
        f"/admin/gifts/{grant_id}/revoke",
        headers=admin_auth_headers(),
        json={},
    )
    assert revoked.status_code == 200, revoked.text
    await db_session.refresh(wallet)
    assert wallet.gift_balance == 0
    assert wallet.balance == 1_000
    await _assert_invariant(db_session, wallet)
