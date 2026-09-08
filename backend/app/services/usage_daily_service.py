"""日聚合：按 capture 流水归属日，写入 usage_daily_summary。

幂等策略：同一 summary_date 先删后插。
收入 = -sum(capture.amount)（计费总额）；
赠金消耗 = -sum(capture.gift_amount)；现金收入 = 计费总额 - 赠金消耗。
成本按 channel_costs 对聚合 token 折算。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_UP, Decimal
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import STATUS_ENABLED
from app.core.logging import get_logger
from app.db.models.channel_cost import ChannelCost
from app.db.models.llm_model import ModelChannelMap
from app.db.models.log import RequestLog, UsageLog
from app.db.models.usage_daily import UsageDailySummary
from app.db.models.wallet import WalletTransaction
from app.services.wallet_service import TX_CAPTURE

logger = get_logger(__name__)

_ONE_MILLION = Decimal("1000000")
_MIN_UNIT = Decimal("1")


@dataclass
class DailyAggregateResult:
    summary_date: date
    deleted: int = 0
    inserted: int = 0
    revenue_total: int = 0
    cost_total: int = 0
    cost_missing_rows: int = 0


def day_bounds_utc(summary_date: date) -> tuple[datetime, datetime]:
    """UTC 日界：[00:00, 次日 00:00)。"""
    start = datetime(summary_date.year, summary_date.month, summary_date.day, tzinfo=UTC)
    end = start + timedelta(days=1)
    return start, end


def _split_revenue(revenue: int, gift_revenue: int) -> tuple[int, int]:
    """将计费总额拆成 (gift_revenue, cash_revenue)，保证两者之和等于 revenue。"""
    if gift_revenue < 0:
        gift_revenue = 0
    if gift_revenue > revenue:
        gift_revenue = revenue
    return gift_revenue, revenue - gift_revenue


def calc_token_cost(*, input_tokens: int, output_tokens: int, cost_input: int, cost_output: int) -> int:
    """按每 1M token 单价折算成本，向上取整到最小货币单位 1。"""
    raw = (
        Decimal(input_tokens) * Decimal(cost_input)
        + Decimal(output_tokens) * Decimal(cost_output)
    ) / _ONE_MILLION
    return int((raw / _MIN_UNIT).to_integral_value(rounding=ROUND_UP) * _MIN_UNIT)


async def _load_effective_costs(
    session: AsyncSession,
    *,
    as_of: datetime,
) -> dict[tuple[UUID, str], ChannelCost]:
    """加载 as_of 时刻各 (channel_id, upstream_model_name) 生效中的成本价。"""
    rows = (
        await session.execute(
            select(ChannelCost)
            .where(
                ChannelCost.status == STATUS_ENABLED,
                ChannelCost.effective_at <= as_of,
            )
            .order_by(
                ChannelCost.channel_id,
                ChannelCost.upstream_model_name,
                ChannelCost.effective_at.desc(),
                ChannelCost.version.desc(),
            )
        )
    ).scalars().all()

    best: dict[tuple[UUID, str], ChannelCost] = {}
    for row in rows:
        key = (row.channel_id, row.upstream_model_name)
        if key not in best:
            best[key] = row
    return best


async def _load_upstream_names(
    session: AsyncSession,
) -> dict[tuple[UUID, UUID], list[str]]:
    """(model_id, channel_id) → upstream_model_name 列表（按 weight 降序）。"""
    rows = (
        await session.execute(
            select(ModelChannelMap).order_by(
                ModelChannelMap.model_id,
                ModelChannelMap.channel_id,
                ModelChannelMap.weight.desc(),
            )
        )
    ).scalars().all()
    out: dict[tuple[UUID, UUID], list[str]] = {}
    for m in rows:
        key = (m.model_id, m.channel_id)
        out.setdefault(key, []).append(m.upstream_model_name)
    return out


def _resolve_cost(
    *,
    model_id: UUID,
    channel_id: UUID | None,
    upstream_by_map: dict[tuple[UUID, UUID], list[str]],
    costs: dict[tuple[UUID, str], ChannelCost],
    input_tokens: int,
    output_tokens: int,
) -> tuple[int, bool]:
    """返回 (cost_amount, cost_price_missing)。"""
    if channel_id is None:
        return 0, True

    names = upstream_by_map.get((model_id, channel_id), [])
    # 优先按映射权重顺序找已配置成本价；无映射时无法识别上游模型
    for name in names:
        rule = costs.get((channel_id, name))
        if rule is not None:
            amount = calc_token_cost(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_input=rule.cost_input,
                cost_output=rule.cost_output,
            )
            return amount, False

    return 0, True


async def aggregate_usage_daily(
    session: AsyncSession,
    *,
    summary_date: date,
    dry_run: bool = False,
) -> DailyAggregateResult:
    """聚合指定 UTC 日的 capture 用量写入 usage_daily_summary。"""
    day_start, day_end = day_bounds_utc(summary_date)
    result = DailyAggregateResult(summary_date=summary_date)

    # 按 capture 流水日归属；收入取 -amount（流水为负数）
    agg_stmt = (
        select(
            RequestLog.model_id,
            RequestLog.user_id,
            RequestLog.org_id,
            RequestLog.channel_id,
            func.count().label("request_count"),
            func.coalesce(func.sum(UsageLog.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(UsageLog.output_tokens), 0).label("output_tokens"),
            func.coalesce(func.sum(UsageLog.cached_tokens), 0).label("cached_tokens"),
            func.coalesce(func.sum(-WalletTransaction.amount), 0).label("revenue_amount"),
            func.coalesce(func.sum(-WalletTransaction.gift_amount), 0).label(
                "gift_revenue_amount"
            ),
        )
        .select_from(WalletTransaction)
        .join(
            RequestLog,
            RequestLog.request_id == WalletTransaction.ref_request_id,
        )
        .join(UsageLog, UsageLog.request_id == RequestLog.request_id)
        .where(
            WalletTransaction.type == TX_CAPTURE,
            WalletTransaction.created_at >= day_start,
            WalletTransaction.created_at < day_end,
            WalletTransaction.ref_request_id.is_not(None),
        )
        .group_by(
            RequestLog.model_id,
            RequestLog.user_id,
            RequestLog.org_id,
            RequestLog.channel_id,
        )
    )
    rows = (await session.execute(agg_stmt)).all()

    costs = await _load_effective_costs(session, as_of=day_end - timedelta(microseconds=1))
    upstream_by_map = await _load_upstream_names(session)

    if dry_run:
        result.inserted = len(rows)
        for r in rows:
            cost_amt, missing = _resolve_cost(
                model_id=r.model_id,
                channel_id=r.channel_id,
                upstream_by_map=upstream_by_map,
                costs=costs,
                input_tokens=int(r.input_tokens),
                output_tokens=int(r.output_tokens),
            )
            result.revenue_total += int(r.revenue_amount)
            result.cost_total += cost_amt
            if missing:
                result.cost_missing_rows += 1
        logger.info(
            "usage_daily_aggregate_dry_run",
            summary_date=str(summary_date),
            rows=len(rows),
            revenue_total=result.revenue_total,
            cost_total=result.cost_total,
        )
        return result

    # 先删后插（幂等）
    del_result = await session.execute(
        delete(UsageDailySummary).where(UsageDailySummary.summary_date == summary_date)
    )
    result.deleted = del_result.rowcount or 0

    for r in rows:
        cost_amt, missing = _resolve_cost(
            model_id=r.model_id,
            channel_id=r.channel_id,
            upstream_by_map=upstream_by_map,
            costs=costs,
            input_tokens=int(r.input_tokens),
            output_tokens=int(r.output_tokens),
        )
        revenue = int(r.revenue_amount)
        gift_revenue, cash_revenue = _split_revenue(
            revenue, int(r.gift_revenue_amount)
        )
        session.add(
            UsageDailySummary(
                summary_date=summary_date,
                model_id=r.model_id,
                user_id=r.user_id,
                org_id=r.org_id,
                channel_id=r.channel_id,
                request_count=int(r.request_count),
                input_tokens=int(r.input_tokens),
                output_tokens=int(r.output_tokens),
                cached_tokens=int(r.cached_tokens),
                revenue_amount=revenue,
                gift_revenue_amount=gift_revenue,
                cash_revenue_amount=cash_revenue,
                cost_amount=cost_amt,
                cost_price_missing=missing,
            )
        )
        result.inserted += 1
        result.revenue_total += revenue
        result.cost_total += cost_amt
        if missing:
            result.cost_missing_rows += 1

    await session.flush()
    logger.info(
        "usage_daily_aggregate_done",
        summary_date=str(summary_date),
        deleted=result.deleted,
        inserted=result.inserted,
        revenue_total=result.revenue_total,
        cost_total=result.cost_total,
        cost_missing_rows=result.cost_missing_rows,
    )
    return result
