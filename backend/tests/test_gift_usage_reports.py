"""赠送余额专项任务 5：用量拆分、日汇总与报表收入口径。

验收：
  - 混合/纯现金/纯赠金调用后 usage_log 拆分正确且和等于 charge_amount
  - 日汇总 gift + cash = revenue_amount
  - 纯赠金消费毛利 = -cost_amount
  - fund_source 筛选
  - 对账能检出赠金拆分不一致
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.channel_key_crypto import encrypt_channel_key
from app.core.constants import STATUS_ENABLED, VISIBILITY_PUBLIC
from app.core.security import create_access_token
from app.db.models.api_key import ApiKey
from app.db.models.channel import Channel, ChannelKey, Provider
from app.db.models.channel_cost import ChannelCost
from app.db.models.llm_model import LLMModel, ModelChannelMap
from app.db.models.log import RequestLog, UsageLog
from app.db.models.usage_daily import UsageDailySummary
from app.db.models.user import User
from app.db.models.wallet import Wallet, WalletTransaction
from app.db.session import get_db
from app.main import app
from app.services import usage_service
from app.services.gift_service import gift_service
from app.services.usage_daily_service import aggregate_usage_daily
from app.services.wallet_service import TX_CAPTURE, wallet_service
from tests.conftest import admin_auth_headers


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    original_commit = db_session.commit

    async def _fake_commit() -> None:
        await db_session.flush()

    db_session.commit = _fake_commit  # type: ignore[method-assign]

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    db_session.commit = original_commit  # type: ignore[method-assign]
    app.dependency_overrides.clear()


async def _seed_user(session: AsyncSession, suffix: str = "") -> User:
    user = User(
        email=f"gift-usage{suffix}-{uuid4().hex[:8]}@example.com",
        status=STATUS_ENABLED,
    )
    session.add(user)
    await session.flush()
    return user


async def _seed_wallet(session: AsyncSession, user: User, *, cash: int = 0) -> Wallet:
    wallet = await wallet_service.get_or_create_personal_wallet(session, user.id)
    if cash > 0:
        wallet.balance = cash
        await session.flush()
    return wallet


async def _grant(session: AsyncSession, wallet: Wallet, user: User, amount: int):
    return await gift_service.grant(
        session,
        wallet_id=wallet.id,
        amount=amount,
        reason="promotion",
        expires_at=None,
        idempotency_key=f"key-{uuid4().hex}",
        granted_by=user.id,
        remark="口径测试赠金",
    )


async def _seed_model(session: AsyncSession) -> LLMModel:
    model = LLMModel(
        alias=f"gu-{uuid4().hex[:6]}",
        display_name="赠金用量测试模型",
        model_type="chat",
        capabilities={},
        visibility=VISIBILITY_PUBLIC,
        status=STATUS_ENABLED,
    )
    session.add(model)
    await session.flush()
    return model


async def _seed_api_key(session: AsyncSession, user: User) -> ApiKey:
    key = ApiKey(
        user_id=user.id,
        name="gift-usage-key",
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
    model = await _seed_model(session)
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


async def _write_request(
    session: AsyncSession,
    *,
    user: User,
    model: LLMModel,
    request_id: str,
    api_key: ApiKey | None = None,
    channel_id=None,
    at: datetime | None = None,
) -> None:
    log = RequestLog(
        request_id=request_id,
        api_key_id=api_key.id if api_key else None,
        user_id=user.id,
        billing_subject=f"user:{user.id}",
        model_id=model.id,
        channel_id=channel_id,
        status="success",
        http_status=200,
        latency_ms=10,
        is_stream=False,
    )
    if at is not None:
        log.created_at = at
    session.add(log)
    await session.flush()


async def _seed_captured_usage(
    session: AsyncSession,
    *,
    user: User,
    api_key: ApiKey,
    model: LLMModel,
    channel: Channel,
    request_id: str,
    charge_amount: int,
    gift_charge: int,
    cost_amount: int,
    input_tokens: int,
    output_tokens: int,
    at: datetime,
) -> None:
    cash_charge = charge_amount - gift_charge
    wallet = await wallet_service.get_or_create_personal_wallet(session, user.id)
    session.add(
        RequestLog(
            request_id=request_id,
            api_key_id=api_key.id,
            user_id=user.id,
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
            actor_user_id=user.id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=0,
            billing_rule_version=1,
            cost_amount=cost_amount,
            charge_amount=charge_amount,
            gift_charge_amount=gift_charge,
            cash_charge_amount=cash_charge,
            is_estimated=False,
            created_at=at,
        )
    )
    session.add(
        WalletTransaction(
            wallet_id=wallet.id,
            type=TX_CAPTURE,
            amount=-charge_amount,
            gift_amount=-gift_charge,
            balance_after=wallet.balance,
            held_after=0,
            ref_request_id=request_id,
            operator_user_id=user.id,
            remark="gift usage test capture",
            created_at=at,
        )
    )
    await session.flush()


# ---------------------------------------------------------------------
# write_usage_log 拆分
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_usage_log_mixed_split_matches_capture(
    db_session: AsyncSession,
) -> None:
    """PRD 示例：赠送 30 + 现金 1000，预扣 100，实扣 60 → gift 30 / cash 30。"""
    user = await _seed_user(db_session, "-mix")
    wallet = await _seed_wallet(db_session, user, cash=1000)
    await _grant(db_session, wallet, user, 30)
    model = await _seed_model(db_session)
    req = f"req-mix-{uuid4().hex[:8]}"

    await wallet_service.hold(
        db_session, wallet_id=wallet.id, amount=100, request_id=req
    )
    await wallet_service.capture(
        db_session,
        wallet_id=wallet.id,
        hold_amount=100,
        actual_amount=60,
        request_id=req,
    )
    await _write_request(db_session, user=user, model=model, request_id=req)
    log = await usage_service.write_usage_log(
        db_session,
        request_id=req,
        input_tokens=10,
        output_tokens=5,
        billing_rule_version=1,
        cost_amount=50,
        charge_amount=60,
        wallet_id=wallet.id,
    )
    assert log.gift_charge_amount == 30
    assert log.cash_charge_amount == 30
    assert log.gift_charge_amount + log.cash_charge_amount == log.charge_amount


@pytest.mark.asyncio
async def test_write_usage_log_cash_only(db_session: AsyncSession) -> None:
    user = await _seed_user(db_session, "-cash")
    wallet = await _seed_wallet(db_session, user, cash=10_000)
    model = await _seed_model(db_session)
    req = f"req-cash-{uuid4().hex[:8]}"

    await wallet_service.hold(
        db_session, wallet_id=wallet.id, amount=200, request_id=req
    )
    await wallet_service.capture(
        db_session,
        wallet_id=wallet.id,
        hold_amount=200,
        actual_amount=80,
        request_id=req,
    )
    await _write_request(db_session, user=user, model=model, request_id=req)
    log = await usage_service.write_usage_log(
        db_session,
        request_id=req,
        input_tokens=1,
        output_tokens=1,
        billing_rule_version=1,
        cost_amount=10,
        charge_amount=80,
    )
    assert log.gift_charge_amount == 0
    assert log.cash_charge_amount == 80
    assert log.gift_charge_amount + log.cash_charge_amount == log.charge_amount


@pytest.mark.asyncio
async def test_write_usage_log_gift_only(db_session: AsyncSession) -> None:
    user = await _seed_user(db_session, "-gift")
    wallet = await _seed_wallet(db_session, user, cash=0)
    await _grant(db_session, wallet, user, 500)
    model = await _seed_model(db_session)
    req = f"req-gift-{uuid4().hex[:8]}"

    await wallet_service.hold(
        db_session, wallet_id=wallet.id, amount=200, request_id=req
    )
    await wallet_service.capture(
        db_session,
        wallet_id=wallet.id,
        hold_amount=200,
        actual_amount=120,
        request_id=req,
    )
    await _write_request(db_session, user=user, model=model, request_id=req)
    log = await usage_service.write_usage_log(
        db_session,
        request_id=req,
        input_tokens=2,
        output_tokens=2,
        billing_rule_version=1,
        cost_amount=90,
        charge_amount=120,
    )
    assert log.gift_charge_amount == 120
    assert log.cash_charge_amount == 0
    assert log.gift_charge_amount + log.cash_charge_amount == log.charge_amount


@pytest.mark.asyncio
async def test_write_usage_log_fallback_without_capture(
    db_session: AsyncSession,
) -> None:
    user = await _seed_user(db_session, "-free")
    model = await _seed_model(db_session)
    req = f"req-free-{uuid4().hex[:8]}"
    await _write_request(db_session, user=user, model=model, request_id=req)
    log = await usage_service.write_usage_log(
        db_session,
        request_id=req,
        input_tokens=1,
        output_tokens=0,
        billing_rule_version=1,
        cost_amount=0,
        charge_amount=42,
    )
    assert log.gift_charge_amount == 0
    assert log.cash_charge_amount == 42


@pytest.mark.asyncio
async def test_write_usage_log_mismatch_falls_back_to_cash(
    db_session: AsyncSession,
) -> None:
    """capture 拆分与 charge_amount 对不上时按全现金写入，不写出自相矛盾的账。"""
    user = await _seed_user(db_session, "-mm")
    wallet = await _seed_wallet(db_session, user, cash=10_000)
    model = await _seed_model(db_session)
    req = f"req-mm-{uuid4().hex[:8]}"

    await wallet_service.hold(
        db_session, wallet_id=wallet.id, amount=100, request_id=req
    )
    await wallet_service.capture(
        db_session,
        wallet_id=wallet.id,
        hold_amount=100,
        actual_amount=80,
        request_id=req,
    )
    await _write_request(db_session, user=user, model=model, request_id=req)
    log = await usage_service.write_usage_log(
        db_session,
        request_id=req,
        input_tokens=1,
        output_tokens=1,
        billing_rule_version=1,
        cost_amount=10,
        charge_amount=99,
    )
    assert log.gift_charge_amount == 0
    assert log.cash_charge_amount == 99
    assert log.gift_charge_amount + log.cash_charge_amount == log.charge_amount


# ---------------------------------------------------------------------
# 日汇总 + 报表毛利
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daily_aggregate_splits_gift_and_cash(
    db_session: AsyncSession,
) -> None:
    user = await _seed_user(db_session, "-agg")
    api_key = await _seed_api_key(db_session, user)
    model, channel = await _seed_model_channel(db_session)
    day = date(2099, 4, 1)
    at = datetime(2099, 4, 1, 12, 0, tzinfo=UTC)
    await _seed_captured_usage(
        db_session,
        user=user,
        api_key=api_key,
        model=model,
        channel=channel,
        request_id=f"agg-{uuid4().hex[:8]}",
        charge_amount=100,
        gift_charge=30,
        cost_amount=40,
        input_tokens=10,
        output_tokens=5,
        at=at,
    )

    result = await aggregate_usage_daily(db_session, summary_date=day)
    assert result.inserted == 1
    row = (
        await db_session.execute(
            select(UsageDailySummary).where(UsageDailySummary.summary_date == day)
        )
    ).scalar_one()
    assert row.revenue_amount == 100
    assert row.gift_revenue_amount == 30
    assert row.cash_revenue_amount == 70
    assert row.gift_revenue_amount + row.cash_revenue_amount == row.revenue_amount


@pytest.mark.asyncio
async def test_report_gross_profit_uses_cash_and_pure_gift_is_negative(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _seed_user(db_session, "-gp")
    api_key = await _seed_api_key(db_session, user)
    model, channel = await _seed_model_channel(db_session, upstream="gift-up")
    db_session.add(
        ChannelCost(
            channel_id=channel.id,
            upstream_model_name="gift-up",
            version=1,
            cost_input=1_000_000,
            cost_output=2_000_000,
            currency="CNY",
            effective_at=datetime(2026, 1, 1, tzinfo=UTC),
            status=STATUS_ENABLED,
        )
    )
    await db_session.flush()

    day = date(2099, 4, 2)
    at = datetime(2099, 4, 2, 8, 0, tzinfo=UTC)
    await _seed_captured_usage(
        db_session,
        user=user,
        api_key=api_key,
        model=model,
        channel=channel,
        request_id=f"gp-{uuid4().hex[:8]}",
        charge_amount=80,
        gift_charge=80,
        cost_amount=0,
        input_tokens=10,
        output_tokens=5,
        at=at,
    )
    await aggregate_usage_daily(db_session, summary_date=day)

    resp = await client.get(
        "/admin/reports/trend",
        headers=admin_auth_headers(),
        params={"from_date": "2099-04-02", "to_date": "2099-04-02"},
    )
    assert resp.status_code == 200, resp.text
    item = resp.json()["items"][0]
    assert item["revenue_amount"] == 80
    assert item["cash_revenue_amount"] == 0
    assert item["gift_consumed"] == 80
    assert item["gift_charge_amount"] == 80
    assert item["cash_charge_amount"] == 0
    # cost = 10*1 + 5*2 = 20
    assert item["cost_amount"] == 20
    assert item["gross_profit"] == -item["cost_amount"]
    assert item["gift_cost"] == item["cost_amount"]


@pytest.mark.asyncio
async def test_fund_source_filter_user_and_admin(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _seed_user(db_session, "-fs")
    model = await _seed_model(db_session)

    async def _row(req: str, gift: int, cash: int) -> None:
        await _write_request(db_session, user=user, model=model, request_id=req)
        session_log = UsageLog(
            request_id=req,
            input_tokens=1,
            output_tokens=1,
            cached_tokens=0,
            billing_rule_version=1,
            cost_amount=1,
            charge_amount=gift + cash,
            gift_charge_amount=gift,
            cash_charge_amount=cash,
            is_estimated=False,
        )
        db_session.add(session_log)
        await db_session.flush()

    req_gift = f"fs-g-{uuid4().hex[:8]}"
    req_cash = f"fs-c-{uuid4().hex[:8]}"
    req_mix = f"fs-m-{uuid4().hex[:8]}"
    await _row(req_gift, gift=10, cash=0)
    await _row(req_cash, gift=0, cash=20)
    await _row(req_mix, gift=5, cash=7)

    token = create_access_token(user_id=user.id)
    headers = {"Authorization": f"Bearer {token}"}

    gift_rows = (
        await client.get("/usage", headers=headers, params={"fund_source": "gift"})
    ).json()
    assert {r["request_id"] for r in gift_rows} == {req_gift}

    cash_rows = (
        await client.get("/usage", headers=headers, params={"fund_source": "cash"})
    ).json()
    assert {r["request_id"] for r in cash_rows} == {req_cash}

    mix_rows = (
        await client.get("/usage", headers=headers, params={"fund_source": "mixed"})
    ).json()
    assert {r["request_id"] for r in mix_rows} == {req_mix}

    admin = (
        await client.get(
            "/admin/usage-logs",
            headers=admin_auth_headers(),
            params={"user_id": str(user.id), "fund_source": "mixed"},
        )
    ).json()
    assert {r["request_id"] for r in admin} == {req_mix}


@pytest.mark.asyncio
async def test_reconcile_detects_gift_split_mismatch(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _seed_user(db_session, "-rc")
    api_key = await _seed_api_key(db_session, user)
    model, channel = await _seed_model_channel(db_session)
    day = date(2099, 4, 3)
    at = datetime(2099, 4, 3, 10, 0, tzinfo=UTC)

    await _seed_captured_usage(
        db_session,
        user=user,
        api_key=api_key,
        model=model,
        channel=channel,
        request_id=f"rc-ok-{uuid4().hex[:8]}",
        charge_amount=50,
        gift_charge=0,
        cost_amount=0,
        input_tokens=1,
        output_tokens=1,
        at=at,
    )
    await aggregate_usage_daily(db_session, summary_date=day)

    # 人为制造：usage_log 记了赠金，capture 流水 gift_amount 仍为 0
    bad_req = f"rc-bad-{uuid4().hex[:8]}"
    session_wallet = await wallet_service.get_or_create_personal_wallet(
        db_session, user.id
    )
    db_session.add(
        RequestLog(
            request_id=bad_req,
            api_key_id=api_key.id,
            user_id=user.id,
            billing_subject="personal",
            model_id=model.id,
            channel_id=channel.id,
            status="success",
            http_status=200,
            created_at=at,
        )
    )
    db_session.add(
        UsageLog(
            request_id=bad_req,
            input_tokens=1,
            output_tokens=0,
            cached_tokens=0,
            billing_rule_version=1,
            cost_amount=0,
            charge_amount=40,
            gift_charge_amount=40,
            cash_charge_amount=0,
            created_at=at,
        )
    )
    db_session.add(
        WalletTransaction(
            wallet_id=session_wallet.id,
            type=TX_CAPTURE,
            amount=-40,
            gift_amount=0,
            balance_after=session_wallet.balance,
            held_after=0,
            ref_request_id=bad_req,
            created_at=at,
        )
    )
    await db_session.flush()

    resp = await client.get(
        "/admin/reports/reconcile",
        headers=admin_auth_headers(),
        params={"from_date": "2099-04-03", "to_date": "2099-04-03"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["gift_matched"] is False
    assert "gift_split_mismatch" in body["alerts"]
    assert body["usage_gift_total"] == 40
    assert body["capture_gift_total"] == 0


@pytest.mark.asyncio
async def test_gift_dashboard_exposure_grant_and_consume(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    empty = await client.get(
        "/admin/reports/gifts",
        headers=admin_auth_headers(),
        params={"from_date": "2098-01-01", "to_date": "2098-01-01"},
    )
    assert empty.status_code == 200, empty.text
    assert empty.json()["granted_amount"] == 0
    assert empty.json()["gift_consumed"] == 0

    user = await _seed_user(db_session, "-dash")
    wallet = await _seed_wallet(db_session, user)
    await _grant(db_session, wallet, user, 1_500)
    await db_session.refresh(wallet)

    today = datetime.now(UTC).date().isoformat()
    granted = await client.get(
        "/admin/reports/gifts",
        headers=admin_auth_headers(),
        params={"from_date": today, "to_date": today},
    )
    assert granted.status_code == 200, granted.text
    gbody = granted.json()
    assert gbody["granted_amount"] >= 1_500
    assert gbody["gift_balance_total"] >= wallet.gift_balance
    assert gbody["revoked_amount"] >= 0
    assert gbody["expired_amount"] >= 0

    api_key = await _seed_api_key(db_session, user)
    model, channel = await _seed_model_channel(db_session, upstream="dash-up")
    day = date(2099, 8, 17)
    at = datetime(2099, 8, 17, 8, 0, tzinfo=UTC)
    await _seed_captured_usage(
        db_session,
        user=user,
        api_key=api_key,
        model=model,
        channel=channel,
        request_id=f"dash-{uuid4().hex[:8]}",
        charge_amount=80,
        gift_charge=80,
        cost_amount=20,
        input_tokens=1,
        output_tokens=1,
        at=at,
    )
    await aggregate_usage_daily(db_session, summary_date=day)

    consumed = await client.get(
        "/admin/reports/gifts",
        headers=admin_auth_headers(),
        params={"from_date": "2099-08-17", "to_date": "2099-08-17"},
    )
    assert consumed.status_code == 200, consumed.text
    cbody = consumed.json()
    assert cbody["gift_consumed"] == 80
    assert cbody["granted_all_time"] >= 1_500
    assert cbody["consumed_all_time"] >= 80
    assert cbody["redemption_rate_bps"] >= 0
