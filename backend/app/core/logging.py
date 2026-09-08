"""结构化日志配置：JSON 输出，并通过 contextvars 绑定 request_id。

``configure_logging`` 把 stdlib root logger 接到 structlog JSONRenderer。
``RequestIdMiddleware`` 在请求进入时 ``bind_request_context(request_id=...)``，
之后本请求内任意 ``get_logger().info(...)`` 都会自动带上 request_id。
请求结束必须 ``clear_request_context``，否则 worker 协程复用会串号。
"""

from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(*, debug: bool = False) -> None:
    """初始化 stdlib logging + structlog；进程内可重复调用（幂等）。"""
    log_level = logging.DEBUG if debug else logging.INFO

    # 使用 stdlib LoggerFactory，避免 PrintLogger 与 add_logger_name 不兼容
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(log_level)


def get_logger(name: str | None = None):
    """拿到绑定了当前 contextvars 的 structlog logger。业务模块用 ``get_logger(__name__)``。"""
    return structlog.get_logger(name)


def bind_request_context(**kwargs: object) -> None:
    """将字段绑定到当前请求的日志上下文（如 request_id）。"""
    structlog.contextvars.bind_contextvars(**kwargs)


def clear_request_context() -> None:
    structlog.contextvars.clear_contextvars()
