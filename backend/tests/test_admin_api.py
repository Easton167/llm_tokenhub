"""管理配置 API 集成测试。"""

from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import STATUS_ENABLED
from app.db.models.channel import ChannelKey
from app.db.models.log import AuditLog
from app.db.models.user import User
from tests.conftest import admin_auth_headers


@pytest_asyncio.fixture
async def admin_client(db_session: AsyncSession) -> AsyncClient:
    from collections.abc import AsyncGenerator

    from httpx import ASGITransport

    from app.db.session import get_db
    from app.main import app

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers=admin_auth_headers(),
    ) as client:
        yield client
    app.dependency_overrides.clear()


async def _create_user(db_session: AsyncSession) -> User:
    user = User(email=f"admin-test-{uuid4().hex}@example.com", status=STATUS_ENABLED)
    db_session.add(user)
    await db_session.flush()
    return user


@pytest.mark.asyncio
async def test_admin_login_and_reject_without_token(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    from app.core.config import settings
    from httpx import ASGITransport, AsyncClient
    from app.db.session import get_db
    from app.main import app

    # 无 token 访问应 401
    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as bare:
        denied = await bare.get("/admin/providers")
        assert denied.status_code == 401

        bad = await bare.post(
            "/admin/auth/login",
            json={"username": "admin", "password": "wrong"},
        )
        assert bad.status_code == 401

        ok = await bare.post(
            "/admin/auth/login",
            json={"username": settings.admin_username, "password": settings.admin_password},
        )
        assert ok.status_code == 200
        token = ok.json()["access_token"]
        me = await bare.get(
            "/admin/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert me.status_code == 200
        assert me.json()["username"] == settings.admin_username
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_admin_full_config_flow(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    provider_resp = await admin_client.post(
        "/admin/providers",
        json={
            "name": f"OpenAI-{uuid4().hex[:6]}",
            "adapter_type": "openai_compatible",
            "default_base_url": "https://api.openai.com/v1",
        },
    )
    assert provider_resp.status_code == 201
    provider = provider_resp.json()

    channel_resp = await admin_client.post(
        "/admin/channels",
        json={
            "provider_id": provider["id"],
            "name": "openai-main",
            "base_url": "https://api.openai.com/v1",
            "adapter_type": "openai_compatible",
            "priority": 100,
            "weight": 10,
        },
    )
    assert channel_resp.status_code == 201
    channel = channel_resp.json()

    key_resp = await admin_client.post(
        f"/admin/channels/{channel['id']}/keys",
        json={"api_key": "sk-upstream-secret-xyz9", "weight": 10},
    )
    assert key_resp.status_code == 201
    key_body = key_resp.json()
    assert "api_key" not in key_body
    assert key_body["key_masked"] == "****xyz9"

    keys_list = await admin_client.get(f"/admin/channels/{channel['id']}/keys")
    assert keys_list.status_code == 200
    keys = keys_list.json()
    assert len(keys) == 1
    assert "api_key" not in keys[0]
    assert keys[0]["key_masked"].startswith("****")

    model_alias = f"gpt-4o-{uuid4().hex[:6]}"
    model_resp = await admin_client.post(
        "/admin/models",
        json={
            "alias": model_alias,
            "display_name": "GPT-4o",
            "model_type": "chat",
            "capabilities": {"stream": True},
            "visibility": "role_restricted",
        },
    )
    assert model_resp.status_code == 201
    model = model_resp.json()

    mapping_resp = await admin_client.post(
        f"/admin/models/{model['id']}/mappings",
        json={
            "channel_id": channel["id"],
            "upstream_model_name": "gpt-4o-2024-11-20",
            "weight": 10,
        },
    )
    assert mapping_resp.status_code == 201

    effective_at = "2026-06-24T00:00:00.000000Z"
    rule1_resp = await admin_client.post(
        f"/admin/models/{model['id']}/billing-rules",
        json={
            "price_input": 180000,
            "price_output": 720000,
            "cost_input": 150000,
            "cost_output": 600000,
            "effective_at": effective_at,
        },
    )
    assert rule1_resp.status_code == 201
    assert rule1_resp.json()["version"] == 1

    rule2_resp = await admin_client.post(
        f"/admin/models/{model['id']}/billing-rules",
        json={
            "price_input": 200000,
            "price_output": 800000,
            "effective_at": effective_at,
        },
    )
    assert rule2_resp.status_code == 201
    assert rule2_resp.json()["version"] == 2

    rules_list = await admin_client.get(f"/admin/models/{model['id']}/billing-rules")
    assert rules_list.status_code == 200
    versions = [r["version"] for r in rules_list.json()]
    assert versions == [2, 1]

    rule2 = rule2_resp.json()
    delete_resp = await admin_client.delete(
        f"/admin/models/{model['id']}/billing-rules/{rule2['id']}"
    )
    assert delete_resp.status_code == 204

    rules_after_delete = await admin_client.get(
        f"/admin/models/{model['id']}/billing-rules"
    )
    assert rules_after_delete.status_code == 200
    by_version = {r["version"]: r for r in rules_after_delete.json()}
    assert by_version[2]["status"] == 1  # 作废（软删除）
    assert by_version[1]["status"] == 0

    role_resp = await admin_client.post(
        "/admin/roles",
        json={"name": f"dev-{uuid4().hex[:6]}", "description": "developer"},
    )
    assert role_resp.status_code == 201
    role = role_resp.json()

    grant_resp = await admin_client.post(
        f"/admin/roles/{role['id']}/models",
        json={"model_id": model["id"]},
    )
    assert grant_resp.status_code == 201

    user = await _create_user(db_session)
    assign_resp = await admin_client.post(
        f"/admin/users/{user.id}/roles",
        json={"role_id": role["id"]},
    )
    assert assign_resp.status_code == 201

    audits = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "provider.create"))
    ).scalars().all()
    assert len(audits) >= 1


