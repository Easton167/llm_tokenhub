"""适配器注册表与网关动态选择适配器测试（Phase2 任务 11）。

覆盖验收标准：
  - 注册表按协议类型返回对应 Adapter 单例。
  - 新增适配器只需注册即可被查到，无需改网关代码。
  - 未知协议类型抛出明确配置错误（adapter_not_configured / 503），而非 500。
  - 网关：渠道 adapter_type 未注册时，请求返回明确配置错误，
    不产生 usage_log，且预扣冻结全额释放。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.openai_compatible import openai_compatible_adapter
from app.adapters.registry import (
    ADAPTER_TYPE_OPENAI_COMPATIBLE,
    AdapterNotConfiguredError,
    get_adapter,
    register_adapter,
    unregister_adapter,
)
from app.core.channel_key_crypto import encrypt_channel_key
from app.core.constants import STATUS_ENABLED, VISIBILITY_PUBLIC
from app.db.models.billing import BillingRule
from app.db.models.channel import Channel, ChannelKey, Provider
from app.db.models.log import RequestLog, UsageLog
from app.db.models.llm_model import LLMModel, ModelChannelMap
from app.db.models.user import User
from app.db.models.wallet import WalletTransaction
from app.db.session import get_db
from app.main import app
from app.services.auth_service import auth_service
from app.services.wallet_service import wallet_service


# =========================================================================
# 注册表单元测试
# =========================================================================


class TestAdapterRegistry:
    def test_openai_compatible_registered_by_default(self) -> None:
        """内置 openai_compatible 适配器默认已注册，返回全局单例。"""
        adapter = get_adapter(ADAPTER_TYPE_OPENAI_COMPATIBLE)
        assert adapter is openai_compatible_adapter

    def test_unknown_adapter_type_raises_config_error(self) -> None:
        """未知协议类型应抛出明确配置错误，而非笼统 500。"""
        with pytest.raises(AdapterNotConfiguredError) as exc_info:
            get_adapter("nonexistent_protocol")
        err = exc_info.value
        assert err.code == "adapter_not_configured"
        assert err.error_type == "configuration_error"
        assert err.status_code != 500
        assert "nonexistent_protocol" in err.message

    def test_register_new_adapter_no_gateway_change_needed(self) -> None:
        """新增适配器只需注册即可被查到（无需修改网关代码）。"""
        fake_adapter = MagicMock()
        adapter_type = f"fake-proto-{uuid4().hex[:6]}"
        try:
            register_adapter(adapter_type, fake_adapter)
            assert get_adapter(adapter_type) is fake_adapter
        finally:
            unregister_adapter(adapter_type)
        with pytest.raises(AdapterNotConfiguredError):
            get_adapter(adapter_type)


# =========================================================================
# 网关集成测试：未知协议类型的渠道
# =========================================================================


@pytest_asyncio.fixture
async def api_client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """覆盖 get_db 的 ASGI 客户端；commit 降级为 flush 以便测试后回滚。"""
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


async def _seed_chat_env_with_adapter_type(
    session: AsyncSession, adapter_type: str
) -> tuple[User, str, LLMModel]:
    """创建一套可下发请求的最小数据：用户/Key/模型/计费规则/渠道/映射/钱包。"""
    user = User(email=f"reg-test-{uuid4().hex[:8]}@example.com", status=STATUS_ENABLED)
    session.add(user)
    await session.flush()

    _, plaintext = await auth_service.create_api_key(
        session, user_id=user.id, name="registry-test-key"
    )

    model = LLMModel(
        alias=f"reg-model-{uuid4().hex[:6]}",
        display_name="Registry Test Model",
        model_type="chat",
        capabilities={},
        visibility=VISIBILITY_PUBLIC,
        status=STATUS_ENABLED,
    )
    session.add(model)
    await session.flush()

    rule = BillingRule(
        model_id=model.id,
        version=1,
        price_input=180_000,
        price_output=720_000,
        price_cached_input=90_000,
        price_per_call=0,
        cost_input=180_000,
        cost_output=720_000,
        multiplier="1.0",
        min_charge_unit=1,
        currency="CNY",
        effective_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        status=0,
    )
    session.add(rule)

    provider = Provider(
        name=f"reg-prov-{uuid4().hex[:6]}",
        adapter_type=adapter_type,
        default_base_url="https://api.example.com/v1",
    )
    session.add(provider)
    await session.flush()

    channel = Channel(
        provider_id=provider.id,
        name=f"reg-chan-{uuid4().hex[:6]}",
        base_url="https://api.example.com/v1",
        adapter_type=adapter_type,
        priority=0,
        weight=1,
        status=STATUS_ENABLED,
        health_status=0,
        timeout_ms=30_000,
        max_retries=2,
    )
    session.add(channel)
    await session.flush()

    session.add(
        ChannelKey(
            channel_id=channel.id,
            api_key_encrypted=encrypt_channel_key("sk-upstream-registry-test"),
            key_masked="****test",
            weight=1,
            status=STATUS_ENABLED,
            health_status=0,
        )
    )
    session.add(
        ModelChannelMap(
            model_id=model.id,
            channel_id=channel.id,
            upstream_model_name="upstream-model",
            weight=1,
            status=STATUS_ENABLED,
        )
    )
    await session.flush()

    wallet = await wallet_service.get_or_create_personal_wallet(session, user.id)
    wallet.balance = 100_000_000
    await session.flush()

    return user, plaintext, model


@pytest.mark.asyncio
async def test_chat_completions_unknown_adapter_type_returns_config_error(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """渠道协议类型未注册：明确配置错误 + 释放冻结 + 不产生 usage_log。"""
    user, plaintext, model = await _seed_chat_env_with_adapter_type(
        db_session, adapter_type=f"unknown-proto-{uuid4().hex[:6]}"
    )

    resp = await api_client.post(
        "/v1/chat/completions",
        json={
            "model": model.alias,
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 50,
        },
        headers={"Authorization": f"Bearer {plaintext}"},
    )

    assert resp.status_code == 503, resp.text
    error = resp.json()["error"]
    assert error["code"] == "adapter_not_configured"
    assert error["type"] == "configuration_error"

    # request_log 应存在且 status=failed，记录错误码
    req_log = (
        await db_session.execute(
            select(RequestLog).where(
                RequestLog.user_id == user.id,
                RequestLog.status == "failed",
            )
        )
    ).scalar_one_or_none()
    assert req_log is not None, "配置错误请求应写入 request_log（status=failed）"
    assert req_log.error_code == "adapter_not_configured"

    # 不产生 usage_log（不计费）
    usage_log = (
        await db_session.execute(
            select(UsageLog).where(UsageLog.request_id == req_log.request_id)
        )
    ).scalar_one_or_none()
    assert usage_log is None, "配置错误请求不应产生 usage_log"

    # 预扣冻结应全额释放
    release_txn = (
        await db_session.execute(
            select(WalletTransaction).where(
                WalletTransaction.type == "release",
                WalletTransaction.ref_request_id == req_log.request_id,
            )
        )
    ).scalar_one_or_none()
    assert release_txn is not None, "配置错误请求应释放预扣冻结"
