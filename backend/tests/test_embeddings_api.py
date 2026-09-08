"""POST /v1/embeddings 集成测试（Phase2 任务 13）。

覆盖验收标准：
  - 成功请求产生 request_log、usage_log、钱包流水
  - 组织身份调用同样受额度约束
  - 未授权 embedding 模型返回 403
  - 响应结构可被 OpenAI SDK embeddings.create 解析
  - model_type=embedding 出现在 /v1/models
  - 按输入 token 计费（output=0）
  - 支持字符串与字符串数组输入
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.openai_compatible import openai_compatible_adapter
from app.core.channel_key_crypto import encrypt_channel_key
from app.core.constants import (
    BILLING_FALLBACK_DENY,
    MODEL_TYPE_EMBEDDING,
    QUOTA_PERIOD_NONE,
    STATUS_ENABLED,
    VISIBILITY_PUBLIC,
    VISIBILITY_ROLE_RESTRICTED,
)
from app.db.models.billing import BillingRule
from app.db.models.channel import Channel, ChannelKey, Provider
from app.db.models.llm_model import LLMModel, ModelChannelMap
from app.db.models.log import RequestLog, UsageLog
from app.db.models.org import Org, OrgMember
from app.db.models.user import Role, RoleModelGrant, User, UserRole
from app.db.models.wallet import Wallet, WalletTransaction
from app.db.session import get_db
from app.main import app
from app.schemas.openai import EmbeddingRequest
from app.services.auth_service import auth_service
from app.services.billing_subject_service import format_org_subject_ref
from app.services.org_service import org_service
from app.services.quota_service import quota_service
from app.services.wallet_service import TX_CAPTURE, TX_HOLD, wallet_service


# ===========================================================================
# helpers
# ===========================================================================


async def _seed_user(session: AsyncSession, suffix: str = "") -> User:
    user = User(
        email=f"emb-{suffix}-{uuid4().hex[:8]}@example.com",
        status=STATUS_ENABLED,
    )
    session.add(user)
    await session.flush()
    return user


async def _seed_api_key(
    session: AsyncSession,
    user: User,
    *,
    billing_subject: str | None = None,
) -> tuple[str, str]:
    api_key_obj, plaintext = await auth_service.create_api_key(
        session,
        user_id=user.id,
        name="emb-key",
        billing_subject=billing_subject,
    )
    return str(api_key_obj.id), plaintext


async def _seed_embedding_model(
    session: AsyncSession,
    *,
    alias: str | None = None,
    visibility: str = VISIBILITY_PUBLIC,
) -> LLMModel:
    alias = alias or f"emb-model-{uuid4().hex[:6]}"
    model = LLMModel(
        alias=alias,
        display_name=alias,
        model_type=MODEL_TYPE_EMBEDDING,
        capabilities={},
        visibility=visibility,
        status=STATUS_ENABLED,
    )
    session.add(model)
    await session.flush()
    return model


async def _seed_chat_model(session: AsyncSession) -> LLMModel:
    model = LLMModel(
        alias=f"chat-model-{uuid4().hex[:6]}",
        display_name="chat",
        model_type="chat",
        capabilities={},
        visibility=VISIBILITY_PUBLIC,
        status=STATUS_ENABLED,
    )
    session.add(model)
    await session.flush()
    return model


async def _seed_channel(session: AsyncSession) -> tuple[Provider, Channel, ChannelKey]:
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
        priority=0,
        weight=1,
        status=STATUS_ENABLED,
        health_status=0,
        timeout_ms=30_000,
        max_retries=2,
    )
    session.add(channel)
    await session.flush()

    channel_key = ChannelKey(
        channel_id=channel.id,
        api_key_encrypted=encrypt_channel_key("sk-upstream-emb"),
        key_masked="****emb",
        weight=1,
        status=STATUS_ENABLED,
        health_status=0,
    )
    session.add(channel_key)
    await session.flush()
    return provider, channel, channel_key


async def _seed_mapping(
    session: AsyncSession,
    model: LLMModel,
    channel: Channel,
    *,
    upstream_model_name: str = "text-embedding-3-small",
) -> ModelChannelMap:
    mapping = ModelChannelMap(
        model_id=model.id,
        channel_id=channel.id,
        upstream_model_name=upstream_model_name,
        weight=1,
        status=STATUS_ENABLED,
    )
    session.add(mapping)
    await session.flush()
    return mapping


async def _seed_billing_rule(
    session: AsyncSession,
    model: LLMModel,
    *,
    price_input: int = 180_000,
    price_output: int = 0,
) -> BillingRule:
    """embedding 计费：仅输入价，输出价为 0。"""
    rule = BillingRule(
        model_id=model.id,
        version=1,
        price_input=price_input,
        price_output=price_output,
        price_cached_input=0,
        price_per_call=0,
        cost_input=price_input,
        cost_output=0,
        multiplier="1.0",
        min_charge_unit=1,
        currency="CNY",
        effective_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        status=0,
    )
    session.add(rule)
    await session.flush()
    return rule


async def _seed_wallet(
    session: AsyncSession,
    user: User,
    *,
    balance: int = 100_000_000,
) -> Wallet:
    w = await wallet_service.get_or_create_personal_wallet(session, user.id)
    w.balance = balance
    await session.flush()
    return w


async def _seed_org_ready(
    session: AsyncSession,
    owner: User,
    *,
    org_balance: int = 100_000_000,
    quota_limit: int = 0,
) -> tuple[Org, OrgMember, Wallet]:
    org = await org_service.create_org(
        session,
        name=f"EmbOrg-{uuid4().hex[:6]}",
        owner_user_id=owner.id,
        billing_fallback=BILLING_FALLBACK_DENY,
    )
    membership = await org_service.get_active_membership(
        session, org_id=org.id, user_id=owner.id
    )
    assert membership is not None
    membership.can_consume_org_wallet = True
    org_wallet = await wallet_service.require_org_wallet(session, org.id)
    org_wallet.balance = org_balance
    if quota_limit > 0:
        await quota_service.configure_member_quota(
            session,
            org=org,
            actor_user_id=owner.id,
            member_id=membership.id,
            can_consume_org_wallet=True,
            quota_period=QUOTA_PERIOD_NONE,
            quota_limit=quota_limit,
        )
    await session.flush()
    return org, membership, org_wallet


def _make_upstream_embedding_response(
    *,
    prompt_tokens: int = 8,
    dims: int = 4,
    n: int = 1,
    model: str = "text-embedding-3-small",
) -> MagicMock:
    data = [
        {
            "object": "embedding",
            "embedding": [0.1 * (i + 1)] * dims,
            "index": i,
        }
        for i in range(n)
    ]
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "object": "list",
        "data": data,
        "model": model,
        "usage": {
            "prompt_tokens": prompt_tokens,
            "total_tokens": prompt_tokens,
        },
    }
    return mock_resp


def _patch_httpx(mock_resp: MagicMock):
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_client.aclose = AsyncMock()
    mock_client_cls = MagicMock()
    mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
    return (
        patch("app.api.v1.embeddings.httpx.AsyncClient", mock_client_cls),
        mock_client,
    )


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


# ===========================================================================
# adapter unit
# ===========================================================================


class TestOpenAICompatibleEmbeddingsAdapter:
    def test_supports_and_url(self) -> None:
        assert openai_compatible_adapter.supports_embeddings() is True
        assert (
            openai_compatible_adapter.build_embeddings_url("https://api.openai.com/v1")
            == "https://api.openai.com/v1/embeddings"
        )

    def test_build_and_parse(self) -> None:
        req = EmbeddingRequest(model="emb-alias", input=["hello", "world"])
        payload = openai_compatible_adapter.build_embeddings_request(
            req, "text-embedding-3-small"
        )
        assert payload["model"] == "text-embedding-3-small"
        assert payload["input"] == ["hello", "world"]

        parsed = openai_compatible_adapter.parse_embeddings_response(
            {
                "object": "list",
                "data": [
                    {"object": "embedding", "embedding": [0.1, 0.2], "index": 0},
                    {"object": "embedding", "embedding": [0.3, 0.4], "index": 1},
                ],
                "model": "text-embedding-3-small",
                "usage": {"prompt_tokens": 4, "total_tokens": 4},
            }
        )
        assert len(parsed.data) == 2
        assert parsed.usage is not None
        assert parsed.usage.prompt_tokens == 4
        assert parsed.usage.total_tokens == 4


# ===========================================================================
# /v1/models 含 embedding
# ===========================================================================


@pytest.mark.asyncio
async def test_list_models_includes_embedding_type(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _seed_user(db_session)
    _, plaintext = await _seed_api_key(db_session, user)
    model = await _seed_embedding_model(db_session)

    resp = await api_client.get(
        "/v1/models", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert resp.status_code == 200
    entry = next(m for m in resp.json()["data"] if m["id"] == model.alias)
    assert entry["model_type"] == MODEL_TYPE_EMBEDDING


# ===========================================================================
# POST /v1/embeddings
# ===========================================================================


@pytest.mark.asyncio
async def test_embeddings_no_auth_returns_401(api_client: AsyncClient) -> None:
    resp = await api_client.post(
        "/v1/embeddings",
        json={"model": "emb", "input": "hello"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_embeddings_unauthorized_model_returns_403(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _seed_user(db_session)
    _, plaintext = await _seed_api_key(db_session, user)
    model = await _seed_embedding_model(
        db_session, visibility=VISIBILITY_ROLE_RESTRICTED
    )

    resp = await api_client.post(
        "/v1/embeddings",
        json={"model": model.alias, "input": "hello"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "model_not_authorized"


@pytest.mark.asyncio
async def test_embeddings_authorized_restricted_model_ok(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """role_restricted embedding 模型授权后可调用。"""
    user = await _seed_user(db_session)
    _, plaintext = await _seed_api_key(db_session, user)
    model = await _seed_embedding_model(
        db_session, visibility=VISIBILITY_ROLE_RESTRICTED
    )
    role = Role(name=f"role-{uuid4().hex[:6]}")
    db_session.add(role)
    await db_session.flush()
    db_session.add(UserRole(user_id=user.id, role_id=role.id))
    db_session.add(RoleModelGrant(role_id=role.id, model_id=model.id))
    await _seed_billing_rule(db_session, model)
    _, channel, _ = await _seed_channel(db_session)
    await _seed_mapping(db_session, model, channel)
    await _seed_wallet(db_session, user)

    http_patch, _ = _patch_httpx(_make_upstream_embedding_response())
    with http_patch:
        resp = await api_client.post(
            "/v1/embeddings",
            json={"model": model.alias, "input": "hello"},
            headers={"Authorization": f"Bearer {plaintext}"},
        )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_embeddings_rejects_chat_model(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _seed_user(db_session)
    _, plaintext = await _seed_api_key(db_session, user)
    model = await _seed_chat_model(db_session)
    await _seed_billing_rule(db_session, model)
    await _seed_wallet(db_session, user)

    resp = await api_client.post(
        "/v1/embeddings",
        json={"model": model.alias, "input": "hello"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 400
    assert "embedding" in resp.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_embeddings_success_string_input_logs_and_billing(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """成功请求：OpenAI 兼容响应 + request/usage log + capture 流水；仅输入计费。"""
    user = await _seed_user(db_session)
    _, plaintext = await _seed_api_key(db_session, user)
    model = await _seed_embedding_model(db_session)
    await _seed_billing_rule(db_session, model, price_input=1_000_000, price_output=0)
    _, channel, _ = await _seed_channel(db_session)
    await _seed_mapping(db_session, model, channel)
    wallet = await _seed_wallet(db_session, user)
    balance_before = wallet.balance

    prompt_tokens = 10
    http_patch, mock_client = _patch_httpx(
        _make_upstream_embedding_response(prompt_tokens=prompt_tokens)
    )
    with http_patch:
        resp = await api_client.post(
            "/v1/embeddings",
            json={"model": model.alias, "input": "The food was delicious"},
            headers={"Authorization": f"Bearer {plaintext}"},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    # OpenAI SDK embeddings.create 期望结构
    assert data["object"] == "list"
    assert data["model"] == model.alias
    assert isinstance(data["data"], list) and len(data["data"]) >= 1
    assert data["data"][0]["object"] == "embedding"
    assert isinstance(data["data"][0]["embedding"], list)
    assert data["data"][0]["index"] == 0
    assert data["usage"]["prompt_tokens"] == prompt_tokens
    assert data["usage"]["total_tokens"] == prompt_tokens
    assert "completion_tokens" not in data["usage"]

    # 上游 URL 应为 /embeddings
    call_args = mock_client.post.await_args
    assert call_args is not None
    assert str(call_args.args[0]).endswith("/embeddings")

    req_log = (
        await db_session.execute(
            select(RequestLog).where(
                RequestLog.user_id == user.id, RequestLog.status == "success"
            )
        )
    ).scalar_one()
    assert req_log.model_id == model.id
    assert req_log.http_status == 200
    assert not req_log.is_stream

    usage = (
        await db_session.execute(
            select(UsageLog).where(UsageLog.request_id == req_log.request_id)
        )
    ).scalar_one()
    assert usage.input_tokens == prompt_tokens
    assert usage.output_tokens == 0
    # price_input=1_000_000 / 1M → 1 单位/token；10 tokens → 10
    assert usage.charge_amount == 10

    capture = (
        await db_session.execute(
            select(WalletTransaction).where(
                WalletTransaction.wallet_id == wallet.id,
                WalletTransaction.type == TX_CAPTURE,
                WalletTransaction.ref_request_id == req_log.request_id,
            )
        )
    ).scalar_one_or_none()
    assert capture is not None

    await db_session.refresh(wallet)
    assert wallet.held == 0
    assert wallet.balance == balance_before - usage.charge_amount


@pytest.mark.asyncio
async def test_embeddings_success_array_input(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _seed_user(db_session)
    _, plaintext = await _seed_api_key(db_session, user)
    model = await _seed_embedding_model(db_session)
    await _seed_billing_rule(db_session, model)
    _, channel, _ = await _seed_channel(db_session)
    await _seed_mapping(db_session, model, channel)
    await _seed_wallet(db_session, user)

    http_patch, mock_client = _patch_httpx(
        _make_upstream_embedding_response(prompt_tokens=6, n=2)
    )
    with http_patch:
        resp = await api_client.post(
            "/v1/embeddings",
            json={"model": model.alias, "input": ["alpha", "beta"]},
            headers={"Authorization": f"Bearer {plaintext}"},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["data"]) == 2
    assert data["data"][1]["index"] == 1

    call_kwargs = mock_client.post.await_args.kwargs
    assert call_kwargs["json"]["input"] == ["alpha", "beta"]


@pytest.mark.asyncio
async def test_embeddings_upstream_failure_no_usage_log(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _seed_user(db_session)
    _, plaintext = await _seed_api_key(db_session, user)
    model = await _seed_embedding_model(db_session)
    await _seed_billing_rule(db_session, model)
    _, channel, _ = await _seed_channel(db_session)
    await _seed_mapping(db_session, model, channel)
    wallet = await _seed_wallet(db_session, user)
    balance_before = wallet.balance

    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.json.return_value = {
        "error": {"message": "boom", "type": "server_error", "code": "server_error"}
    }
    http_patch, _ = _patch_httpx(mock_resp)
    with http_patch:
        resp = await api_client.post(
            "/v1/embeddings",
            json={"model": model.alias, "input": "hello"},
            headers={"Authorization": f"Bearer {plaintext}"},
        )

    assert resp.status_code == 500

    req_log = (
        await db_session.execute(
            select(RequestLog).where(
                RequestLog.user_id == user.id, RequestLog.status == "failed"
            )
        )
    ).scalar_one_or_none()
    assert req_log is not None

    usage = (
        await db_session.execute(
            select(UsageLog).where(UsageLog.request_id == req_log.request_id)
        )
    ).scalar_one_or_none()
    assert usage is None

    await db_session.refresh(wallet)
    assert wallet.held == 0
    assert wallet.balance == balance_before


@pytest.mark.asyncio
async def test_embeddings_org_billing_quota_and_logs(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """组织身份调用 embeddings：组织钱包扣费 + 成员额度累计。"""
    user = await _seed_user(db_session, "org")
    personal = await wallet_service.get_or_create_personal_wallet(db_session, user.id)
    personal.balance = 100_000_000
    org, member, org_wallet = await _seed_org_ready(
        db_session, user, org_balance=100_000_000, quota_limit=50_000
    )
    _, plaintext = await _seed_api_key(
        db_session, user, billing_subject=format_org_subject_ref(org.id)
    )
    model = await _seed_embedding_model(db_session)
    await _seed_billing_rule(db_session, model, price_input=1_000_000, price_output=0)
    _, channel, _ = await _seed_channel(db_session)
    await _seed_mapping(db_session, model, channel)
    await db_session.flush()

    org_before = org_wallet.balance
    personal_before = personal.balance

    http_patch, _ = _patch_httpx(
        _make_upstream_embedding_response(prompt_tokens=10)
    )
    with http_patch:
        resp = await api_client.post(
            "/v1/embeddings",
            json={"model": model.alias, "input": "org billing test"},
            headers={"Authorization": f"Bearer {plaintext}"},
        )

    assert resp.status_code == 200, resp.text

    req_log = (
        await db_session.execute(
            select(RequestLog).where(
                RequestLog.user_id == user.id, RequestLog.status == "success"
            )
        )
    ).scalar_one()
    assert req_log.billing_subject == format_org_subject_ref(org.id)
    assert req_log.org_id == org.id
    assert req_log.actor_user_id == user.id

    usage = (
        await db_session.execute(
            select(UsageLog).where(UsageLog.request_id == req_log.request_id)
        )
    ).scalar_one()
    assert usage.org_id == org.id
    assert usage.charge_amount == 10

    hold_tx = (
        await db_session.execute(
            select(WalletTransaction).where(
                WalletTransaction.wallet_id == org_wallet.id,
                WalletTransaction.type == TX_HOLD,
                WalletTransaction.ref_request_id == req_log.request_id,
            )
        )
    ).scalar_one()
    assert hold_tx.org_id == org.id

    capture_tx = (
        await db_session.execute(
            select(WalletTransaction).where(
                WalletTransaction.wallet_id == org_wallet.id,
                WalletTransaction.type == TX_CAPTURE,
                WalletTransaction.ref_request_id == req_log.request_id,
            )
        )
    ).scalar_one()
    assert capture_tx.org_id == org.id

    await db_session.refresh(org_wallet)
    await db_session.refresh(personal)
    assert org_wallet.held == 0
    assert org_wallet.balance == org_before - usage.charge_amount
    assert personal.balance == personal_before

    quota = await quota_service.get_quota(db_session, member.id)
    assert quota is not None
    assert quota.quota_used == usage.charge_amount


@pytest.mark.asyncio
async def test_embeddings_org_quota_exhausted_returns_402(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """组织额度耗尽且不允许回退时，embeddings 返回 402，不调用上游。"""
    user = await _seed_user(db_session, "q")
    await wallet_service.get_or_create_personal_wallet(db_session, user.id)
    org, member, org_wallet = await _seed_org_ready(
        db_session, user, org_balance=100_000_000, quota_limit=1
    )
    await quota_service.quota_consume(
        db_session, org_member_id=member.id, amount=1
    )
    await db_session.flush()

    _, plaintext = await _seed_api_key(
        db_session, user, billing_subject=format_org_subject_ref(org.id)
    )
    model = await _seed_embedding_model(db_session)
    await _seed_billing_rule(db_session, model, price_input=1_000_000)
    _, channel, _ = await _seed_channel(db_session)
    await _seed_mapping(db_session, model, channel)

    http_patch, mock_client = _patch_httpx(_make_upstream_embedding_response())
    with http_patch:
        resp = await api_client.post(
            "/v1/embeddings",
            json={"model": model.alias, "input": "quota check"},
            headers={"Authorization": f"Bearer {plaintext}"},
        )

    assert resp.status_code == 402
    assert mock_client.post.await_count == 0
    await db_session.refresh(org_wallet)
    assert org_wallet.held == 0