@pytest.mark.asyncio
async def test_admin_reset_user_password(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    from app.core.security import verify_password

    user = await _create_user(db_session)

    too_short = await admin_client.put(
        f"/admin/users/{user.id}/password", json={"password": "short"}
    )
    assert too_short.status_code == 422

    resp = await admin_client.put(
        f"/admin/users/{user.id}/password", json={"password": "new-secret-123"}
    )
    assert resp.status_code == 204
    await db_session.refresh(user)
    assert user.password_hash and verify_password("new-secret-123", user.password_hash)

    missing = await admin_client.put(
        f"/admin/users/{uuid4()}/password", json={"password": "new-secret-123"}
    )
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_model_alias_unique_conflict(admin_client: AsyncClient) -> None:
    alias = f"dup-model-{uuid4().hex[:6]}"
    payload = {
        "alias": alias,
        "display_name": "Dup",
        "model_type": "chat",
    }
    first = await admin_client.post("/admin/models", json=payload)
    assert first.status_code == 201
    second = await admin_client.post("/admin/models", json=payload)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "conflict"


@pytest.mark.asyncio
async def test_channel_inherits_provider_defaults(admin_client: AsyncClient) -> None:
    provider = (
        await admin_client.post(
            "/admin/providers",
            json={
                "name": f"Inherit-{uuid4().hex[:6]}",
                "adapter_type": "openai_compatible",
                "default_base_url": "https://api.openai.com/v1",
            },
        )
    ).json()

    inherited = await admin_client.post(
        "/admin/channels",
        json={"provider_id": provider["id"], "name": "inherited-ch"},
    )
    assert inherited.status_code == 201
    body = inherited.json()
    assert body["adapter_type"] == "openai_compatible"
    assert body["base_url"] == "https://api.openai.com/v1"

    override = await admin_client.post(
        "/admin/channels",
        json={
            "provider_id": provider["id"],
            "name": "override-ch",
            "base_url": "https://proxy.example.com/v1",
            "adapter_type": "anthropic",
        },
    )
    assert override.status_code == 201
    override_body = override.json()
    assert override_body["base_url"] == "https://proxy.example.com/v1"
    assert override_body["adapter_type"] == "anthropic"

    bare_provider = (
        await admin_client.post(
            "/admin/providers",
            json={"name": f"Bare-{uuid4().hex[:6]}", "adapter_type": "openai_compatible"},
        )
    ).json()
    missing_url = await admin_client.post(
        "/admin/channels",
        json={"provider_id": bare_provider["id"], "name": "no-url"},
    )
    assert missing_url.status_code == 400
    assert missing_url.json()["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_channel_key_encrypted_in_db(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    provider = (
        await admin_client.post(
            "/admin/providers",
            json={"name": f"P-{uuid4().hex[:6]}", "adapter_type": "openai_compatible"},
        )
    ).json()
    channel = (
        await admin_client.post(
            "/admin/channels",
            json={
                "provider_id": provider["id"],
                "name": "ch",
                "base_url": "https://api.example.com/v1",
                "adapter_type": "openai_compatible",
            },
        )
    ).json()
    secret = "sk-my-secret-key-0001"
    await admin_client.post(
        f"/admin/channels/{channel['id']}/keys",
        json={"api_key": secret},
    )
    row = (
        await db_session.execute(
            select(ChannelKey).where(ChannelKey.channel_id == channel["id"])
        )
    ).scalar_one()
    assert secret.encode() not in row.api_key_encrypted
    assert row.key_masked == "****0001"
