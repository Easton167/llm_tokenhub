"""用户端 API：注册/登录 JWT、Key 管理、钱包与用量。"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import STATUS_ENABLED, VISIBILITY_PUBLIC
from app.core.security import create_access_token, hash_password
from app.db.models.llm_model import LLMModel
from app.db.models.user import User
from app.db.session import get_db
from app.main import app
from app.services import usage_service
from app.services.auth_service import auth_service
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


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_register_login_and_me(user_client: AsyncClient) -> None:
    email = f"u-{uuid4().hex[:8]}@example.com"
    password = "password123"

    reg = await user_client.post(
        "/auth/register",
        json={"email": email, "password": password, "nickname": "Tester"},
    )
    assert reg.status_code == 201
    body = reg.json()
    assert body["email"] == email
    assert body["nickname"] == "Tester"
    assert "password_hash" not in body

    dup = await user_client.post(
        "/auth/register",
        json={"email": email, "password": password},
    )
    assert dup.status_code == 409

    login = await user_client.post(
        "/auth/login",
        json={"email": email, "password": password},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]
    assert login.json()["token_type"] == "bearer"

    me = await user_client.get("/auth/me", headers=_auth_header(token))
    assert me.status_code == 200
    assert me.json()["email"] == email


@pytest.mark.asyncio
async def test_login_invalid_credentials(user_client: AsyncClient) -> None:
    email = f"bad-{uuid4().hex[:8]}@example.com"
    await user_client.post(
        "/auth/register",
        json={"email": email, "password": "password123"},
    )
    resp = await user_client.post(
        "/auth/login",
        json={"email": email, "password": "wrong-password"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


@pytest.mark.asyncio
async def test_keys_crud_and_plaintext_once(
    user_client: AsyncClient, db_session: AsyncSession
) -> None:
    email = f"key-{uuid4().hex[:8]}@example.com"
    password = "password123"
    await user_client.post(
        "/auth/register",
        json={"email": email, "password": password},
    )
    token = (
        await user_client.post(
            "/auth/login", json={"email": email, "password": password}
        )
    ).json()["access_token"]

    created = await user_client.post(
        "/keys",
        headers=_auth_header(token),
        json={"name": "dev-key"},
    )
    assert created.status_code == 201
    data = created.json()
    assert data["name"] == "dev-key"
    assert data["key"].startswith("sk-")
    assert "prefix" in data
    plaintext = data["key"]
    key_id = data["id"]

    listed = await user_client.get("/keys", headers=_auth_header(token))
    assert listed.status_code == 200
    items = listed.json()
    assert len(items) >= 1
    assert "key" not in items[0]
    assert items[0]["prefix"]

    # Key 可用于 /v1
    models_ok = await user_client.get(
        "/v1/models", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert models_ok.status_code == 200

    deleted = await user_client.delete(
        f"/keys/{key_id}", headers=_auth_header(token)
    )
    assert deleted.status_code == 204

    models_fail = await user_client.get(
        "/v1/models", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert models_fail.status_code == 401


@pytest.mark.asyncio
async def test_wallet_and_usage_require_jwt(
    user_client: AsyncClient, db_session: AsyncSession
) -> None:
    email = f"wal-{uuid4().hex[:8]}@example.com"
    password = "password123"
    await user_client.post(
        "/auth/register",
        json={"email": email, "password": password},
    )
    token = (
        await user_client.post(
            "/auth/login", json={"email": email, "password": password}
        )
    ).json()["access_token"]

    no_auth = await user_client.get("/wallets/me")
    assert no_auth.status_code == 401

    wallet = await user_client.get("/wallets/me", headers=_auth_header(token))
    assert wallet.status_code == 200
    assert wallet.json()["balance"] == 0
    assert wallet.json()["owner_type"] == "personal"

    txs = await user_client.get(
        "/wallets/me/transactions", headers=_auth_header(token)
    )
    assert txs.status_code == 200
    assert isinstance(txs.json(), list)

    usage = await user_client.get("/usage", headers=_auth_header(token))
    assert usage.status_code == 200
    assert usage.json() == []


@pytest.mark.asyncio
async def test_usage_scoped_with_jwt(
    user_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = User(
        email=f"usage-{uuid4().hex[:8]}@example.com",
        password_hash=hash_password("password123"),
        status=STATUS_ENABLED,
    )
    db_session.add(user)
    await db_session.flush()
    await wallet_service.get_or_create_personal_wallet(db_session, user.id)

    api_key, _ = await auth_service.create_api_key(
        db_session, user_id=user.id, name="u-key"
    )
    model = LLMModel(
        alias=f"m-{uuid4().hex[:6]}",
        display_name="M",
        status=STATUS_ENABLED,
        visibility=VISIBILITY_PUBLIC,
    )
    db_session.add(model)
    await db_session.flush()

    request_id = f"req-{uuid4().hex[:12]}"
    await usage_service.write_request_log(
        db_session,
        request_id=request_id,
        api_key_id=api_key.id,
        user_id=user.id,
        billing_subject=f"user:{user.id}",
        model_id=model.id,
        channel_id=None,
        status="success",
        http_status=200,
    )
    await usage_service.write_usage_log(
        db_session,
        request_id=request_id,
        input_tokens=3,
        output_tokens=5,
        billing_rule_version=1,
        cost_amount=10,
        charge_amount=20,
    )
    await db_session.flush()

    token = create_access_token(user_id=user.id)
    resp = await user_client.get(
        "/usage",
        headers=_auth_header(token),
        params={"model": model.alias},
    )
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) >= 1
    assert items[0]["request_id"] == request_id


@pytest.mark.asyncio
async def test_delete_foreign_key_returns_404(
    user_client: AsyncClient, db_session: AsyncSession
) -> None:
    other = User(
        email=f"other-{uuid4().hex[:8]}@example.com",
        password_hash=hash_password("password123"),
        status=STATUS_ENABLED,
    )
    db_session.add(other)
    await db_session.flush()
    api_key, _ = await auth_service.create_api_key(
        db_session, user_id=other.id, name="other-key"
    )
    await db_session.flush()

    email = f"self-{uuid4().hex[:8]}@example.com"
    await user_client.post(
        "/auth/register",
        json={"email": email, "password": "password123"},
    )
    token = (
        await user_client.post(
            "/auth/login", json={"email": email, "password": "password123"}
        )
    ).json()["access_token"]

    resp = await user_client.delete(
        f"/keys/{api_key.id}", headers=_auth_header(token)
    )
    assert resp.status_code == 404
    await db_session.refresh(api_key)
    assert api_key.status == STATUS_ENABLED
