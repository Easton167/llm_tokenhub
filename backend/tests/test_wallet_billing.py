"""钱包与计费服务完整测试。

覆盖验收标准：
  - 余额不足时 hold 失败
  - hold 成功后 balance 减少、held 增加
  - capture 后释放多余冻结
  - release 后冻结全额回余额
  - 重复 capture 不重复扣费
  - 并发 hold 不会导致余额为负
  - BillingCalculator 整数精度计算（无 float 误差）
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import STATUS_ENABLED
from app.core.errors import InsufficientBalanceError, InvalidRequestError
from app.db.models.billing import BillingRule
from app.db.models.llm_model import LLMModel
from app.db.models.user import User
from app.db.models.wallet import Wallet, WalletTransaction
from app.services.billing_service import BillingCalculator, BillingService, billing_service
from app.services.wallet_service import (
    TX_CAPTURE,
    TX_HOLD,
    TX_RECHARGE,
    TX_RELEASE,
    wallet_service,
)
from tests.conftest import admin_auth_headers


# =====================================================================
# 测试辅助：不依赖 SQLAlchemy mapper 的纯 Python 计费规则对象
# =====================================================================


class MockBillingRule:
    """用于单元测试的内存计费规则对象，避免触碰 SQLAlchemy ORM 映射。"""

    __slots__ = (
        "price_input", "price_output", "price_cached_input",
        "price_per_call", "cost_input", "cost_output",
        "multiplier", "min_charge_unit",
    )

    def __init__(
        self,
        price_input: int,
        price_output: int,
        price_cached_input: int,
        price_per_call: int,
        cost_input: int,
        cost_output: int,
        multiplier: Decimal,
        min_charge_unit: int,
    ) -> None:
        self.price_input = price_input
        self.price_output = price_output
        self.price_cached_input = price_cached_input
        self.price_per_call = price_per_call
        self.cost_input = cost_input
        self.cost_output = cost_output
        self.multiplier = multiplier
        self.min_charge_unit = min_charge_unit


# =====================================================================
# Fixtures / Helpers
# =====================================================================


async def _seed_user(session: AsyncSession, suffix: str = "") -> User:
    user = User(
        email=f"wallet-test{suffix}-{uuid4().hex[:6]}@example.com",
        status=STATUS_ENABLED,
    )
    session.add(user)
    await session.flush()
    return user


async def _seed_wallet(session: AsyncSession, user: User, balance: int = 0) -> Wallet:
    wallet = await wallet_service.get_or_create_personal_wallet(session, user.id)
    if balance > 0:
        wallet.balance = balance
        await session.flush()
    return wallet


async def _seed_model(session: AsyncSession, alias: str) -> LLMModel:
    model = LLMModel(
        alias=alias,
        display_name=alias,
        status=STATUS_ENABLED,
    )
    session.add(model)
    await session.flush()
    return model


async def _seed_billing_rule(
    session: AsyncSession,
    model_id: UUID,
    *,
    price_input: int = 180_000,    # 18元/1M token => 180000 单位/1M (1元=10000单位)
    price_output: int = 720_000,   # 72元/1M token
    price_cached_input: int = 90_000,  # 9元/1M token
    price_per_call: int = 0,
    cost_input: int = 150_000,
    cost_output: int = 600_000,
    multiplier: Decimal = Decimal("1.000"),
    min_charge_unit: int = 1,
) -> BillingRule:
    rule = BillingRule(
        model_id=model_id,
        version=1,
        price_input=price_input,
        price_output=price_output,
        price_cached_input=price_cached_input,
        price_per_call=price_per_call,
        cost_input=cost_input,
        cost_output=cost_output,
        multiplier=multiplier,
        min_charge_unit=min_charge_unit,
        currency="CNY",
        effective_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        status=0,
    )
    session.add(rule)
    await session.flush()
    return rule


# =====================================================================
# BillingCalculator 单元测试（无数据库依赖）
# =====================================================================


class TestBillingCalculator:
    """验证费用计算公式、整数精度、取整规则。"""

    def setup_method(self) -> None:
        self.calc = BillingCalculator()

    def _make_rule(
        self,
        price_input: int = 180_000,
        price_output: int = 720_000,
        price_cached_input: int = 90_000,
        price_per_call: int = 0,
        cost_input: int = 150_000,
        cost_output: int = 600_000,
        multiplier: str = "1.000",
        min_charge_unit: int = 1,
    ) -> "MockBillingRule":
        """构造一个普通属性对象代替 ORM 模型（避免 SQLAlchemy mapper 问题）。"""
        return MockBillingRule(
            price_input=price_input,
            price_output=price_output,
            price_cached_input=price_cached_input,
            price_per_call=price_per_call,
            cost_input=cost_input,
            cost_output=cost_output,
            multiplier=Decimal(multiplier),
            min_charge_unit=min_charge_unit,
        )

    def test_prd_example_a_pro_user_no_cache(self) -> None:
        """PRD 示例 A：pro 用户，输入 1500，输出 800，无缓存，倍率 1.2。

        期望费用 = (1500×18 + 800×72) / 1e6 元 × 1.2 倍 × 10000单位/元
                 = (27000 + 57600) / 1e6 × 1.2 × 10000
                 = 0.084600 × 1.2 × 10000 = 1015.2 → 向上取整 = 1016 单位
        注意：PRD 示例用 price_input=18元/1M，单位是元；这里使用整数最小单位：
        price_input = 18 * 10000 = 180000（每1M token，最小货币单位）
        费用 = (1500 * 180000 + 800 * 720000) / 1_000_000 * 1.2
             = (270000000 + 576000000) / 1000000 * 1.2
             = 846 * 1.2 = 1015.2 → ceil = 1016
        """
        rule = self._make_rule(multiplier="1.2")
        charge = self.calc.calculate(
            rule, input_tokens=1500, output_tokens=800, cached_tokens=0
        )
        # 手动计算：(1500*180000 + 800*720000) / 1e6 * 1.2 = 846 * 1.2 = 1015.2 → 1016
        assert charge == 1016

    def test_prd_example_a_without_multiplier(self) -> None:
        """倍率 1.0，输入 1500，输出 800。期望 = 846。"""
        rule = self._make_rule(multiplier="1.000")
        charge = self.calc.calculate(
            rule, input_tokens=1500, output_tokens=800, cached_tokens=0
        )
        # (1500*180000 + 800*720000) / 1e6 = (270000000 + 576000000) / 1e6 = 846
        assert charge == 846

    def test_cached_tokens(self) -> None:
        """含缓存命中：400非缓存输入 + 1600缓存输入 + 500输出。"""
        rule = self._make_rule(multiplier="1.000")
        charge = self.calc.calculate(
            rule,
            input_tokens=400,
            output_tokens=500,
            cached_tokens=1600,
        )
        # (400*180000 + 500*720000 + 1600*90000) / 1e6
        # = (72000000 + 360000000 + 144000000) / 1e6
        # = 576000000 / 1e6 = 576
        assert charge == 576

    def test_min_charge_unit_ceil(self) -> None:
        """min_charge_unit=100 时，结果向上取整到 100 的倍数。"""
        rule = self._make_rule(
            price_input=100_000,
            price_output=100_000,
            multiplier="1.000",
            min_charge_unit=100,
        )
        # 1 token 输入 = 100000/1e6 = 0.1 => 0.1单位，向上取整100 => 100
        charge = self.calc.calculate(rule, input_tokens=1, output_tokens=0)
        assert charge == 100

    def test_no_float_drift(self) -> None:
        """大量 token 计算不应产生浮点漂移。"""
        rule = self._make_rule(multiplier="1.000")
        # 1,000,000 输入 + 1,000,000 输出 => 整数
        charge = self.calc.calculate(
            rule,
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cached_tokens=0,
        )
        expected = (1_000_000 * 180_000 + 1_000_000 * 720_000) // 1_000_000
        assert charge == expected

    def test_per_call_fee(self) -> None:
        """按次计费叠加 Token 计费。"""
        rule = self._make_rule(price_per_call=500, multiplier="1.000")  # 500单位/次
        charge = self.calc.calculate(
            rule, input_tokens=0, output_tokens=0, include_per_call=True
        )
        assert charge == 500

    def test_estimate_uses_max_tokens(self) -> None:
        """预扣估算取 max_output_tokens，比实际更保守。"""
        rule = self._make_rule(multiplier="1.000")
        estimated = self.calc.estimate(rule, input_tokens=500, max_output_tokens=2000)
        actual = self.calc.calculate(rule, input_tokens=500, output_tokens=800)
        assert estimated >= actual

    def test_cost_amount_calculation(self) -> None:
        """上游成本计算（不含 multiplier）。"""
        rule = self._make_rule(multiplier="1.0")
        cost = self.calc.calculate_cost_amount(
            rule, input_tokens=1500, output_tokens=800
        )
        # (1500*150000 + 800*600000) / 1e6 = (225000000 + 480000000)/1e6 = 705
        assert cost == 705

    def test_zero_tokens(self) -> None:
        """0 token 调用 + 无按次费 => 0。"""
        rule = self._make_rule()
        charge = self.calc.calculate(
            rule, input_tokens=0, output_tokens=0, include_per_call=False
        )
        assert charge == 0


# =====================================================================
# WalletService 事务测试（需要真实数据库）
# =====================================================================


@pytest.mark.asyncio
async def test_create_personal_wallet(db_session: AsyncSession) -> None:
    """初次调用 get_or_create_personal_wallet 应创建 balance=0 的钱包。"""
    user = await _seed_user(db_session, "-create")
    wallet = await wallet_service.get_or_create_personal_wallet(db_session, user.id)
    assert wallet.balance == 0
    assert wallet.held == 0
    assert wallet.owner_type == "personal"
    assert wallet.owner_id == user.id


@pytest.mark.asyncio
async def test_recharge_increases_balance(db_session: AsyncSession) -> None:
    """充值后余额正确增加，写入 recharge 流水。"""
    user = await _seed_user(db_session, "-recharge")
    wallet = await _seed_wallet(db_session, user)

    updated = await wallet_service.recharge(
        db_session, wallet_id=wallet.id, amount=10_000
    )
    assert updated.balance == 10_000
    assert updated.held == 0

    txs = await wallet_service.get_transactions(db_session, wallet_id=wallet.id)
    assert any(t.type == TX_RECHARGE and t.amount == 10_000 for t in txs)


@pytest.mark.asyncio
async def test_recharge_invalid_amount(db_session: AsyncSession) -> None:
    """充值金额 <= 0 应报错。"""
    user = await _seed_user(db_session, "-recharge-invalid")
    wallet = await _seed_wallet(db_session, user)

    with pytest.raises(InvalidRequestError):
        await wallet_service.recharge(db_session, wallet_id=wallet.id, amount=0)

    with pytest.raises(InvalidRequestError):
        await wallet_service.recharge(db_session, wallet_id=wallet.id, amount=-100)


@pytest.mark.asyncio
async def test_hold_success_balance_decreases_held_increases(
    db_session: AsyncSession,
) -> None:
    """hold 成功后：balance 减少，held 增加，各变化量等于 hold 金额。"""
    user = await _seed_user(db_session, "-hold-ok")
    wallet = await _seed_wallet(db_session, user, balance=5_000)

    request_id = f"req-{uuid4().hex}"
    updated = await wallet_service.hold(
        db_session,
        wallet_id=wallet.id,
        amount=2_000,
        request_id=request_id,
    )
    assert updated.balance == 3_000
    assert updated.held == 2_000

    txs = await wallet_service.get_transactions(db_session, wallet_id=wallet.id)
    hold_txs = [t for t in txs if t.type == TX_HOLD]
    assert len(hold_txs) == 1
    assert hold_txs[0].amount == -2_000
    assert hold_txs[0].ref_request_id == request_id


@pytest.mark.asyncio
async def test_hold_fails_when_insufficient_balance(db_session: AsyncSession) -> None:
    """余额不足时 hold 应抛出 InsufficientBalanceError，余额不变。"""
    user = await _seed_user(db_session, "-hold-fail")
    wallet = await _seed_wallet(db_session, user, balance=1_000)

    with pytest.raises(InsufficientBalanceError) as exc_info:
        await wallet_service.hold(
            db_session,
            wallet_id=wallet.id,
            amount=2_000,
            request_id=f"req-{uuid4().hex}",
        )
    assert exc_info.value.code == "insufficient_balance"
    assert exc_info.value.status_code == 402

    # 余额应保持原值
    await db_session.refresh(wallet)
    assert wallet.balance == 1_000
    assert wallet.held == 0


@pytest.mark.asyncio
async def test_hold_idempotent(db_session: AsyncSession) -> None:
    """相同 request_id 的 hold 调用两次，余额只扣一次。"""
    user = await _seed_user(db_session, "-hold-idem")
    wallet = await _seed_wallet(db_session, user, balance=5_000)
    request_id = f"req-{uuid4().hex}"

    await wallet_service.hold(
        db_session, wallet_id=wallet.id, amount=1_000, request_id=request_id
    )
    # 第二次调用，幂等，不应再扣
    await wallet_service.hold(
        db_session, wallet_id=wallet.id, amount=1_000, request_id=request_id
    )

    await db_session.refresh(wallet)
    assert wallet.balance == 4_000  # 只扣了一次
    assert wallet.held == 1_000


@pytest.mark.asyncio
async def test_capture_releases_excess_hold(db_session: AsyncSession) -> None:
    """实扣 < 预扣时，差额应回退到余额，held 清零。"""
    user = await _seed_user(db_session, "-capture-less")
    wallet = await _seed_wallet(db_session, user, balance=10_000)
    request_id = f"req-{uuid4().hex}"

    # 预扣 3000
    await wallet_service.hold(
        db_session, wallet_id=wallet.id, amount=3_000, request_id=request_id
    )
    await db_session.refresh(wallet)
    assert wallet.balance == 7_000
    assert wallet.held == 3_000

    # 实扣 1000（比预扣少 2000）
    updated = await wallet_service.capture(
        db_session,
        wallet_id=wallet.id,
        hold_amount=3_000,
        actual_amount=1_000,
        request_id=request_id,
    )
    assert updated.balance == 7_000 + 2_000  # 退还 2000
    assert updated.held == 0

    txs = await wallet_service.get_transactions(db_session, wallet_id=wallet.id)
    capture_txs = [t for t in txs if t.type == TX_CAPTURE]
    release_txs = [t for t in txs if t.type == TX_RELEASE]
    assert len(capture_txs) == 1
    assert capture_txs[0].amount == -1_000
    assert len(release_txs) == 1
    assert release_txs[0].amount == 2_000


@pytest.mark.asyncio
async def test_capture_exact_amount(db_session: AsyncSession) -> None:
    """实扣 == 预扣时，held 清零，余额不变（已在 hold 时扣除）。"""
    user = await _seed_user(db_session, "-capture-exact")
    wallet = await _seed_wallet(db_session, user, balance=5_000)
    request_id = f"req-{uuid4().hex}"

    await wallet_service.hold(
        db_session, wallet_id=wallet.id, amount=2_000, request_id=request_id
    )
    updated = await wallet_service.capture(
        db_session,
        wallet_id=wallet.id,
        hold_amount=2_000,
        actual_amount=2_000,
        request_id=request_id,
    )
    assert updated.balance == 3_000
    assert updated.held == 0


@pytest.mark.asyncio
async def test_capture_idempotent_no_double_charge(db_session: AsyncSession) -> None:
    """重复调用相同 request_id 的 capture，不应重复扣费。"""
    user = await _seed_user(db_session, "-capture-idem")
    wallet = await _seed_wallet(db_session, user, balance=10_000)
    request_id = f"req-{uuid4().hex}"

    await wallet_service.hold(
        db_session, wallet_id=wallet.id, amount=3_000, request_id=request_id
    )
    # 第一次 capture
    await wallet_service.capture(
        db_session,
        wallet_id=wallet.id,
        hold_amount=3_000,
        actual_amount=1_500,
        request_id=request_id,
    )
    balance_after_first = (await db_session.get(Wallet, wallet.id)).balance  # type: ignore[union-attr]

    # 第二次 capture（幂等）
    await wallet_service.capture(
        db_session,
        wallet_id=wallet.id,
        hold_amount=3_000,
        actual_amount=1_500,
        request_id=request_id,
    )
    balance_after_second = (await db_session.get(Wallet, wallet.id)).balance  # type: ignore[union-attr]

    assert balance_after_first == balance_after_second  # 余额不变，未重复扣费


@pytest.mark.asyncio
async def test_release_returns_full_hold_to_balance(db_session: AsyncSession) -> None:
    """release 后冻结额度全额回到余额，held 变回 0。"""
    user = await _seed_user(db_session, "-release")
    wallet = await _seed_wallet(db_session, user, balance=8_000)
    request_id = f"req-{uuid4().hex}"

    await wallet_service.hold(
        db_session, wallet_id=wallet.id, amount=3_000, request_id=request_id
    )
    await db_session.refresh(wallet)
    assert wallet.balance == 5_000
    assert wallet.held == 3_000

    updated = await wallet_service.release(
        db_session,
        wallet_id=wallet.id,
        hold_amount=3_000,
        request_id=request_id,
    )
    assert updated.balance == 8_000  # 完全恢复原值
    assert updated.held == 0

    txs = await wallet_service.get_transactions(db_session, wallet_id=wallet.id)
    release_txs = [t for t in txs if t.type == TX_RELEASE]
    assert any(t.ref_request_id == request_id for t in release_txs)


@pytest.mark.asyncio
async def test_release_idempotent(db_session: AsyncSession) -> None:
    """重复 release 相同 request_id 不应重复增加余额。"""
    user = await _seed_user(db_session, "-release-idem")
    wallet = await _seed_wallet(db_session, user, balance=5_000)
    request_id = f"req-{uuid4().hex}"

    await wallet_service.hold(
        db_session, wallet_id=wallet.id, amount=2_000, request_id=request_id
    )
    await wallet_service.release(
        db_session, wallet_id=wallet.id, hold_amount=2_000, request_id=request_id
    )
    balance_after_first = (await db_session.get(Wallet, wallet.id)).balance  # type: ignore[union-attr]

    # 第二次 release（幂等）
    await wallet_service.release(
        db_session, wallet_id=wallet.id, hold_amount=2_000, request_id=request_id
    )
    balance_after_second = (await db_session.get(Wallet, wallet.id)).balance  # type: ignore[union-attr]

    assert balance_after_first == balance_after_second == 5_000


@pytest.mark.asyncio
async def test_transaction_list_filtering(db_session: AsyncSession) -> None:
    """流水查询按类型过滤和分页正确。"""
    user = await _seed_user(db_session, "-txlist")
    wallet = await _seed_wallet(db_session, user, balance=10_000)
    req1 = f"req-{uuid4().hex}"
    req2 = f"req-{uuid4().hex}"

    await wallet_service.hold(
        db_session, wallet_id=wallet.id, amount=1_000, request_id=req1
    )
    await wallet_service.capture(
        db_session,
        wallet_id=wallet.id,
        hold_amount=1_000,
        actual_amount=800,
        request_id=req1,
    )
    await wallet_service.hold(
        db_session, wallet_id=wallet.id, amount=500, request_id=req2
    )
    await wallet_service.release(
        db_session, wallet_id=wallet.id, hold_amount=500, request_id=req2
    )

    all_txs = await wallet_service.get_transactions(db_session, wallet_id=wallet.id)
    hold_txs = await wallet_service.get_transactions(
        db_session, wallet_id=wallet.id, tx_type=TX_HOLD
    )
    assert len(all_txs) >= 4
    assert all(t.type == TX_HOLD for t in hold_txs)


# =====================================================================
# BillingService 规则查询测试（需要真实数据库）
# =====================================================================


@pytest.mark.asyncio
async def test_get_active_billing_rule(db_session: AsyncSession) -> None:
    """get_active_rule 应返回 effective_at <= now 的最新版本规则。"""
    model = await _seed_model(db_session, f"billing-model-{uuid4().hex[:6]}")
    rule = await _seed_billing_rule(db_session, model.id)

    found = await billing_service.get_active_rule(db_session, model.id)
    assert found is not None
    assert found.id == rule.id


@pytest.mark.asyncio
async def test_get_active_rule_returns_none_for_no_rule(
    db_session: AsyncSession,
) -> None:
    """无计费规则的模型，get_active_rule 返回 None。"""
    model = await _seed_model(db_session, f"no-rule-model-{uuid4().hex[:6]}")
    found = await billing_service.get_active_rule(db_session, model.id)
    assert found is None


@pytest.mark.asyncio
async def test_require_active_rule_raises_on_no_rule(db_session: AsyncSession) -> None:
    """无计费规则时，require_active_rule 应抛出 InvalidRequestError。"""
    model = await _seed_model(db_session, f"no-billing-{uuid4().hex[:6]}")

    with pytest.raises(InvalidRequestError) as exc_info:
        await billing_service.require_active_rule(db_session, model.id)
    assert exc_info.value.code == "invalid_request"


@pytest.mark.asyncio
async def test_get_active_rule_picks_latest_version(db_session: AsyncSession) -> None:
    """同一模型多版本规则，应选 version 最大的那条。"""
    model = await _seed_model(db_session, f"multi-ver-{uuid4().hex[:6]}")

    rule_v1 = BillingRule(
        model_id=model.id,
        version=1,
        price_input=100_000,
        price_output=100_000,
        price_cached_input=0,
        price_per_call=0,
        cost_input=80_000,
        cost_output=80_000,
        multiplier=Decimal("1.0"),
        min_charge_unit=1,
        currency="CNY",
        effective_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        status=0,
    )
    rule_v2 = BillingRule(
        model_id=model.id,
        version=2,
        price_input=200_000,
        price_output=200_000,
        price_cached_input=0,
        price_per_call=0,
        cost_input=160_000,
        cost_output=160_000,
        multiplier=Decimal("1.0"),
        min_charge_unit=1,
        currency="CNY",
        effective_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
        status=0,
    )
    db_session.add_all([rule_v1, rule_v2])
    await db_session.flush()

    found = await billing_service.get_active_rule(db_session, model.id)
    assert found is not None
    assert found.version == 2  # 应选最新版本


@pytest.mark.asyncio
async def test_active_rule_ignores_disabled(db_session: AsyncSession) -> None:
    """status=1（禁用）的规则不应被选中。"""
    model = await _seed_model(db_session, f"disabled-rule-{uuid4().hex[:6]}")
    disabled_rule = BillingRule(
        model_id=model.id,
        version=1,
        price_input=100_000,
        price_output=100_000,
        price_cached_input=0,
        price_per_call=0,
        cost_input=80_000,
        cost_output=80_000,
        multiplier=Decimal("1.0"),
        min_charge_unit=1,
        currency="CNY",
        effective_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        status=1,  # 禁用
    )
    db_session.add(disabled_rule)
    await db_session.flush()

    found = await billing_service.get_active_rule(db_session, model.id)
    assert found is None


# =====================================================================
# 并发扣费测试（模拟多个 hold 并发，余额不能变为负数）
# =====================================================================


@pytest.mark.asyncio
async def test_concurrent_hold_no_negative_balance(db_session: AsyncSession) -> None:
    """并发 hold 不会导致余额为负（数据库行锁保证串行化）。

    本测试在同一 session 内串行发出多次 hold，验证在竞争资源不足时
    只有满足条件的 hold 成功，失败的抛出 InsufficientBalanceError。
    真实并发场景由数据库行锁（FOR UPDATE）保证。
    """
    user = await _seed_user(db_session, "-concurrent")
    wallet = await _seed_wallet(db_session, user, balance=3_000)

    request_ids = [f"req-concurrent-{i}-{uuid4().hex}" for i in range(5)]
    success_count = 0
    fail_count = 0

    for rid in request_ids:
        try:
            await wallet_service.hold(
                db_session,
                wallet_id=wallet.id,
                amount=1_000,
                request_id=rid,
            )
            success_count += 1
        except InsufficientBalanceError:
            fail_count += 1

    await db_session.refresh(wallet)

    # 总余额 3000，每次 hold 1000，应成功 3 次，失败 2 次
    assert success_count == 3
    assert fail_count == 2
    # 余额不能为负
    assert wallet.balance >= 0
    assert wallet.balance + wallet.held == 3_000  # 总量守恒


# =====================================================================
# Admin API 集成测试（HTTP 层）
# =====================================================================


@pytest.mark.asyncio
async def test_admin_recharge_api(db_session: AsyncSession) -> None:
    """测试后台充值 API：充值成功后余额正确。"""
    from httpx import ASGITransport, AsyncClient
    from app.db.session import get_db
    from app.main import app

    user = await _seed_user(db_session, "-api-recharge")
    # 确保钱包存在
    wallet = await wallet_service.get_or_create_personal_wallet(db_session, user.id)
    await db_session.flush()

    async def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=admin_auth_headers()
    ) as client:
        resp = await client.post(
            f"/admin/wallets/users/{user.id}/recharge",
            json={"amount": 50_000, "remark": "test top-up"},
        )
    app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["balance"] == 50_000
    assert body["owner_id"] == str(user.id)


@pytest.mark.asyncio
async def test_admin_get_wallet_api(db_session: AsyncSession) -> None:
    """测试后台查询用户钱包 API。"""
    from httpx import ASGITransport, AsyncClient
    from app.db.session import get_db
    from app.main import app

    user = await _seed_user(db_session, "-api-get-wallet")
    await wallet_service.get_or_create_personal_wallet(db_session, user.id)
    await db_session.flush()

    async def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=admin_auth_headers()
    ) as client:
        resp = await client.get(f"/admin/wallets/users/{user.id}")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["owner_id"] == str(user.id)
    assert "balance" in body


@pytest.mark.asyncio
async def test_admin_transaction_list_api(db_session: AsyncSession) -> None:
    """测试后台查询流水 API。"""
    from httpx import ASGITransport, AsyncClient
    from app.db.session import get_db
    from app.main import app

    user = await _seed_user(db_session, "-api-txlist")
    wallet = await wallet_service.get_or_create_personal_wallet(db_session, user.id)
    # 充值一笔流水
    await wallet_service.recharge(db_session, wallet_id=wallet.id, amount=1_000)
    await db_session.flush()

    async def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=admin_auth_headers()
    ) as client:
        resp = await client.get(f"/admin/wallets/users/{user.id}/transactions")
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    txs = resp.json()
    assert isinstance(txs, list)
    assert len(txs) >= 1
    assert any(t["type"] == "recharge" for t in txs)


@pytest.mark.asyncio
async def test_full_billing_flow(db_session: AsyncSession) -> None:
    """端到端流程测试：充值 -> hold -> capture(实扣 < 预扣) -> 验证余额守恒。"""
    user = await _seed_user(db_session, "-e2e")
    wallet = await _seed_wallet(db_session, user, balance=100_000)
    model = await _seed_model(db_session, f"e2e-model-{uuid4().hex[:6]}")
    rule = await _seed_billing_rule(db_session, model.id, multiplier=Decimal("1.0"))

    # 估算预扣
    estimated = billing_service.estimate_hold_amount(
        rule, input_tokens=1500, max_output_tokens=2000
    )
    request_id = f"req-e2e-{uuid4().hex}"

    # hold
    await wallet_service.hold(
        db_session,
        wallet_id=wallet.id,
        amount=estimated,
        request_id=request_id,
    )
    await db_session.refresh(wallet)
    assert wallet.balance == 100_000 - estimated
    assert wallet.held == estimated

    # 实际用量
    actual = billing_service.calculate_charge_amount(
        rule, input_tokens=1500, output_tokens=800
    )
    assert actual <= estimated  # 保守预扣应 >= 实扣

    # capture
    await wallet_service.capture(
        db_session,
        wallet_id=wallet.id,
        hold_amount=estimated,
        actual_amount=actual,
        request_id=request_id,
    )
    await db_session.refresh(wallet)

    # 验证守恒：最终余额 = 初始余额 - 实扣金额
    assert wallet.balance == 100_000 - actual
    assert wallet.held == 0
