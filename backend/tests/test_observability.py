"""任务 9：日志、对账与可观测性相关测试。"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import STATUS_ENABLED, VISIBILITY_ROLE_RESTRICTED
from app.core.security import create_access_token, hash_password
from app.db.models.api_key import ApiKey
from app.db.models.llm_model import LLMModel
from app.db.models.user import User
from app.db.session import get_db
from app.main import app
from app.services import usage_service
from app.services.auth_service import auth_service
from app.services.wallet_service import TX_CAPTURE, TX_HOLD, TX_RELEASE, wallet_service
from app.tasks.reconcile_holds import reconcile_stale_holds
from tests.conftest import admin_auth_headers


@pytest_asyncio.fixture
async def obs_client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
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
        email=f"obs-{uuid4().hex[:8]}@example.com",
        password_hash=hash_password("password123"),
        status=STATUS_ENABLED,
    )
    session.add(user)
    await session.flush()
    return user


async def _seed_api_key(session: AsyncSession, user: User) -> tuple[ApiKey, str]:
    api_key, raw = await auth_service.create_api_key(
        session, user_id=user.id, name="obs-key"
    )
    return api_key, raw


async def _seed_model(session: AsyncSession, alias: str | None = None) -> LLMModel:
    model = LLMModel(
        alias=alias or f"obs-model-{uuid4().hex[:6]}",
        display_name="Obs Model",
        status=STATUS_ENABLED,
        visibility=VISIBILITY_ROLE_RESTRICTED,
    )
    session.add(model)
    await session.flush()
    return model


@pytest.mark.asyncio
async def test_health_returns_request_id_header(obs_client: AsyncClient) -> None:
    resp = await obs_client.get("/health", headers={"X-Request-ID": "client-req-001"})
    assert resp.status_code == 200
    assert resp.headers.get("X-Request-ID") == "client-req-001"


@pytest.mark.asyncio
async def test_health_generates_request_id_when_missing(obs_client: AsyncClient) -> None:
    resp = await obs_client.get("/health")
    assert resp.status_code == 200
    assert resp.headers.get("X-Request-ID")


@pytest.mark.asyncio
async def test_metrics_endpoint(obs_client: AsyncClient) -> None:
    await obs_client.get("/health")
    resp = await obs_client.get("/metrics")
    assert resp.status_code == 200
    body = resp.text
    assert "tokenhub_http_requests_total" in body
    assert "tokenhub_http_request_duration_seconds" in body


@pytest.mark.asyncio
async def test_admin_usage_query_apis(
    obs_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _seed_user(db_session)
    api_key, _ = await _seed_api_key(db_session, user)
    model = await _seed_model(db_session)
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
        latency_ms=42,
    )
    await usage_service.write_usage_log(
        db_session,
        request_id=request_id,
        input_tokens=10,
        output_tokens=20,
        cached_tokens=0,
        billing_rule_version=1,
        cost_amount=100,
        charge_amount=200,
    )
    await db_session.flush()

    req_resp = await obs_client.get(
        "/admin/request-logs",
        params={"user_id": str(user.id), "model_id": str(model.id), "status": "success"},
        headers=admin_auth_headers(),
    )
    assert req_resp.status_code == 200
    req_items = req_resp.json()
    assert len(req_items) >= 1
    assert req_items[0]["request_id"] == request_id

    usage_resp = await obs_client.get(
        "/admin/usage-logs",
        params={"user_id": str(user.id), "model_id": str(model.id)},
        headers=admin_auth_headers(),
    )
    assert usage_resp.status_code == 200
    usage_items = usage_resp.json()
    assert any(item["request_id"] == request_id for item in usage_items)

    summary_resp = await obs_client.get(
        "/admin/usage-summary",
        params={"group_by": "model"},
        headers=admin_auth_headers(),
    )
    assert summary_resp.status_code == 200
    summary = summary_resp.json()
    assert any(str(row.get("model_id")) == str(model.id) for row in summary)


@pytest.mark.asyncio
async def test_admin_request_logs_allow_null_api_key_id(
    obs_client: AsyncClient, db_session: AsyncSession
) -> None:
    """内部调用方写入的 request_log.api_key_id 为 NULL，列表接口不得 500。"""
    user = await _seed_user(db_session)
    model = await _seed_model(db_session)
    request_id = f"req-internal-{uuid4().hex[:12]}"

    await usage_service.write_request_log(
        db_session,
        request_id=request_id,
        api_key_id=None,
        user_id=user.id,
        billing_subject=f"user:{user.id}",
        model_id=model.id,
        channel_id=None,
        status="success",
        http_status=200,
        latency_ms=10,
        source="kb_ingest",
    )
    await db_session.flush()

    resp = await obs_client.get(
        "/admin/request-logs",
        params={"user_id": str(user.id)},
        headers=admin_auth_headers(),
    )
    assert resp.status_code == 200
    items = resp.json()
    assert any(item["request_id"] == request_id and item["api_key_id"] is None for item in items)


@pytest.mark.asyncio
async def test_user_usage_api_scoped_to_self(
    obs_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _seed_user(db_session)
    api_key, _raw_key = await _seed_api_key(db_session, user)
    model = await _seed_model(db_session, alias=f"gpt-obs-{uuid4().hex[:4]}")
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
    resp = await obs_client.get(
        "/usage",
        headers={"Authorization": f"Bearer {token}"},
        params={"model": model.alias},
    )
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) >= 1
    assert items[0]["request_id"] == request_id
    assert items[0]["user_id"] == str(user.id)


@pytest.mark.asyncio
async def test_user_usage_filter_by_source(
    obs_client: AsyncClient, db_session: AsyncSession
) -> None:
    """用量页可按来源筛选（如知识库处理 kb_ingest）。"""
    from app.core.constants import USAGE_SOURCE_API, USAGE_SOURCE_KB_INGEST

    user = await _seed_user(db_session)
    api_key, _raw_key = await _seed_api_key(db_session, user)
    model = await _seed_model(db_session, alias=f"emb-obs-{uuid4().hex[:4]}")

    req_api = f"req-api-{uuid4().hex[:8]}"
    await usage_service.write_request_log(
        db_session,
        request_id=req_api,
        api_key_id=api_key.id,
        user_id=user.id,
        billing_subject=f"user:{user.id}",
        model_id=model.id,
        channel_id=None,
        status="success",
        http_status=200,
        source=USAGE_SOURCE_API,
    )
    await usage_service.write_usage_log(
        db_session,
        request_id=req_api,
        input_tokens=1,
        output_tokens=0,
        billing_rule_version=1,
        cost_amount=1,
        charge_amount=2,
        source=USAGE_SOURCE_API,
    )

    req_ingest = f"req-kb-{uuid4().hex[:8]}"
    await usage_service.write_request_log(
        db_session,
        request_id=req_ingest,
        api_key_id=api_key.id,
        user_id=user.id,
        billing_subject=f"user:{user.id}",
        model_id=model.id,
        channel_id=None,
        status="success",
        http_status=200,
        source=USAGE_SOURCE_KB_INGEST,
    )
    await usage_service.write_usage_log(
        db_session,
        request_id=req_ingest,
        input_tokens=10,
        output_tokens=0,
        billing_rule_version=1,
        cost_amount=5,
        charge_amount=8,
        source=USAGE_SOURCE_KB_INGEST,
    )
    await db_session.flush()

    token = create_access_token(user_id=user.id)
    resp = await obs_client.get(
        "/usage",
        headers={"Authorization": f"Bearer {token}"},
        params={"source": USAGE_SOURCE_KB_INGEST},
    )
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) >= 1
    assert all(row.get("source") == USAGE_SOURCE_KB_INGEST for row in items)
    assert any(row["request_id"] == req_ingest for row in items)
    assert not any(row["request_id"] == req_api for row in items)


@pytest.mark.asyncio
async def test_reconcile_releases_stale_hold(db_session: AsyncSession) -> None:
    user = await _seed_user(db_session)
    wallet = await wallet_service.get_or_create_personal_wallet(db_session, user.id)
    wallet.balance = 10_000
    await db_session.flush()

    request_id = f"stale-{uuid4().hex[:12]}"
    await wallet_service.hold(
        db_session,
        wallet_id=wallet.id,
        amount=1000,
        request_id=request_id,
    )
    await db_session.flush()

    # 将 hold 流水时间回拨，使其超过过期阈值
    from sqlalchemy import select, update

    from app.db.models.wallet import WalletTransaction

    stale_time = datetime.now(timezone.utc) - timedelta(hours=2)
    await db_session.execute(
        update(WalletTransaction)
        .where(
            WalletTransaction.wallet_id == wallet.id,
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

    await db_session.refresh(wallet)
    assert wallet.held == 0
    assert wallet.balance == 10_000

    release_tx = await db_session.scalar(
        select(WalletTransaction).where(
            WalletTransaction.wallet_id == wallet.id,
            WalletTransaction.ref_request_id == request_id,
            WalletTransaction.type == TX_RELEASE,
        )
    )
    assert release_tx is not None


@pytest.mark.asyncio
async def test_reconcile_captures_when_usage_exists(db_session: AsyncSession) -> None:
    user = await _seed_user(db_session)
    api_key, _ = await _seed_api_key(db_session, user)
    model = await _seed_model(db_session)
    wallet = await wallet_service.get_or_create_personal_wallet(db_session, user.id)
    wallet.balance = 10_000
    await db_session.flush()

    request_id = f"cap-{uuid4().hex[:12]}"
    await wallet_service.hold(
        db_session,
        wallet_id=wallet.id,
        amount=1000,
        request_id=request_id,
    )
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
        input_tokens=10,
        output_tokens=10,
        billing_rule_version=1,
        cost_amount=50,
        charge_amount=300,
    )
    await db_session.flush()

    from sqlalchemy import select, update

    from app.db.models.wallet import WalletTransaction

    stale_time = datetime.now(timezone.utc) - timedelta(hours=2)
    await db_session.execute(
        update(WalletTransaction)
        .where(
            WalletTransaction.wallet_id == wallet.id,
            WalletTransaction.ref_request_id == request_id,
            WalletTransaction.type == TX_HOLD,
        )
        .values(created_at=stale_time)
    )
    await db_session.flush()

    result = await reconcile_stale_holds(
        db_session, expire_seconds=3600, dry_run=False
    )
    assert result.captured >= 1
    assert result.errors == 0

    capture_tx = await db_session.scalar(
        select(WalletTransaction).where(
            WalletTransaction.wallet_id == wallet.id,
            WalletTransaction.ref_request_id == request_id,
            WalletTransaction.type == TX_CAPTURE,
        )
    )
    assert capture_tx is not None

    await db_session.refresh(wallet)
    assert wallet.held == 0
    assert wallet.balance == 10_000 - 300
