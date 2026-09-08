"""用量日聚合任务：按 UTC 日写入 usage_daily_summary，可重跑幂等。

用法示例：
  python -m app.tasks.aggregate_usage_daily --date 2026-07-22
  python -m app.tasks.aggregate_usage_daily --yesterday
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import date, timedelta

from app.core.config import settings
from app.core.datetime_utils import utc_now
from app.core.logging import configure_logging, get_logger
from app.db.session import close_db, get_session_factory
from app.services.usage_daily_service import aggregate_usage_daily

logger = get_logger(__name__)


async def _async_main(args: argparse.Namespace) -> int:
    configure_logging(debug=settings.debug)

    if args.yesterday:
        summary_date = utc_now().date() - timedelta(days=1)
    elif args.date:
        summary_date = date.fromisoformat(args.date)
    else:
        summary_date = utc_now().date() - timedelta(days=1)

    session_factory = get_session_factory()
    async with session_factory() as session:
        result = await aggregate_usage_daily(
            session,
            summary_date=summary_date,
            dry_run=args.dry_run,
        )
        if not args.dry_run:
            await session.commit()

    await close_db()
    print(
        f"usage daily aggregate done: date={result.summary_date} "
        f"deleted={result.deleted} inserted={result.inserted} "
        f"revenue={result.revenue_total} cost={result.cost_total} "
        f"cost_missing_rows={result.cost_missing_rows} dry_run={args.dry_run}"
    )
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Aggregate usage into usage_daily_summary")
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="UTC date YYYY-MM-DD (default: yesterday)",
    )
    parser.add_argument(
        "--yesterday",
        action="store_true",
        help="Aggregate yesterday (UTC); same as default",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute totals without writing usage_daily_summary",
    )
    args = parser.parse_args(argv)
    raise SystemExit(asyncio.run(_async_main(args)))


if __name__ == "__main__":
    main()
