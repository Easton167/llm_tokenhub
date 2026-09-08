"""报表查询：读 usage_daily_summary；查询窗口含 UTC 当日时先重算当日。

时区口径：summary_date / 过滤日期均为 UTC 日历日，
与日聚合任务（capture.created_at 按 UTC 日归属）一致。

收入口径：
- revenue_amount = 计费总额（含赠金抵扣，不是平台收入）
- cash_revenue_amount = 平台真实收入
- gross_profit = cash_revenue_amount - cost_amount（纯赠金消费时为负，属预期）
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.datetime_utils import utc_now
from app.core.errors import InvalidRequestError
from app.db.models.log import UsageLog
from app.db.models.usage_daily import UsageDailySummary
from app.db.models.wallet import GiftGrant, Wallet, WalletTransaction
from app.services.usage_daily_service import aggregate_usage_daily, day_bounds_utc
from app.services.wallet_service import (
    TX_CAPTURE,
    TX_GIFT_EXPIRE,
    TX_GIFT_GRANT,
    TX_GIFT_REVOKE,
)

TOP_DIMENSIONS = ("user", "org", "model")


@dataclass(frozen=True)
class ReportFilters:
    from_date: date
    to_date: date
    org_id: UUID | None = None
    user_id: UUID | None = None
    model_id: UUID | None = None
    channel_id: UUID | None = None


def _validate_range(from_date: date, to_date: date) -> None:
    if from_date > to_date:
        raise InvalidRequestError("from_date must be <= to_date")


async def _refresh_today_if_in_range(
    session: AsyncSession, from_date: date, to_date: date
) -> None:
    """查询窗口含 UTC 当日时重跑当日聚合，避免报表滞后于用量明细。

    ponytail: 每次报表查询全量重算当日；上限是当日 capture 扫描。
    升级路径是写入 usage_log 时增量更新，或短 TTL 缓存。
    对账接口不走这里，以便检出 cron 未跑导致的历史缺口。
    """
    today = utc_now().date()
    if from_date <= today <= to_date:
        await aggregate_usage_daily(session, summary_date=today)


async def _prepare_range(
    session: AsyncSession, from_date: date, to_date: date
) -> None:
    _validate_range(from_date, to_date)
    await _refresh_today_if_in_range(session, from_date, to_date)


def _base_filter(stmt: Select, filters: ReportFilters) -> Select:
    stmt = stmt.where(
        UsageDailySummary.summary_date >= filters.from_date,
        UsageDailySummary.summary_date <= filters.to_date,
    )
    if filters.org_id is not None:
        stmt = stmt.where(UsageDailySummary.org_id == filters.org_id)
    if filters.user_id is not None:
        stmt = stmt.where(UsageDailySummary.user_id == filters.user_id)
    if filters.model_id is not None:
        stmt = stmt.where(UsageDailySummary.model_id == filters.model_id)
    if filters.channel_id is not None:
        stmt = stmt.where(UsageDailySummary.channel_id == filters.channel_id)
    return stmt


def _gift_cost(*, gift_revenue: int, revenue: int, cost: int) -> int:
    """赠金消费对应的上游成本。

    ponytail: 日汇总按维度聚合后再按 gift/revenue 整数比例分摊，
    混合行最多差 1 单位；升级路径是聚合时按 usage_log 逐条累加 gift_cost。
    """
    if gift_revenue <= 0 or cost <= 0:
        return 0
    if revenue <= 0:
        return cost
    return cost * gift_revenue // revenue


def _metric_columns():
    return (
        func.coalesce(func.sum(UsageDailySummary.request_count), 0).label("request_count"),
        func.coalesce(func.sum(UsageDailySummary.input_tokens), 0).label("input_tokens"),
        func.coalesce(func.sum(UsageDailySummary.output_tokens), 0).label("output_tokens"),
        func.coalesce(func.sum(UsageDailySummary.cached_tokens), 0).label("cached_tokens"),
        func.coalesce(func.sum(UsageDailySummary.revenue_amount), 0).label("revenue_amount"),
        func.coalesce(func.sum(UsageDailySummary.gift_revenue_amount), 0).label(
            "gift_revenue_amount"
        ),
        func.coalesce(func.sum(UsageDailySummary.cash_revenue_amount), 0).label(
            "cash_revenue_amount"
        ),
        func.coalesce(func.sum(UsageDailySummary.cost_amount), 0).label("cost_amount"),
    )


def _row_to_metrics(row) -> dict:
    revenue = int(row.revenue_amount)
    gift = int(row.gift_revenue_amount)
    cash = int(row.cash_revenue_amount)
    cost = int(row.cost_amount)
    return {
        "request_count": int(row.request_count),
        "input_tokens": int(row.input_tokens),
        "output_tokens": int(row.output_tokens),
        "cached_tokens": int(row.cached_tokens),
        "revenue_amount": revenue,
        "cash_revenue_amount": cash,
        "gift_consumed": gift,
        "gift_charge_amount": gift,
        "cash_charge_amount": cash,
        "gift_cost": _gift_cost(gift_revenue=gift, revenue=revenue, cost=cost),
        "cost_amount": cost,
        "gross_profit": cash - cost,
    }


async def trend_by_day(
    session: AsyncSession,
    filters: ReportFilters,
) -> list[dict]:
    """收入/成本/毛利按天趋势。平台收入取 cash_revenue_amount。"""
    await _prepare_range(session, filters.from_date, filters.to_date)
    stmt = _base_filter(
        select(UsageDailySummary.summary_date, *_metric_columns()),
        filters,
    ).group_by(UsageDailySummary.summary_date).order_by(UsageDailySummary.summary_date)
    rows = (await session.execute(stmt)).all()
    return [
        {"summary_date": r.summary_date, **_row_to_metrics(r)}
        for r in rows
    ]


async def top_by_dimension(
    session: AsyncSession,
    filters: ReportFilters,
    *,
    dimension: str,
    limit: int = 10,
) -> list[dict]:
    """Top 用户 / 组织 / 模型排行（按现金收入降序）。"""
    await _prepare_range(session, filters.from_date, filters.to_date)
    if dimension not in TOP_DIMENSIONS:
        raise InvalidRequestError(
            f"dimension must be one of: {', '.join(TOP_DIMENSIONS)}"
        )
    dim_col = {
        "user": UsageDailySummary.user_id,
        "org": UsageDailySummary.org_id,
        "model": UsageDailySummary.model_id,
    }[dimension]

    stmt = _base_filter(
        select(dim_col.label("dim_id"), *_metric_columns()),
        filters,
    )
    # 组织排行排除个人计费（org_id IS NULL）
    if dimension == "org":
        stmt = stmt.where(UsageDailySummary.org_id.is_not(None))

    stmt = (
        stmt.group_by(dim_col)
        .order_by(func.sum(UsageDailySummary.cash_revenue_amount).desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [{"dim_id": r.dim_id, **_row_to_metrics(r)} for r in rows]


async def channel_cost_share(
    session: AsyncSession,
    filters: ReportFilters,
) -> tuple[int, list[dict]]:
    """渠道成本占比（按 cost_amount 降序）。"""
    await _prepare_range(session, filters.from_date, filters.to_date)
    stmt = (
        _base_filter(
            select(UsageDailySummary.channel_id, *_metric_columns()),
            filters,
        )
        .group_by(UsageDailySummary.channel_id)
        .order_by(func.sum(UsageDailySummary.cost_amount).desc())
    )
    rows = (await session.execute(stmt)).all()
    items = [{**_row_to_metrics(r), "channel_id": r.channel_id} for r in rows]
    total_cost = sum(i["cost_amount"] for i in items)
    for item in items:
        if total_cost <= 0:
            item["cost_share_bps"] = 0
        else:
            item["cost_share_bps"] = int(
                round(item["cost_amount"] * 10_000 / total_cost)
            )
    return total_cost, items


async def reconcile_revenue(
    session: AsyncSession,
    *,
    from_date: date,
    to_date: date,
) -> dict:
    """对账：聚合表计费总额 vs 同期 capture 流水；赠金拆分 vs usage_logs。"""
    _validate_range(from_date, to_date)
    day_start, _ = day_bounds_utc(from_date)
    _, day_end = day_bounds_utc(to_date)

    summary_revenue = int(
        await session.scalar(
            select(func.coalesce(func.sum(UsageDailySummary.revenue_amount), 0)).where(
                UsageDailySummary.summary_date >= from_date,
                UsageDailySummary.summary_date <= to_date,
            )
        )
        or 0
    )
    capture_revenue = int(
        await session.scalar(
            select(func.coalesce(func.sum(-WalletTransaction.amount), 0)).where(
                WalletTransaction.type == TX_CAPTURE,
                WalletTransaction.created_at >= day_start,
                WalletTransaction.created_at < day_end,
                WalletTransaction.ref_request_id.is_not(None),
            )
        )
        or 0
    )
    usage_gift_total = int(
        await session.scalar(
            select(func.coalesce(func.sum(UsageLog.gift_charge_amount), 0)).where(
                UsageLog.created_at >= day_start,
                UsageLog.created_at < day_end,
            )
        )
        or 0
    )
    capture_gift_total = int(
        await session.scalar(
            select(func.coalesce(func.sum(-WalletTransaction.gift_amount), 0)).where(
                WalletTransaction.type == TX_CAPTURE,
                WalletTransaction.created_at >= day_start,
                WalletTransaction.created_at < day_end,
                WalletTransaction.ref_request_id.is_not(None),
            )
        )
        or 0
    )
    matched = summary_revenue == capture_revenue
    gift_matched = usage_gift_total == capture_gift_total
    alerts: list[str] = []
    if not matched:
        alerts.append("revenue_mismatch")
    if not gift_matched:
        alerts.append("gift_split_mismatch")
    return {
        "summary_revenue": summary_revenue,
        "capture_revenue": capture_revenue,
        "matched": matched,
        "usage_gift_total": usage_gift_total,
        "capture_gift_total": capture_gift_total,
        "gift_matched": gift_matched,
        "alerts": alerts,
    }


async def gift_dashboard(
    session: AsyncSession, *, from_date: date, to_date: date
) -> dict:
    """赠金看板：敞口为当前快照；发放/撤回/过期/消耗按 UTC 日范围。"""
    await _prepare_range(session, from_date, to_date)
    day_start, _ = day_bounds_utc(from_date)
    _, day_end = day_bounds_utc(to_date)

    gift_balance_total = int(
        await session.scalar(
            select(func.coalesce(func.sum(Wallet.gift_balance + Wallet.gift_held), 0))
        )
        or 0
    )

    async def _tx_sum(tx_type: str) -> int:
        return int(
            await session.scalar(
                select(func.coalesce(func.sum(WalletTransaction.amount), 0)).where(
                    WalletTransaction.type == tx_type,
                    WalletTransaction.created_at >= day_start,
                    WalletTransaction.created_at < day_end,
                )
            )
            or 0
        )

    granted_amount = await _tx_sum(TX_GIFT_GRANT)
    revoked_amount = -(await _tx_sum(TX_GIFT_REVOKE))
    expired_amount = -(await _tx_sum(TX_GIFT_EXPIRE))

    metric_row = (
        await session.execute(
            select(*_metric_columns()).where(
                UsageDailySummary.summary_date >= from_date,
                UsageDailySummary.summary_date <= to_date,
            )
        )
    ).one()
    metrics = _row_to_metrics(metric_row)

    granted_all_time = int(
        await session.scalar(select(func.coalesce(func.sum(GiftGrant.amount), 0))) or 0
    )
    consumed_all_time = int(
        await session.scalar(
            select(func.coalesce(func.sum(UsageLog.gift_charge_amount), 0))
        )
        or 0
    )
    redemption_rate_bps = (
        consumed_all_time * 10_000 // granted_all_time if granted_all_time > 0 else 0
    )
    return {
        "gift_balance_total": gift_balance_total,
        "granted_amount": granted_amount,
        "revoked_amount": revoked_amount,
        "expired_amount": expired_amount,
        "gift_consumed": metrics["gift_consumed"],
        "gift_cost": metrics["gift_cost"],
        "granted_all_time": granted_all_time,
        "consumed_all_time": consumed_all_time,
        "redemption_rate_bps": redemption_rate_bps,
    }


async def user_daily_usage(
    session: AsyncSession,
    *,
    user_id: UUID,
    from_date: date,
    to_date: date,
    model_id: UUID | None = None,
) -> list[dict]:
    """用户个人按天用量（仅本人 user_id）。"""
    return await trend_by_day(
        session,
        ReportFilters(
            from_date=from_date,
            to_date=to_date,
            user_id=user_id,
            model_id=model_id,
        ),
    )


async def org_daily_usage(
    session: AsyncSession,
    *,
    org_id: UUID,
    from_date: date,
    to_date: date,
    model_id: UUID | None = None,
) -> list[dict]:
    """组织按天用量汇总（组织数据隔离）。"""
    return await trend_by_day(
        session,
        ReportFilters(
            from_date=from_date,
            to_date=to_date,
            org_id=org_id,
            model_id=model_id,
        ),
    )


async def org_member_share(
    session: AsyncSession,
    *,
    org_id: UUID,
    from_date: date,
    to_date: date,
    model_id: UUID | None = None,
) -> list[dict]:
    """组织成员分摊（按 user_id 聚合）；仅供 Owner/Admin 调用。"""
    await _prepare_range(session, from_date, to_date)
    filters = ReportFilters(
        from_date=from_date,
        to_date=to_date,
        org_id=org_id,
        model_id=model_id,
    )
    stmt = (
        _base_filter(
            select(UsageDailySummary.user_id.label("user_id"), *_metric_columns()),
            filters,
        )
        .group_by(UsageDailySummary.user_id)
        .order_by(func.sum(UsageDailySummary.cash_revenue_amount).desc())
    )
    rows = (await session.execute(stmt)).all()
    return [{"user_id": r.user_id, **_row_to_metrics(r)} for r in rows]
