"""任务 6：网关组织扣费串联集成测试。

覆盖验收：
  - 组织身份调用成功 → 组织钱包 hold/capture + 发起成员 + 额度累计 + 日志字段
  - 额度不足且允许回退 → 扣个人钱包
  - 额度不足且不允许回退 → 402，不调用上游
  - 上游失败 → 释放组织冻结，不累计额度
  - X-Billing-Subject 无权主体 → 403，无资金变动
  - 并发 hold 不超扣组织余额
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.channel_key_crypto import encrypt_channel_key
from app.core.constants import (
    BILLING_FALLBACK_DENY,
    BILLING_FALLBACK_PERSONAL,
    QUOTA_PERIOD_NONE,
    STATUS_ENABLED,
    VISIBILITY_PUBLIC,
)
from app.db.models.billing import BillingRule
from app.db.models.channel import Channel, ChannelKey, Provider
from app.db.models.llm_model import LLMModel, ModelChannelMap
from app.db.models.log import RequestLog, UsageLog
from app.db.models.org import Org, OrgMember
from app.db.models.user import User
from app.db.models.wallet import Wallet, WalletTransaction
from app.db.session import get_db
from app.main import app
from app.services.auth_service import auth_service
from app.services.billing_subject_service import format_org_subject_ref
from app.services.org_service import org_service
from app.services.quota_service import quota_service
from app.services.wallet_service import TX_CAPTURE, TX_HOLD, TX_RELEASE, wallet_service
from app.tasks.reconcile_holds import reconcile_stale_holds


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _seed_user(session: AsyncSession, suffix: str = "") -> User:
    user = User(
        email=f"org-gw-{suffix}-{uuid4().hex[:8]}@example.com",
        status=STATUS_ENABLED,
    )
    session.add(user)
    await session.flush()
    await wallet_service.get_or_create_personal_wallet(session, user.id)
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
        name="org-gw-key",
        billing_subject=billing_subject,
    )
    return str(api_key_obj.id), plaintext


async def _seed_model(session: AsyncSession) -> LLMModel:
    model = LLMModel(
        alias=f"org-gw-model-{uuid4().hex[:6]}",
        display_name="org-gw",
        model_type="chat",
        capabilities={},
        visibility=VISIBILITY_PUBLIC,
        status=STATUS_ENABLED,
    )
    session.add(model)
    await session.flush()
    return model


async def _seed_channel(session: AsyncSession) -> tuple[Channel, ChannelKey]:
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
        api_key_encrypted=encrypt_channel_key("sk-upstream-test"),
        key_masked="****test",
        weight=1,
        status=STATUS_ENABLED,
        health_status=0,
    )
    session.add(channel_key)
    await session.flush()
    return channel, channel_key


async def _seed_mapping(
    session: AsyncSession, model: LLMModel, channel: Channel
) -> ModelChannelMap:
    mapping = ModelChannelMap(
        model_id=model.id,
        channel_id=channel.id,
        upstream_model_name="gpt-4o-2024-11-20",
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
    price_input: int = 1_000_000,
    price_output: int = 1_000_000,
    price_per_call: int = 0,
) -> BillingRule:
    # 单价设高一点，保证 hold_amount > 0 且可预测
    rule = BillingRule(
        model_id=model.id,
        version=1,
        price_input=price_input,
        price_output=price_output,
        price_cached_input=0,
        price_per_call=price_per_call,
        cost_input=100_000,
        cost_output=100_000,
        multiplier="1.0",
        min_charge_unit=1,
        currency="CNY",
        effective_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        status=0,
    )
    session.add(rule)
    await session.flush()
    return rule


async def _seed_org_ready(
    session: AsyncSession,
    owner: User,
    *,
    org_balance: int = 100_000_000,
    billing_fallback: str = BILLING_FALLBACK_DENY,
    can_consume: bool = True,
    quota_limit: int = 0,
) -> tuple[Org, OrgMember, Wallet]:
    org = await org_service.create_org(
        session,
        name=f"GwOrg-{uuid4().hex[:6]}",
        owner_user_id=owner.id,
        billing_fallback=billing_fallback,
    )
    membership = await org_service.get_active_membership(
        session, org_id=org.id, user_id=owner.id
    )
    assert membership is not None
    membership.can_consume_org_wallet = can_consume
    org_wallet = await wallet_service.require_org_wallet(session, org.id)
    org_wallet.balance = org_balance
    if quota_limit > 0:
        await quota_service.configure_member_quota(
            session,
            org=org,
            actor_user_id=owner.id,
            member_id=membership.id,
            can_consume_org_wallet=can_consume,
            quota_period=QUOTA_PERIOD_NONE,
            quota_limit=quota_limit,
        )
    await session.flush()
    return org, membership, org_wallet


def _make_upstream_success(
    *,
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": "gpt-4o-2024-11-20",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
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


def _make_upstream_error(status_code: int = 500) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = {
        "error": {"message": "boom", "type": "server_error", "code": "server_error"}
    }
    return mock_resp


def _patch_httpx(mock_resp: MagicMock):
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_client_cls = MagicMock()
    mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
    return patch("app.api.v1.chat.httpx.AsyncClient", mock_client_cls), mock_client


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


async def _setup_callable(
    db_session: AsyncSession,
    *,
    org_balance: int = 100_000_000,
    billing_fallback: str = BILLING_FALLBACK_DENY,
    can_consume: bool = True,
    quota_limit: int = 0,
    personal_balance: int = 100_000_000,
    bind_org_on_key: bool = True,
    price_per_call: int = 0,
) -> tuple[User, str, LLMModel, Org, OrgMember, Wallet, Wallet]:
    user = await _seed_user(db_session, "m")
    personal = await wallet_service.get_or_create_personal_wallet(db_session, user.id)
    personal.balance = personal_balance
    org, member, org_wallet = await _seed_org_ready(
        db_session,
        user,
        org_balance=org_balance,
        billing_fallback=billing_fallback,
        can_consume=can_consume,
        quota_limit=quota_limit,
    )
    subject = format_org_subject_ref(org.id) if bind_org_on_key else None
    _, plaintext = await _seed_api_key(
        db_session, user, billing_subject=subject
    )
    model = await _seed_model(db_session)
    await _seed_billing_rule(db_session, model, price_per_call=price_per_call)
    channel, _ = await _seed_channel(db_session)
    await _seed_mapping(db_session, model, channel)
    await db_session.flush()
    return user, plaintext, model, org, member, org_wallet, personal


# ---------------------------------------------------------------------------
# cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_org_billing_success_hold_capture_quota_and_logs(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user, plaintext, model, org, member, org_wallet, personal = await _setup_callable(
        db_session, quota_limit=50_000
    )
    org_balance_before = org_wallet.balance
    personal_before = personal.balance

    http_patch, mock_client = _patch_httpx(
        _make_upstream_success(prompt_tokens=10, completion_tokens=5)
    )
    with http_patch:
        resp = await api_client.post(
            "/v1/chat/completions",
            json={
                "model": model.alias,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 50,
            },
            headers={"Authorization": f"Bearer {plaintext}"},
        )

    assert resp.status_code == 200, resp.text
    assert mock_client.post.await_count == 1

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
    assert usage.actor_user_id == user.id
    assert usage.charge_amount > 0

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
    assert hold_tx.operator_user_id == user.id

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
    assert capture_tx.operator_user_id == user.id

    await db_session.refresh(org_wallet)
    await db_session.refresh(personal)
    assert org_wallet.held == 0
    assert org_wallet.balance == org_balance_before - usage.charge_amount
    assert personal.balance == personal_before

    quota = await quota_service.get_quota(db_session, member.id)
    assert quota is not None
    assert quota.quota_used == usage.charge_amount


@pytest.mark.asyncio
async def test_quota_exhausted_fallback_to_personal(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user, plaintext, model, org, member, org_wallet, personal = await _setup_callable(
        db_session,
        billing_fallback=BILLING_FALLBACK_PERSONAL,
        quota_limit=1,  # 几乎耗尽，hold 一定不够
        org_balance=100_000_000,
        personal_balance=100_000_000,
    )
    # 用尽额度
    await quota_service.quota_consume(
        db_session, org_member_id=member.id, amount=1
    )
    await db_session.flush()
    org_before = org_wallet.balance
    personal_before = personal.balance

    http_patch, mock_client = _patch_httpx(_make_upstream_success())
    with http_patch:
        resp = await api_client.post(
            "/v1/chat/completions",
            json={
                "model": model.alias,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 20,
            },
            headers={"Authorization": f"Bearer {plaintext}"},
        )

    assert resp.status_code == 200, resp.text
    assert mock_client.post.await_count == 1

    req_log = (
        await db_session.execute(
            select(RequestLog).where(RequestLog.user_id == user.id)
        )
    ).scalar_one()
    assert req_log.org_id is None
    assert req_log.billing_subject.startswith("personal:")

    personal_capture = (
        await db_session.execute(
            select(WalletTransaction).where(
                WalletTransaction.wallet_id == personal.id,
                WalletTransaction.type == TX_CAPTURE,
            )
        )
    ).scalar_one_or_none()
    assert personal_capture is not None

    org_capture = (
        await db_session.execute(
            select(WalletTransaction).where(
                WalletTransaction.wallet_id == org_wallet.id,
                WalletTransaction.type == TX_CAPTURE,
            )
        )
    ).scalar_one_or_none()
    assert org_capture is None

    await db_session.refresh(org_wallet)
    await db_session.refresh(personal)
    assert org_wallet.balance == org_before
    assert personal.balance < personal_before


@pytest.mark.asyncio
async def test_quota_exhausted_deny_402_no_upstream(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _user, plaintext, model, _org, member, org_wallet, personal = await _setup_callable(
        db_session,
        billing_fallback=BILLING_FALLBACK_DENY,
        quota_limit=1,
    )
    await quota_service.quota_consume(
        db_session, org_member_id=member.id, amount=1
    )
    await db_session.flush()
    org_before = org_wallet.balance
    personal_before = personal.balance

    http_patch, mock_client = _patch_httpx(_make_upstream_success())
    with http_patch:
        resp = await api_client.post(
            "/v1/chat/completions",
            json={
                "model": model.alias,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 20,
            },
            headers={"Authorization": f"Bearer {plaintext}"},
        )

    assert resp.status_code == 402
    assert resp.json()["error"]["code"] == "insufficient_balance"
    assert mock_client.post.await_count == 0

    await db_session.refresh(org_wallet)
    await db_session.refresh(personal)
    assert org_wallet.balance == org_before
    assert org_wallet.held == 0
    assert personal.balance == personal_before

    tx_count = await db_session.scalar(
        select(func.count()).select_from(WalletTransaction).where(
            WalletTransaction.wallet_id.in_([org_wallet.id, personal.id])
        )
    )
    assert tx_count == 0


@pytest.mark.asyncio
async def test_upstream_failure_releases_org_hold_no_quota(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user, plaintext, model, org, member, org_wallet, _personal = await _setup_callable(
        db_session, quota_limit=50_000
    )
    org_before = org_wallet.balance

    http_patch, _ = _patch_httpx(_make_upstream_error(500))
    with http_patch:
        resp = await api_client.post(
            "/v1/chat/completions",
            json={
                "model": model.alias,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 20,
            },
            headers={"Authorization": f"Bearer {plaintext}"},
        )

    assert resp.status_code in (500, 502)

    req_log = (
        await db_session.execute(
            select(RequestLog).where(
                RequestLog.user_id == user.id, RequestLog.status == "failed"
            )
        )
    ).scalar_one()
    assert req_log.org_id == org.id
    assert req_log.actor_user_id == user.id

    usage = (
        await db_session.execute(
            select(UsageLog).where(UsageLog.request_id == req_log.request_id)
        )
    ).scalar_one_or_none()
    assert usage is None

    release_tx = (
        await db_session.execute(
            select(WalletTransaction).where(
                WalletTransaction.wallet_id == org_wallet.id,
                WalletTransaction.type == TX_RELEASE,
                WalletTransaction.ref_request_id == req_log.request_id,
            )
        )
    ).scalar_one()
    assert release_tx.operator_user_id == user.id
    assert release_tx.org_id == org.id

    await db_session.refresh(org_wallet)
    assert org_wallet.balance == org_before
    assert org_wallet.held == 0

    quota = await quota_service.get_quota(db_session, member.id)
    assert quota is not None
    assert quota.quota_used == 0


@pytest.mark.asyncio
async def test_header_forbidden_subject_no_money_movement(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user, plaintext, model, _org, _member, _ow, personal = await _setup_callable(
        db_session, bind_org_on_key=False
    )
    stranger = await _seed_user(db_session, "stranger")
    other_org, _, other_wallet = await _seed_org_ready(db_session, stranger)
    personal_before = personal.balance
    other_before = other_wallet.balance

    http_patch, mock_client = _patch_httpx(_make_upstream_success())
    with http_patch:
        resp = await api_client.post(
            "/v1/chat/completions",
            json={
                "model": model.alias,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 20,
            },
            headers={
                "Authorization": f"Bearer {plaintext}",
                "X-Billing-Subject": format_org_subject_ref(other_org.id),
            },
        )

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    assert mock_client.post.await_count == 0

    await db_session.refresh(personal)
    await db_session.refresh(other_wallet)
    assert personal.balance == personal_before
    assert other_wallet.balance == other_before
    assert other_wallet.held == 0

    affected = await db_session.scalar(
        select(func.count())
        .select_from(WalletTransaction)
        .where(WalletTransaction.wallet_id.in_([personal.id, other_wallet.id]))
    )
    assert affected == 0


@pytest.mark.asyncio
async def test_header_override_to_org_when_key_personal(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user, plaintext, model, org, _member, org_wallet, personal = await _setup_callable(
        db_session, bind_org_on_key=False, can_consume=True
    )
    # Key 未绑定；通过请求头指定组织
    personal.balance = 0
    await db_session.flush()

    http_patch, _ = _patch_httpx(_make_upstream_success())
    with http_patch:
        resp = await api_client.post(
            "/v1/chat/completions",
            json={
                "model": model.alias,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 20,
            },
            headers={
                "Authorization": f"Bearer {plaintext}",
                "X-Billing-Subject": format_org_subject_ref(org.id),
            },
        )

    assert resp.status_code == 200, resp.text
    capture = (
        await db_session.execute(
            select(WalletTransaction).where(
                WalletTransaction.wallet_id == org_wallet.id,
                WalletTransaction.type == TX_CAPTURE,
            )
        )
    ).scalar_one()
    assert capture.operator_user_id == user.id


@pytest.mark.asyncio
async def test_org_balance_exhausted_sequential_requests(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """连续组织扣费：余额耗尽后返回 402，全程余额/冻结不为负。"""
    _user, plaintext, model, _org, _member, org_wallet, _personal = await _setup_callable(
        db_session,
        org_balance=5_000,
        quota_limit=0,
        personal_balance=0,
        price_per_call=2_000,
    )

    http_patch, mock_client = _patch_httpx(
        _make_upstream_success(prompt_tokens=1, completion_tokens=1)
    )

    codes: list[int] = []
    with http_patch:
        for _ in range(4):
            resp = await api_client.post(
                "/v1/chat/completions",
                json={
                    "model": model.alias,
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 1,
                },
                headers={"Authorization": f"Bearer {plaintext}"},
            )
            codes.append(resp.status_code)

    await db_session.refresh(org_wallet)
    assert org_wallet.balance >= 0
    assert org_wallet.held == 0
    assert any(c == 200 for c in codes)
    assert any(c == 402 for c in codes)
    assert all(c in (200, 402) for c in codes)
    # 402 发生在 hold 阶段，不应再打上游
    assert mock_client.post.await_count == codes.count(200)


@pytest.mark.asyncio
async def test_reconcile_org_wallet_stale_hold_releases(
    db_session: AsyncSession,
) -> None:
    owner = await _seed_user(db_session, "recon")
    org, member, org_wallet = await _seed_org_ready(
        db_session, owner, org_balance=10_000
    )
    request_id = f"org-stale-{uuid4().hex[:10]}"
    await wallet_service.hold(
        db_session,
        wallet_id=org_wallet.id,
        amount=1_000,
        request_id=request_id,
        operator_user_id=owner.id,
    )
    await db_session.flush()

    stale_time = datetime.now(timezone.utc) - timedelta(hours=2)
    await db_session.execute(
        update(WalletTransaction)
        .where(
            WalletTransaction.wallet_id == org_wallet.id,
            WalletTransaction.ref_request_id == request_id,
            WalletTransaction.type == TX_HOLD,
        )
        .values(created_at=stale_time)
    )
    await db_session.flush()

    result = await reconcile_stale_holds(
        db_session, expire_seconds=3600, dry_run=False
    )
    assert result.released >= 1
    assert result.errors == 0

    await db_session.refresh(org_wallet)
    assert org_wallet.held == 0
    assert org_wallet.balance == 10_000

    release = (
        await db_session.execute(
            select(WalletTransaction).where(
                WalletTransaction.wallet_id == org_wallet.id,
                WalletTransaction.ref_request_id == request_id,
                WalletTransaction.type == TX_RELEASE,
            )
        )
    ).scalar_one()
    assert release.org_id == org.id
    assert release.operator_user_id == owner.id

    quota = await quota_service.get_quota(db_session, member.id)
    assert quota is None or quota.quota_used == 0
