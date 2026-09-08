"""RequestLog、UsageLog 写入与查询服务。

设计原则：
- 函数式：无状态，调用方传入 session 并负责 commit。
- 只追加：日志记录不允许更新或删除。
- request_id 作为全链路追踪 ID。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.llm_model import LLMModel
from app.db.models.log import RequestLog, UsageLog
from app.db.models.wallet import WalletTransaction
from app.services.wallet_service import TX_CAPTURE, wallet_service

logger = get_logger(__name__)

FUND_SOURCES = ("gift", "cash", "mixed")


async def write_request_log(
    session: AsyncSession,
    *,
    request_id: str,
    api_key_id: UUID | None,
    user_id: UUID,
    billing_subject: str,
    model_id: UUID,
    channel_id: UUID | None,
    status: str,          # "success" / "failed" / "partial"
    http_status: int,
    latency_ms: int | None = None,
    is_stream: bool = False,
    error_code: str | None = None,
    org_id: UUID | None = None,
    actor_user_id: UUID | None = None,
    source: str | None = None,
) -> RequestLog:
    """写入一条请求级日志；调用方负责 commit。

    失败也要写（status=failed）：这是排障主表。channel_id 在选路前失败时可为 None。
    source 区分 api / chat / kb_ingest / kb_query，空则走库默认 api。
    """
    log = RequestLog(
        request_id=request_id,
        api_key_id=api_key_id,
        user_id=user_id,
        org_id=org_id,
        actor_user_id=actor_user_id,
        billing_subject=billing_subject,
        model_id=model_id,
        channel_id=channel_id,
        status=status,
        http_status=http_status,
        latency_ms=latency_ms,
        is_stream=is_stream,
        error_code=error_code,
        source=source,
    )
    session.add(log)
    await session.flush()
    return log


async def _resolve_charge_split(
    session: AsyncSession,
    *,
    request_id: str,
    charge_amount: int,
    wallet_id: UUID | None = None,
) -> tuple[int, int]:
    """从 capture 流水取 (gift_charge, cash_charge)；拿不到或对不上则回退为全现金。

    必须在 capture 之后调用。无 capture（未走钱包 / 免费调用）时
    gift=0、cash=charge_amount。
    """
    fallback = (0, int(charge_amount))
    resolved_wallet_id = wallet_id
    if resolved_wallet_id is None:
        resolved_wallet_id = await session.scalar(
            select(WalletTransaction.wallet_id)
            .where(
                WalletTransaction.ref_request_id == request_id,
                WalletTransaction.type == TX_CAPTURE,
            )
            .limit(1)
        )
        if resolved_wallet_id is None:
            return fallback

    gift_charge, cash_charge = await wallet_service.get_charge_split(
        session, wallet_id=resolved_wallet_id, request_id=request_id
    )
    # (0, 0) 表示无 capture，与「实扣为 0」无法区分；charge>0 时按未走钱包回退
    if gift_charge == 0 and cash_charge == 0:
        return fallback
    if gift_charge + cash_charge != charge_amount:
        logger.error(
            "usage_charge_split_mismatch",
            request_id=request_id,
            charge_amount=charge_amount,
            gift_charge_amount=gift_charge,
            cash_charge_amount=cash_charge,
        )
        return fallback
    return gift_charge, cash_charge


async def write_usage_log(
    session: AsyncSession,
    *,
    request_id: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
    billing_rule_version: int,
    cost_amount: int,
    charge_amount: int,
    is_estimated: bool = False,
    org_id: UUID | None = None,
    actor_user_id: UUID | None = None,
    source: str | None = None,
    wallet_id: UUID | None = None,
) -> UsageLog:
    """写入一条用量与费用明细日志；仅在成功/部分成功请求后调用。调用方负责 commit。

    必须先 capture 再写本函数：gift/cash 拆分从 capture 流水读。
    失败请求不要写 usage_log（没有实扣就没有用量账单）。
    gift_charge_amount + cash_charge_amount 应对齐 charge_amount；对不上则记 error 日志并回退为全现金。
    """
    gift_charge, cash_charge = await _resolve_charge_split(
        session,
        request_id=request_id,
        charge_amount=charge_amount,
        wallet_id=wallet_id,
    )
    log = UsageLog(
        request_id=request_id,
        org_id=org_id,
        actor_user_id=actor_user_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        billing_rule_version=billing_rule_version,
        cost_amount=cost_amount,
        charge_amount=charge_amount,
        gift_charge_amount=gift_charge,
        cash_charge_amount=cash_charge,
        is_estimated=is_estimated,
        source=source,
    )
    session.add(log)
    await session.flush()
    return log


def _apply_time_range(
    stmt: Select,
    column,
    *,
    from_time: datetime | None,
    to_time: datetime | None,
) -> Select:
    if from_time is not None:
        stmt = stmt.where(column >= from_time)
    if to_time is not None:
        stmt = stmt.where(column <= to_time)
    return stmt


async def list_request_logs(
    session: AsyncSession,
    *,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    user_id: UUID | None = None,
    model_id: UUID | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[RequestLog]:
    """按时间/用户/模型/状态查询请求日志。"""
    stmt = select(RequestLog).order_by(RequestLog.created_at.desc())
    stmt = _apply_time_range(
        stmt, RequestLog.created_at, from_time=from_time, to_time=to_time
    )
    if user_id is not None:
        stmt = stmt.where(RequestLog.user_id == user_id)
    if model_id is not None:
        stmt = stmt.where(RequestLog.model_id == model_id)
    if status is not None:
        stmt = stmt.where(RequestLog.status == status)
    stmt = stmt.offset(offset).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_usage_logs(
    session: AsyncSession,
    *,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    user_id: UUID | None = None,
    model_id: UUID | None = None,
    model_alias: str | None = None,
    source: str | None = None,
    fund_source: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[tuple[UsageLog, RequestLog]]:
    """查询用量日志（join request_logs，便于按用户/模型/来源/资金来源过滤）。"""
    stmt = (
        select(UsageLog, RequestLog)
        .join(RequestLog, UsageLog.request_id == RequestLog.request_id)
        .order_by(UsageLog.created_at.desc())
    )
    stmt = _apply_time_range(
        stmt, UsageLog.created_at, from_time=from_time, to_time=to_time
    )
    if user_id is not None:
        stmt = stmt.where(RequestLog.user_id == user_id)
    if model_id is not None:
        stmt = stmt.where(RequestLog.model_id == model_id)
    if model_alias is not None:
        stmt = stmt.join(LLMModel, RequestLog.model_id == LLMModel.id).where(
            LLMModel.alias == model_alias
        )
    if source is not None:
        # 存量行 source 可能为 null，筛选 api 时一并包含
        if source == "api":
            stmt = stmt.where(
                (UsageLog.source == source) | (UsageLog.source.is_(None))
            )
        else:
            stmt = stmt.where(UsageLog.source == source)
    if fund_source == "gift":
        stmt = stmt.where(
            UsageLog.gift_charge_amount > 0, UsageLog.cash_charge_amount == 0
        )
    elif fund_source == "cash":
        stmt = stmt.where(
            UsageLog.cash_charge_amount > 0, UsageLog.gift_charge_amount == 0
        )
    elif fund_source == "mixed":
        stmt = stmt.where(
            UsageLog.gift_charge_amount > 0, UsageLog.cash_charge_amount > 0
        )
    stmt = stmt.offset(offset).limit(limit)
    result = await session.execute(stmt)
    return list(result.all())


async def usage_summary(
    session: AsyncSession,
    *,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    user_id: UUID | None = None,
    group_by: str = "model",
) -> list[dict]:
    """用量聚合；MVP 支持 group_by=model|user。"""
    if group_by not in {"model", "user"}:
        group_by = "model"

    if group_by == "user":
        group_col = RequestLog.user_id
        group_key = "user_id"
    else:
        group_col = RequestLog.model_id
        group_key = "model_id"

    stmt = (
        select(
            group_col.label("group_id"),
            func.count(UsageLog.id).label("request_count"),
            func.coalesce(func.sum(UsageLog.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(UsageLog.output_tokens), 0).label("output_tokens"),
            func.coalesce(func.sum(UsageLog.cached_tokens), 0).label("cached_tokens"),
            func.coalesce(func.sum(UsageLog.cost_amount), 0).label("cost_amount"),
            func.coalesce(func.sum(UsageLog.charge_amount), 0).label("charge_amount"),
            func.coalesce(func.sum(UsageLog.gift_charge_amount), 0).label(
                "gift_charge_amount"
            ),
            func.coalesce(func.sum(UsageLog.cash_charge_amount), 0).label(
                "cash_charge_amount"
            ),
        )
        .select_from(UsageLog)
        .join(RequestLog, UsageLog.request_id == RequestLog.request_id)
        .group_by(group_col)
        .order_by(func.sum(UsageLog.charge_amount).desc())
    )
    stmt = _apply_time_range(
        stmt, UsageLog.created_at, from_time=from_time, to_time=to_time
    )
    if user_id is not None:
        stmt = stmt.where(RequestLog.user_id == user_id)

    result = await session.execute(stmt)
    rows = []
    for row in result.all():
        rows.append(
            {
                group_key: row.group_id,
                "request_count": int(row.request_count),
                "input_tokens": int(row.input_tokens),
                "output_tokens": int(row.output_tokens),
                "cached_tokens": int(row.cached_tokens),
                "cost_amount": int(row.cost_amount),
                "charge_amount": int(row.charge_amount),
                "gift_charge_amount": int(row.gift_charge_amount),
                "cash_charge_amount": int(row.cash_charge_amount),
            }
        )
    return rows
