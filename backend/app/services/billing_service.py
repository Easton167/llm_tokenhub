"""计费服务：计费规则查询、预扣估算、实扣计算。

与 WalletService 的分工：
- 本模块只算「该收多少最小货币单位」，不碰余额。
- WalletService 负责 hold/capture/release 的原子性与幂等。

公式（固定单价，PRD）：
    费用 = (input × price_input + output × price_output
            + cached × price_cached_input) / 1_000_000 × multiplier
          + price_per_call × multiplier
    再按 min_charge_unit 向上取整。

价格字段语义：price_* / cost_* 都是「每 1M token 的最小货币单位整数」。
全程 Decimal，禁止 float。cost_* 只用于利润核算，不向用户收取。
"""

from __future__ import annotations

import math
from datetime import datetime

from app.core.datetime_utils import utc_now
from decimal import ROUND_UP, Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.billing import BillingRule
from app.db.models.llm_model import LLMModel
from app.core.errors import InvalidRequestError


class BillingCalculator:
    """无状态费用计算器，所有金额以最小货币单位整数表示。

    PRD 公式（固定单价）：
        费用 = (input_tokens × price_input
                + output_tokens × price_output
                + cached_tokens × price_cached_input
                + price_per_call) / 1_000_000 × multiplier
        取整方式：向上取整到 min_charge_unit
    """

    def calculate(
        self,
        rule: BillingRule,
        *,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int = 0,
        include_per_call: bool = True,
    ) -> int:
        """按实际用量计算费用，返回最小货币单位整数。

        include_per_call：预扣和实扣都计次费；若将来按 token 补差可关。
        cached_tokens 走更便宜的 price_cached_input（prompt cache 命中）。
        """
        # 使用 Decimal 进行精确计算，避免浮点误差
        m = Decimal(str(rule.multiplier))

        token_cost = (
            Decimal(input_tokens) * Decimal(rule.price_input)
            + Decimal(output_tokens) * Decimal(rule.price_output)
            + Decimal(cached_tokens) * Decimal(rule.price_cached_input)
        ) / Decimal("1000000") * m

        if include_per_call and rule.price_per_call > 0:
            token_cost += Decimal(rule.price_per_call) * m

        # 向上取整到最小计费单位
        unit = Decimal(rule.min_charge_unit)
        charged = (token_cost / unit).to_integral_value(rounding=ROUND_UP) * unit
        return int(charged)

    def estimate(
        self,
        rule: BillingRule,
        *,
        input_tokens: int,
        max_output_tokens: int,
    ) -> int:
        """预扣估算：取保守上界（输入估算值 + max_tokens，cached 按 0）。

        宁可多冻、结算再退（capture 会写一条 release 差额流水）。
        不算 cache 折扣，避免预扣不足导致 capture 超 hold 再二次扣余额。
        """
        return self.calculate(
            rule,
            input_tokens=input_tokens,
            output_tokens=max_output_tokens,
            cached_tokens=0,
            include_per_call=True,
        )

    def calculate_cost_amount(
        self,
        rule: BillingRule,
        *,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int = 0,
    ) -> int:
        """计算上游成本金额（用于利润核算，不向用户收取）。

        成本不计 multiplier、不计 price_per_call、不计 cached 折扣价，
        只用 cost_input / cost_output。
        """
        cost = (
            Decimal(input_tokens) * Decimal(rule.cost_input)
            + Decimal(output_tokens) * Decimal(rule.cost_output)
        ) / Decimal("1000000")

        unit = Decimal(rule.min_charge_unit)
        result = (cost / unit).to_integral_value(rounding=ROUND_UP) * unit
        return int(result)


class BillingService:
    """计费服务：规则查询 + 费用计算 + 与 WalletService 交互的编排层。

    WalletService 负责钱包原子操作，BillingService 负责规则查询与金额计算。
    """

    def __init__(self) -> None:
        self.calculator = BillingCalculator()

    async def get_active_rule(
        self,
        session: AsyncSession,
        model_id: UUID,
        at_time: datetime | None = None,
    ) -> BillingRule | None:
        """查找指定模型在 at_time 时刻生效的最新计费规则版本。

        生效条件：status=0（启用）且 effective_at <= at_time，取 version 最大的那条。
        """
        if at_time is None:
            at_time = utc_now()

        stmt = (
            select(BillingRule)
            .where(
                BillingRule.model_id == model_id,
                BillingRule.status == 0,
                BillingRule.effective_at <= at_time,
            )
            .order_by(BillingRule.version.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def require_active_rule(
        self,
        session: AsyncSession,
        model_id: UUID,
        at_time: datetime | None = None,
    ) -> BillingRule:
        """同 get_active_rule，但无规则时抛出 InvalidRequestError（防漏计费）。

        网关在 hold 前必须走这条，而不是 get_active_rule 后默默按 0 收费。
        """
        rule = await self.get_active_rule(session, model_id, at_time)
        if rule is None:
            raise InvalidRequestError(
                "No active billing rule found for this model. Request rejected to prevent billing errors."
            )
        return rule

    def estimate_hold_amount(
        self,
        rule: BillingRule,
        input_tokens: int,
        max_output_tokens: int,
    ) -> int:
        """计算预扣（hold）金额：用于请求受理时冻结余额。"""
        return self.calculator.estimate(
            rule,
            input_tokens=input_tokens,
            max_output_tokens=max_output_tokens,
        )

    def calculate_charge_amount(
        self,
        rule: BillingRule,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int = 0,
    ) -> int:
        """计算实扣（capture）金额：用于请求结束后的实际结算。"""
        return self.calculator.calculate(
            rule,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
        )

    def calculate_cost_amount(
        self,
        rule: BillingRule,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int = 0,
    ) -> int:
        """计算上游成本金额（仅供利润核算，不影响用户扣费）。"""
        return self.calculator.calculate_cost_amount(
            rule,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
        )


# 进程单例。计算器无状态，并发共享安全。
billing_service = BillingService()
