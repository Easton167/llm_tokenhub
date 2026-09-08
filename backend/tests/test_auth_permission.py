"""鉴权与模型权限集成测试。"""

from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    STATUS_DISABLED,
    STATUS_ENABLED,
    VISIBILITY_PUBLIC,
    VISIBILITY_ROLE_RESTRICTED,
)
from app.core.errors import InvalidApiKeyError, ModelNotAuthorizedError, ModelNotFoundError
from app.db.models.llm_model import LLMModel
from app.db.models.user import Role, RoleModelGrant, User, UserRole
from app.services.auth_service import auth_service
from app.services.permission_service import permission_service


async def _seed_user(session: AsyncSession, *, email_suffix: str = "") -> User:
    user = User(
        email=f"user{email_suffix}@example.com",
        status=STATUS_ENABLED,
    )
    session.add(user)
    await session.flush()
    return user


async def _seed_role(session: AsyncSession, name: str | None = None) -> Role:
    role = Role(name=name or f"role-{uuid4().hex[:8]}")
    session.add(role)
    await session.flush()
    return role


async def _seed_model(
    session: AsyncSession,
    *,
    alias: str,
    visibility: str = VISIBILITY_ROLE_RESTRICTED,
    status: int = STATUS_ENABLED,
) -> LLMModel:
    model = LLMModel(
        alias=alias,
        display_name=alias,
        visibility=visibility,
        status=status,
    )
    session.add(model)
    await session.flush()
    return model


@pytest.mark.asyncio
async def test_verify_api_key_success(db_session: AsyncSession) -> None:
    user = await _seed_user(db_session)
    api_key, plaintext = await auth_service.create_api_key(
        db_session, user_id=user.id, name="test-key"
    )

    ctx = await auth_service.verify_api_key(db_session, plaintext)
    assert ctx.user.id == user.id
    assert ctx.api_key.id == api_key.id
    assert ctx.billing_subject == "personal"


@pytest.mark.asyncio
async def test_verify_api_key_invalid_hash(db_session: AsyncSession) -> None:
    with pytest.raises(InvalidApiKeyError) as exc_info:
        await auth_service.verify_api_key(db_session, "sk-does-not-exist")
    assert exc_info.value.code == "invalid_api_key"
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_api_key_disabled(db_session: AsyncSession) -> None:
    user = await _seed_user(db_session, email_suffix="-disabled")
    api_key, plaintext = await auth_service.create_api_key(
        db_session, user_id=user.id, name="disabled-key"
    )
    api_key.status = STATUS_DISABLED
    await db_session.flush()

    with pytest.raises(InvalidApiKeyError):
        await auth_service.verify_api_key(db_session, plaintext)


@pytest.mark.asyncio
async def test_verify_api_key_bad_format(db_session: AsyncSession) -> None:
    with pytest.raises(InvalidApiKeyError):
        await auth_service.verify_api_key(db_session, "invalid-format")


@pytest.mark.asyncio
async def test_check_model_access_authorized(db_session: AsyncSession) -> None:
    user = await _seed_user(db_session, email_suffix="-perm")
    role = await _seed_role(db_session)
    model = await _seed_model(db_session, alias="gpt-4o")
    db_session.add_all(
        [
            UserRole(user_id=user.id, role_id=role.id),
            RoleModelGrant(role_id=role.id, model_id=model.id),
        ]
    )
    await db_session.flush()

    access = await permission_service.check_model_access(
        db_session, user_id=user.id, model_alias="gpt-4o"
    )
    assert access.model_alias == "gpt-4o"


@pytest.mark.asyncio
async def test_check_model_access_public_model(db_session: AsyncSession) -> None:
    user = await _seed_user(db_session, email_suffix="-public")
    await _seed_model(db_session, alias="public-model", visibility=VISIBILITY_PUBLIC)

    access = await permission_service.check_model_access(
        db_session, user_id=user.id, model_alias="public-model"
    )
    assert access.model_alias == "public-model"


@pytest.mark.asyncio
async def test_check_model_access_not_authorized(db_session: AsyncSession) -> None:
    user = await _seed_user(db_session, email_suffix="-no-grant")
    await _seed_model(db_session, alias="restricted-model")

    with pytest.raises(ModelNotAuthorizedError) as exc_info:
        await permission_service.check_model_access(
            db_session, user_id=user.id, model_alias="restricted-model"
        )
    assert exc_info.value.code == "model_not_authorized"
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_check_model_access_not_found(db_session: AsyncSession) -> None:
    user = await _seed_user(db_session, email_suffix="-not-found")

    with pytest.raises(ModelNotFoundError) as exc_info:
        await permission_service.check_model_access(
            db_session, user_id=user.id, model_alias="missing-model"
        )
    assert exc_info.value.code == "model_not_found"
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_check_model_access_disabled_model(db_session: AsyncSession) -> None:
    user = await _seed_user(db_session, email_suffix="-disabled-model")
    await _seed_model(db_session, alias="disabled-model", status=STATUS_DISABLED)

    with pytest.raises(ModelNotFoundError):
        await permission_service.check_model_access(
            db_session, user_id=user.id, model_alias="disabled-model"
        )


@pytest.mark.asyncio
async def test_list_authorized_models(db_session: AsyncSession) -> None:
    user = await _seed_user(db_session, email_suffix="-list")
    role = await _seed_role(db_session)
    granted = await _seed_model(db_session, alias="granted-model")
    await _seed_model(db_session, alias="public-model", visibility=VISIBILITY_PUBLIC)
    await _seed_model(db_session, alias="other-restricted")
    db_session.add_all(
        [
            UserRole(user_id=user.id, role_id=role.id),
            RoleModelGrant(role_id=role.id, model_id=granted.id),
        ]
    )
    await db_session.flush()

    models = await permission_service.list_authorized_models(db_session, user_id=user.id)
    aliases = {m.alias for m in models}
    assert {"granted-model", "public-model"} <= aliases
    assert "other-restricted" not in aliases


@pytest.mark.asyncio
async def test_auth_dependency_invalid_key(auth_client: AsyncClient) -> None:
    response = await auth_client.get(
        "/test/auth",
        headers={"Authorization": "Bearer sk-invalid"},
    )
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "invalid_api_key"


@pytest.mark.asyncio
async def test_auth_dependency_valid_key(auth_client: AsyncClient, db_session: AsyncSession) -> None:
    user = await _seed_user(db_session, email_suffix="-dep")
    _, plaintext = await auth_service.create_api_key(
        db_session, user_id=user.id, name="dep-key"
    )

    response = await auth_client.get(
        "/test/auth",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == str(user.id)
    assert body["billing_subject"] == "personal"
