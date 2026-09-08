"""后台 RBAC 端到端：登录、菜单过滤、按权限码放行/拒绝、提权防护。"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import STATUS_DISABLED
from app.core.security import hash_password
from app.db.models.admin import AdminRole, AdminRolePermission, AdminUser, AdminUserRole
from app.db.session import get_db
from app.main import app


@pytest_asyncio.fixture
async def api_client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


PASSWORD = "admin-pass-1234"


async def _seed_admin(
    session: AsyncSession,
    *,
    permissions: list[str] | None = None,
    is_superadmin: bool = False,
    status: int = 0,
) -> AdminUser:
    admin = AdminUser(
        username=f"rbac-{uuid4().hex[:8]}",
        password_hash=hash_password(PASSWORD),
        is_superadmin=is_superadmin,
        status=status,
    )
    session.add(admin)
    await session.flush()
    if permissions:
        role = AdminRole(code=f"r-{uuid4().hex[:8]}", name="测试角色")
        role.permissions = [AdminRolePermission(code=c) for c in permissions]
        session.add(role)
        await session.flush()
        session.add(AdminUserRole(admin_user_id=admin.id, role_id=role.id))
        await session.flush()
    return admin


async def _login(client: AsyncClient, username: str, password: str = PASSWORD) -> str:
    resp = await client.post(
        "/admin/auth/login", json={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def _headers(client: AsyncClient, admin: AdminUser) -> dict[str, str]:
    return {"Authorization": f"Bearer {await _login(client, admin.username)}"}


@pytest.mark.asyncio
async def test_env_fallback_admin_is_superadmin(api_client: AsyncClient) -> None:
    """库里没有同名账号时，环境变量账号仍可登录并拿到全部菜单。"""
    from app.core.config import settings

    token = await _login(api_client, settings.admin_username, settings.admin_password)
    me = await api_client.get("/admin/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200, me.text
    body = me.json()
    assert body["is_superadmin"] is True
    assert {m["key"] for m in body["menus"]} >= {"channel", "model", "admin_account"}


@pytest.mark.asyncio
async def test_menus_filtered_by_view_permission(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin = await _seed_admin(db_session, permissions=["channel:view", "model:view"])
    me = await api_client.get("/admin/auth/me", headers=await _headers(api_client, admin))
    assert me.status_code == 200, me.text
    body = me.json()
    assert body["is_superadmin"] is False
    assert [m["key"] for m in body["menus"]] == ["channel", "model"]


@pytest.mark.asyncio
async def test_view_permission_does_not_grant_write(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin = await _seed_admin(db_session, permissions=["channel:view"])
    headers = await _headers(api_client, admin)

    assert (await api_client.get("/admin/channels", headers=headers)).status_code == 200

    created = await api_client.post(
        "/admin/channels",
        headers=headers,
        json={"name": "x", "provider_id": str(uuid4()), "base_url": "http://x"},
    )
    assert created.status_code == 403
    assert created.json()["error"]["code"] == "forbidden"

    # 没有 model:view 就连读都不行
    assert (await api_client.get("/admin/models", headers=headers)).status_code == 403


@pytest.mark.asyncio
async def test_delete_is_separate_from_manage(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin = await _seed_admin(db_session, permissions=["channel:view", "channel:manage"])
    headers = await _headers(api_client, admin)
    resp = await api_client.delete(f"/admin/channels/{uuid4()}", headers=headers)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_gift_is_separable_from_wallet(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """钱包写权限不应顺带把赠送额度也给出去。"""
    admin = await _seed_admin(db_session, permissions=["wallet:view", "wallet:manage"])
    headers = await _headers(api_client, admin)
    resp = await api_client.post(
        f"/admin/wallets/users/{uuid4()}/gifts",
        headers=headers,
        json={"amount": 100, "reason": "promotion", "idempotency_key": uuid4().hex},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_disabled_account_cannot_use_existing_token(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin = await _seed_admin(db_session, permissions=["channel:view"])
    headers = await _headers(api_client, admin)
    assert (await api_client.get("/admin/channels", headers=headers)).status_code == 200

    admin.status = STATUS_DISABLED
    await db_session.flush()
    assert (await api_client.get("/admin/channels", headers=headers)).status_code == 401


@pytest.mark.asyncio
async def test_cannot_grant_permissions_you_do_not_hold(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """有账号管理权的非超管，不能给角色塞自己没有的权限来自我提权。"""
    admin = await _seed_admin(
        db_session, permissions=["admin_account:view", "admin_account:manage"]
    )
    headers = await _headers(api_client, admin)
    resp = await api_client.post(
        "/admin/admin-roles",
        headers=headers,
        json={
            "code": f"esc-{uuid4().hex[:8]}",
            "name": "提权尝试",
            "permissions": ["wallet:manage"],
        },
    )
    assert resp.status_code == 403
    assert "wallet:manage" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_cannot_assign_a_role_stronger_than_your_own(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """挡住权限码还不够：直接把现成的高权角色挂到自己头上也是提权。"""
    strong = AdminRole(code=f"strong-{uuid4().hex[:8]}", name="财务")
    strong.permissions = [AdminRolePermission(code="wallet:manage")]
    db_session.add(strong)
    await db_session.flush()

    admin = await _seed_admin(
        db_session, permissions=["admin_account:view", "admin_account:manage"]
    )
    resp = await api_client.patch(
        f"/admin/accounts/{admin.id}",
        headers=await _headers(api_client, admin),
        json={"role_ids": [str(strong.id)]},
    )
    assert resp.status_code == 403
    assert "wallet:manage" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_non_superadmin_cannot_create_superadmin(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin = await _seed_admin(
        db_session, permissions=["admin_account:view", "admin_account:manage"]
    )
    resp = await api_client.post(
        "/admin/accounts",
        headers=await _headers(api_client, admin),
        json={
            "username": f"new-{uuid4().hex[:8]}",
            "password": PASSWORD,
            "is_superadmin": True,
        },
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_superadmin_manages_roles_and_accounts(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    root = await _seed_admin(db_session, is_superadmin=True)
    headers = await _headers(api_client, root)

    created_role = await api_client.post(
        "/admin/admin-roles",
        headers=headers,
        json={
            "code": f"ops-{uuid4().hex[:8]}",
            "name": "运营",
            "permissions": ["report:view", "stats:view"],
        },
    )
    assert created_role.status_code == 201, created_role.text
    role_id = created_role.json()["id"]
    assert created_role.json()["permissions"] == ["report:view", "stats:view"]

    created_account = await api_client.post(
        "/admin/accounts",
        headers=headers,
        json={
            "username": f"ops-{uuid4().hex[:8]}",
            "password": PASSWORD,
            "role_ids": [role_id],
        },
    )
    assert created_account.status_code == 201, created_account.text
    assert created_account.json()["role_ids"] == [role_id]

    # 覆盖式更新授权后，持该角色的账号立刻只剩 report:view
    patched = await api_client.patch(
        f"/admin/admin-roles/{role_id}",
        headers=headers,
        json={"permissions": ["report:view"]},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["permissions"] == ["report:view"]

    ops_headers = {
        "Authorization": f"Bearer {await _login(api_client, created_account.json()['username'])}"
    }
    assert (await api_client.get("/admin/reports/trend", headers=ops_headers)).status_code != 403
    assert (await api_client.get("/admin/usage-logs", headers=ops_headers)).status_code == 403


@pytest.mark.asyncio
async def test_unknown_permission_code_rejected(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    root = await _seed_admin(db_session, is_superadmin=True)
    resp = await api_client.post(
        "/admin/admin-roles",
        headers=await _headers(api_client, root),
        json={"code": f"bad-{uuid4().hex[:8]}", "name": "x", "permissions": ["nope:view"]},
    )
    assert resp.status_code == 400
    assert "nope:view" in resp.json()["error"]["message"]
