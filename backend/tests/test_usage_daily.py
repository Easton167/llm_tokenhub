"""任务 9：渠道成本价配置与日聚合正确性。

验收：
  - 聚合收入合计 = 当日 capture 流水合计
  - 重跑同一天不产生重复行
  - 未配置成本价 → cost=0 且 cost_price_missing=True
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.channel_key_crypto import encrypt_channel_key
from app.core.constants import STATUS_DISABLED, STATUS_ENABLED, VISIBILITY_PUBLIC
from app.db.models.api_key import ApiKey
from app.db.models.channel import Channel, ChannelKey, Provider
from app.db.models.channel_cost import ChannelCost
from app.db.models.llm_model import LLMModel, ModelChannelMap
from app.db.models.log import RequestLog, UsageLog
from app.db.models.usage_daily import UsageDailySummary
from app.db.models.user import User
from app.db.models.wallet import WalletTransaction
from app.db.session import get_db
from app.main import app
from app.services.usage_daily_service import (
    aggregate_usage_daily,
    calc_token_cost,
    day_bounds_utc,
)
from app.services.wallet_service import TX_CAPTURE, wallet_service
from tests.conftest import admin_auth_headers


@pytest_asyncio.fixture
async def admin_client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
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


async def _seed_user(session: AsyncSession, suffix: str = "") -> User:
    user = User(
        email=f"daily-{suffix}-{uuid4().hex[:8]}@example.com",
        status=STATUS_ENABLED,
    )
    session.add(user)
    await session.flush()
    await wallet_service.get_or_create_personal_wallet(session, user.id)
    return user


async def _seed_api_key(session: AsyncSession, user: User) -> ApiKey:
    key = ApiKey(
        user_id=user.id,
        name="daily-key",
        key_prefix="sk-test",
        key_hash=f"hash-{uuid4().hex}",
        status=STATUS_ENABLED,
    )
    session.add(key)
    await session.flush()
    return key


async def _seed_model_channel(
    session: AsyncSession,
    *,
    upstream: str = "gpt-4o-upstream",
) -> tuple[LLMModel, Channel]:
    provider = Provider(
        name=f"prov-{uuid4().hex[:6]}",
        adapter_type="openai_compatible",
        default_base_url="https://api.example.com/v1",
    )
    session.add(provider)
    await session.flush()

    channel = Channel(
        provider_id=provider.id,
        name=f"chan-{uuid4().hex[:6]}",
        base_url="https://api.example.com/v1",
        adapter_type="openai_compatible",
        priority=0,
        weight=1,
        status=STATUS_ENABLED,
        health_status=0,
        timeout_ms=30_000,
        max_retries=1,
    )
    session.add(channel)
    await session.flush()

    session.add(
        ChannelKey(
            channel_id=channel.id,
            api_key_encrypted=encrypt_channel_key("sk-up"),
            key_masked="****up",
            weight=1,
            status=STATUS_ENABLED,
            health_status=0,
        )
    )

    model = LLMModel(
        alias=f"m-{uuid4().hex[:6]}",
        display_name="daily-model",
        model_type="chat",
        capabilities={},
        visibility=VISIBILITY_PUBLIC,
        status=STATUS_ENABLED,
    )
    session.add(model)
    await session.flush()

    session.add(
        ModelChannelMap(
            model_id=model.id,
            channel_id=channel.id,
            upstream_model_name=upstream,
            weight=1,
            status=STATUS_ENABLED,
        )
    )
    await session.flush()
    return model, channel


async def _seed_captured_usage(
    session: AsyncSession,
    *,
    user: User,
    api_key: ApiKey,
    model: LLMModel,
    channel: Channel,
    request_id: str,
    input_tokens: int,
    output_tokens: int,
    charge_amount: int,
    at: datetime,
    org_id=None,
) -> None:
    wallet = await wallet_service.get_or_create_personal_wallet(session, user.id)
    session.add(
        RequestLog(
            request_id=request_id,
            api_key_id=api_key.id,
            user_id=user.id,
            org_id=org_id,
            actor_user_id=user.id,
            billing_subject="personal",
            model_id=model.id,
            channel_id=channel.id,
            status="success",
            http_status=200,
            latency_ms=10,
            is_stream=False,
            created_at=at,
        )
    )
    session.add(
        UsageLog(
            request_id=request_id,
            org_id=org_id,
            actor_user_id=user.id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=0,
            billing_rule_version=1,
            cost_amount=0,
            charge_amount=charge_amount,
            is_estimated=False,
            created_at=at,
        )
    )
    session.add(
        WalletTransaction(
            wallet_id=wallet.id,
            type=TX_CAPTURE,
            amount=-charge_amount,
            balance_after=wallet.balance,
            held_after=0,
            ref_request_id=request_id,
            org_id=org_id,
            operator_user_id=user.id,
            remark="test capture",
            created_at=at,
        )
    )
    await session.flush()


def test_calc_token_cost_rounds_up() -> None:
    # 1 token * 1 / 1e6 → 向上取整为 1
    assert calc_token_cost(input_tokens=1, output_tokens=0, cost_input=1, cost_output=0) == 1
    assert (
        calc_token_cost(
            input_tokens=1_000_000,
            output_tokens=0,
            cost_input=100,
            cost_output=0,
        )
        == 100
    )


def test_day_bounds_utc() -> None:
    start, end = day_bounds_utc(date(2026, 7, 22))
    assert start == datetime(2026, 7, 22, tzinfo=UTC)
    assert end == datetime(2026, 7, 23, tzinfo=UTC)


@pytest.mark.asyncio
async def test_channel_cost_api_versioned(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    _, channel = await _seed_model_channel(db_session)
    headers = admin_auth_headers()

    r1 = await admin_client.post(
        f"/admin/channels/{channel.id}/costs",
        headers=headers,
        json={
            "upstream_model_name": "gpt-4o-upstream",
            "cost_input": 100,
            "cost_output": 200,
            "effective_at": "2026-01-01T00:00:00Z",
        },
    )
    assert r1.status_code == 201, r1.text
    assert r1.json()["version"] == 1

    r2 = await admin_client.post(
        f"/admin/channels/{channel.id}/costs",
        headers=headers,
        json={
            "upstream_model_name": "gpt-4o-upstream",
            "cost_input": 150,
            "cost_output": 250,
            "effective_at": "2026-06-01T00:00:00Z",
        },
    )
    assert r2.status_code == 201, r2.text
    assert r2.json()["version"] == 2

    listed = await admin_client.get(
        f"/admin/channels/{channel.id}/costs", headers=headers
    )
    assert listed.status_code == 200
    assert len(listed.json()) == 2

    void = await admin_client.patch(
        f"/admin/channels/{channel.id}/costs/{r1.json()['id']}",
        headers=headers,
        json={"status": STATUS_DISABLED},
    )
    assert void.status_code == 200
    assert void.json()["status"] == STATUS_DISABLED


@pytest.mark.asyncio
async def test_aggregate_revenue_matches_captures(
    db_session: AsyncSession,
) -> None:
    user = await _seed_user(db_session, "rev")
    api_key = await _seed_api_key(db_session, user)
    model, channel = await _seed_model_channel(db_session, upstream="u-model-a")

    day = date(2099, 1, 10)
    at = datetime(2099, 1, 10, 10, 0, tzinfo=UTC)

    # 配置成本：每 1M 输入 1_000_000 → 每 token 成本 1
    db_session.add(
        ChannelCost(
            channel_id=channel.id,
            upstream_model_name="u-model-a",
            version=1,
            cost_input=1_000_000,
            cost_output=2_000_000,
            currency="CNY",
            effective_at=datetime(2026, 1, 1, tzinfo=UTC),
            status=STATUS_ENABLED,
        )
    )
    await session_flush_usage(
        db_session,
        user=user,
        api_key=api_key,
        model=model,
        channel=channel,
        at=at,
    )

    result = await aggregate_usage_daily(db_session, summary_date=day)
    assert result.inserted == 1
    assert result.revenue_total == 3000 + 1500  # 两笔 charge
    assert result.cost_missing_rows == 0

    capture_sum = await db_session.scalar(
        select(func.coalesce(func.sum(-WalletTransaction.amount), 0)).where(
            WalletTransaction.type == TX_CAPTURE,
            WalletTransaction.created_at >= day_bounds_utc(day)[0],
            WalletTransaction.created_at < day_bounds_utc(day)[1],
        )
    )
    assert int(capture_sum) == result.revenue_total

    rows = (
        await db_session.execute(
            select(UsageDailySummary).where(UsageDailySummary.summary_date == day)
        )
    ).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.revenue_amount == result.revenue_total
    assert row.request_count == 2
    assert row.input_tokens == 1000 + 500
    assert row.output_tokens == 200 + 100
    # cost = 1500*1 + 300*2 = 2100
    expected_cost = calc_token_cost(
        input_tokens=1500,
        output_tokens=300,
        cost_input=1_000_000,
        cost_output=2_000_000,
    )
    assert row.cost_amount == expected_cost
    assert row.cost_price_missing is False


async def session_flush_usage(
    session: AsyncSession,
    *,
    user: User,
    api_key: ApiKey,
    model: LLMModel,
    channel: Channel,
    at: datetime,
) -> None:
    await _seed_captured_usage(
        session,
        user=user,
        api_key=api_key,
        model=model,
        channel=channel,
        request_id=f"req-{uuid4().hex[:12]}",
        input_tokens=1000,
        output_tokens=200,
        charge_amount=3000,
        at=at,
    )
    await _seed_captured_usage(
        session,
        user=user,
        api_key=api_key,
        model=model,
        channel=channel,
        request_id=f"req-{uuid4().hex[:12]}",
        input_tokens=500,
        output_tokens=100,
        charge_amount=1500,
        at=at + timedelta(minutes=1),
    )


@pytest.mark.asyncio
async def test_aggregate_rerun_idempotent(db_session: AsyncSession) -> None:
    user = await _seed_user(db_session, "idem")
    api_key = await _seed_api_key(db_session, user)
    model, channel = await _seed_model_channel(db_session)
    day = date(2099, 1, 11)
    at = datetime(2099, 1, 11, 8, 0, tzinfo=UTC)

    await _seed_captured_usage(
        db_session,
        user=user,
        api_key=api_key,
        model=model,
        channel=channel,
        request_id=f"req-{uuid4().hex[:12]}",
        input_tokens=100,
        output_tokens=10,
        charge_amount=99,
        at=at,
    )

    r1 = await aggregate_usage_daily(db_session, summary_date=day)
    r2 = await aggregate_usage_daily(db_session, summary_date=day)
    assert r1.inserted == 1
    assert r2.deleted == 1
    assert r2.inserted == 1

    count = await db_session.scalar(
        select(func.count()).select_from(UsageDailySummary).where(
            UsageDailySummary.summary_date == day
        )
    )
    assert count == 1


@pytest.mark.asyncio
async def test_aggregate_missing_cost_price(db_session: AsyncSession) -> None:
    user = await _seed_user(db_session, "nocost")
    api_key = await _seed_api_key(db_session, user)
    model, channel = await _seed_model_channel(db_session, upstream="no-cost-model")
    day = date(2099, 1, 12)
    at = datetime(2099, 1, 12, 12, 0, tzinfo=UTC)

    await _seed_captured_usage(
        db_session,
        user=user,
        api_key=api_key,
        model=model,
        channel=channel,
        request_id=f"req-{uuid4().hex[:12]}",
        input_tokens=10_000,
        output_tokens=1_000,
        charge_amount=500,
        at=at,
    )

    result = await aggregate_usage_daily(db_session, summary_date=day)
    assert result.inserted == 1
    assert result.cost_total == 0
    assert result.cost_missing_rows == 1

    row = (
        await db_session.execute(
            select(UsageDailySummary).where(UsageDailySummary.summary_date == day)
        )
    ).scalar_one()
    assert row.cost_amount == 0
    assert row.cost_price_missing is True
    assert row.revenue_amount == 500


@pytest.mark.asyncio
async def test_admin_daily_aggregate_endpoint(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _seed_user(db_session, "api")
    api_key = await _seed_api_key(db_session, user)
    model, channel = await _seed_model_channel(db_session)
    day = date(2099, 1, 13)
    at = datetime(2099, 1, 13, 1, 0, tzinfo=UTC)
    await _seed_captured_usage(
        db_session,
        user=user,
        api_key=api_key,
        model=model,
        channel=channel,
        request_id=f"req-{uuid4().hex[:12]}",
        input_tokens=1,
        output_tokens=1,
        charge_amount=42,
        at=at,
    )

    resp = await admin_client.post(
        "/admin/usage/daily-aggregate",
        headers=admin_auth_headers(),
        json={"summary_date": day.isoformat()},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["summary_date"] == day.isoformat()
    assert body["inserted"] == 1
    assert body["revenue_total"] == 42
    assert body["cost_missing_rows"] == 1
