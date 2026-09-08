"""UTC 时间约定：API 统一序列化为 ISO8601，后缀 Z。

库内全部存 timezone-aware UTC（DateTime(timezone=True)）。
业务日历（赠金到期、操作人日累计）用 ``business_timezone``（默认 Asia/Shanghai）
算「自然日结束」，再转回 UTC 入库，避免 UTC 零点导致「还没到本地午夜就过期」。

示例：2026-07-21T09:48:20.546306Z
"""

from datetime import UTC, datetime, time, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from pydantic import PlainSerializer


def utc_now() -> datetime:
    """当前 UTC aware datetime。禁止用 ``datetime.utcnow()``（naive，写入 PG 会踩时区）。"""
    return datetime.now(UTC)


def to_utc_aware(dt: datetime) -> datetime:
    """将 naive 视为 UTC；aware 则转换到 UTC。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def business_day_end_utc(
    days_from_now: int,
    *,
    now: datetime | None = None,
    tz_name: str = "Asia/Shanghai",
) -> datetime:
    """业务时区下（今天 + days_from_now）当日 23:59:59，再转 UTC。"""
    tz = ZoneInfo(tz_name)
    local_now = to_utc_aware(now or utc_now()).astimezone(tz)
    target_date = (local_now + timedelta(days=days_from_now)).date()
    local_end = datetime.combine(target_date, time(23, 59, 59), tzinfo=tz)
    return local_end.astimezone(UTC)


def business_day_bounds_utc(
    *,
    now: datetime | None = None,
    tz_name: str = "Asia/Shanghai",
) -> tuple[datetime, datetime]:
    """业务时区当日 [00:00:00, 次日 00:00:00) 对应的 UTC 区间。"""
    tz = ZoneInfo(tz_name)
    local_now = to_utc_aware(now or utc_now()).astimezone(tz)
    start_local = datetime.combine(local_now.date(), time.min, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def to_utc_iso(dt: datetime) -> str:
    """序列化为带 Z 的 UTC ISO8601 字符串。"""
    return to_utc_aware(dt).isoformat().replace("+00:00", "Z")


# Pydantic 字段注解：JSON 输出带 Z 的 UTC 字符串，Python 侧仍是 datetime
UtcDateTime = Annotated[
    datetime,
    PlainSerializer(to_utc_iso, return_type=str, when_used="json"),
]
