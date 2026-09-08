"""知识库 ingest worker：DB 拉取 pending job，执行解析→分块→向量化流水线。

用法::

    python -m app.tasks.kb_worker --once
    python -m app.tasks.kb_worker --loop

多实例并行依赖 SELECT ... FOR UPDATE SKIP LOCKED，不会重复消费同一 job。
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.constants import KB_JOB_STATUS_SUCCESS
from app.core.logging import configure_logging, get_logger
from app.core.metrics import (
    KB_DOC_PROCESS_DURATION,
    KB_DOC_PROCESS_OPS,
    KB_INGEST_OPS,
)
from app.db.session import close_db, get_session_factory
from app.services.kb_ingest_service import kb_ingest_service

logger = get_logger(__name__)


@dataclass
class WorkerResult:
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    idle: bool = False


async def run_worker_once(
    session: AsyncSession,
    *,
    max_jobs: int = 1,
    max_concurrent: int | None = None,
    batch_probe: int | None = None,
    commit: bool = False,
) -> WorkerResult:
    """处理最多 max_jobs 个任务；无任务时 idle=True。"""
    import time

    result = WorkerResult()
    for _ in range(max(1, max_jobs)):
        started = time.perf_counter()
        job = await kb_ingest_service.process_one(
            session,
            max_concurrent=max_concurrent,
            batch_probe=batch_probe,
        )
        if job is None:
            result.idle = result.processed == 0
            break
        elapsed = time.perf_counter() - started
        result.processed += 1
        status_label = "success" if job.status == KB_JOB_STATUS_SUCCESS else "failed"
        KB_DOC_PROCESS_DURATION.labels(
            job_type=job.job_type, status=status_label
        ).observe(elapsed)
        KB_DOC_PROCESS_OPS.labels(
            job_type=job.job_type, status=status_label
        ).inc()
        if job.status == KB_JOB_STATUS_SUCCESS:
            result.succeeded += 1
            KB_INGEST_OPS.labels(action="success").inc()
        else:
            result.failed += 1
            KB_INGEST_OPS.labels(action="failed").inc()
        if commit:
            await session.commit()
        else:
            await session.flush()
    if result.processed == 0:
        result.idle = True
        KB_INGEST_OPS.labels(action="idle").inc()
    return result


async def _async_main(args: argparse.Namespace) -> int:
    configure_logging(debug=settings.debug)
    session_factory = get_session_factory()
    total = WorkerResult()

    if args.loop:
        poll = (
            args.poll_interval
            if args.poll_interval is not None
            else settings.kb_worker_poll_interval_seconds
        )
        logger.info("kb_worker_loop_start", poll_interval=poll)
        try:
            while True:
                async with session_factory() as session:
                    batch = await run_worker_once(
                        session,
                        max_jobs=args.batch_size
                        or settings.kb_worker_batch_size,
                        commit=True,
                    )
                total.processed += batch.processed
                total.succeeded += batch.succeeded
                total.failed += batch.failed
                if batch.idle:
                    await asyncio.sleep(max(0.1, poll))
        except asyncio.CancelledError:
            logger.info("kb_worker_cancelled")
            raise
        finally:
            await close_db()
        return 1 if total.failed else 0

    # --once：单会话处理一批后退出
    async with session_factory() as session:
        batch = await run_worker_once(
            session,
            max_jobs=args.batch_size or settings.kb_worker_batch_size,
            commit=True,
        )
        total = batch
    await close_db()
    print(
        f"kb worker done: processed={total.processed} "
        f"succeeded={total.succeeded} failed={total.failed} idle={total.idle}"
    )
    return 1 if total.failed else 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Knowledge base ingest worker")
    parser.add_argument(
        "--once",
        action="store_true",
        default=True,
        help="Process one batch and exit (default)",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Poll continuously until interrupted",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="Max jobs per batch (default KB_WORKER_BATCH_SIZE)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=None,
        help="Seconds between idle polls in --loop mode",
    )
    args = parser.parse_args(argv)
    if args.loop:
        args.once = False
    raise SystemExit(asyncio.run(_async_main(args)))


if __name__ == "__main__":
    main()
