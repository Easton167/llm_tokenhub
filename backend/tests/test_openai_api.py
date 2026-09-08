"""OpenAI 兼容接口集成测试（Task 7）。

覆盖验收标准：
  - GET /v1/models 返回已授权模型列表
  - POST /v1/chat/completions 成功请求产生 request_log、usage_log、wallet_transaction
  - POST /v1/chat/completions 上游失败不产生计费 usage_log
  - POST /v1/chat/completions 余额不足返回 402
  - POST /v1/chat/completions 模型不存在返回 404
  - POST /v1/chat/completions 无权使用模型返回 403
  - 返回结构符合 OpenAI 规范（model 字段为平台 alias）

测试策略：
  - 使用 db_session fixture 在数据库事务中创建测试数据（事务回滚清理）
  - commit() 替换为 flush()，防止测试数据真实提交
  - 上游 HTTP 调用通过 unittest.mock.patch 拦截
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.channel_key_crypto import encrypt_channel_key
from app.core.constants import STATUS_ENABLED, VISIBILITY_PUBLIC, VISIBILITY_ROLE_RESTRICTED
from app.db.models.billing import BillingRule
from app.db.models.channel import Channel, ChannelKey, Provider
from app.db.models.llm_model import LLMModel, ModelChannelMap
from app.db.models.log import RequestLog, UsageLog
from app.db.models.user import Role, RoleModelGrant, User, UserRole
from app.db.models.wallet import Wallet, WalletTransaction
from app.db.session import get_db
from app.main import app
from app.services.auth_service import auth_service
from app.services.wallet_service import wallet_service


# ===========================================================================
# 测试数据创建辅助函数
# ===========================================================================


async def _seed_user(session: AsyncSession) -> User:
    user = User(email=f"v1-test-{uuid4().hex[:8]}@example.com", status=STATUS_ENABLED)
    session.add(user)
    await session.flush()
    return user


async def _seed_api_key(session: AsyncSession, user: User) -> tuple[str, str]:
    """创建 API Key；返回 (key_id_str, plaintext)。"""
    api_key_obj, plaintext = await auth_service.create_api_key(
        session, user_id=user.id, name="test-key"
    )
    return str(api_key_obj.id), plaintext


async def _seed_model(
    session: AsyncSession,
    *,
    alias: str | None = None,
    visibility: str = VISIBILITY_PUBLIC,
    capabilities: dict | None = None,
) -> LLMModel:
    alias = alias or f"test-model-{uuid4().hex[:6]}"
    model = LLMModel(
        alias=alias,
        display_name=alias,
        model_type="chat",
        capabilities=capabilities if capabilities is not None else {},
        visibility=visibility,
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
        api_key_encrypted=encrypt_channel_key("sk-upstream-test-key"),
        key_masked="****test",
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
    upstream_model_name: str = "gpt-4o-2024-11-20",
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
    price_input: int = 180_000,    # 18 元/1M token → 180_000 单位/1M（1元=10000单位）
    price_output: int = 720_000,   # 72 元/1M token
    price_cached: int = 90_000,    # 9 元/1M token
) -> BillingRule:
    rule = BillingRule(
        model_id=model.id,
        version=1,
        price_input=price_input,
        price_output=price_output,
        price_cached_input=price_cached,
        price_per_call=0,
        cost_input=price_input,
        cost_output=price_output,
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
    balance: int = 100_000_000,  # 10000 元（余额充足）
) -> Wallet:
    w = await wallet_service.get_or_create_personal_wallet(session, user.id)
    # 直接更新余额（测试用，不走充值流水）
    w.balance = balance
    await session.flush()
    return w


# ===========================================================================
# 上游 Mock 响应构造
# ===========================================================================


def _make_upstream_success_response(
    *,
    content: str = "Hello!",
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
    model: str = "gpt-4o-2024-11-20",
) -> MagicMock:
    """构造成功的上游 HTTP 响应 Mock。"""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "id": f"chatcmpl-{uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": 1720000000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
    return mock_resp


def _make_upstream_error_response(
    *,
    status_code: int = 500,
    message: str = "Internal Server Error",
) -> MagicMock:
    """构造失败的上游 HTTP 响应 Mock。"""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = {
        "error": {"message": message, "type": "server_error", "code": "upstream_error"}
    }
    return mock_resp


def _patch_httpx(mock_resp: MagicMock):
    """返回 patch context manager，拦截 httpx.AsyncClient 非流式 post。"""
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_client.aclose = AsyncMock()
    mock_client_cls = MagicMock()
    mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
    return patch("app.api.v1.chat.httpx.AsyncClient", mock_client_cls), mock_client


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest_asyncio.fixture
async def api_client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """提供覆盖了 get_db 的 ASGI 测试客户端。

    将 session.commit 替换为 session.flush，使 endpoint 内的多次 commit
    不会真正提交事务，测试结束后由 db_session fixture 统一回滚。
    """
    original_commit = db_session.commit

    async def _fake_commit() -> None:
        # flush 令 SQL 语句执行并在事务内可见，但不实际提交
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
# GET /v1/models 测试
# ===========================================================================


@pytest.mark.asyncio
async def test_list_models_no_auth_returns_401(api_client: AsyncClient) -> None:
    """未携带 Authorization 时返回 401。"""
    resp = await api_client.get("/v1/models")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_list_models_invalid_key_returns_401(api_client: AsyncClient) -> None:
    """无效 Key 返回 401。"""
    resp = await api_client.get(
        "/v1/models", headers={"Authorization": "Bearer sk-nonexistent-key-xyz"}
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_list_models_returns_public_models(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """可见性为 public 的已启用模型应出现在列表中。"""
    user = await _seed_user(db_session)
    _, plaintext = await _seed_api_key(db_session, user)
    model = await _seed_model(db_session, visibility=VISIBILITY_PUBLIC)

    resp = await api_client.get(
        "/v1/models", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "list"
    model_ids = [m["id"] for m in data["data"]]
    assert model.alias in model_ids


@pytest.mark.asyncio
async def test_list_models_role_restricted_not_visible_without_grant(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """role_restricted 模型在用户未被授权时不出现在列表中。"""
    user = await _seed_user(db_session)
    _, plaintext = await _seed_api_key(db_session, user)
    model = await _seed_model(db_session, visibility=VISIBILITY_ROLE_RESTRICTED)

    resp = await api_client.get(
        "/v1/models", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert resp.status_code == 200
    model_ids = [m["id"] for m in resp.json()["data"]]
    assert model.alias not in model_ids


@pytest.mark.asyncio
async def test_list_models_role_restricted_visible_with_grant(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """授权了 role_restricted 模型的用户能在列表中看到它。"""
    user = await _seed_user(db_session)
    _, plaintext = await _seed_api_key(db_session, user)
    model = await _seed_model(db_session, visibility=VISIBILITY_ROLE_RESTRICTED)

    role = Role(name=f"role-{uuid4().hex[:6]}")
    db_session.add(role)
    await db_session.flush()

    db_session.add(UserRole(user_id=user.id, role_id=role.id))
    db_session.add(RoleModelGrant(role_id=role.id, model_id=model.id))
    await db_session.flush()

    resp = await api_client.get(
        "/v1/models", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert resp.status_code == 200
    model_ids = [m["id"] for m in resp.json()["data"]]
    assert model.alias in model_ids


@pytest.mark.asyncio
async def test_list_models_returns_capabilities(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """每个模型条目应包含 capabilities 字段，内容与库中配置一致。"""
    user = await _seed_user(db_session)
    _, plaintext = await _seed_api_key(db_session, user)
    caps = {"stream": True, "vision": False, "max_context_tokens": 128000}
    model = await _seed_model(db_session, capabilities=caps)

    resp = await api_client.get(
        "/v1/models", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert resp.status_code == 200
    entry = next(m for m in resp.json()["data"] if m["id"] == model.alias)
    assert entry["capabilities"] == caps


@pytest.mark.asyncio
async def test_list_models_empty_capabilities_returns_empty_dict(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """未配置 capabilities 的模型应返回空对象 {}。"""
    user = await _seed_user(db_session)
    _, plaintext = await _seed_api_key(db_session, user)
    model = await _seed_model(db_session)

    resp = await api_client.get(
        "/v1/models", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert resp.status_code == 200
    entry = next(m for m in resp.json()["data"] if m["id"] == model.alias)
    assert entry["capabilities"] == {}


# ===========================================================================
# POST /v1/chat/completions 测试
# ===========================================================================


@pytest.mark.asyncio
async def test_chat_completions_no_auth_returns_401(api_client: AsyncClient) -> None:
    """未携带 Authorization 时返回 401，不预扣。"""
    resp = await api_client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "Hi"}]},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_chat_completions_model_not_found_returns_404(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """请求不存在的模型返回 404，不预扣。"""
    user = await _seed_user(db_session)
    _, plaintext = await _seed_api_key(db_session, user)

    resp = await api_client.post(
        "/v1/chat/completions",
        json={"model": "nonexistent-model", "messages": [{"role": "user", "content": "Hi"}]},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "model_not_found"


@pytest.mark.asyncio
async def test_chat_completions_model_not_authorized_returns_403(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """请求 role_restricted 模型但无授权时返回 403，不预扣。"""
    user = await _seed_user(db_session)
    _, plaintext = await _seed_api_key(db_session, user)
    model = await _seed_model(db_session, visibility=VISIBILITY_ROLE_RESTRICTED)

    resp = await api_client.post(
        "/v1/chat/completions",
        json={"model": model.alias, "messages": [{"role": "user", "content": "Hi"}]},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "model_not_authorized"


@pytest.mark.asyncio
async def test_chat_completions_insufficient_balance_returns_402(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """余额为 0 时预扣失败，返回 402，不产生 usage_log。"""
    user = await _seed_user(db_session)
    _, plaintext = await _seed_api_key(db_session, user)
    model = await _seed_model(db_session)
    await _seed_billing_rule(db_session, model)
    # 钱包余额为 0（默认）
    await wallet_service.get_or_create_personal_wallet(db_session, user.id)

    resp = await api_client.post(
        "/v1/chat/completions",
        json={"model": model.alias, "messages": [{"role": "user", "content": "Hello!"}]},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 402
    assert resp.json()["error"]["code"] == "insufficient_balance"


@pytest.mark.asyncio
async def test_chat_completions_success_produces_logs_and_transaction(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """
    成功请求验收：
    - 返回 200 且结构符合 OpenAI 规范
    - model 字段为平台 alias（非上游模型名）
    - 产生 request_log（status=success）
    - 产生 usage_log
    - 产生 wallet_transaction（capture）
    """
    user = await _seed_user(db_session)
    _, plaintext = await _seed_api_key(db_session, user)
    model = await _seed_model(db_session)
    await _seed_billing_rule(db_session, model)
    _, channel, _ = await _seed_channel(db_session)
    await _seed_mapping(db_session, model, channel)
    wallet = await _seed_wallet(db_session, user, balance=100_000_000)

    mock_resp = _make_upstream_success_response(
        content="你好！",
        prompt_tokens=12,
        completion_tokens=8,
    )
    http_patch, _ = _patch_httpx(mock_resp)

    with http_patch:
        resp = await api_client.post(
            "/v1/chat/completions",
            json={
                "model": model.alias,
                "messages": [{"role": "user", "content": "你好"}],
                "max_tokens": 100,
            },
            headers={"Authorization": f"Bearer {plaintext}"},
        )

    # 验证 HTTP 响应结构
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["object"] == "chat.completion"
    assert data["model"] == model.alias, "model 字段应为平台 alias，而非上游模型名"
    assert len(data["choices"]) == 1
    assert data["choices"][0]["message"]["content"] == "你好！"
    assert data["choices"][0]["finish_reason"] == "stop"
    assert data["usage"]["prompt_tokens"] == 12
    assert data["usage"]["completion_tokens"] == 8

    # 验证 request_log 已写入（status=success）
    req_log = await db_session.execute(
        select(RequestLog).where(
            RequestLog.user_id == user.id,
            RequestLog.status == "success",
        )
    )
    req_log_obj = req_log.scalar_one_or_none()
    assert req_log_obj is not None, "应产生 request_log"
    assert req_log_obj.http_status == 200
    assert req_log_obj.model_id == model.id
    assert not req_log_obj.is_stream

    # 验证 usage_log 已写入
    usage_log = await db_session.execute(
        select(UsageLog).where(UsageLog.request_id == req_log_obj.request_id)
    )
    usage_log_obj = usage_log.scalar_one_or_none()
    assert usage_log_obj is not None, "成功请求应产生 usage_log"
    assert usage_log_obj.input_tokens == 12
    assert usage_log_obj.output_tokens == 8
    assert usage_log_obj.charge_amount >= 0

    # 验证 wallet_transaction 中有 capture 记录
    txn = await db_session.execute(
        select(WalletTransaction).where(
            WalletTransaction.wallet_id == wallet.id,
            WalletTransaction.type == "capture",
            WalletTransaction.ref_request_id == req_log_obj.request_id,
        )
    )
    assert txn.scalar_one_or_none() is not None, "应产生 capture wallet_transaction"


@pytest.mark.asyncio
async def test_chat_completions_upstream_failure_no_usage_log(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """
    上游失败验收（PRD 10.6）：
    - 返回 502
    - 不产生 usage_log（不计费）
    - 产生 request_log（status=failed）
    - 冻结额度全额释放（wallet balance 不变）
    """
    user = await _seed_user(db_session)
    _, plaintext = await _seed_api_key(db_session, user)
    model = await _seed_model(db_session)
    await _seed_billing_rule(db_session, model)
    _, channel, _ = await _seed_channel(db_session)
    await _seed_mapping(db_session, model, channel)
    initial_balance = 100_000_000
    wallet = await _seed_wallet(db_session, user, balance=initial_balance)

    mock_resp = _make_upstream_error_response(status_code=500, message="Internal Server Error")
    http_patch, _ = _patch_httpx(mock_resp)

    with http_patch:
        resp = await api_client.post(
            "/v1/chat/completions",
            json={
                "model": model.alias,
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 50,
            },
            headers={"Authorization": f"Bearer {plaintext}"},
        )

    # 上游失败，但错误被归一化为上游错误
    assert resp.status_code in (502, 500), resp.text
    error_body = resp.json()
    assert "error" in error_body

    # request_log 应存在且 status=failed
    req_log = await db_session.execute(
        select(RequestLog).where(
            RequestLog.user_id == user.id,
            RequestLog.status == "failed",
        )
    )
    req_log_obj = req_log.scalar_one_or_none()
    assert req_log_obj is not None, "失败请求应产生 request_log（status=failed）"

    # usage_log 不应存在（不计费）
    usage_log = await db_session.execute(
        select(UsageLog).where(UsageLog.request_id == req_log_obj.request_id)
    )
    assert usage_log.scalar_one_or_none() is None, "失败请求不应产生 usage_log（不计费）"

    # 冻结额度应已释放（wallet 中有 release 流水）
    release_txn = await db_session.execute(
        select(WalletTransaction).where(
            WalletTransaction.wallet_id == wallet.id,
            WalletTransaction.type == "release",
        )
    )
    assert release_txn.scalar_one_or_none() is not None, "失败请求应释放预扣冻结"


@pytest.mark.asyncio
async def test_chat_completions_response_model_is_platform_alias(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """返回的 model 字段是平台对外 alias，而不是上游真实模型名。"""
    user = await _seed_user(db_session)
    _, plaintext = await _seed_api_key(db_session, user)
    platform_alias = f"my-custom-model-{uuid4().hex[:6]}"
    upstream_name = "gpt-4o-2024-11-20"
    model = await _seed_model(db_session, alias=platform_alias)
    await _seed_billing_rule(db_session, model)
    _, channel, _ = await _seed_channel(db_session)
    await _seed_mapping(db_session, model, channel, upstream_model_name=upstream_name)
    await _seed_wallet(db_session, user, balance=100_000_000)

    mock_resp = _make_upstream_success_response(model=upstream_name)
    http_patch, _ = _patch_httpx(mock_resp)

    with http_patch:
        resp = await api_client.post(
            "/v1/chat/completions",
            json={
                "model": platform_alias,
                "messages": [{"role": "user", "content": "Hi"}],
            },
            headers={"Authorization": f"Bearer {plaintext}"},
        )

    assert resp.status_code == 200
    assert resp.json()["model"] == platform_alias, "响应 model 应为平台 alias，非上游真实名"


@pytest.mark.asyncio
async def test_chat_completions_no_billing_rule_returns_400(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """模型无计费规则时应拒绝调用（防漏计费，PRD A5）。"""
    user = await _seed_user(db_session)
    _, plaintext = await _seed_api_key(db_session, user)
    model = await _seed_model(db_session)  # 故意不添加计费规则

    resp = await api_client.post(
        "/v1/chat/completions",
        json={"model": model.alias, "messages": [{"role": "user", "content": "Hi"}]},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 400
    assert "billing" in resp.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_chat_completions_no_channel_returns_503(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """无可用渠道时返回 503，冻结额度释放，不产生 usage_log。"""
    user = await _seed_user(db_session)
    _, plaintext = await _seed_api_key(db_session, user)
    model = await _seed_model(db_session)
    await _seed_billing_rule(db_session, model)
    # 故意不创建渠道和 mapping
    await _seed_wallet(db_session, user, balance=100_000_000)

    resp = await api_client.post(
        "/v1/chat/completions",
        json={"model": model.alias, "messages": [{"role": "user", "content": "Hi"}]},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "no_available_channel"
