"""Stripe 支付渠道：验签、回调重放、掉单对账。"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient, MockTransport, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    PAYMENT_PROVIDER_MOCK,
    PAYMENT_PROVIDER_STRIPE,
    PAYMENT_STATUS_CREATED,
    PAYMENT_STATUS_PAID,
    PAYMENT_TARGET_PERSONAL,
    STATUS_ENABLED,
)
from app.core.errors import InvalidRequestError
from app.db.models.user import User
from app.db.session import get_db
from app.main import app
from app.payments import get_payment_provider, register_payment_provider
from app.payments.mock import MockPaymentProvider
from app.payments.stripe import (
    StripePaymentProvider,
    build_stripe_signature,
    from_stripe_amount,
    to_stripe_amount,
    verify_stripe_signature,
)
from app.services.payment_service import payment_service
from app.services.wallet_service import wallet_service
from app.tasks.reconcile_payments import reconcile_stale_payments

WEBHOOK_SECRET = "whsec_test_secret_task8"
STRIPE_SECRET = "sk_test_task8"


def _checkout_completed_event(
    *,
    order_no: str,
    session_id: str,
    amount_internal: int,
) -> dict:
    return {
        "id": f"evt_{uuid4().hex[:16]}",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": session_id,
                "object": "checkout.session",
                "client_reference_id": order_no,
                "metadata": {"order_no": order_no},
                "amount_total": to_stripe_amount(amount_internal),
                "currency": "cny",
                "payment_status": "paid",
                "status": "complete",
            }
        },
    }


@pytest_asyncio.fixture
async def user_client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
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


@pytest.fixture
def stripe_provider() -> StripePaymentProvider:
    provider = StripePaymentProvider(
        secret_key=STRIPE_SECRET,
        webhook_secret=WEBHOOK_SECRET,
    )
    register_payment_provider(provider)
    yield provider
    register_payment_provider(StripePaymentProvider())
    register_payment_provider(MockPaymentProvider())


async def _seed_user(session: AsyncSession, suffix: str = "") -> User:
    user = User(
        email=f"stripe{suffix}-{uuid4().hex[:6]}@example.com",
        status=STATUS_ENABLED,
    )
    session.add(user)
    await session.flush()
    return user


def test_stripe_amount_roundtrip() -> None:
    assert to_stripe_amount(10_000) == 100  # 1 元
    assert from_stripe_amount(100) == 10_000
    with pytest.raises(InvalidRequestError):
        to_stripe_amount(1)  # 不可整除到「分」


def test_stripe_signature_accepts_valid_and_rejects_forged() -> None:
    payload = b'{"id":"evt_1","type":"checkout.session.completed"}'
    sig = build_stripe_signature(payload, secret=WEBHOOK_SECRET, timestamp=1_700_000_000)
    verify_stripe_signature(
        payload=payload,
        signature_header=sig,
        secret=WEBHOOK_SECRET,
        tolerance_seconds=10**9,
    )
    with pytest.raises(InvalidRequestError, match="signature invalid"):
        verify_stripe_signature(
            payload=payload,
            signature_header="t=1700000000,v1=deadbeef",
            secret=WEBHOOK_SECRET,
            tolerance_seconds=10**9,
        )


@pytest.mark.asyncio
async def test_stripe_webhook_forged_signature_rejected_no_credit(
    user_client: AsyncClient,
    db_session: AsyncSession,
    stripe_provider: StripePaymentProvider,
) -> None:
    user = await _seed_user(db_session, "-forge")
    # 用 mock 创建订单，再假装走 stripe 回调（测验签，不依赖 Stripe API）
    order, _ = await payment_service.create_order(
        db_session,
        user_id=user.id,
        amount=10_000,
        target_type=PAYMENT_TARGET_PERSONAL,
        provider_name=PAYMENT_PROVIDER_MOCK,
    )
    # 改写为 stripe 渠道以便 webhook 路径处理
    order.provider = PAYMENT_PROVIDER_STRIPE
    order.external_order_no = f"cs_test_{uuid4().hex[:12]}"
    await db_session.flush()

    wallet = await wallet_service.get_or_create_personal_wallet(db_session, user.id)
    assert wallet.balance == 0

    event = _checkout_completed_event(
        order_no=order.order_no,
        session_id=order.external_order_no,
        amount_internal=order.amount,
    )
    raw = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    bad = await user_client.post(
        "/payments/callbacks/stripe",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "Stripe-Signature": "t=1700000000,v1=forged_signature",
        },
    )
    assert bad.status_code == 400, bad.text

    await db_session.refresh(order)
    await db_session.refresh(wallet)
    assert order.status == PAYMENT_STATUS_CREATED
    assert wallet.balance == 0


@pytest.mark.asyncio
async def test_stripe_webhook_valid_and_replay_credits_once(
    user_client: AsyncClient,
    db_session: AsyncSession,
    stripe_provider: StripePaymentProvider,
) -> None:
    user = await _seed_user(db_session, "-ok")
    order, _ = await payment_service.create_order(
        db_session,
        user_id=user.id,
        amount=20_000,
        target_type=PAYMENT_TARGET_PERSONAL,
        provider_name=PAYMENT_PROVIDER_MOCK,
    )
    order.provider = PAYMENT_PROVIDER_STRIPE
    order.external_order_no = f"cs_test_{uuid4().hex[:12]}"
    await db_session.flush()

    wallet = await wallet_service.get_or_create_personal_wallet(db_session, user.id)
    event = _checkout_completed_event(
        order_no=order.order_no,
        session_id=order.external_order_no or "",
        amount_internal=order.amount,
    )
    raw = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sig = build_stripe_signature(raw, secret=WEBHOOK_SECRET)

    for _ in range(3):
        resp = await user_client.post(
            "/payments/callbacks/stripe",
            content=raw,
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": sig,
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.text == "ok"

    await db_session.refresh(order)
    await db_session.refresh(wallet)
    assert order.status == PAYMENT_STATUS_PAID
    assert wallet.balance == 20_000
    assert (
        await payment_service.count_recharge_txs(
            db_session, order_no=order.order_no, wallet_id=wallet.id
        )
        == 1
    )


@pytest.mark.asyncio
async def test_payment_reconcile_recovers_lost_callback(
    db_session: AsyncSession,
) -> None:
    """掉单：渠道已 paid 但本地仍 created → 对账补入账。"""
    mock = MockPaymentProvider()
    register_payment_provider(mock)
    try:
        user = await _seed_user(db_session, "-recon")
        order, create_result = await payment_service.create_order(
            db_session,
            user_id=user.id,
            amount=15_000,
            target_type=PAYMENT_TARGET_PERSONAL,
            provider_name=PAYMENT_PROVIDER_MOCK,
        )
        # 模拟回调丢失：仅渠道侧变为 paid
        mock.set_query_status(
            order.order_no,
            status="paid",
            external_order_no=create_result.external_order_no,
            amount=order.amount,
        )
        # 让订单进入「可扫描」窗口
        order.created_at = datetime.now(timezone.utc) - timedelta(seconds=600)
        await db_session.flush()

        wallet = await wallet_service.get_or_create_personal_wallet(db_session, user.id)
        assert wallet.balance == 0
        assert order.status == PAYMENT_STATUS_CREATED

        result = await reconcile_stale_payments(
            db_session, stale_seconds=60, dry_run=False
        )
        assert result.paid >= 1
        assert result.errors == 0

        await db_session.refresh(order)
        await db_session.refresh(wallet)
        assert order.status == PAYMENT_STATUS_PAID
        assert wallet.balance == 15_000
        assert (
            await payment_service.count_recharge_txs(
                db_session, order_no=order.order_no, wallet_id=wallet.id
            )
            == 1
        )

        # 再次对账幂等
        result2 = await reconcile_stale_payments(
            db_session, stale_seconds=60, dry_run=False
        )
        assert result2.paid == 0
        await db_session.refresh(wallet)
        assert wallet.balance == 15_000
    finally:
        register_payment_provider(MockPaymentProvider())


@pytest.mark.asyncio
async def test_stripe_create_and_query_with_mock_http(
    stripe_provider: StripePaymentProvider,
) -> None:
    def handler(request: Request) -> Response:
        if request.method == "POST" and request.url.path.endswith("/checkout/sessions"):
            return Response(
                200,
                json={
                    "id": "cs_test_abc",
                    "url": "https://checkout.stripe.com/c/pay/cs_test_abc",
                    "status": "open",
                    "payment_status": "unpaid",
                    "amount_total": 100,
                    "metadata": {"order_no": "POTEST"},
                },
            )
        if request.method == "GET" and "/checkout/sessions/" in request.url.path:
            return Response(
                200,
                json={
                    "id": "cs_test_abc",
                    "status": "complete",
                    "payment_status": "paid",
                    "amount_total": 100,
                    "metadata": {"order_no": "POTEST"},
                    "client_reference_id": "POTEST",
                },
            )
        return Response(404, json={"error": {"message": "not found"}})

    transport = MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = StripePaymentProvider(
            secret_key=STRIPE_SECRET,
            webhook_secret=WEBHOOK_SECRET,
            http_client=client,
            api_base="https://api.stripe.com/v1",
        )
        created = await provider.create_payment(
            order_no="POTEST",
            amount=10_000,
            currency="CNY",
            subject="test",
            notify_url="http://localhost:8000/payments/callbacks/stripe",
        )
        assert created.external_order_no == "cs_test_abc"
        assert created.pay_url and "checkout.stripe.com" in created.pay_url

        queried = await provider.query_order(
            order_no="POTEST", external_order_no="cs_test_abc"
        )
        assert queried.status == "paid"
        assert queried.amount == 10_000


@pytest.mark.asyncio
async def test_get_payment_provider_stripe_registered(
    stripe_provider: StripePaymentProvider,
) -> None:
    assert get_payment_provider(PAYMENT_PROVIDER_STRIPE) is stripe_provider
