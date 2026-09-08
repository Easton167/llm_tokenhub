"""支付渠道注册表。"""

from __future__ import annotations

from app.core.constants import PAYMENT_PROVIDER_MOCK, PAYMENT_PROVIDER_STRIPE
from app.core.errors import InvalidRequestError
from app.payments.base import PaymentProvider
from app.payments.mock import MockPaymentProvider
from app.payments.stripe import StripePaymentProvider

_providers: dict[str, PaymentProvider] = {
    PAYMENT_PROVIDER_MOCK: MockPaymentProvider(),
    PAYMENT_PROVIDER_STRIPE: StripePaymentProvider(),
}


def get_payment_provider(name: str) -> PaymentProvider:
    provider = _providers.get(name)
    if provider is None:
        raise InvalidRequestError(f"Unsupported payment provider: {name}")
    return provider


def register_payment_provider(provider: PaymentProvider) -> None:
    """注册或替换渠道实现（测试可注入自定义 Mock / Stripe）。"""
    _providers[provider.name] = provider
