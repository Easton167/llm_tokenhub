"""任务 14：Key 级模型范围与限流。

覆盖：
  - RPM 并发超限返回 429 + Retry-After，无预扣、无上游调用
  - Key allowed_models 与角色授权取交集；范围外 403；/v1/models 只返回交集
  - 过期 Key 返回 401
  - 渠道 Key RPM 打满时路由跳过并切换其他 Key
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.channel_key_crypto import encrypt_channel_key
from app.core.constants import STATUS_ENABLED, VISIBILITY_PUBLIC
from app.core.datetime_utils import utc_now
from app.core.errors import RateLimitExceededError
from app.core.security import create_access_token
from app.db.models.billing import BillingRule
from app.db.models.channel import Channel, ChannelKey, Provider
from app.db.models.llm_model import LLMModel, ModelChannelMap
from app.db.models.user import User
from app.db.models.wallet import WalletTransaction
from app.db.session import get_db
from app.main import app
from app.services.auth_service import auth_service
from app.services.rate_limit_service import rate_limit_service
from app.services.routing_service import NoAvailableChannelError, routing_service
from app.services.wallet_service import TX_HOLD, wallet_service


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    rate_limit_service.reset_for_tests()
    rate_limit_service.force_memory_backend()
    yield
    rate_limit_service.reset_for_tests()


@pytest_asyncio.fixture
async def api_client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
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


async def _seed_user(session: AsyncSession) -> User:
    user = User(
        email=f"rl-{uuid4().hex[:8]}@example.com",
        status=STATUS_ENABLED,
    )
    session.add(user)
    await session.flush()
    wallet = await wallet_service.get_or_create_personal_wallet(session, user.id)
    wallet.balance = 100_000_000
    await session.flush()
    return user


async def _seed_model(session: AsyncSession, alias: str) -> LLMModel:
    model = LLMModel(
        alias=alias,
        display_name=alias,
        model_type="chat",
        capabilities={},
        visibility=VISIBILITY_PUBLIC,
        status=STATUS_ENABLED,
    )
    session.add(model)
    await session.flush()
    return model


async def _seed_billing(session: AsyncSession, model: LLMModel) -> BillingRule:
    from datetime import datetime, timezone

    rule = BillingRule(
        model_id=model.id,
        version=1,
        price_input=1000,
        price_output=2000,
        price_cached_input=0,
        cost_input=1000,
        cost_output=2000,
        multiplier="1.0",
        min_charge_unit=1,
        currency="CNY",
        effective_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        status=STATUS_ENABLED,
    )
    session.add(rule)
    await session.flush()
    return rule


async def _seed_channel_stack(
    session: AsyncSession,
    model: LLMModel,
    *,
    keys: list[dict],
) -> list[ChannelKey]:
    provider = Provider(
        name=f"prov-{uuid4().hex[:6]}",
        adapter_type="openai_compatible",
        default_base_url="https://api.openai.com/v1",
    )
    session.add(provider)
    await session.flush()

    channel = Channel(
        provider_id=provider.id,
        name=f"chan-{uuid4().hex[:6]}",
        base_url="https://api.openai.com/v1",
        adapter_type="openai_compatible",
        priority=100,
        weight=1,
        status=STATUS_ENABLED,
        health_status=0,
        timeout_ms=30_000,
        max_retries=2,
    )
    session.add(channel)
    await session.flush()

    created: list[ChannelKey] = []
    for i, spec in enumerate(keys):
        ck = ChannelKey(
            channel_id=channel.id,
            api_key_encrypted=encrypt_channel_key(spec.get("plain", f"sk-up-{i}")),
            key_masked=f"****{i}",
            weight=spec.get("weight", 1),
            rpm_limit=spec.get("rpm_limit"),
            tpm_limit=spec.get("tpm_limit"),
            status=STATUS_ENABLED,
            health_status=0,
        )
        session.add(ck)
        created.append(ck)
    await session.flush()

    mapping = ModelChannelMap(
        model_id=model.id,
        channel_id=channel.id,
        upstream_model_name=model.alias,
        weight=1,
        status=STATUS_ENABLED,
    )
    session.add(mapping)
    await session.flush()
    return created


def _fake_upstream_response(model_alias: str) -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": model_alias,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    }


# ---------------------------------------------------------------------------
# 进程内限流并发
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_rpm_limit_exact() -> None:
    """并发请求下 RPM 精确放行 limit 次，其余被拒。"""
    rate_limit_service.force_memory_backend()
    key_id = str(uuid4())
    limit = 5

    async def once() -> bool:
        try:
            await rate_limit_service.enforce_api_key_limits(
                api_key_id=key_id,
                rpm_limit=limit,
                tpm_limit=None,
                estimated_tokens=1,
            )
            return True
        except RateLimitExceededError:
            return False

    results = await asyncio.gather(*[once() for _ in range(20)])
    assert sum(results) == limit
    assert results.count(False) == 20 - limit


@pytest.mark.asyncio
async def test_tpm_limit_blocks_oversized_request() -> None:
    rate_limit_service.force_memory_backend()
    key_id = str(uuid4())
    await rate_limit_service.enforce_api_key_limits(
        api_key_id=key_id,
        rpm_limit=None,
        tpm_limit=100,
        estimated_tokens=60,
    )
    with pytest.raises(RateLimitExceededError) as exc:
        await rate_limit_service.enforce_api_key_limits(
            api_key_id=key_id,
            rpm_limit=None,
            tpm_limit=100,
            estimated_tokens=50,
        )
    assert exc.value.status_code == 429
    assert exc.value.code == "rate_limit_exceeded"
    assert "Retry-After" in exc.value.headers


# ---------------------------------------------------------------------------
# 网关集成：429 无资金变动、无上游
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_rpm_exceeded_no_hold_no_upstream(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _seed_user(db_session)
    model = await _seed_model(db_session, f"rl-model-{uuid4().hex[:6]}")
    await _seed_billing(db_session, model)
    await _seed_channel_stack(db_session, model, keys=[{"plain": "sk-a"}])

    api_key, plaintext = await auth_service.create_api_key(
        db_session,
        user_id=user.id,
        name="limited",
        rpm_limit=1,
    )
    await db_session.flush()

    mock_post = AsyncMock()
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json = MagicMock(return_value=_fake_upstream_response(model.alias))
        mock_client.post = mock_post
        mock_post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        body = {
            "model": model.alias,
            "messages": [{"role": "user", "content": "hi"}],
        }
        headers = {"Authorization": f"Bearer {plaintext}"}

        ok = await api_client.post("/v1/chat/completions", json=body, headers=headers)
        assert ok.status_code == 200

        limited = await api_client.post(
            "/v1/chat/completions", json=body, headers=headers
        )
        assert limited.status_code == 429
        err = limited.json()["error"]
        assert err["code"] == "rate_limit_exceeded"
        assert err["type"] == "rate_limit_error"
        assert limited.headers.get("retry-after") is not None

    # 第二次被拒：上游只应被调用一次（429 发生在预扣前）
    assert mock_post.await_count == 1

    wallet = await wallet_service.get_or_create_personal_wallet(db_session, user.id)
    hold_txs = (
        await db_session.execute(
            select(WalletTransaction).where(
                WalletTransaction.wallet_id == wallet.id,
                WalletTransaction.type == TX_HOLD,
            )
        )
    ).scalars().all()
    # 仅成功请求产生 hold；429 不预扣
    assert len(hold_txs) == 1


# ---------------------------------------------------------------------------
# 模型范围 / 过期
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_allowed_models_intersection_and_models_list(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _seed_user(db_session)
    m1 = await _seed_model(db_session, f"allow-a-{uuid4().hex[:4]}")
    m2 = await _seed_model(db_session, f"allow-b-{uuid4().hex[:4]}")
    await _seed_billing(db_session, m1)
    await _seed_billing(db_session, m2)
    await _seed_channel_stack(db_session, m1, keys=[{"plain": "sk-a"}])

    _, plaintext = await auth_service.create_api_key(
        db_session,
        user_id=user.id,
        name="scoped",
        allowed_models=[m1.alias],
    )

    headers = {"Authorization": f"Bearer {plaintext}"}
    listed = await api_client.get("/v1/models", headers=headers)
    assert listed.status_code == 200
    ids = {item["id"] for item in listed.json()["data"]}
    assert m1.alias in ids
    assert m2.alias not in ids

    denied = await api_client.post(
        "/v1/chat/completions",
        headers=headers,
        json={
            "model": m2.alias,
            "messages": [{"role": "user", "content": "x"}],
        },
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "model_not_authorized"


@pytest.mark.asyncio
async def test_expired_api_key_returns_401(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _seed_user(db_session)
    _, plaintext = await auth_service.create_api_key(
        db_session,
        user_id=user.id,
        name="expired",
        expires_at=utc_now() - timedelta(minutes=1),
    )
    resp = await api_client.get(
        "/v1/models",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_api_key"


@pytest.mark.asyncio
async def test_keys_api_create_update_scope_fields(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _seed_user(db_session)
    token = create_access_token(user_id=user.id)
    headers = {"Authorization": f"Bearer {token}"}

    created = await api_client.post(
        "/keys",
        headers=headers,
        json={
            "name": "with-limits",
            "allowed_models": ["gpt-4o", "gpt-4o"],
            "rpm_limit": 30,
            "tpm_limit": 10000,
        },
    )
    assert created.status_code == 201
    data = created.json()
    assert data["allowed_models"] == ["gpt-4o"]
    assert data["rpm_limit"] == 30
    assert data["tpm_limit"] == 10000
    key_id = data["id"]

    updated = await api_client.patch(
        f"/keys/{key_id}",
        headers=headers,
        json={"rpm_limit": None, "allowed_models": []},
    )
    assert updated.status_code == 200
    assert updated.json()["rpm_limit"] is None
    assert updated.json()["allowed_models"] == []


# ---------------------------------------------------------------------------
# 渠道 Key 打满跳过
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_channel_key_rpm_full_switches_to_other_key(
    db_session: AsyncSession,
) -> None:
    model = await _seed_model(db_session, f"route-rl-{uuid4().hex[:6]}")
    keys = await _seed_channel_stack(
        db_session,
        model,
        keys=[
            {"plain": "sk-limited", "rpm_limit": 1, "weight": 100},
            {"plain": "sk-backup", "rpm_limit": 100, "weight": 1},
        ],
    )
    limited_id = str(keys[0].id)
    backup_id = str(keys[1].id)

    # 打满高权重 Key 的 RPM
    assert await rate_limit_service.try_consume_channel_key(
        channel_key_id=limited_id,
        rpm_limit=1,
        tpm_limit=None,
        estimated_tokens=1,
    )

    for _ in range(5):
        route = await routing_service.pick_route(db_session, model.alias)
        assert route.channel_key_id == backup_id


@pytest.mark.asyncio
async def test_all_channel_keys_rate_limited_raises(
    db_session: AsyncSession,
) -> None:
    model = await _seed_model(db_session, f"route-full-{uuid4().hex[:6]}")
    keys = await _seed_channel_stack(
        db_session,
        model,
        keys=[{"plain": "sk-only", "rpm_limit": 1}],
    )
    kid = str(keys[0].id)
    assert await rate_limit_service.try_consume_channel_key(
        channel_key_id=kid,
        rpm_limit=1,
        tpm_limit=None,
        estimated_tokens=1,
    )
    with pytest.raises(NoAvailableChannelError):
        await routing_service.pick_route(db_session, model.alias)
