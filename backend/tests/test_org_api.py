"""组织与成员管理 API：权限矩阵与验收场景。"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ORG_ROLE_ADMIN,
    ORG_ROLE_MEMBER,
    ORG_ROLE_OWNER,
    STATUS_DISABLED,
    STATUS_ENABLED,
)
from app.db.models.log import AuditLog
from app.db.models.org import OrgMember
from app.db.session import get_db
from app.main import app
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
    from uuid import uuid4

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


@pytest.mark.asyncio
async def test_create_org_creator_is_owner(user_client: AsyncClient) -> None:
    _, token = await _register_login(user_client, prefix="owner")
    resp = await user_client.post(
        "/orgs",
        headers=_auth(token),
        json={"name": "Acme Org", "billing_fallback": "deny"},
    )
    assert resp.status_code == 201, resp.text
    org = resp.json()
    assert org["name"] == "Acme Org"
    assert org["status"] == STATUS_ENABLED

    mine = await user_client.get("/orgs", headers=_auth(token))
    assert mine.status_code == 200
    assert any(
        o["id"] == org["id"] and o["my_org_role"] == ORG_ROLE_OWNER
        for o in mine.json()
    )

    detail = await user_client.get(f"/orgs/{org['id']}", headers=_auth(token))
    assert detail.status_code == 200
    members = detail.json()["members"]
    assert len(members) == 1
    assert members[0]["org_role"] == ORG_ROLE_OWNER
    assert members[0]["user_id"] == org["owner_user_id"]


@pytest.mark.asyncio
async def test_permission_matrix_member_forbidden_on_member_ops(
    user_client: AsyncClient,
) -> None:
    owner_id, owner_token = await _register_login(user_client, prefix="own")
    admin_id, admin_token = await _register_login(user_client, prefix="adm")
    member_id, member_token = await _register_login(user_client, prefix="mem")
    outsider_id, _outsider_token = await _register_login(user_client, prefix="out")

    org = (
        await user_client.post(
            "/orgs",
            headers=_auth(owner_token),
            json={"name": "Matrix Org"},
        )
    ).json()
    org_id = org["id"]
    assert org["owner_user_id"] == owner_id

    # Owner 邀请 Admin / Member
    admin_m = await user_client.post(
        f"/orgs/{org_id}/members",
        headers=_auth(owner_token),
        json={"user_id": admin_id, "org_role": ORG_ROLE_ADMIN},
    )
    assert admin_m.status_code == 201, admin_m.text
    member_m = await user_client.post(
        f"/orgs/{org_id}/members",
        headers=_auth(owner_token),
        json={"user_id": member_id, "org_role": ORG_ROLE_MEMBER},
    )
    assert member_m.status_code == 201, member_m.text
    member_mid = member_m.json()["id"]

    # Member 只读：可看详情与成员列表
    assert (
        await user_client.get(f"/orgs/{org_id}", headers=_auth(member_token))
    ).status_code == 200
    assert (
        await user_client.get(
            f"/orgs/{org_id}/members", headers=_auth(member_token)
        )
    ).status_code == 200

    # Member 调用成员管理 → 403
    invite_as_member = await user_client.post(
        f"/orgs/{org_id}/members",
        headers=_auth(member_token),
        json={"user_id": outsider_id, "org_role": ORG_ROLE_MEMBER},
    )
    assert invite_as_member.status_code == 403
    assert invite_as_member.json()["error"]["code"] == "forbidden"

    role_as_member = await user_client.patch(
        f"/orgs/{org_id}/members/{member_mid}",
        headers=_auth(member_token),
        json={"org_role": ORG_ROLE_ADMIN},
    )
    assert role_as_member.status_code == 403

    remove_as_member = await user_client.delete(
        f"/orgs/{org_id}/members/{member_mid}",
        headers=_auth(member_token),
    )
    assert remove_as_member.status_code == 403

    # Member 编辑组织 → 403（仅 Owner）
    patch_as_member = await user_client.patch(
        f"/orgs/{org_id}",
        headers=_auth(member_token),
        json={"name": "Hacked"},
    )
    assert patch_as_member.status_code == 403

    # Admin 可邀请成员，不可编辑组织
    invite_as_admin = await user_client.post(
        f"/orgs/{org_id}/members",
        headers=_auth(admin_token),
        json={"user_id": outsider_id, "org_role": ORG_ROLE_MEMBER},
    )
    assert invite_as_admin.status_code == 201, invite_as_admin.text

    patch_as_admin = await user_client.patch(
        f"/orgs/{org_id}",
        headers=_auth(admin_token),
        json={"name": "Admin Rename"},
    )
    assert patch_as_admin.status_code == 403

    # Owner 可编辑组织
    patch_as_owner = await user_client.patch(
        f"/orgs/{org_id}",
        headers=_auth(owner_token),
        json={"name": "Renamed Org", "billing_fallback": "personal"},
    )
    assert patch_as_owner.status_code == 200
    assert patch_as_owner.json()["name"] == "Renamed Org"
    assert patch_as_owner.json()["billing_fallback"] == "personal"

    # 非成员无法访问
    _, stranger_token = await _register_login(user_client, prefix="str")
    stranger_get = await user_client.get(
        f"/orgs/{org_id}", headers=_auth(stranger_token)
    )
    assert stranger_get.status_code == 403


@pytest.mark.asyncio
async def test_remove_member_loses_access_history_row_kept(
    user_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, owner_token = await _register_login(user_client, prefix="own2")
    member_id, member_token = await _register_login(user_client, prefix="mem2")

    org_id = (
        await user_client.post(
            "/orgs",
            headers=_auth(owner_token),
            json={"name": "Remove Org"},
        )
    ).json()["id"]

    invited = await user_client.post(
        f"/orgs/{org_id}/members",
        headers=_auth(owner_token),
        json={"user_id": member_id, "org_role": ORG_ROLE_MEMBER},
    )
    assert invited.status_code == 201
    mid = invited.json()["id"]

    assert (
        await user_client.get(f"/orgs/{org_id}", headers=_auth(member_token))
    ).status_code == 200

    removed = await user_client.delete(
        f"/orgs/{org_id}/members/{mid}", headers=_auth(owner_token)
    )
    assert removed.status_code == 204

    # 移除后无法再访问组织资源
    after = await user_client.get(f"/orgs/{org_id}", headers=_auth(member_token))
    assert after.status_code == 403

    mine = await user_client.get("/orgs", headers=_auth(member_token))
    assert all(o["id"] != org_id for o in mine.json())

    # 历史成员行仍在（软删除），不影响历史关联
    row = await db_session.get(OrgMember, UUID(mid))
    assert row is not None
    assert row.status == STATUS_DISABLED
    assert row.user_id == UUID(member_id)

    # 审计日志
    audits = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "org.member.remove")
        )
    ).scalars().all()
    assert any(f"member:{mid}" in a.target for a in audits)


@pytest.mark.asyncio
async def test_deactivate_org_rejects_writes(
    user_client: AsyncClient,
) -> None:
    _, owner_token = await _register_login(user_client, prefix="own3")
    member_id, _ = await _register_login(user_client, prefix="mem3")

    org_id = (
        await user_client.post(
            "/orgs",
            headers=_auth(owner_token),
            json={"name": "Disable Me"},
        )
    ).json()["id"]

    # 管理端列表 / 详情
    listed = await user_client.get("/admin/orgs", headers=admin_auth_headers())
    assert listed.status_code == 200
    assert any(o["id"] == org_id for o in listed.json())

    detail = await user_client.get(
        f"/admin/orgs/{org_id}", headers=admin_auth_headers()
    )
    assert detail.status_code == 200
    assert detail.json()["id"] == org_id

    deactivated = await user_client.post(
        f"/admin/orgs/{org_id}/deactivate",
        headers=admin_auth_headers(),
        json={"reason": "policy"},
    )
    assert deactivated.status_code == 200
    assert deactivated.json()["status"] == STATUS_DISABLED

    # 停用后拒绝写操作
    invite = await user_client.post(
        f"/orgs/{org_id}/members",
        headers=_auth(owner_token),
        json={"user_id": member_id, "org_role": ORG_ROLE_MEMBER},
    )
    assert invite.status_code == 403

    patch = await user_client.patch(
        f"/orgs/{org_id}",
        headers=_auth(owner_token),
        json={"name": "Nope"},
    )
    assert patch.status_code == 403

    # 读操作仍允许（Owner 仍是活跃成员）
    read = await user_client.get(f"/orgs/{org_id}", headers=_auth(owner_token))
    assert read.status_code == 200
    assert read.json()["status"] == STATUS_DISABLED


@pytest.mark.asyncio
async def test_role_update_audited_and_admin_cannot_grant_owner(
    user_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, owner_token = await _register_login(user_client, prefix="own4")
    admin_id, admin_token = await _register_login(user_client, prefix="adm4")
    member_id, _ = await _register_login(user_client, prefix="mem4")

    org_id = (
        await user_client.post(
            "/orgs",
            headers=_auth(owner_token),
            json={"name": "Role Org"},
        )
    ).json()["id"]

    admin_m = (
        await user_client.post(
            f"/orgs/{org_id}/members",
            headers=_auth(owner_token),
            json={"user_id": admin_id, "org_role": ORG_ROLE_ADMIN},
        )
    ).json()
    member_m = (
        await user_client.post(
            f"/orgs/{org_id}/members",
            headers=_auth(owner_token),
            json={"user_id": member_id, "org_role": ORG_ROLE_MEMBER},
        )
    ).json()

    # Admin 不能把成员升为 Owner
    promote = await user_client.patch(
        f"/orgs/{org_id}/members/{member_m['id']}",
        headers=_auth(admin_token),
        json={"org_role": ORG_ROLE_OWNER},
    )
    assert promote.status_code == 403

    # Admin 可以把 Member 升为 Admin
    to_admin = await user_client.patch(
        f"/orgs/{org_id}/members/{member_m['id']}",
        headers=_auth(admin_token),
        json={"org_role": ORG_ROLE_ADMIN},
    )
    assert to_admin.status_code == 200
    assert to_admin.json()["org_role"] == ORG_ROLE_ADMIN

    audits = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "org.member.role_update")
        )
    ).scalars().all()
    assert any(f"member:{member_m['id']}" in a.target for a in audits)

    # 不能移除最后一个 Owner
    members = (
        await user_client.get(
            f"/orgs/{org_id}/members", headers=_auth(owner_token)
        )
    ).json()
    owner_row = next(m for m in members if m["org_role"] == ORG_ROLE_OWNER)
    bad_remove = await user_client.delete(
        f"/orgs/{org_id}/members/{owner_row['id']}",
        headers=_auth(owner_token),
    )
    assert bad_remove.status_code == 400
