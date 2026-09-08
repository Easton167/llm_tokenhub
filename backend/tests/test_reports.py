"""任务 10：报表 API 查询与权限隔离。

验收：
  - 报表收入合计与钱包 capture 流水对账一致
  - 组织成员只能看到本组织数据；Member 看不到其他成员明细
  - 时间范围按 UTC 日历日过滤正确
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.channel_key_crypto import encrypt_channel_key
from app.core.constants import (
    ORG_ROLE_ADMIN,
    ORG_ROLE_MEMBER,
    STATUS_ENABLED,
    VISIBILITY_PUBLIC,
)
from app.db.models.api_key import ApiKey
from app.db.models.channel import Channel, ChannelKey, Provider
from app.db.models.channel_cost import ChannelCost
from app.db.models.llm_model import LLMModel, ModelChannelMap
from app.db.models.log import RequestLog, UsageLog
from app.db.models.user import User
from app.db.models.wallet import WalletTransaction
from app.db.session import get_db
from app.main import app
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


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _register_login(client: AsyncClient, *, prefix: str) -> tuple[str, str]:
    email = f"{prefix}-{uuid4().hex[:8]}@example.com"
    password = "password123"
    reg = await client.post(
        "/auth/register",
        json={"email": email, "password": password, "nickname": prefix},
    )
    assert reg.status_code == 201, reg.text
    login = await client.post(
        "/auth/login", json={"email": email, "password": password}
    )
    assert login.status_code == 200, login.text
    return reg.json()["id"], login.json()["access_token"]


async def _seed_user(session: AsyncSession, suffix: str = "") -> User:
    user = User(
        email=f"rpt-{suffix}-{uuid4().hex[:8]}@example.com",
        status=STATUS_ENABLED,
    )
    session.add(user)
    await session.flush()
    await wallet_service.get_or_create_personal_wallet(session, user.id)
    return user


async def _seed_api_key(session: AsyncSession, user: User) -> ApiKey:
    key = ApiKey(
        user_id=user.id,
        name="rpt-key",
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
        display_name="report-model",
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
            billing_subject="org" if org_id else "personal",
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


@pytest.mark.asyncio
async def test_admin_reports_trend_top_share_and_reconcile(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user_a = await _seed_user(db_session, "a")
    user_b = await _seed_user(db_session, "b")
    key_a = await _seed_api_key(db_session, user_a)
    key_b = await _seed_api_key(db_session, user_b)
    model, channel = await _seed_model_channel(db_session)

    db_session.add(
        ChannelCost(
            channel_id=channel.id,
            upstream_model_name="gpt-4o-upstream",
            version=1,
            cost_input=1_000_000,  # 1 per input token
            cost_output=2_000_000,
            currency="CNY",
            effective_at=datetime(2026, 1, 1, tzinfo=UTC),
            status=STATUS_ENABLED,
        )
    )
    await db_session.flush()

    day1 = date(2099, 1, 10)
    day2 = date(2099, 1, 11)
    await _seed_captured_usage(
        db_session,
        user=user_a,
        api_key=key_a,
        model=model,
        channel=channel,
        request_id=f"req-a-{uuid4().hex[:8]}",
        input_tokens=10,
        output_tokens=5,
        charge_amount=100,
        at=datetime(2099, 1, 10, 12, 0, tzinfo=UTC),
    )
    await _seed_captured_usage(
        db_session,
        user=user_b,
        api_key=key_b,
        model=model,
        channel=channel,
        request_id=f"req-b-{uuid4().hex[:8]}",
        input_tokens=20,
        output_tokens=10,
        charge_amount=250,
        at=datetime(2099, 1, 11, 8, 0, tzinfo=UTC),
    )
    # 范围外，不应计入
    await _seed_captured_usage(
        db_session,
        user=user_a,
        api_key=key_a,
        model=model,
        channel=channel,
        request_id=f"req-out-{uuid4().hex[:8]}",
        input_tokens=1,
        output_tokens=1,
        charge_amount=999,
        at=datetime(2099, 1, 12, 1, 0, tzinfo=UTC),
    )

    await aggregate_usage_daily(db_session, summary_date=day1)
    await aggregate_usage_daily(db_session, summary_date=day2)
    await aggregate_usage_daily(db_session, summary_date=date(2099, 1, 12))

    headers = admin_auth_headers()
    trend = await client.get(
        "/admin/reports/trend",
        headers=headers,
        params={"from_date": "2099-01-10", "to_date": "2099-01-11"},
    )
    assert trend.status_code == 200, trend.text
    body = trend.json()
    assert body["timezone"] == "UTC"
    assert len(body["items"]) == 2
    assert body["items"][0]["summary_date"] == "2099-01-10"
    assert body["items"][0]["revenue_amount"] == 100
    assert body["items"][0]["gross_profit"] == (
        body["items"][0]["revenue_amount"] - body["items"][0]["cost_amount"]
    )
    assert sum(i["revenue_amount"] for i in body["items"]) == 350

    top_users = await client.get(
        "/admin/reports/top",
        headers=headers,
        params={
            "dimension": "user",
            "from_date": "2099-01-10",
            "to_date": "2099-01-11",
            "limit": 5,
        },
    )
    assert top_users.status_code == 200, top_users.text
    top_items = top_users.json()["items"]
    assert len(top_items) == 2
    assert top_items[0]["dim_id"] == str(user_b.id)
    assert top_items[0]["revenue_amount"] == 250

    top_models = await client.get(
        "/admin/reports/top",
        headers=headers,
        params={
            "dimension": "model",
            "from_date": "2099-01-10",
            "to_date": "2099-01-11",
        },
    )
    assert top_models.status_code == 200
    assert top_models.json()["items"][0]["dim_id"] == str(model.id)

    share = await client.get(
        "/admin/reports/channel-share",
        headers=headers,
        params={"from_date": "2099-01-10", "to_date": "2099-01-11"},
    )
    assert share.status_code == 200, share.text
    share_body = share.json()
    assert share_body["items"][0]["channel_id"] == str(channel.id)
    assert share_body["total_cost"] == share_body["items"][0]["cost_amount"]
    assert share_body["items"][0]["cost_share_bps"] == 10000

    reconcile = await client.get(
        "/admin/reports/reconcile",
        headers=headers,
        params={"from_date": "2099-01-10", "to_date": "2099-01-11"},
    )
    assert reconcile.status_code == 200, reconcile.text
    rec = reconcile.json()
    assert rec["summary_revenue"] == 350
    assert rec["capture_revenue"] == 350
    assert rec["matched"] is True

    # 单日过滤不含 day2
    day_filter = await client.get(
        "/admin/reports/trend",
        headers=headers,
        params={"from_date": "2099-01-10", "to_date": "2099-01-10"},
    )
    assert day_filter.status_code == 200
    assert len(day_filter.json()["items"]) == 1
    assert day_filter.json()["items"][0]["revenue_amount"] == 100


@pytest.mark.asyncio
async def test_user_daily_usage_isolated(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user_id, token = await _register_login(client, prefix="me")
    other_id, _ = await _register_login(client, prefix="other")

    model, channel = await _seed_model_channel(db_session)
    me = await db_session.get(User, user_id)
    other = await db_session.get(User, other_id)
    assert me is not None and other is not None
    key_me = await _seed_api_key(db_session, me)
    key_other = await _seed_api_key(db_session, other)

    await _seed_captured_usage(
        db_session,
        user=me,
        api_key=key_me,
        model=model,
        channel=channel,
        request_id=f"me-{uuid4().hex[:8]}",
        input_tokens=3,
        output_tokens=1,
        charge_amount=40,
        at=datetime(2099, 2, 18, 10, 0, tzinfo=UTC),
    )
    await _seed_captured_usage(
        db_session,
        user=other,
        api_key=key_other,
        model=model,
        channel=channel,
        request_id=f"ot-{uuid4().hex[:8]}",
        input_tokens=9,
        output_tokens=9,
        charge_amount=900,
        at=datetime(2099, 2, 18, 11, 0, tzinfo=UTC),
    )
    await aggregate_usage_daily(db_session, summary_date=date(2099, 2, 18))

    resp = await client.get(
        "/usage/daily",
        headers=_auth(token),
        params={"from_date": "2099-02-18", "to_date": "2099-02-18"},
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["revenue_amount"] == 40


@pytest.mark.asyncio
async def test_org_reports_permission_and_isolation(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    owner_id, owner_token = await _register_login(client, prefix="own")
    admin_id, admin_token = await _register_login(client, prefix="adm")
    member_id, member_token = await _register_login(client, prefix="mem")
    outsider_id, outsider_token = await _register_login(client, prefix="out")

    org = (
        await client.post(
            "/orgs",
            headers=_auth(owner_token),
            json={"name": "Report Org"},
        )
    ).json()
    org_id = org["id"]

    await client.post(
        f"/orgs/{org_id}/members",
        headers=_auth(owner_token),
        json={"user_id": admin_id, "org_role": ORG_ROLE_ADMIN},
    )
    await client.post(
        f"/orgs/{org_id}/members",
        headers=_auth(owner_token),
        json={"user_id": member_id, "org_role": ORG_ROLE_MEMBER},
    )

    # 另一组织，不应泄漏
    other_org = (
        await client.post(
            "/orgs",
            headers=_auth(outsider_token),
            json={"name": "Other Org"},
        )
    ).json()

    model, channel = await _seed_model_channel(db_session)
    owner = await db_session.get(User, owner_id)
    member = await db_session.get(User, member_id)
    outsider = await db_session.get(User, outsider_id)
    assert owner and member and outsider
    key_owner = await _seed_api_key(db_session, owner)
    key_member = await _seed_api_key(db_session, member)
    key_outsider = await _seed_api_key(db_session, outsider)

    org_uuid = UUID(org_id)
    other_uuid = UUID(other_org["id"])

    await _seed_captured_usage(
        db_session,
        user=owner,
        api_key=key_owner,
        model=model,
        channel=channel,
        request_id=f"org-o-{uuid4().hex[:8]}",
        input_tokens=5,
        output_tokens=5,
        charge_amount=70,
        at=datetime(2099, 3, 19, 9, 0, tzinfo=UTC),
        org_id=org_uuid,
    )
    await _seed_captured_usage(
        db_session,
        user=member,
        api_key=key_member,
        model=model,
        channel=channel,
        request_id=f"org-m-{uuid4().hex[:8]}",
        input_tokens=2,
        output_tokens=2,
        charge_amount=30,
        at=datetime(2099, 3, 19, 10, 0, tzinfo=UTC),
        org_id=org_uuid,
    )
    await _seed_captured_usage(
        db_session,
        user=outsider,
        api_key=key_outsider,
        model=model,
        channel=channel,
        request_id=f"org-x-{uuid4().hex[:8]}",
        input_tokens=50,
        output_tokens=50,
        charge_amount=5000,
        at=datetime(2099, 3, 19, 11, 0, tzinfo=UTC),
        org_id=other_uuid,
    )
    await aggregate_usage_daily(db_session, summary_date=date(2099, 3, 19))

    # 成员可见组织日汇总，且不含其他组织
    daily = await client.get(
        f"/orgs/{org_id}/reports/daily",
        headers=_auth(member_token),
        params={"from_date": "2099-03-19", "to_date": "2099-03-19"},
    )
    assert daily.status_code == 200, daily.text
    assert daily.json()["org_id"] == org_id
    assert daily.json()["items"][0]["revenue_amount"] == 100

    # 外人不可见
    forbidden = await client.get(
        f"/orgs/{org_id}/reports/daily",
        headers=_auth(outsider_token),
        params={"from_date": "2099-03-19", "to_date": "2099-03-19"},
    )
    assert forbidden.status_code == 403

    # Member 不可看成员分摊
    mem_share = await client.get(
        f"/orgs/{org_id}/reports/members",
        headers=_auth(member_token),
        params={"from_date": "2099-03-19", "to_date": "2099-03-19"},
    )
    assert mem_share.status_code == 403

    # Admin 可见成员分摊
    share = await client.get(
        f"/orgs/{org_id}/reports/members",
        headers=_auth(admin_token),
        params={"from_date": "2099-03-19", "to_date": "2099-03-19"},
    )
    assert share.status_code == 200, share.text
    items = share.json()["items"]
    assert {i["user_id"] for i in items} == {owner_id, member_id}
    by_user = {i["user_id"]: i["revenue_amount"] for i in items}
    assert by_user[owner_id] == 70
    assert by_user[member_id] == 30

    # Owner 同样可见；且不含其他组织成员
    owner_share = await client.get(
        f"/orgs/{org_id}/reports/members",
        headers=_auth(owner_token),
        params={"from_date": "2099-03-19", "to_date": "2099-03-19"},
    )
    assert owner_share.status_code == 200
    assert outsider_id not in {i["user_id"] for i in owner_share.json()["items"]}

    # 管理端 Top org 仅含本组织收入
    top_org = await client.get(
        "/admin/reports/top",
        headers=admin_auth_headers(),
        params={
            "dimension": "org",
            "from_date": "2099-03-19",
            "to_date": "2099-03-19",
        },
    )
    assert top_org.status_code == 200
    org_items = {i["dim_id"]: i["revenue_amount"] for i in top_org.json()["items"]}
    assert org_items[org_id] == 100
    assert org_items[other_org["id"]] == 5000


@pytest.mark.asyncio
async def test_invalid_date_range(client: AsyncClient) -> None:
    headers = admin_auth_headers()
    resp = await client.get(
        "/admin/reports/trend",
        headers=headers,
        params={"from_date": "2026-07-22", "to_date": "2026-07-20"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_user_daily_includes_today_without_prior_aggregate(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """当日明细已写入时，/usage/daily 即使未跑过 cron 也应能查出。"""
    user_id, token = await _register_login(client, prefix="live")
    model, channel = await _seed_model_channel(db_session)
    me = await db_session.get(User, user_id)
    assert me is not None
    key = await _seed_api_key(db_session, me)
    now = datetime.now(UTC)
    today = now.date().isoformat()

    await _seed_captured_usage(
        db_session,
        user=me,
        api_key=key,
        model=model,
        channel=channel,
        request_id=f"live-{uuid4().hex[:8]}",
        input_tokens=4,
        output_tokens=2,
        charge_amount=55,
        at=now,
    )

    resp = await client.get(
        "/usage/daily",
        headers=_auth(token),
        params={"from_date": today, "to_date": today},
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["revenue_amount"] == 55
    assert items[0]["request_count"] == 1

    await _seed_captured_usage(
        db_session,
        user=me,
        api_key=key,
        model=model,
        channel=channel,
        request_id=f"live2-{uuid4().hex[:8]}",
        input_tokens=1,
        output_tokens=1,
        charge_amount=10,
        at=now,
    )
    again = await client.get(
        "/usage/daily",
        headers=_auth(token),
        params={"from_date": today, "to_date": today},
    )
    assert again.status_code == 200, again.text
    assert again.json()["items"][0]["revenue_amount"] == 65
    assert again.json()["items"][0]["request_count"] == 2
